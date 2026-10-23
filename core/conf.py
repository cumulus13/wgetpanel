"""Database backed settings.

    from core import conf
    conf.get("scheduler.max_downloads")
    conf.set("scheduler.max_downloads", 5)

A new setting is one entry in ``REGISTRY``; the Settings page is generated from it.
Values are cached for 5 s per process.
"""
import time
from dataclasses import dataclass, field

from django.conf import settings as dj_settings

_CACHE = {"at": 0.0, "values": {}}
CACHE_TTL = 5.0


@dataclass(frozen=True)
class Opt:
    key: str
    type: str  # str | int | bool | choice | lines
    default: object
    label: str
    help: str = ""
    choices: tuple = field(default_factory=tuple)
    min: int | None = None
    max: int | None = None
    group: str = "General"


def _default_block_private():
    return bool(dj_settings.WGP_BLOCK_PRIVATE_ADDRESSES)


REGISTRY = {
    o.key: o
    for o in [
        Opt("site.name", "str", "wgetpanel", "Site name", group="General"),
        Opt("ui.theme", "choice", "auto", "Default theme", choices=("auto", "light", "dark"), group="General"),
        Opt("ui.poll_interval_ms", "int", 1000, "Dashboard refresh (ms)", min=500, max=60000, group="General"),
        Opt("scheduler.max_downloads", "int", 3, "Max simultaneous downloads",
            "0 = unlimited. Extra downloads stay queued (FIFO) until a slot frees up.", min=0, max=1000, group="Scheduler"),
        Opt("limits.max_queue_per_user", "int", 200, "Max unfinished downloads per user", "0 = unlimited.", min=0, max=100000, group="Scheduler"),
        Opt("history.retention_days", "int", 30, "Keep finished downloads (days)", "0 = keep forever.", min=0, max=3650, group="History"),
        Opt("retry.default_max", "int", 0, "Auto retry: default attempts",
            "0 = off, -1 = retry forever. Only transient errors are retried (network failure, HTTP 408/425/429/5xx); a 404 or a wrong password never is. "
            "Failures are counted in a row: a run that downloaded new bytes resets the counter.", min=-1, max=1000, group="Retry"),
        Opt("retry.delay_seconds", "int", 30, "Auto retry: first delay (seconds)", "Doubles after every failed attempt in a row.", min=1, max=86400, group="Retry"),
        Opt("retry.max_delay_seconds", "int", 900, "Auto retry: longest delay (seconds)", min=1, max=604800, group="Retry"),
        Opt("files.delete_on_remove", "bool", False, "Delete the file from disk when a download is removed or canceled",
            "Default when the request does not say. Only files inside the allowed download directories are ever deleted.", group="Files"),
        Opt("files.delete_on_retention", "bool", False, "Delete files of finished downloads removed by the retention cleanup", group="Files"),
        Opt("wget.default_tries", "int", 5, "Default retries (--tries)", min=0, max=100, group="wget"),
        Opt("wget.timeout", "int", 30, "Network timeout (seconds)", min=1, max=3600, group="wget"),
        Opt("wget.user_agent", "str", "", "Default User-Agent", "Empty = wget's own.", group="wget"),
        Opt("wget.default_limit_rate", "int", 0, "Default speed limit per download (KB/s)", "0 = unlimited.", min=0, max=10_000_000, group="wget"),
        Opt("security.allowed_download_dirs", "lines", "", "Allowed download directories",
            "One absolute path per line. The download root from the environment is always allowed.", group="Security"),
        Opt("security.block_private_addresses", "bool", None, "Block private / loopback addresses",
            "SSRF guard: reject URLs whose host resolves to a non-public address. Redirects are NOT re-checked by wget, "
            "use an egress firewall for strong isolation.", group="Security"),
    ]
}


def _load():
    from core.models import Setting

    now = time.monotonic()
    if now - _CACHE["at"] < CACHE_TTL and _CACHE["values"] is not None and _CACHE["at"]:
        return _CACHE["values"]
    values = {s.key: s.value for s in Setting.objects.all()}
    _CACHE.update(at=now, values=values)
    return values


def invalidate():
    _CACHE.update(at=0.0, values={})


def _coerce(opt: Opt, value):
    if opt.type == "int":
        v = int(value)
        if opt.min is not None and v < opt.min:
            raise ValueError(f"{opt.label}: minimum is {opt.min}")
        if opt.max is not None and v > opt.max:
            raise ValueError(f"{opt.label}: maximum is {opt.max}")
        return v
    if opt.type == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    if opt.type == "choice":
        if value not in opt.choices:
            raise ValueError(f"{opt.label}: must be one of {', '.join(opt.choices)}")
        return value
    value = "" if value is None else str(value)
    if opt.type == "str" and ("\n" in value or "\r" in value):
        raise ValueError(f"{opt.label}: single line only")
    return value.strip() if opt.type == "str" else "\n".join(l.strip() for l in value.splitlines() if l.strip())


def default(key):
    opt = REGISTRY[key]
    if key == "security.block_private_addresses":
        return _default_block_private()
    return opt.default


def get(key):
    opt = REGISTRY[key]
    values = _load()
    if key in values:
        try:
            return _coerce(opt, values[key])
        except (ValueError, TypeError):
            pass
    return default(key)


def set(key, value):  # noqa: A001 - mirrors get()
    from core.models import Setting

    opt = REGISTRY[key]
    value = _coerce(opt, value)
    Setting.objects.update_or_create(key=key, defaults={"value": value})
    invalidate()
    return value


def all_values():
    return {k: get(k) for k in REGISTRY}


def allowed_dirs():
    import os

    roots = [os.path.realpath(dj_settings.WGP_DOWNLOAD_ROOT)]
    for line in str(get("security.allowed_download_dirs")).splitlines():
        line = line.strip()
        if line and os.path.isabs(line):
            roots.append(os.path.realpath(line))
    return roots
