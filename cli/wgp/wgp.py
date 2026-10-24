#!/usr/bin/env python3

# File: cli/wgp/wgp.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-24
# Description: wgp - command line client for wgetpanel (API v1)
# License: MIT

"""wgp - command line client for wgetpanel (API v1).

    wgp login http://localhost:8000 -u admin          # token is stored in ~/.config/wgetpanel/config.json (0600)
    wgp add https://example.com/big.iso --retry always --limit 500 --at 02:00
    wgp list | wgp show 12 --log | wgp graph 12 | wgp watch
    wgp pause 12 | wgp cancel 12 --delete-files | wgp get 12 -o ~/Downloads
    wgp settings | wgp settings set scheduler.max_downloads 5 | wgp rules add Office --from 09:00 --to 17:00 --limit 256

Environment: WGP_HOST, WGP_TOKEN, WGP_CONFIG (config file), NO_COLOR, WGP_ASCII=1 (no unicode bars).
Exit codes: 0 ok, 1 the server refused / nothing done / a waited download failed, 2 usage or connection problem.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from urllib3.exceptions import HTTPError as Urllib3Error

try:
    import asciichartpy
except ImportError:  # the graph commands then explain what to install
    asciichartpy = None

__version__ = "0.1.0"
TERMINAL = ("completed", "error", "canceled")
STATUSES = ("queued", "running", "paused", "completed", "error", "canceled")
ACTION_VERBS = {"pause": "paused", "resume": "resumed", "retry": "re-queued", "cancel": "canceled", "remove": "removed",
                "top": "moved to top", "bottom": "moved to bottom", "up": "moved up", "down": "moved down", "purge": "purged"}


class CliError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ output helpers
def _unicode():
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    return enc.startswith("utf") and os.environ.get("WGP_ASCII") != "1"


def _color_on():
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text, *codes):
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if _color_on() and codes else str(text)


STATUS_COLOR = {"running": "34", "queued": "37", "paused": "33", "completed": "32", "error": "31", "canceled": "90"}


def human_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_rate(n):
    return human_bytes(n) + "/s"


def human_eta(s):
    if s is None:
        return "-"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def human_time(iso):
    if not iso:
        return "-"
    try:
        return dt.datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def term_width(default=100):
    return shutil.get_terminal_size((default, 24)).columns


def clip(text, width):
    text = str(text).replace("\n", " ")
    return text if len(text) <= width else text[: max(width - 1, 0)] + ("…" if _unicode() else "~")


def bar(pct, width=14):
    full, empty = ("█", "░") if _unicode() else ("#", "-")
    if pct is None:
        return "?" * width if not _unicode() else "·" * width
    n = int(round(max(0, min(pct, 100)) / 100 * width))
    return full * n + empty * (width - n)


def table(headers, rows, widths=None):
    """Left aligned text table fitted to the terminal; the first column that is allowed to shrink is the 2nd."""
    cols = len(headers)
    natural = [max(len(str(h)), *(len(_plain(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
    avail = term_width() - 2 * (cols - 1)
    if sum(natural) > avail and cols > 1:
        shrink = max(range(cols), key=lambda i: natural[i])
        natural[shrink] = max(10, natural[shrink] - (sum(natural) - avail))
    out = ["  ".join(str(h).ljust(natural[i]) for i, h in enumerate(headers))]
    out[0] = paint(out[0], "1")
    for r in rows:
        cells = []
        for i, cell in enumerate(r):
            txt = _plain(cell)
            pad = natural[i] - len(txt)
            if len(txt) > natural[i]:
                cell, pad = clip(txt, natural[i]), 0
            cells.append(str(cell) + " " * max(pad, 0))
        out.append("  ".join(cells))
    return "\n".join(out)


def _plain(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(s))


def downsample(values, n):
    """Average ``values`` into at most ``n`` buckets."""
    if n <= 0 or len(values) <= n:
        return list(values)
    step, out = len(values) / n, []
    for i in range(n):
        lo = int(i * step)
        chunk = values[lo: max(int((i + 1) * step), lo + 1)]
        out.append(sum(chunk) / len(chunk))
    return out


def speed_graph(series, height=10, width=None, title="speed"):
    """ASCII line chart (asciichartpy) of [{'t': unix, 'speed': bytes/s}, ...]; unit chosen from the peak."""
    if asciichartpy is None:
        return "(install asciichartpy for graphs:  pip install asciichartpy)"
    if not series:
        return "(no speed data yet)"
    width = width or term_width()
    peak = max(p["speed"] for p in series)
    unit, div = "B/s", 1
    for name, d in (("MB/s", 1024 ** 2), ("KB/s", 1024)):
        if peak >= d:
            unit, div = name, d
            break
    vals = downsample([p["speed"] / div for p in series], max(width - 14, 10))
    cfg = {"height": height, "format": "{:8.1f} "}
    if _color_on() and hasattr(asciichartpy, "blue"):
        cfg["colors"] = [asciichartpy.blue]
    chart = asciichartpy.plot(vals, cfg)
    span = int(series[-1]["t"] - series[0]["t"])
    return f"{title} ({unit}), last {human_eta(span)}\n{chart}"


# ------------------------------------------------------------------ config
def config_path():
    if os.environ.get("WGP_CONFIG"):
        return Path(os.environ["WGP_CONFIG"])
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "wgetpanel" / "config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "wgetpanel" / "config.json"


def load_config():
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=2)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def normalize_host(host):
    host = (host or "").strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", host):
        raise CliError("host must start with http:// or https:// (e.g. http://localhost:8000)", 2)
    return host


# ------------------------------------------------------------------ API client
class Api:
    def __init__(self, host, token=None, timeout=30, verify=True):
        self.host, self.token, self.timeout, self.verify = host, token, timeout, verify
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"wgp-cli/{__version__}"

    def _headers(self, auth=True, extra=None):
        h = dict(extra or {})
        if auth:
            if not self.token:
                raise CliError("not logged in: run  wgp login <host>  (or set WGP_HOST / WGP_TOKEN)", 2)
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def request(self, method, path, body=None, params=None, auth=True, stream=False, timeout=None):
        try:
            return self.session.request(method, self.host + path, json=body, params=params, headers=self._headers(auth), stream=stream,
                                        timeout=timeout or self.timeout, verify=self.verify)
        except requests.exceptions.SSLError as exc:
            raise CliError(f"TLS error talking to {self.host}: {exc}", 2) from exc
        except requests.exceptions.RequestException as exc:
            raise CliError(f"cannot reach {self.host}: {exc.__class__.__name__}: {exc}", 2) from exc

    def call(self, method, path, body=None, params=None, auth=True, ok=(200, 201)):
        """JSON result; HTTP errors become CliError with the server's message."""
        r = self.request(method, path, body, params, auth)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code in ok:
            return data
        raise CliError(error_text(r.status_code, data), 2 if r.status_code == 401 else 1)


