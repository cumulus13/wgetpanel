import json
import os
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from . import conf, live, services, wget
from . import snapshot as snapshot_mod
from .models import Download, SchedulerState, SpeedRule


def staff_required(view):
    @login_required
    def wrapper(request, *a, **kw):
        if not request.user.is_staff:
            return JsonResponse({"error": "Staff only"}, status=403) if request.path.startswith("/api/") else HttpResponse("Staff only", status=403)
        return view(request, *a, **kw)
    wrapper.__name__ = view.__name__
    return wrapper


def api_login_required(view):
    def wrapper(request, *a, **kw):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)
        return view(request, *a, **kw)
    wrapper.__name__ = view.__name__
    return wrapper


def _body(request):
    if request.content_type == "application/json":
        try:
            return json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return None
    return request.POST.dict() | {"urls": request.POST.get("urls", "")}


# ---------------------------------------------------------------- pages
@login_required
def dashboard(request):
    return render(request, "dashboard.html", {"is_staff": request.user.is_staff, "max_downloads": conf.get("scheduler.max_downloads")})


@staff_required
def settings_page(request):
    groups = {}
    for opt in conf.REGISTRY.values():
        groups.setdefault(opt.group, []).append({"opt": opt, "value": conf.get(opt.key)})
    return render(request, "settings.html", {"groups": groups})


@staff_required
def rules_page(request):
    return render(request, "rules.html")


@require_GET
def healthz(request):
    status, ok = {}, True
    try:
        connection.ensure_connection()
        status["database"] = "ok"
    except Exception as exc:
        status["database"], ok = f"error: {exc.__class__.__name__}", False
    status["redis"] = "ok" if live.ping() else "error"
    ok = ok and status["redis"] == "ok"
    return JsonResponse(status, status=200 if ok else 503)


# ---------------------------------------------------------------- read endpoints (shared logic: core/snapshot.py)
@require_GET
@api_login_required
def snapshot(request):
    return JsonResponse(snapshot_mod.build_snapshot(request.user))


@require_GET
@api_login_required
def detail(request, pk):
    data = snapshot_mod.build_detail(request.user, pk)
    if data is None:
        raise Http404
    return JsonResponse(data)


# ---------------------------------------------------------------- mutations
@require_POST
@api_login_required
def add(request):
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    urls = data.get("urls") or []
    if isinstance(urls, str):
        urls = urls.splitlines()
    nb = None
    if data.get("not_before"):
        nb = parse_datetime(str(data["not_before"]))
        if nb is None:
            return JsonResponse({"error": "Invalid start time"}, status=400)
        if timezone.is_naive(nb):
            nb = timezone.make_aware(nb)
    options = data.get("options") or {}
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid options"}, status=400)
    created, errors = services.create_downloads(
        request.user, urls, directory=data.get("directory"), filename=data.get("filename"), options=options,
        not_before=nb, http_user=(data.get("http_user") or "")[:255], http_password=data.get("http_password") or "")
    return JsonResponse({"created": [d.pk for d in created], "errors": errors}, status=201 if created else 400)


ACTIONS = {"pause", "resume", "retry", "cancel", "remove", "purge", "top", "bottom", "up", "down"}


@require_POST
@api_login_required
def action(request):
    data = _body(request)
    if not isinstance(data, dict) or data.get("action") not in ACTIONS:
        return JsonResponse({"error": "Unknown action"}, status=400)
    ids = data.get("ids") or []
    if isinstance(ids, str):
        ids = [i for i in ids.split(",") if i.strip().isdigit()]
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid ids"}, status=400)
    if data["action"] != "purge" and not ids:
        return JsonResponse({"error": "No ids"}, status=400)
    n = services.apply_action(request.user, data["action"], ids, delete_files=bool(data.get("delete_files")))
    return JsonResponse({"affected": n})


@require_GET
@api_login_required
def file_download(request, pk):
    found = services.completed_file(request.user, pk)
    if found is None:
        raise Http404
    path, name = found
    return FileResponse(open(path, "rb"), as_attachment=True, filename=name)


# ---------------------------------------------------------------- settings / rules (staff)
@staff_required
def settings_api(request):
    if request.method == "POST":
        data = _body(request)
        if not isinstance(data, dict):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        errors = {}
        for key, value in data.items():
            if key not in conf.REGISTRY:
                continue
            try:
                conf.set(key, value)
            except (ValueError, TypeError) as exc:
                errors[key] = str(exc)
        if errors:
            return JsonResponse({"errors": errors, "values": conf.all_values()}, status=400)
    return JsonResponse({"values": conf.all_values()})


def _rule_dict(r):
    return {"id": r.pk, "name": r.name, "enabled": r.enabled, "days": r.days, "start": r.start.strftime("%H:%M"),
            "end": r.end.strftime("%H:%M"), "limit_kbps": r.limit_kbps, "active_now": r.is_active()}


@staff_required
def rules_api(request):
    if request.method == "POST":
        data = _body(request)
        if not isinstance(data, dict):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        if data.get("delete"):
            SpeedRule.objects.filter(pk=data["delete"]).delete()
        else:
            try:
                fields = {
                    "name": str(data.get("name") or "Rule")[:100],
                    "enabled": bool(data.get("enabled", True)),
                    "days": ",".join(str(int(x)) for x in str(data.get("days") or "").split(",") if x.strip() != "" and 0 <= int(x) <= 6),
                    "start": datetime.strptime(data.get("start", "00:00"), "%H:%M").time(),
                    "end": datetime.strptime(data.get("end", "23:59"), "%H:%M").time(),
                    "limit_kbps": max(0, int(data.get("limit_kbps") or 0)),
                }
            except (ValueError, TypeError):
                return JsonResponse({"error": "Invalid rule"}, status=400)
            if data.get("id"):
                SpeedRule.objects.filter(pk=data["id"]).update(**fields)
            else:
                SpeedRule.objects.create(**fields)
    return JsonResponse({"rules": [_rule_dict(r) for r in SpeedRule.objects.all()]})
