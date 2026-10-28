"""A user's own notification settings (shared by the web UI, /api/v1 and the CLI)."""
import secrets

from django.conf import settings

from . import notify
from .models import UserNotify

FIELDS = {"enabled", "ntfy_enabled", "rotate_topic", "gntp_enabled", "gntp_host", "gntp_port", "gntp_password", "clear_gntp_password",
          "events", "retry_interval"}


def get(user):
    return UserNotify.objects.get_or_create(user=user)[0]


def new_topic():
    return f"{settings.NTFY_USER_TOPIC_PREFIX}-{secrets.token_urlsafe(12)}"  # 96 random bits, only [A-Za-z0-9_-]


def to_dict(user):
    p = get(user)
    ntfy_ok, gntp_ok = bool(settings.NTFY_URL), bool(notify.gntp_networks())
    return {
        "enabled": p.enabled,
        "ntfy": {"available": ntfy_ok, "enabled": p.ntfy_enabled, "topic": p.ntfy_topic,
                 "subscribe_url": f"{settings.NTFY_URL}/{p.ntfy_topic}" if ntfy_ok and p.ntfy_topic else ""},
        "gntp": {"available": gntp_ok, "enabled": p.gntp_enabled, "host": p.gntp_host, "port": p.gntp_port, "has_password": bool(p.gntp_password_enc),
                 "allowed_networks": [str(n) for n in notify.gntp_networks()]},
        "events": [e for e in p.events.split(",") if e] or None,
        "retry_interval": p.retry_interval,
        "defaults": {"events": sorted(notify.enabled_events()), "retry_interval": settings.NOTIFY_RETRY_INTERVAL},
        "known_events": list(notify.KINDS),
        "global_scope": settings.NOTIFY_GLOBAL_SCOPE,
        "active": bool(notify.user_target(user.pk)),
    }


def _bool(data, key):
    v = data[key]
    if not isinstance(v, bool):
        raise ValueError(f"{key} must be true or false")
    return v


def update(user, data):
    """Apply a partial update, validated as a whole (nothing is saved when anything is wrong). Returns to_dict(user)."""
    unknown = sorted(set(data) - FIELDS)
    if unknown:
        raise ValueError("unknown field(s): " + ", ".join(unknown))
    p = get(user)
    if "enabled" in data:
        p.enabled = _bool(data, "enabled")
    ntfy_wanted = _bool(data, "ntfy_enabled") if "ntfy_enabled" in data else p.ntfy_enabled
    if (ntfy_wanted and "ntfy_enabled" in data) or data.get("rotate_topic"):
        if not settings.NTFY_URL:
            raise ValueError("ntfy is not configured on this server (NTFY_URL is empty)")
    if "ntfy_enabled" in data:
        p.ntfy_enabled = ntfy_wanted
        if ntfy_wanted and not p.ntfy_topic:
            p.ntfy_topic = new_topic()
    if data.get("rotate_topic") is True:
        p.ntfy_topic = new_topic()
    if "gntp_host" in data or "gntp_port" in data or "gntp_password" in data or data.get("gntp_enabled"):
        if not notify.gntp_networks():
            raise ValueError("GNTP for users is not enabled on this server (NOTIFY_USER_GNTP_ALLOW is empty)")
    if "gntp_host" in data:
        host = str(data["gntp_host"] or "").strip()
        if host and not notify.gntp_host_allowed(host):
            raise ValueError(f"GNTP host {host!r} must resolve inside the networks the admin allows: " + ", ".join(str(n) for n in notify.gntp_networks()))
        p.gntp_host = host
    if "gntp_port" in data:
        try:
            port = int(data["gntp_port"])
        except (TypeError, ValueError):
            raise ValueError("gntp_port must be a number") from None
        if not 1 <= port <= 65535:
            raise ValueError("gntp_port must be between 1 and 65535")
        p.gntp_port = port
    if "gntp_password" in data and data["gntp_password"] not in (None, ""):
        pw = str(data["gntp_password"])
        if "\n" in pw or "\r" in pw:
            raise ValueError("invalid GNTP password")
        p.gntp_password = pw
    if data.get("clear_gntp_password") is True:
        p.gntp_password_enc = ""
    if "gntp_enabled" in data:
        want = _bool(data, "gntp_enabled")
        if want and not p.gntp_host:
            raise ValueError("set gntp_host before enabling GNTP")
        p.gntp_enabled = want
    if "events" in data:
        ev = data["events"]
        if ev in (None, "", "default") or ev == []:
            p.events = ""
        else:
            names = ev.split(",") if isinstance(ev, str) else list(ev)
            canon = notify.canonical_events(names, warn=False)
            bad = [n for n in names if str(n).strip().lower() not in notify.KINDS and str(n).strip().lower() not in notify.ALIASES]
            if bad or not canon:
                raise ValueError(f"unknown event(s) {bad or names}; choose from: {', '.join(notify.KINDS)}")
            p.events = ",".join(sorted(canon))
    if "retry_interval" in data:
        ri = data["retry_interval"]
        if ri in (None, "", "default"):
            p.retry_interval = None
        else:
            try:
                ri = int(ri)
            except (TypeError, ValueError):
                raise ValueError("retry_interval must be a number of seconds (0 = every retry, -1 = never)") from None
            if not -1 <= ri <= 86400:
                raise ValueError("retry_interval must be between -1 and 86400")
            p.retry_interval = ri
    p.save()
    return to_dict(user)


def test(user, scope="me"):
    """(http_status, payload) for a test notification to the user's own targets ("me") or the global channel ("global", staff)."""
    if scope == "global":
        if not user.is_staff:
            return 403, {"error": {"code": "forbidden", "message": "Staff only"}}
        target = notify.global_target()
        if target is None:
            return 400, {"error": {"code": "not_configured", "message":
                                   "No global notification transport configured: set NTFY_URL + NTFY_TOPIC and/or GNTP_HOST in the server environment"}}
    elif scope == "me":
        target = notify.user_target(user.pk)
        if target is None:
            return 400, {"error": {"code": "no_target", "message":
                                   "You have no notification target enabled (turn on ntfy and/or GNTP first, and make sure notifications are enabled)"}}
    else:
        return 400, {"error": {"code": "bad_request", "message": "target must be 'me' or 'global'"}}
    return 200, {"target": scope, "results": notify.send_test(target), "events": sorted(target.events), "retry_interval": target.interval}
