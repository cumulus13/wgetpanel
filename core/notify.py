"""Notifications to ntfy and GNTP (Growl) when a download completes, fails, is paused or is retried.

Targets
  * the *global* channel configured in the environment (NTFY_URL + NTFY_TOPIC, GNTP_HOST); it receives the events of all
    users unless NOTIFY_GLOBAL_SCOPE=none;
  * the *owner's own* channel (core.notify_prefs): an unguessable ntfy topic generated per user and, if the admin allows
    it, a GNTP receiver of their own. Users only ever get their own downloads.

Nothing is sent unless a target is configured. Sending happens in a Celery task (a slow notification server never delays a
download or a web request); the task payload carries only the message and a target key, secrets are looked up at delivery.

Retry notices are throttled **per download and per target** with a leading edge: the first retry is announced at once,
further retries inside the target's retry interval are only counted, and the next notice says how many were skipped. The
retry delay itself has no influence. Every other event is sent immediately; a final ``completed`` / ``error`` message
reports how often the download was retried and closes the throttle window.
"""
import base64
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit
import urllib.error
import urllib.request

from django.conf import settings

from . import live
from .models import Download, UserNotify

log = logging.getLogger(__name__)

KINDS = ("completed", "error", "paused", "retry", "canceled")
FINAL = ("completed", "error", "canceled")
ALIASES = {"success": "completed", "done": "completed", "finished": "completed", "failed": "error", "failure": "error",
           "pause": "paused", "cancel": "canceled"}
LABEL = {"completed": "Download complete", "error": "Download failed", "paused": "Download paused", "retry": "Retrying download",
         "canceled": "Download canceled"}
NTFY_PRIORITY = {"completed": 3, "error": 4, "paused": 2, "retry": 2, "canceled": 2}
NTFY_TAGS = {"completed": ["white_check_mark"], "error": ["x"], "paused": ["pause_button"], "retry": ["repeat"], "canceled": ["no_entry_sign"]}
GNTP_PRIORITY = {"completed": 0, "error": 1, "paused": 0, "retry": -1, "canceled": -1}
STATUS_RE = re.compile(r"awaiting response\.\.\.\s+(\d{3}[^\n]*)")


# ------------------------------------------------------------------ configuration
def canonical_events(names, warn=True):
    out = set()
    for name in names:
        name = ALIASES.get(str(name).strip().lower(), str(name).strip().lower())
        if name in KINDS:
            out.add(name)
        elif warn:
            log.warning("unknown notification event %r ignored (known: %s)", name, ", ".join(KINDS))
    return out


def enabled_events():
    return canonical_events(settings.NOTIFY_EVENTS)


def gntp_networks():
    nets = []
    for token in settings.NOTIFY_USER_GNTP_ALLOW:
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            log.warning("NOTIFY_USER_GNTP_ALLOW: %r is not a CIDR network, ignored", token)
    return nets


def gntp_host_allowed(host):
    """True if every address ``host`` resolves to lies inside NOTIFY_USER_GNTP_ALLOW (checked again at every send)."""
    nets = gntp_networks()
    if not nets or not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        ips = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    except (OSError, ValueError):
        return False
    return bool(ips) and all(any(ip in n for n in nets) for ip in ips)


@dataclass
class Target:
    key: str  # "g" (global) or "u<user id>"
    ntfy: dict = None
    gntp: dict = None
    events: set = field(default_factory=set)
    interval: int = 60
    show_owner: bool = True

    def transports(self):
        return [n for n in ("ntfy", "gntp") if getattr(self, n)]


def _ntfy_auth():
    return {"url": settings.NTFY_URL, "token": settings.NTFY_TOKEN, "user": settings.NTFY_USER, "password": settings.NTFY_PASSWORD}


def global_target():
    ntfy = {**_ntfy_auth(), "topic": settings.NTFY_TOPIC} if settings.NTFY_URL and settings.NTFY_TOPIC else None
    gntp = {"host": settings.GNTP_HOST, "port": settings.GNTP_PORT, "password": settings.GNTP_PASSWORD, "app": settings.GNTP_APP_NAME} if settings.GNTP_HOST else None
    if not (ntfy or gntp):
        return None
    return Target("g", ntfy, gntp, enabled_events(), settings.NOTIFY_RETRY_INTERVAL, show_owner=True)


