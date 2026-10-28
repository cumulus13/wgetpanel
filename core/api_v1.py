"""Mobile / third party API, version 1.

* Authentication: ``Authorization: Bearer wgp_...`` only. Session cookies are ignored, which is also why
  these views can be CSRF exempt.
* Errors: ``{"error": {"code": "...", "message": "..."}}`` with a matching HTTP status.
* Live updates: ``GET /api/v1/events`` (Server-Sent Events).
"""
import json
import logging
import time

from django.conf import settings
from django.db import connection
from django.contrib.auth import authenticate
from django.http import FileResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import conf, events, filesend, live, notify, notify_prefs, services, snapshot, tokens
from .models import ApiToken, Download, SpeedRule

log = logging.getLogger(__name__)
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW = 900  # seconds


def error(status, code, message, **headers):
    resp = JsonResponse({"error": {"code": code, "message": message}}, status=status)
    for k, v in headers.items():
        resp[k.replace("_", "-")] = v
    return resp


def bearer(view):
    @csrf_exempt
    def wrapper(request, *args, **kwargs):
        tok = tokens.authenticate(request)
        if tok is None:
            return error(401, "unauthorized", "Missing, invalid or expired token", WWW_Authenticate='Bearer realm="wgetpanel"')
        request.user, request.api_token = tok.user, tok
        return view(request, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


def _json(request):
    if request.content_type != "application/json":
        return None
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _client_ip(request):
    return request.META.get("REMOTE_ADDR", "?")


def _token_dict(t, current_id=None):
    return {"id": t.pk, "name": t.name, "prefix": t.prefix, "created": t.created_at.isoformat(),
            "last_used": t.last_used_at.isoformat() if t.last_used_at else None,
            "expires": t.expires_at.isoformat() if t.expires_at else None, "current": t.pk == current_id}


def _user_dict(u):
    return {"id": u.pk, "username": u.get_username(), "is_staff": u.is_staff}


# ------------------------------------------------------------------ auth
@csrf_exempt
@require_http_methods(["POST", "DELETE"])
def token(request):
    if request.method == "DELETE":  # revoke the token that made the request (logout)
        tok = tokens.authenticate(request)
        if tok is None:
            return error(401, "unauthorized", "Missing, invalid or expired token", WWW_Authenticate='Bearer realm="wgetpanel"')
        tok.delete()
        return JsonResponse({"revoked": True})
    data = _json(request)
    if data is None:
        return error(400, "bad_request", "JSON body required")
    username, password = str(data.get("username") or "")[:150], str(data.get("password") or "")
    key = f"wgp:login:{_client_ip(request)}:{username.lower()}"
    try:
        r = live.client()
        attempts = r.incr(key)
        if attempts == 1:
            r.expire(key, LOGIN_WINDOW)
        if attempts > LOGIN_MAX_ATTEMPTS:
            return error(429, "too_many_attempts", "Too many failed logins, try again later", Retry_After=str(max(r.ttl(key), 1)))
    except Exception:
        log.exception("login rate limiter unavailable")
        return error(503, "unavailable", "Service temporarily unavailable")
    user = authenticate(request, username=username, password=password) if username and password else None
    if user is None or not user.is_active:
        return error(401, "invalid_credentials", "Wrong username or password")
    live.client().delete(key)
    tok, raw = tokens.issue(user, data.get("device_name") or "")
    return JsonResponse({"token": raw, "token_info": _token_dict(tok, tok.pk), "user": _user_dict(user)}, status=201)


@bearer
@require_http_methods(["GET"])
def me(request):
    return JsonResponse({"user": _user_dict(request.user), "token": _token_dict(request.api_token, request.api_token.pk)})


@bearer
@require_http_methods(["GET"])
def token_list(request):
    cur = request.api_token.pk
    return JsonResponse({"tokens": [_token_dict(t, cur) for t in ApiToken.objects.filter(user=request.user)]})


@bearer
@require_http_methods(["DELETE"])
def token_detail(request, pk):
    n, _ = ApiToken.objects.filter(pk=pk, user=request.user).delete()
    if not n:
        return error(404, "not_found", "No such token")
    return JsonResponse({"revoked": True})


# ------------------------------------------------------------------ downloads
@bearer
@require_http_methods(["GET", "POST"])
def downloads(request):
    if request.method == "GET":
        status = request.GET.get("status") or None
        if status and status not in dict(Download.STATUSES):
            return error(400, "bad_request", "Unknown status")
        return JsonResponse(snapshot.build_snapshot(request.user, status=status))
    data = _json(request)
    if data is None:
        return error(400, "bad_request", "JSON body required")
    urls = data.get("urls") or ([data["url"]] if data.get("url") else [])
    if isinstance(urls, str):
        urls = urls.splitlines()
    nb = None
    if data.get("not_before"):
        nb = parse_datetime(str(data["not_before"]))
        if nb is None:
            return error(400, "bad_request", "Invalid not_before (ISO 8601 expected)")
        if timezone.is_naive(nb):
            nb = timezone.make_aware(nb)
    created, errors = services.create_downloads(
        request.user, urls, directory=data.get("directory"), filename=data.get("filename"), options=data.get("options") or {},
        not_before=nb, http_user=str(data.get("http_user") or "")[:255], http_password=str(data.get("http_password") or ""),
        retry_max=data.get("retry"))
    body = {"created": [d.pk for d in created], "errors": errors}
    return JsonResponse(body, status=201 if created else 400)


@bearer
@require_http_methods(["GET"])
def summary(request):
    return JsonResponse(snapshot.build_snapshot(request.user, include_downloads=False))


@bearer
@require_http_methods(["GET"])
def download_detail(request, pk):
    data = snapshot.build_detail(request.user, pk)
    if data is None:
        return error(404, "not_found", "No such download")
    return JsonResponse(data)


ACTIONS = {"pause", "resume", "retry", "cancel", "remove", "purge", "top", "bottom", "up", "down"}


@bearer
@require_http_methods(["POST"])
def actions(request):
    data = _json(request)
    if data is None or data.get("action") not in ACTIONS:
        return error(400, "bad_request", f"JSON body with action in {sorted(ACTIONS)} required")
    try:
        ids = [int(i) for i in (data.get("ids") or [])]
    except (TypeError, ValueError):
        return error(400, "bad_request", "ids must be integers")
    if data["action"] != "purge" and not ids:
        return error(400, "bad_request", "ids required")
    stats = {}
    delete_files = bool(data["delete_files"]) if data.get("delete_files") is not None else None  # None = server setting
    n = services.apply_action(request.user, data["action"], ids, delete_files=delete_files, stats=stats)
    return JsonResponse({"affected": n, **stats})


@bearer
@require_http_methods(["GET", "HEAD"])
def file(request, pk):
    """Finished file with Range / If-Range / HEAD support (resumable)."""
    found = services.completed_file(request.user, pk)
    if found is None:
        return error(404, "not_found", "No such completed download")
    path, name = found
    return filesend.serve(request, path, name)


# ------------------------------------------------------------------ admin: settings + speed rules (staff only)
def staff_bearer(view):
    @bearer
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            return error(403, "forbidden", "Staff only")
        return view(request, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


def _schema():
    return [{"key": o.key, "type": o.type, "label": o.label, "help": o.help, "group": o.group, "default": conf.default(o.key),
             "choices": list(o.choices), "min": o.min, "max": o.max} for o in conf.REGISTRY.values()]


@staff_bearer
@require_http_methods(["GET", "POST"])
def settings_endpoint(request):
    if request.method == "POST":
        data = _json(request)
        if data is None:
            return error(400, "bad_request", "JSON object of {key: value} required")
        unknown = sorted(k for k in data if k not in conf.REGISTRY)
        errors = {}
        for key, value in data.items():
            if key in conf.REGISTRY:
                try:
                    conf.set(key, value)
                except (ValueError, TypeError) as exc:
                    errors[key] = str(exc)
        if errors or unknown:
            return JsonResponse({"error": {"code": "invalid_settings", "message": "Some values were rejected (valid ones were saved)"},
                                 "errors": errors, "unknown": unknown, "values": conf.all_values()}, status=400)
    return JsonResponse({"values": conf.all_values(), "schema": _schema()})


@staff_bearer
@require_http_methods(["GET", "POST"])
def rules(request):
    if request.method == "POST":
        data = _json(request)
        if data is None:
            return error(400, "bad_request", "JSON body required")
        try:
            rule = SpeedRule.objects.create(**services.parse_rule(data))
        except ValueError as exc:
            return error(400, "bad_request", str(exc))
        return JsonResponse({"rule": services.rule_dict(rule)}, status=201)
    return JsonResponse({"rules": [services.rule_dict(r) for r in SpeedRule.objects.all()]})


@staff_bearer
@require_http_methods(["PATCH", "DELETE"])
def rule_detail(request, pk):
    rule = SpeedRule.objects.filter(pk=pk).first()
    if rule is None:
        return error(404, "not_found", "No such rule")
    if request.method == "DELETE":
        rule.delete()
        return JsonResponse({"deleted": True})
    data = _json(request)
    if data is None:
        return error(400, "bad_request", "JSON body required")
    try:
        fields = services.parse_rule(data, partial=True)
    except ValueError as exc:
        return error(400, "bad_request", str(exc))
    SpeedRule.objects.filter(pk=pk).update(**fields)
    rule.refresh_from_db()
    return JsonResponse({"rule": services.rule_dict(rule)})


@bearer
@require_http_methods(["GET", "POST"])
def notify_settings(request):
    """The caller's own notification targets (ntfy topic, GNTP receiver), events and retry-notice interval."""
    if request.method == "POST":
        data = _json(request)
        if data is None:
            return error(400, "bad_request", "JSON object required")
        try:
            return JsonResponse(notify_prefs.update(request.user, data))
        except ValueError as exc:
            return error(400, "invalid_notify_settings", str(exc))
    return JsonResponse(notify_prefs.to_dict(request.user))


@bearer
@require_http_methods(["POST"])
def notify_test(request):
    """Send a test message. Body {"target": "me" (default) | "global" (staff)}."""
    data = _json(request) or {}
    status, payload = notify_prefs.test(request.user, str(data.get("target") or "me"))
    return JsonResponse(payload, status=status)


# ------------------------------------------------------------------ server sent events
def _sse(event, data, eid=None):
    out = ""
    if eid is not None:
        out += f"id: {eid}\n"
    return out + f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def event_stream(user, max_seconds=None, token_pk=None):
    """Generator behind GET /api/v1/events.

    Order matters: subscribe first, snapshot second, so no transition is lost in between.
    Events: ``snapshot`` (once, full state), ``status`` / ``removed`` (immediately on change),
    ``progress`` (about once a second, only for running downloads), comment lines as keep-alive."""
    max_seconds = max_seconds or settings.WGP_SSE_MAX_SECONDS
    started = last_progress = last_ping = last_check = time.monotonic()
    pubsub = live.client().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(events.CHANNEL)
    seq = 0
    try:
        snap = snapshot.build_snapshot(user)
        running = {r["id"] for r in snap["downloads"] if r["status"] == Download.RUNNING}
        connection.close()  # an hour long stream must not hold a PostgreSQL connection
        seq += 1
        yield _sse("snapshot", snap, seq)
        while time.monotonic() - started < max_seconds:
            msg = pubsub.get_message(timeout=1.0)
            while msg:
                try:
                    ev = json.loads(msg["data"])
                except (ValueError, TypeError):
                    ev = None
                if ev and (user.is_staff or ev.get("owner_id") == user.pk):
                    ev.pop("owner_id", None)
                    if ev["status"] == Download.RUNNING:
                        running.add(ev["id"])
                    else:
                        running.discard(ev["id"])
                    seq += 1
                    yield _sse(ev["type"], ev, seq)
                msg = pubsub.get_message(timeout=0)
            now = time.monotonic()
            if running and now - last_progress >= 1.0:
                last_progress = now
                lives = live.get_live_many(sorted(running))
                items = [{"id": pk, "downloaded": int(h.get("downloaded") or 0), "total": int(h["total"]) if h.get("total") else None,
                          "speed": int(h.get("speed") or 0)} for pk, h in lives.items() if h]
                if items:
                    seq += 1
                    yield _sse("progress", {"items": items, "speed": sum(i["speed"] for i in items)}, seq)
            if now - last_check >= 30:  # stop streaming for a revoked token (checked before anything else is sent)
                last_check = now
                if token_pk is not None:
                    alive = ApiToken.objects.filter(pk=token_pk).exists()
                    connection.close()
                    if not alive:
                        return
            if now - last_ping >= 15:
                last_ping = now
                yield ": keep-alive\n\n"
    finally:
        pubsub.close()


@bearer
@require_http_methods(["GET"])
def event_endpoint(request):
    resp = StreamingHttpResponse(event_stream(request.user, token_pk=request.api_token.pk), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # nginx: do not buffer
    return resp


# ------------------------------------------------------------------ docs
@require_http_methods(["GET"])
def openapi(request):
    from .openapi import spec
    return JsonResponse(spec(request.build_absolute_uri("/")))