def error_text(status, data):
    err = data.get("error")
    msg = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else None)
    if status == 401 and (not err or (isinstance(err, dict) and err.get("code") == "unauthorized")):
        return "not logged in or token expired/revoked: run  wgp login <host>"
    extra = []
    for e in data.get("errors") or []:
        extra.append(f"{e.get('key', '')}{': ' if e.get('key') else ''}{e.get('error', e)}" if isinstance(e, dict) else str(e))
    if isinstance(data.get("errors"), dict):
        extra = [f"{k}: {v}" for k, v in data["errors"].items()]
    if data.get("unknown"):
        extra.append("unknown keys: " + ", ".join(data["unknown"]))
    text = msg or f"HTTP {status}"
    return text + ("\n  " + "\n  ".join(extra) if extra else "")


def iter_chunks(resp):
    """Bytes as they arrive. ``read1`` returns whatever is available, which also works for non-chunked (HTTP/1.0) bodies where
    iter_content(None) would block until the stream ends."""
    read1 = getattr(resp.raw, "read1", None)
    if read1 is None:
        yield from resp.iter_content(chunk_size=1)
        return
    while True:
        chunk = read1(65536)
        if not chunk:
            return
        yield chunk


def sse_events(resp):
    """Yield (event, data) from a streaming response; ``("keepalive", None)`` for comment-only blocks."""
    buf = ""
    for chunk in iter_chunks(resp):
        buf += chunk.decode("utf-8", "replace")
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            ev, data = "message", []
            for line in block.splitlines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
            if data:
                try:
                    yield ev, json.loads("\n".join(data))
                except ValueError:
                    continue
            else:
                yield "keepalive", None


def stream_events(api, max_seconds=None, on_state=None):
    """Endless event iterator with automatic reconnect (exponential backoff). Ends after ``max_seconds``."""
    started, backoff = time.monotonic(), 1

    def left():
        return None if max_seconds is None else max_seconds - (time.monotonic() - started)

    while True:
        if left() is not None and left() <= 0:
            return
        read_timeout = 20 if left() is None else max(0.5, min(20, left() + 0.5))  # server keep-alive is 15 s
        try:
            r = api.request("GET", "/api/v1/events", stream=True, timeout=(10, read_timeout))
            if r.status_code != 200:
                try:
                    data = r.json()
                except ValueError:
                    data = {}
                raise CliError(error_text(r.status_code, data), 2 if r.status_code == 401 else 1)
            if on_state:
                on_state("connected")
            backoff = 1
            for ev, data in sse_events(r):
                if left() is not None and left() <= 0:
                    r.close()
                    return
                if ev != "keepalive":
                    yield ev, data
        except (requests.exceptions.RequestException, Urllib3Error, OSError, CliError) as exc:
            if isinstance(exc, CliError) and exc.code == 2:
                raise
            if left() is not None and left() <= 0.6:
                return
            if on_state:
                on_state(f"disconnected ({exc.__class__.__name__}), retrying in {backoff}s")
        if left() is not None and left() <= 0:
            return
        time.sleep(backoff if left() is None else min(backoff, max(left(), 0)))
        backoff = min(backoff * 2, 30)


# ------------------------------------------------------------------ parsing helpers
def parse_ids(tokens):
    """['1', '3-5', '7,9'] -> [1, 3, 4, 5, 7, 9]"""
    ids = []
    for tok in tokens:
        for part in str(tok).split(","):
            part = part.strip().lstrip("#")
            if not part:
                continue
            m = re.fullmatch(r"(\d+)-(\d+)", part)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if b < a or b - a > 5000:
                    raise CliError(f"bad id range: {part}", 2)
                ids.extend(range(a, b + 1))
            elif part.isdigit():
                ids.append(int(part))
            else:
                raise CliError(f"bad id: {part!r} (use 12, 3-7 or 1,4,9)", 2)
    return list(dict.fromkeys(ids))


def parse_retry(value):
    v = str(value).strip().lower()
    if v in ("always", "infinite", "inf", "forever", "unlimited", "-1"):
        return "always"
    if v in ("off", "none", "no", "0"):
        return "off"
    if v == "default":
        return None
    if v.isdigit() and int(v) <= 1000:
        return int(v)
    raise CliError("--retry must be a number, 'always' or 'off'", 2)


def parse_when(at=None, delay=None, now=None):
    """--at '2026-10-24 02:00' | '02:00' (next occurrence) ; --in 90m | 2h30m | 1d  ->  ISO 8601 with local offset."""
    now = (now or dt.datetime.now()).astimezone()
    if delay:
        m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", delay.strip().lower())
        if not m or not any(m.groups()):
            raise CliError("--in expects e.g. 90m, 2h30m, 1d", 2)
        d, h, mi, s = (int(x or 0) for x in m.groups())
        return (now + dt.timedelta(days=d, hours=h, minutes=mi, seconds=s)).isoformat(timespec="seconds")
    if at:
        text = at.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return dt.datetime.strptime(text, fmt).astimezone().isoformat(timespec="seconds")
            except ValueError:
                pass
        try:
            t = dt.datetime.strptime(text, "%H:%M").time()
        except ValueError as exc:
            raise CliError("--at expects 'YYYY-MM-DD HH:MM' or 'HH:MM' (next occurrence)", 2) from exc
        cand = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if cand <= now:
            cand += dt.timedelta(days=1)
        return cand.isoformat(timespec="seconds")
    return None


DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_days(text):
    t = (text or "").strip().lower()
    if t in ("", "all", "every", "daily", "*"):
        return ""
    if t in ("weekdays", "weekday"):
        return "0,1,2,3,4"
    if t in ("weekend", "weekends"):
        return "5,6"
    out = []
    for part in t.split(","):
        part = part.strip()
        m = re.fullmatch(r"([a-z]{3})-([a-z]{3})", part)
        if m and m.group(1) in DAY_NAMES and m.group(2) in DAY_NAMES:
            a, b = DAY_NAMES.index(m.group(1)), DAY_NAMES.index(m.group(2))
            out.extend(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
        elif part[:3] in DAY_NAMES:
            out.append(DAY_NAMES.index(part[:3]))
        elif part.isdigit() and 0 <= int(part) <= 6:
            out.append(int(part))
        else:
            raise CliError(f"bad day {part!r}: use mon,tue / mon-fri / weekdays / weekend / 0-6", 2)
    return ",".join(str(d) for d in dict.fromkeys(out))


def days_text(days):
    return "every day" if not days else ",".join(DAY_NAMES[int(d)] for d in days.split(",") if d != "")


def read_urls(args):
    urls = list(args.urls or [])
    for path in args.file or []:
        fh = sys.stdin if path == "-" else open(path, encoding="utf-8")
        try:
            urls += [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]
        finally:
            if fh is not sys.stdin:
                fh.close()
    return urls


def emit_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ------------------------------------------------------------------ rendering of downloads
def status_cell(status):
    return paint(status, STATUS_COLOR.get(status, "0"))


def retry_text(d):
    lim = d.get("retry_limit", 0)
    if lim == 0:
        return "off"
    return f"{d.get('retry_count', 0)} failed in a row, limit {'always' if lim == -1 else lim}"


def download_rows(downloads, with_owner=False):
    rows = []
    for d in downloads:
        pct = 100.0 if d["status"] == "completed" else d.get("progress")
        progress = f"{bar(pct)} {'--' if pct is None else f'{pct:5.1f}%'}"
        eta = human_eta(d.get("eta")) if d["status"] == "running" else "-"
        if d["status"] == "queued" and d.get("not_before") and dt.datetime.fromisoformat(d["not_before"]) > dt.datetime.now().astimezone():
            eta = "at " + dt.datetime.fromisoformat(d["not_before"]).astimezone().strftime("%H:%M:%S")
        row = [d["id"], status_cell(d["status"]), d["filename"], progress, f"{human_bytes(d.get('downloaded'))}/{human_bytes(d.get('total'))}",
               human_rate(d["speed"]) if d["status"] == "running" else "-", eta]
        if with_owner:
            row.append(d.get("owner") or "-")
        rows.append(row)
    return rows


def download_table(downloads, with_owner=False):
    if not downloads:
        return "(no downloads)"
    headers = ["ID", "STATUS", "NAME", "PROGRESS", "SIZE", "SPEED", "ETA"] + (["OWNER"] if with_owner else [])
    return table(headers, download_rows(downloads, with_owner))


def summary_text(s):
    c = s["counts"]
    parts = [f"{paint(str(c[k]), STATUS_COLOR[k])} {k}" for k in STATUSES]
    slots = s["max_downloads"] or "no limit"
    health = []
    if not s.get("scheduler_alive"):
        health.append(paint("scheduler NOT running", "31"))
    if not s.get("redis"):
        health.append(paint("redis DOWN", "31"))
    line2 = f"speed {paint(human_rate(s['speed']), '1')}   slots {c['running']}/{slots}   " + ("  ".join(health) if health else "scheduler ok")
    return "  ".join(parts) + "\n" + line2


# ------------------------------------------------------------------ commands
def cmd_login(args, api, cfg):
    host = normalize_host(args.host or os.environ.get("WGP_HOST") or cfg.get("host"))
    user = args.user or input("username: ")
    password = args.password or os.environ.get("WGP_PASSWORD") or getpass.getpass("password: ")
    api = Api(host, timeout=args.timeout, verify=not args.insecure_tls)
    data = api.call("POST", "/api/v1/auth/token", {"username": user, "password": password, "device_name": args.device or socket.gethostname()}, auth=False)
    save_config({"host": host, "token": data["token"], "user": data["user"]["username"], "verify_tls": not args.insecure_tls})
    host_name = urlparse(host).hostname or ""
    if host.startswith("http://") and host_name not in ("localhost", "127.0.0.1", "::1"):
        print(paint("warning: plain http, the token travels unencrypted; use https", "33"), file=sys.stderr)
    print(f"logged in to {host} as {data['user']['username']}{' (staff)' if data['user']['is_staff'] else ''}; token saved to {config_path()}")
    return 0


def cmd_logout(args, api, cfg):
    try:
        api.call("DELETE", "/api/v1/auth/token")
        print("token revoked on the server")
    except CliError as exc:
        print(f"server side revoke skipped: {exc}", file=sys.stderr)
    cfg.pop("token", None)
    save_config(cfg)
    print("local token removed")
    return 0


def cmd_whoami(args, api, cfg):
    d = api.call("GET", "/api/v1/auth/me")
    if args.json:
        return emit_json(d) or 0
    u, t = d["user"], d["token"]
    print(f"{u['username']}{' (staff)' if u['is_staff'] else ''} @ {api.host}")
    print(f"token {t['prefix']}…  device {t['name'] or '-'}  created {human_time(t['created'])}  expires {human_time(t['expires']) if t['expires'] else 'never'}")
    return 0


def cmd_tokens(args, api, cfg):
    if args.token_action == "revoke":
        api.call("DELETE", f"/api/v1/auth/tokens/{args.id}")
        print(f"token {args.id} revoked")
        return 0
    data = api.call("GET", "/api/v1/auth/tokens")
    if args.json:
        return emit_json(data) or 0
    rows = [[t["id"], t["prefix"] + "…", t["name"] or "-", human_time(t["created"]), human_time(t["last_used"]), human_time(t["expires"]) if t["expires"] else "never",
             "yes" if t["current"] else ""] for t in data["tokens"]]
    print(table(["ID", "TOKEN", "DEVICE", "CREATED", "LAST USED", "EXPIRES", "THIS"], rows))
    return 0


def cmd_health(args, api, cfg):
    r = api.request("GET", "/healthz", auth=False)
    data = r.json() if r.content else {}
    if args.json:
        emit_json(data)
    else:
        print(f"{api.host}: HTTP {r.status_code}  " + "  ".join(f"{k}={v}" for k, v in data.items()))
    return 0 if r.status_code == 200 else 1


def cmd_summary(args, api, cfg):
    s = api.call("GET", "/api/v1/summary")
    if args.json:
        return emit_json(s) or 0
    print(summary_text(s))
    if not args.no_graph:
        print()
        print(speed_graph(s["series"], height=args.height, title="total speed"))
    return 0


def cmd_list(args, api, cfg):
    s = api.call("GET", "/api/v1/downloads", params={"status": args.status} if args.status else None)
    items = s["downloads"][: args.limit] if args.limit else s["downloads"]
    if args.json:
        return emit_json({**s, "downloads": items}) or 0
    print(download_table(items, with_owner=len({d.get("owner") for d in items}) > 1))
    if not args.quiet:
        print("\n" + summary_text(s))
    return 0


def cmd_show(args, api, cfg):
    data = api.call("GET", f"/api/v1/downloads/{args.id}")
    d = data["download"]
    if args.json:
        return emit_json(data) or 0
    pct = 100.0 if d["status"] == "completed" else d.get("progress")
    print(paint(f"#{d['id']}  {d['filename']}", "1"))
    facts = [("status", status_cell(d["status"])), ("url", d["url"]), ("saved to", f"{d['directory']}/{d['filename']}"),
             ("progress", f"{bar(pct, 30)} {'?' if pct is None else f'{pct:.1f}%'}  {human_bytes(d['downloaded'])} / {human_bytes(d['total'])}"),
             ("speed / eta", f"{human_rate(d['speed'])} / {human_eta(d['eta'])}" if d["status"] == "running" else "-"),
             ("auto retry", retry_text(d)), ("exit code", data.get("exit_code") if data.get("exit_code") is not None else "-"),
             ("worker restarts", data.get("requeues", 0)), ("owner", d.get("owner") or "-"), ("created", human_time(d["created"])),
             ("scheduled", human_time(d["not_before"]) if d.get("not_before") else "-"), ("finished", human_time(d["finished"]))]
    if data.get("options"):
        facts.append(("options", json.dumps(data["options"])))
    for k, v in facts:
        print(f"  {k:<16}{v}")
    if d.get("error"):
        print(paint("\nerror:", "31"))
        print("  " + d["error"].replace("\n", "\n  "))
    if not args.no_graph and data.get("series"):
        print("\n" + speed_graph(data["series"], height=args.height, title=f"#{d['id']} speed"))
    if args.log is not None:
        lines = (data.get("log") or "").splitlines()
        print(paint("\nwget log (tail):", "1"))
        print("\n".join(lines[-args.log:]) if lines else "(empty)")
    return 0


def build_options(args):
    opts = {}
    if args.limit:
        opts["limit_rate"] = int(args.limit * 1024)
    for key, attr in (("tries", "tries"), ("timeout", "net_timeout"), ("user_agent", "user_agent"), ("referer", "referer")):
        if getattr(args, attr, None) not in (None, ""):
            opts[key] = getattr(args, attr)
    if args.header:
        opts["headers"] = list(args.header)
    if args.insecure:
        opts["check_certificate"] = False
    return opts


def cmd_add(args, api, cfg):
    urls = read_urls(args)
    if not urls:
        raise CliError("no URL given (arguments, --file FILE, or --file - for stdin)", 2)
    body = {"urls": urls}
    if args.dir:
        body["directory"] = args.dir
    if args.name:
        body["filename"] = args.name
    opts = build_options(args)
    if opts:
        body["options"] = opts
    retry = parse_retry(args.retry) if args.retry is not None else None
    if retry is not None:
        body["retry"] = retry
    when = parse_when(args.at, args.delay)
    if when:
        body["not_before"] = when
    if args.auth_user:
        body["http_user"] = args.auth_user
        body["http_password"] = args.auth_password or os.environ.get("WGP_HTTP_PASSWORD") or getpass.getpass(f"password for {args.auth_user}: ")
    r = api.request("POST", "/api/v1/downloads", body)
    data = r.json() if r.content else {}
    if r.status_code == 401:
        raise CliError(error_text(401, data), 2)
    if r.status_code not in (200, 201, 400):
        raise CliError(error_text(r.status_code, data))
    created = data.get("created", [])
    if args.json:
        emit_json(data)
    else:
        if created:
            print(f"queued {len(created)} download(s): " + ", ".join(f"#{i}" for i in created) + (f"  (starts {human_time(when)})" if when else ""))
        for e in data.get("errors", []):
            print(paint(f"  {'line ' + str(e['line']) + ': ' if e.get('line') else ''}{e['error']}", "31") + (f"  <- {clip(e['url'], 60)}" if e.get("url") else ""), file=sys.stderr)
    if args.wait and created:
        return follow(api, created, args)
    return 0 if created and not data.get("errors") else 1


def follow(api, ids, args):
    """Block until every download in ``ids`` is finished (or removed). Exit code 0 only if all completed."""
    pending, final, last_print = set(ids), {}, 0.0
    rows = {}
    tty = sys.stdout.isatty()
    deadline = args.timeout_wait
    try:
        for ev, data in stream_events(api, max_seconds=deadline):
            if ev == "snapshot":
                for d in data["downloads"]:
                    if d["id"] in pending:
                        rows[d["id"]] = d
                        if d["status"] in TERMINAL:
                            final[d["id"]] = d["status"]
                            pending.discard(d["id"])
            elif ev in ("status", "removed") and data["id"] in pending:
                if ev == "removed":
                    final[data["id"]] = "removed"
                    pending.discard(data["id"])
                else:
                    rows.setdefault(data["id"], {}).update(status=data["status"], error=data.get("error", ""), total=data.get("total"))
                    if data["status"] in TERMINAL:
                        final[data["id"]] = data["status"]
                        pending.discard(data["id"])
                        print(("\r\x1b[K" if tty else "") + f"#{data['id']} {status_cell(data['status'])}" + (f"  {data['error']}" if data.get("error") else ""))
                    else:
                        print(("\r\x1b[K" if tty else "") + f"#{data['id']} {data['status']}" + (f"  {data['error']}" if data.get("error") else ""))
            elif ev == "progress":
                now = time.monotonic()
                mine = [i for i in data["items"] if i["id"] in pending]
                if mine and now - last_print >= (0.5 if tty else 5):
                    last_print = now
                    msg = "  ".join(f"#{i['id']} {bar(i['downloaded'] * 100 / i['total'] if i['total'] else None, 12)} {human_bytes(i['downloaded'])} {human_rate(i['speed'])}" for i in mine[:3])
                    print(("\r\x1b[K" + clip(msg, term_width() - 1)) if tty else msg, end="" if tty else "\n", flush=True)
            if not pending:
                break
    except KeyboardInterrupt:
        print("\n(stopped waiting; downloads continue on the server)")
        return 130
    if tty:
        print()
    if pending:
        print(f"timed out waiting for: {', '.join('#' + str(i) for i in sorted(pending))}", file=sys.stderr)
        return 1
    return 0 if all(v == "completed" for v in final.values()) else 1


def cmd_wait(args, api, cfg):
    return follow(api, parse_ids(args.ids), args)


def cmd_action(args, api, cfg):
    body = {"action": args.command}
    if args.command != "purge" or args.ids:
        ids = parse_ids(args.ids)
        if not ids and args.command != "purge":
            raise CliError("no id given", 2)
        body["ids"] = ids
    if getattr(args, "delete_files", False):
        body["delete_files"] = True
    elif getattr(args, "keep_files", False):
        body["delete_files"] = False
    r = api.call("POST", "/api/v1/downloads/actions", body)
    if args.json:
        return emit_json(r) or 0
    msg = f"{r['affected']} download(s) {ACTION_VERBS[args.command]}"
    if r.get("files_deleted") or r.get("files_pending"):
        msg += f"; files deleted: {r.get('files_deleted', 0)}" + (f", {r['files_pending']} more will be deleted as soon as the worker stops wget" if r.get("files_pending") else "")
    print(msg)
    return 0 if r["affected"] or args.command == "purge" else 1


def content_disposition_name(header):
    if not header:
        return None
    m = re.search(r"filename\*=UTF-8''([^;]+)", header, re.I)
    if m:
        return unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', header, re.I)
    return m.group(1) if m else None


def cmd_get(args, api, cfg):
    r = api.request("GET", f"/api/v1/downloads/{args.id}/file", stream=True, timeout=(10, 120))
    if r.status_code != 200:
        try:
            raise CliError(error_text(r.status_code, r.json()), 2 if r.status_code == 401 else 1)
        except ValueError:
            raise CliError(f"HTTP {r.status_code}") from None
    name = os.path.basename(content_disposition_name(r.headers.get("Content-Disposition")) or f"download-{args.id}")
    out = Path(args.output) if args.output else Path.cwd()
    if out.is_dir() or (args.output and str(args.output).endswith(("/", os.sep))):
        out.mkdir(parents=True, exist_ok=True)
        out = out / name
    if out.exists() and not args.force:
        raise CliError(f"{out} exists (use --force to overwrite)", 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    total = int(r.headers.get("Content-Length") or 0)
    part, done, t0, last = out.with_name(out.name + ".part"), 0, time.monotonic(), 0.0
    try:
        with open(part, "wb") as fh:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if sys.stderr.isatty() and now - last > 0.2:
                    last = now
                    rate = done / max(now - t0, 1e-6)
                    pct = done * 100 / total if total else None
                    sys.stderr.write(f"\r\x1b[K{bar(pct, 24)} {'' if pct is None else f'{pct:5.1f}% '}{human_bytes(done)}/{human_bytes(total) if total else '?'}  {human_rate(rate)}")
                    sys.stderr.flush()
        if total and done != total:
            raise CliError(f"incomplete download ({done} of {total} bytes); partial data kept in {part}")
        os.replace(part, out)
    except requests.exceptions.RequestException as exc:
        raise CliError(f"transfer interrupted: {exc}; partial data kept in {part}") from exc
    if sys.stderr.isatty():
        sys.stderr.write("\r\x1b[K")
    print(f"saved {out}  ({human_bytes(done)} in {time.monotonic() - t0:.1f}s)")
    return 0


def cmd_graph(args, api, cfg):
    def draw():
        if args.id:
            data = api.call("GET", f"/api/v1/downloads/{args.id}")
            d = data["download"]
            head = f"#{d['id']} {d['filename']}  {status_cell(d['status'])}  {human_rate(d['speed'])}"
            return head + "\n" + speed_graph(data["series"], height=args.height, title=f"#{d['id']} speed")
        s = api.call("GET", "/api/v1/summary")
        return summary_text(s) + "\n\n" + speed_graph(s["series"], height=args.height, title="total speed")

    if not args.follow:
        print(draw())
        return 0
    started = time.monotonic()
    try:
        while args.max_seconds is None or time.monotonic() - started < args.max_seconds:
            frame = draw()
            print(("\x1b[H\x1b[J" if sys.stdout.isatty() else "") + frame, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_events(args, api, cfg):
    n = 0
    try:
        for ev, data in stream_events(api, max_seconds=args.max_seconds, on_state=lambda m: print(f"# {m}", file=sys.stderr)):
            if args.type and ev not in args.type:
                continue
            print(json.dumps({"event": ev, "data": data}, ensure_ascii=False) if args.json else f"{dt.datetime.now().strftime('%H:%M:%S')} {ev:<9} {json.dumps(data, ensure_ascii=False)[:term_width() - 20]}", flush=True)
            n += 1
            if args.count and n >= args.count:
                break
    except KeyboardInterrupt:
        pass
    return 0


class WatchState:
    def __init__(self):
        self.rows, self.series, self.status = {}, [], "connecting"
        self.max_downloads, self.scheduler_alive, self.redis, self.last_speed = 0, True, True, 0

    def apply(self, ev, data):
        if ev == "snapshot":
            self.rows = {d["id"]: d for d in data["downloads"]}
            self.series = list(data["series"])
            self.max_downloads, self.scheduler_alive, self.redis = data["max_downloads"], data["scheduler_alive"], data["redis"]
        elif ev == "status":
            row = self.rows.setdefault(data["id"], {"id": data["id"], "url": "", "speed": 0, "eta": None, "progress": None, "owner": None})
            row.update(status=data["status"], filename=data["filename"], error=data.get("error", ""), downloaded=data["downloaded"] or 0, total=data["total"])
            if data["status"] != "running":
                row["speed"], row["eta"] = 0, None
        elif ev == "removed":
            self.rows.pop(data["id"], None)
        elif ev == "progress":
            seen = set()
            for i in data["items"]:
                row = self.rows.get(i["id"])
                if row:
                    row.update(downloaded=i["downloaded"], total=i["total"], speed=i["speed"])
                    row["progress"] = round(i["downloaded"] * 100 / i["total"], 1) if i["total"] else None
                    row["eta"] = int((i["total"] - i["downloaded"]) / i["speed"]) if i["speed"] and i["total"] and i["total"] > i["downloaded"] else None
                seen.add(i["id"])
            for pk, row in self.rows.items():  # running downloads missing from the batch have no live data (yet)
                if pk not in seen and row.get("status") != "running":
                    row["speed"] = 0
            self.last_speed = data["speed"]
            self.series.append({"t": int(time.time()), "speed": data["speed"], "bytes": 0})
            self.series = self.series[-300:]

    def counts(self):
        c = {k: 0 for k in STATUSES}
        for r in self.rows.values():
            c[r.get("status", "queued")] = c.get(r.get("status", "queued"), 0) + 1
        return c

    def frame(self, height, only=None):
        rows = [r for r in sorted(self.rows.values(), key=lambda r: (r.get("status") in TERMINAL, r.get("position", 0), r["id"]))]
        if only:
            rows = [r for r in rows if r["id"] in only]
        speed = sum(r.get("speed", 0) for r in rows if r.get("status") == "running")
        summary = {"counts": self.counts(), "max_downloads": self.max_downloads, "speed": speed, "scheduler_alive": self.scheduler_alive, "redis": self.redis}
        shown = rows[: max(shutil.get_terminal_size((100, 40)).lines - height - 10, 5)]
        out = [paint("wgp watch", "1") + f"  {self.status}  {dt.datetime.now().strftime('%H:%M:%S')}  (Ctrl-C to quit)", summary_text(summary), "",
               speed_graph(self.series, height=height, title="total speed"), "", download_table(shown, with_owner=len({r.get("owner") for r in shown}) > 1)]
        if len(rows) > len(shown):
            out.append(f"... {len(rows) - len(shown)} more (use `wgp list`)")
        return "\n".join(out)


def cmd_watch(args, api, cfg):
    state, only = WatchState(), set(parse_ids(args.ids)) if args.ids else None
    last, tty = 0.0, sys.stdout.isatty() and not args.no_clear

    def render(force=False):
        nonlocal last
        now = time.monotonic()
        if force or now - last >= args.interval:
            last = now
            print(("\x1b[H\x1b[J" if tty else "") + state.frame(args.height, only), flush=True)

    def on_state(msg):
        state.status = msg
    try:
        for ev, data in stream_events(api, max_seconds=args.max_seconds, on_state=on_state):
            state.apply(ev, data)
            state.status = "live"
            render(force=ev in ("snapshot", "status", "removed"))
    except KeyboardInterrupt:
        pass
    if not tty or args.max_seconds is not None:
        render(force=True) if state.rows or state.series else None
    return 0


def cmd_settings(args, api, cfg):
    data = api.call("GET", "/api/v1/settings")
    schema = {s["key"]: s for s in data["schema"]}
    if args.settings_action == "set":
        pairs = args.pairs
        if len(pairs) % 2:
            raise CliError("usage: wgp settings set KEY VALUE [KEY VALUE ...]", 2)
        body = {}
        for key, value in zip(pairs[0::2], pairs[1::2]):
            if key not in schema:
                raise CliError(f"unknown setting {key!r} (see `wgp settings`)", 2)
            t = schema[key]["type"]
            if t == "int":
                try:
                    body[key] = int(value)
                except ValueError:
                    raise CliError(f"{key}: {value!r} is not a number", 2) from None
            elif t == "bool":
                if value.lower() not in ("1", "0", "true", "false", "on", "off", "yes", "no"):
                    raise CliError(f"{key}: use true/false", 2)
                body[key] = value.lower() in ("1", "true", "on", "yes")
            else:
                body[key] = value.replace("\\n", "\n")
        r = api.call("POST", "/api/v1/settings", body)
        data = r
        print("saved: " + ", ".join(f"{k}={body[k]!r}" for k in body))
        return 0
    values = data["values"]
    if args.settings_action == "get":
        if args.key not in values:
            raise CliError(f"unknown setting {args.key!r}", 2)
        print(values[args.key])
        return 0
    if args.json:
        return emit_json(data) or 0
    group = None
    for s in data["schema"]:
        if s["group"] != group:
            group = s["group"]
            print(paint(f"\n[{group}]", "1"))
        v = values[s["key"]]
        shown = json.dumps(v) if s["type"] != "lines" else json.dumps(v)
        mark = "" if v == s["default"] else paint("  (changed)", "33")
        print(f"  {s['key']:<36} {shown}{mark}")
        if args.verbose and s["help"]:
            print(f"      {s['label']}: {s['help']}")
    return 0


def cmd_rules(args, api, cfg):
    a = args.rules_action or "list"
    if a == "add":
        body = {"name": args.name, "start": args.start, "end": args.end, "limit_kbps": args.limit, "days": parse_days(args.days), "enabled": True}
        r = api.call("POST", "/api/v1/rules", body)
        print(f"rule #{r['rule']['id']} created")
        return 0
    if a == "edit":
        body = {k: v for k, v in (("name", args.name), ("start", args.start), ("end", args.end), ("limit_kbps", args.limit)) if v is not None}
        if args.days is not None:
            body["days"] = parse_days(args.days)
        if not body:
            raise CliError("nothing to change", 2)
        api.call("PATCH", f"/api/v1/rules/{args.id}", body)
        print(f"rule #{args.id} updated")
        return 0
    if a in ("enable", "disable"):
        api.call("PATCH", f"/api/v1/rules/{args.id}", {"enabled": a == "enable"})
        print(f"rule #{args.id} {a}d")
        return 0
    if a == "delete":
        api.call("DELETE", f"/api/v1/rules/{args.id}")
        print(f"rule #{args.id} deleted")
        return 0
    data = api.call("GET", "/api/v1/rules")
    if args.json:
        return emit_json(data) or 0
    if not data["rules"]:
        print("(no speed rules)")
        return 0
    rows = [[r["id"], paint("on", "32") if r["enabled"] else paint("off", "90"), r["name"], days_text(r["days"]), f"{r['start']}-{r['end']}",
             f"{r['limit_kbps']} KB/s" if r["limit_kbps"] else "unlimited", paint("ACTIVE", "1;33") if r["active_now"] else ""] for r in data["rules"]]
    print(table(["ID", "", "NAME", "DAYS", "TIME", "LIMIT/DOWNLOAD", ""], rows))
    return 0


# ------------------------------------------------------------------ argument parser
def build_parser():
    try:
        from rchf import CustomRichHelpFormatter
    except:
        CustomRichHelpFormatter = argparse.RawDescriptionHelpFormatter
    p = argparse.ArgumentParser(prog="wgp", description="Command line client for wgetpanel.", formatter_class=CustomRichHelpFormatter,
                                epilog=__doc__.split("\n", 2)[2] if __doc__ else None)
    common = argparse.ArgumentParser(add_help=False)  # same options, valid before AND after the sub-command

    def global_options(parser, suppress):
        d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        parser.add_argument("--host", default=d(None), help="server URL (default: saved by `wgp login`, or $WGP_HOST)")
        parser.add_argument("--token", default=d(None), help="API token (default: saved, or $WGP_TOKEN)")
        parser.add_argument("--timeout", type=float, default=d(30), help="HTTP timeout in seconds (default 30)")
        parser.add_argument("--insecure-tls", action="store_true", default=d(False), help="do not verify the server TLS certificate")
        parser.add_argument("--json", action="store_true", default=d(False), help="print raw JSON where supported (for scripts)")

    global_options(p, suppress=False)
    global_options(common, suppress=True)
    p.add_argument("--version", action="version", version=f"wgp {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, fn, help, **kw):
        sp = sub.add_parser(name, help=help, description=help, formatter_class=CustomRichHelpFormatter, parents=[common], **kw)
        sp.set_defaults(func=fn)
        return sp

    s = add("login", cmd_login, "log in and store a token for this device")
    s.add_argument("host", nargs="?")
    s.add_argument("-u", "--user")
    s.add_argument("-p", "--password", help="(prefer the prompt or $WGP_PASSWORD, arguments show up in `ps`)")
    s.add_argument("--device", help="device name shown in `wgp tokens` (default: hostname)")
    add("logout", cmd_logout, "revoke this device's token and forget it")
    add("whoami", cmd_whoami, "show the current user and token")
    add("w", cmd_whoami, "show the current user and token")
    s = add("tokens", cmd_tokens, "list the devices (tokens) of your account, or revoke one")
    s.add_argument("token_action", nargs="?", choices=["list", "revoke"], default="list")
    s.add_argument("id", nargs="?", type=int)
    add("health", cmd_health, "server health (database, redis), no login needed")

    s = add("summary", cmd_summary, "counts per status, speed, scheduler state and a speed graph")
    s.add_argument("--no-graph", action="store_true")
    s.add_argument("--height", type=int, default=10)
    s = add("list", cmd_list, "list downloads", aliases=["ls"])
    s.add_argument("-s", "--status", choices=STATUSES)
    s.add_argument("-n", "--limit", type=int, help="show at most N rows")
    s.add_argument("-q", "--quiet", action="store_true", help="no summary line")
    s = add("show", cmd_show, "details of one download (retry state, options, error, speed graph, log)", aliases=["info"])
    s.add_argument("id", type=int)
    s.add_argument("--log", nargs="?", type=int, const=20, metavar="LINES", help="show the last LINES (default 20) of the wget log")
    s.add_argument("--no-graph", action="store_true")
    s.add_argument("--height", type=int, default=8)

    s = add("add", cmd_add, "queue downloads (URLs as arguments, from files, or stdin)")
    s.add_argument("urls", nargs="*", metavar="URL")
    s.add_argument("-f", "--file", action="append", help="file with one URL per line (# comments allowed), '-' = stdin")
    s.add_argument("-d", "--dir", help="target directory (must be inside the server's allowed directories)")
    s.add_argument("-o", "--name", help="file name (single URL only)")
    s.add_argument("--limit", type=float, metavar="KB/S", help="speed limit for this download in KB/s")
    s.add_argument("--tries", type=int, help="wget --tries for one run")
    s.add_argument("--timeout-net", dest="net_timeout", type=int, metavar="SECONDS", help="wget network timeout for this download")
    s.add_argument("--user-agent")
    s.add_argument("--referer")
    s.add_argument("-H", "--header", action="append", help="extra request header 'Name: value' (repeatable)")
    s.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate of the download server")
    s.add_argument("--auth-user", help="HTTP/FTP user name")
    s.add_argument("--auth-password", help="(prefer the prompt or $WGP_HTTP_PASSWORD)")
    s.add_argument("--retry", metavar="N|always|off", help="auto retry temporary errors: a number, 'always' (never give up) or 'off'; default = server setting")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--at", help="start time: 'YYYY-MM-DD HH:MM' or 'HH:MM' (next occurrence), local time")
    g.add_argument("--in", dest="delay", help="start after a delay: 90m, 2h30m, 1d")
    s.add_argument("-w", "--wait", action="store_true", help="wait until the download(s) finish, exit 1 if one fails")
    s.add_argument("--timeout-wait", type=float, default=None, help="give up waiting after N seconds")

    s = add("wait", cmd_wait, "wait until downloads finish (live progress); exit 0 only if all completed")
    s.add_argument("ids", nargs="+")
    s.add_argument("--timeout-wait", type=float, default=None)

    for name, hlp in (("pause", "pause downloads (running ones are stopped, resumable)"), ("resume", "resume paused downloads"),
                      ("retry", "re-queue failed / canceled downloads (resets the auto retry counter)"), ("cancel", "cancel downloads"),
                      ("remove", "remove downloads from the list"), ("top", "move queued downloads to the front of the queue"),
                      ("bottom", "move queued downloads to the back"), ("up", "move queued downloads one place up"), ("down", "move queued downloads one place down")):
        s = add(name, cmd_action, hlp)
        s.add_argument("ids", nargs="+", metavar="ID", help="ids, ranges (3-7) or lists (1,4,9)")
        if name in ("cancel", "remove"):
            g = s.add_mutually_exclusive_group()
            g.add_argument("--delete-files", action="store_true", help="also delete the file from the server's disk")
            g.add_argument("--keep-files", action="store_true", help="never delete the file (overrides the server default)")
    s = add("purge", cmd_action, "remove all finished downloads (or only the given ids) from the list")
    s.add_argument("ids", nargs="*", metavar="ID")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--delete-files", action="store_true")
    g.add_argument("--keep-files", action="store_true")

    s = add("get", cmd_get, "download a finished file from the server to this machine")
    s.add_argument("id", type=int)
    s.add_argument("-o", "--output", help="file or directory (default: current directory)")
    s.add_argument("--force", action="store_true", help="overwrite an existing file")

    s = add("graph", cmd_graph, "ASCII speed graph (total, or of one download)")
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--height", type=int, default=12)
    s.add_argument("--follow", action="store_true", help="keep redrawing")
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--max-seconds", type=float)
    s = add("watch", cmd_watch, "live dashboard (SSE): table + speed graph, updates as events arrive")
    s.add_argument("ids", nargs="*", help="only these downloads")
    s.add_argument("--height", type=int, default=8)
    s.add_argument("--interval", type=float, default=1.0, help="minimum seconds between redraws")
    s.add_argument("--max-seconds", type=float, help="stop after N seconds (default: until Ctrl-C)")
    s.add_argument("--no-clear", action="store_true", help="print frames one after another instead of redrawing")
    s = add("events", cmd_events, "raw event stream (one line per event), for scripts")
    s.add_argument("--type", action="append", choices=["snapshot", "status", "removed", "progress"])
    s.add_argument("--count", type=int, help="stop after N events")
    s.add_argument("--max-seconds", type=float)

    s = add("settings", cmd_settings, "show or change server settings (staff)")
    s.add_argument("settings_action", nargs="?", choices=["list", "get", "set"], default="list")
    s.add_argument("key", nargs="?", help="for `get`")
    s.add_argument("pairs", nargs="*", help="for `set`: KEY VALUE pairs")
    s.add_argument("-v", "--verbose", action="store_true", help="show descriptions")
    s = add("rules", cmd_rules, "speed rules (staff): list, add, edit, enable, disable, delete")
    s.add_argument("rules_action", nargs="?", choices=["list", "add", "edit", "enable", "disable", "delete"], default="list")
    s.add_argument("id", nargs="?", type=int, help="rule id for edit / enable / disable / delete")
    s.add_argument("--name")
    s.add_argument("--from", dest="start", metavar="HH:MM")
    s.add_argument("--to", dest="end", metavar="HH:MM")
    s.add_argument("--limit", type=int, metavar="KB/S", help="limit per running download, 0 = unlimited")
    s.add_argument("--days", help="mon-fri | weekdays | weekend | mon,wed | all")
    return p


def normalize_rules_argv(argv):
    """`wgp rules add Office ...` -> positional name for add (kept friendly: the first word after `add` is the name)."""
    if "rules" in argv:
        i = argv.index("rules")
        if len(argv) > i + 2 and argv[i + 1] == "add" and not argv[i + 2].startswith("-"):
            return argv[: i + 2] + ["--name", argv[i + 2]] + argv[i + 3:]
    return argv


def main(argv=None):
    argv = normalize_rules_argv(list(sys.argv[1:] if argv is None else argv))
    if os.name == "nt":
        os.system("")  # enables ANSI escape sequences in the Windows 10+ console
    if not sys.stdout.isatty() and (getattr(sys.stdout, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit()
    args = parser.parse_args(argv)
    cfg = load_config()
    try:
        if args.command == "rules" and args.rules_action == "add" and not (args.name and args.start and args.end):
            raise CliError("usage: wgp rules add NAME --from HH:MM --to HH:MM --limit KB/S [--days mon-fri]", 2)
        if args.command == "rules" and args.rules_action in ("edit", "enable", "disable", "delete") and args.id is None:
            raise CliError(f"usage: wgp rules {args.rules_action} ID", 2)
        if args.command == "tokens" and args.token_action == "revoke" and args.id is None:
            raise CliError("usage: wgp tokens revoke ID", 2)
        if args.command == "settings" and args.settings_action == "get" and not args.key:
            raise CliError("usage: wgp settings get KEY", 2)
        if args.command == "settings" and args.settings_action == "set":
            args.pairs = ([args.key] if args.key else []) + args.pairs
        host = args.host or os.environ.get("WGP_HOST") or cfg.get("host")
        if args.command == "login":
            api = None
        else:
            if not host:
                raise CliError("no server configured: run  wgp login <host>  or set WGP_HOST", 2)
            api = Api(normalize_host(host), args.token or os.environ.get("WGP_TOKEN") or cfg.get("token"), args.timeout,
                      verify=not args.insecure_tls and cfg.get("verify_tls", True))
        return args.func(args, api, cfg) or 0
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