def user_target(user_id):
    prefs = UserNotify.objects.filter(user_id=user_id, enabled=True).first()
    if prefs is None:
        return None
    ntfy = {**_ntfy_auth(), "topic": prefs.ntfy_topic} if prefs.ntfy_enabled and prefs.ntfy_topic and settings.NTFY_URL else None
    gntp = None
    if prefs.gntp_enabled and prefs.gntp_host:
        if gntp_host_allowed(prefs.gntp_host):
            gntp = {"host": prefs.gntp_host, "port": prefs.gntp_port, "password": prefs.gntp_password, "app": settings.GNTP_APP_NAME}
        else:
            log.warning("user %s: GNTP host %r is outside NOTIFY_USER_GNTP_ALLOW, not used", user_id, prefs.gntp_host)
    if not (ntfy or gntp):
        return None
    events = canonical_events(prefs.events.split(","), warn=False) if prefs.events else enabled_events()
    interval = prefs.retry_interval if prefs.retry_interval is not None else settings.NOTIFY_RETRY_INTERVAL
    return Target(f"u{user_id}", ntfy, gntp, events, interval, show_owner=False)


def resolve(key):
    if key == "g":
        return global_target()
    if key.startswith("u") and key[1:].isdigit():
        return user_target(int(key[1:]))
    return None


def targets_for(download):
    out = []
    if settings.NOTIFY_GLOBAL_SCOPE == "all":
        t = global_target()
        if t:
            out.append(t)
    if download.owner_id:
        t = user_target(download.owner_id)
        if t:
            out.append(t)
    return out


def transports():
    """Transports of the global channel (kept for the status endpoint / tests)."""
    t = global_target()
    return t.transports() if t else []


def anything_configured():
    return bool(global_target() or settings.NTFY_URL or settings.NOTIFY_USER_GNTP_ALLOW)


# ------------------------------------------------------------------ message text
def _size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _dur(sec):
    sec = int(max(sec, 0))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _live(d):
    try:
        return live.get_live_many([d.pk]).get(d.pk) or {}
    except Exception:
        return {}


def build_message(kind, d, extra=None, suppressed=0, show_owner=True):
    """Plain text title/body. The URL is never included (signed links carry secrets): only its host."""
    extra = extra or {}
    host = urlsplit(d.url).hostname or ""
    lines = []
    if kind == "completed":
        size = d.total_bytes or d.downloaded_bytes
        took = (d.finished_at - d.started_at).total_seconds() if d.finished_at and d.started_at else None
        lines.append(f"{_size(size)}" + (f" in {_dur(took)}" if took is not None else ""))
        if d.retry_total:
            lines.append(f"Succeeded after {d.retry_total} automatic retr{'y' if d.retry_total == 1 else 'ies'}")
    elif kind == "error":
        first = (d.error or "").split("\n")[0] or "unknown error"
        lines.append(first)
        m = STATUS_RE.search(d.error or "")
        if m:
            lines.append(f"Server answered: {m.group(1).strip()}")
        if d.retry_total:
            lines.append(f"Gave up after {d.retry_total} automatic retr{'y' if d.retry_total == 1 else 'ies'}")
    elif kind == "paused":
        lv = _live(d)
        done = int(lv.get("downloaded") or d.downloaded_bytes or 0)
        total = int(lv["total"]) if lv.get("total") else d.total_bytes
        lines.append(f"{_size(done)}" + (f" of {_size(total)} ({done * 100 / total:.1f}%)" if total else ""))
    elif kind == "retry":
        limit = extra.get("limit", 0)
        lines.append(f"Attempt {extra.get('count', d.retry_count)} of {'unlimited' if limit == -1 else limit} failed: {extra.get('reason', 'error')}")
        lines.append(f"Next try in {_dur(extra.get('delay', 0))}")
        if suppressed:
            lines.append(f"{suppressed} more retr{'y' if suppressed == 1 else 'ies'} since the last notice")
    elif kind == "canceled":
        lines.append("Canceled by user")
    meta = " · ".join(x for x in (f"from {host}" if host else "", f"by {d.owner.get_username()}" if show_owner and d.owner_id else "") if x)
    if meta:
        lines.append(meta)
    panel = settings.NOTIFY_PANEL_URL
    return {"kind": kind, "id": d.pk, "title": f"{LABEL[kind]}: {d.filename}", "body": "\n".join(lines), "click": panel + "/" if panel else "",
            "priority": NTFY_PRIORITY[kind], "tags": NTFY_TAGS[kind], "sticky": kind == "error", "gntp_priority": GNTP_PRIORITY[kind]}


# ------------------------------------------------------------------ entry point used by the runner / services
def _keys(pk, target):
    return f"wgp:notify:retry:{pk}:{target}", f"wgp:notify:retry_skipped:{pk}:{target}"


