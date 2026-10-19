"""Everything that turns user input into a safe wget invocation.

User input never reaches a shell and never becomes a raw command line option:
options are validated against a whitelist and written to a private wgetrc
(mode 0600, so credentials never show up in ``ps``).
"""
import ipaddress
import os
import re
import socket
import tempfile
from urllib.parse import unquote, urlsplit

from django.conf import settings

from . import conf

ALLOWED_SCHEMES = ("http", "https", "ftp")
MAX_URL_LEN = 4096
RATE_RE = re.compile(r"^(\d+)([kKmM]?)$")
HEADER_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}: ?[^\r\n]{0,1024}$")
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}

EXIT_MESSAGES = {
    1: "Generic error",
    2: "Parse error",
    3: "File I/O error (disk full or permission denied?)",
    4: "Network failure",
    5: "SSL verification failure",
    6: "Username/password authentication failure",
    7: "Protocol error",
    8: "Server issued an error response",
}


class ValidationError(ValueError):
    pass


# ---------------------------------------------------------------- urls
def _is_public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_global
    except ValueError:
        return False


def validate_url(url: str, block_private=None) -> str:
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LEN:
        raise ValidationError("URL is empty or too long")
    if any(c in url for c in "\r\n\x00") or " " in url:
        raise ValidationError("URL contains whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValidationError(f"Scheme must be one of {', '.join(ALLOWED_SCHEMES)}")
    if not parts.hostname:
        raise ValidationError("URL has no host")
    if parts.username or parts.password:
        raise ValidationError("Do not put credentials in the URL, use the username/password fields")
    if block_private is None:
        block_private = conf.get("security.block_private_addresses")
    if block_private:
        try:
            infos = socket.getaddrinfo(parts.hostname, parts.port or 80, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise ValidationError(f"Cannot resolve host {parts.hostname}: {exc}") from exc
        if not infos or not all(_is_public(i[4][0]) for i in infos):
            raise ValidationError("Host resolves to a private, loopback or otherwise non-public address")
    return url


# ---------------------------------------------------------------- paths
def safe_filename(name: str) -> str:
    name = unquote(name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    if name in ("", ".", ".."):
        return ""
    return name[:200]


def filename_from_url(url: str) -> str:
    return safe_filename(urlsplit(url).path) or "download"


def validate_directory(directory: str | None) -> str:
    roots = conf.allowed_dirs()
    directory = (directory or "").strip() or roots[0]
    if not os.path.isabs(directory):
        directory = os.path.join(roots[0], directory)
    real = os.path.realpath(directory)
    if not any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
        raise ValidationError("Directory is outside the allowed download directories")
    return real


def unique_name(directory: str, filename: str, taken: set[str]) -> str:
    base, ext = os.path.splitext(filename)
    candidate, n = filename, 0
    while os.path.exists(os.path.join(directory, candidate)) or os.path.join(directory, candidate) in taken:
        n += 1
        candidate = f"{base} ({n}){ext}"
    return candidate


# ---------------------------------------------------------------- options
def parse_rate(value) -> int:
    """'500k' / '2m' / '1024' -> bytes per second (wget semantics: k=1024)."""
    m = RATE_RE.match(str(value).strip())
    if not m:
        raise ValidationError("Speed limit must look like 500k, 2m or a plain number of bytes")
    n = int(m.group(1))
    return n * {"": 1, "k": 1024, "m": 1024 * 1024}[m.group(2).lower()]


def validate_options(opts: dict | None) -> dict:
    """Whitelist of per download options."""
    opts = dict(opts or {})
    out = {}
    if opts.get("limit_rate") not in (None, "", 0, "0"):
        out["limit_rate"] = parse_rate(opts["limit_rate"])
    for key, lo, hi in (("tries", 0, 100), ("timeout", 1, 3600)):
        if opts.get(key) not in (None, ""):
            try:
                v = int(opts[key])
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{key} must be a number") from exc
            if not lo <= v <= hi:
                raise ValidationError(f"{key} must be between {lo} and {hi}")
            out[key] = v
    for key in ("user_agent", "referer"):
        v = (opts.get(key) or "").strip()
        if v:
            if len(v) > 1024 or any(c in v for c in "\r\n\x00"):
                raise ValidationError(f"Invalid {key}")
            out[key] = v
    if "check_certificate" in opts and opts["check_certificate"] in (False, "false", "0", 0, "off"):
        out["check_certificate"] = False
    headers = opts.get("headers") or []
    if isinstance(headers, str):
        headers = [h for h in headers.splitlines() if h.strip()]
    if len(headers) > 10:
        raise ValidationError("At most 10 custom headers")
    clean = []
    for h in headers:
        h = h.strip()
        if not HEADER_RE.match(h) or h.split(":", 1)[0].lower() in FORBIDDEN_HEADERS:
            raise ValidationError(f"Invalid or forbidden header: {h[:60]!r}")
        clean.append(h)
    if clean:
        out["headers"] = clean
    return out


def effective_rate(download, now=None) -> int:
    """Smallest non-zero limit among: download option, default setting, active speed rules (bytes/s, 0 = none)."""
    from .models import SpeedRule

    limits = []
    if download.options.get("limit_rate"):
        limits.append(int(download.options["limit_rate"]))
    default_kb = conf.get("wget.default_limit_rate")
    if default_kb:
        limits.append(default_kb * 1024)
    for rule in SpeedRule.objects.filter(enabled=True):
        if rule.limit_kbps and rule.is_active(now):
            limits.append(rule.limit_kbps * 1024)
    return min(limits) if limits else 0


# ---------------------------------------------------------------- command
def write_wgetrc(download, rate: int) -> str:
    """Private config file for one wget run. Caller removes it afterwards."""
    o = download.options
    lines = [
        f"tries = {o.get('tries', conf.get('wget.default_tries'))}",
        f"timeout = {o.get('timeout', conf.get('wget.timeout'))}",
        "continue = on",
        "robots = off",
        "hsts = off",
        "verbose = on",
        "dot_style = mega",
    ]
    if rate:
        lines.append(f"limit_rate = {rate}")
    ua = o.get("user_agent") or conf.get("wget.user_agent")
    if ua:
        lines.append(f"user_agent = {ua}")
    if o.get("referer"):
        lines.append(f"referer = {o['referer']}")
    if o.get("check_certificate") is False:
        lines.append("check_certificate = off")
    for h in o.get("headers", []):
        lines.append(f"header = {h}")
    if download.http_user:
        if any(c in download.http_user for c in "\r\n"):
            raise ValidationError("Invalid username")
        lines.append(f"http_user = {download.http_user}")
        pw = download.http_password
        if any(c in pw for c in "\r\n"):
            raise ValidationError("Invalid password")
        lines.append(f"http_password = {pw}")
        lines.append(f"ftp_user = {download.http_user}")
        lines.append(f"ftp_password = {pw}")
    fd, path = tempfile.mkstemp(prefix=f"wgp-{download.pk}-", suffix=".wgetrc")
    with os.fdopen(fd, "w") as fh:  # mkstemp creates the file 0600
        fh.write("\n".join(lines) + "\n")
    return path


def build_command(download, rcfile: str) -> list[str]:
    return [
        settings.WGP_WGET_BINARY,
        f"--config={rcfile}",
        "--progress=dot:mega",
        "-a", download.log_path,
        "-O", download.path,
        "--", download.url,
    ]


def describe_exit(code: int, log_tail: str = "") -> str:
    msg = EXIT_MESSAGES.get(code, f"wget exited with status {code}")
    return f"{msg} (exit {code})" + (f"\n{log_tail}" if log_tail else "")