def dispatch(kind, pk, **extra):
    """Queue notifications for download ``pk`` (one per interested target). Never raises. True if any was queued."""
    try:
        kind = ALIASES.get(kind, kind)
        if not anything_configured():
            return False
        d = Download.objects.select_related("owner").filter(pk=pk).first()
        if d is None:
            return False
        from .tasks import send_notification

        queued = False
        for t in targets_for(d):
            if kind in FINAL:  # the retry series of this download is over for this target
                try:
                    live.client().delete(*_keys(pk, t.key))
                except Exception:
                    pass
            if kind not in t.events:
                continue
            suppressed = 0
            if kind == "retry":
                if t.interval < 0:
                    continue
                if t.interval > 0:
                    gate, skipped = _keys(pk, t.key)
                    r = live.client()
                    if not r.set(gate, "1", nx=True, ex=t.interval):
                        r.incr(skipped)
                        r.expire(skipped, 86400)
                        continue
                    pipe = r.pipeline()
                    pipe.get(skipped)
                    pipe.delete(skipped)
                    suppressed = int(pipe.execute()[0] or 0)
            msg = build_message(kind, d, extra, suppressed, show_owner=t.show_owner)
            msg["target"] = t.key
            send_notification.delay(msg)
            queued = True
        return queued
    except Exception:
        log.warning("could not queue %s notification for download %s", kind, pk, exc_info=True)
        return False


# ------------------------------------------------------------------ delivery (runs in the Celery task)
def _send_ntfy(cfg, msg):
    body = {"topic": cfg["topic"], "title": msg["title"][:250], "message": msg["body"][:3500] or msg["title"],
            "priority": msg["priority"], "tags": msg["tags"]}
    if msg.get("click"):
        body["click"] = msg["click"]
    headers = {"Content-Type": "application/json"}
    if cfg.get("token"):
        headers["Authorization"] = "Bearer " + cfg["token"]
    elif cfg.get("user"):
        headers["Authorization"] = "Basic " + base64.b64encode(f"{cfg['user']}:{cfg.get('password', '')}".encode()).decode()
    if urlsplit(cfg["url"]).scheme not in ("http", "https"):
        raise RuntimeError("NTFY_URL must start with http:// or https://")
    req = urllib.request.Request(cfg["url"] + "/", data=json.dumps(body).encode(), headers=headers, method="POST")  # JSON goes to the ROOT url
    try:
        with urllib.request.urlopen(req, timeout=settings.NOTIFY_TIMEOUT) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ntfy answered HTTP {exc.code}: {exc.read(200).decode('utf-8', 'replace')}") from exc


def _send_gntp(cfg, msg):
    import gntp.notifier

    notifier = gntp.notifier.GrowlNotifier(
        applicationName=cfg.get("app") or "wgetpanel", notifications=list(KINDS), defaultNotifications=list(KINDS),
        hostname=cfg["host"], port=cfg["port"], password=cfg.get("password") or None)
    notifier.socketTimeout = settings.NOTIFY_TIMEOUT
    result = notifier.register()  # the library returns an error object instead of raising
    if result is not True:
        raise RuntimeError(f"GNTP register refused: {result}")
    text = msg["body"] + (f"\n{msg['click']}" if msg.get("click") else "")
    result = notifier.notify(noteType=msg["kind"], title=msg["title"], description=text, sticky=msg["sticky"], priority=msg["gntp_priority"])
    if result is not True:
        raise RuntimeError(f"GNTP notify refused: {result}")


SENDERS = {"ntfy": _send_ntfy, "gntp": _send_gntp}


def deliver(msg, only=None):
    """Send to every transport of the message's target (default: global), or just ``only``.
    Returns {transport: error text} for the failures. A target that was disabled meanwhile is skipped."""
    target = resolve(msg.get("target", "g"))
    failed = {}
    if target is None:
        return failed
    for name in target.transports():
        if only and name not in only:
            continue
        try:
            SENDERS[name](getattr(target, name), msg)
        except Exception as exc:
            log.warning("notification via %s (target %s) failed: %s", name, target.key, exc)
            failed[name] = f"{exc.__class__.__name__}: {exc}"
    return failed


def send_test(target):
    """Synchronous test message to ``target``: {transport: 'ok' | error text}."""
    msg = {"kind": "completed", "id": 0, "target": target.key, "title": "wgetpanel test notification", "body": "If you can read this, notifications work.",
           "click": settings.NOTIFY_PANEL_URL + "/" if settings.NOTIFY_PANEL_URL else "", "priority": 3, "tags": ["bell"], "sticky": False, "gntp_priority": 0}
    failed = deliver(msg)
    return {name: failed.get(name, "ok") for name in target.transports()}
