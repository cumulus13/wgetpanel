"""Supervises one wget process.

wget has no RPC, so progress is measured from the outside: the size of the file
being written is sampled every second (speed = bytes delta / time delta, smoothed),
the total comes from the ``Length:`` line wget writes to its log.

Pause / cancel / speed-rule changes all work by terminating wget and, if needed,
starting it again: ``wget -c`` resumes from the bytes already on disk.
"""
import logging
import os
import re
import signal
import subprocess
import time

from django.conf import settings
from django.utils import timezone

from . import live, wget
from .models import Download

log = logging.getLogger(__name__)

LENGTH_RE = re.compile(r"^Length:\s+(\d+)", re.M)
POLL = 1.0
CHECKPOINT_EVERY = 5.0
RULE_CHECK_EVERY = 5.0
ALPHA = 0.4  # EWMA smoothing of speed


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _log_tail(path, nbytes=4096):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - nbytes))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _total_from_log(path):
    """Total size from the most recent ``Length:`` line (log is appended on resume)."""
    matches = LENGTH_RE.findall(_log_tail(path, 65536))
    return int(matches[-1]) if matches else None


def terminate(proc, grace=5.0):
    """SIGTERM the whole process group, SIGKILL after ``grace`` seconds."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _checkpoint(pk, size, total):
    fields = {"downloaded_bytes": size}
    if total is not None:
        fields["total_bytes"] = total
    Download.objects.filter(pk=pk).update(**fields)


def run(pk):
    """Run (and re-run, on speed rule changes) wget for download ``pk`` until it stops."""
    d = Download.objects.filter(pk=pk).first()
    if d is None or d.status != Download.RUNNING:
        return
    os.makedirs(d.directory, exist_ok=True)
    os.makedirs(settings.WGP_LOG_DIR, exist_ok=True)
    Download.objects.filter(pk=pk).update(started_at=d.started_at or timezone.now(), error="", exit_code=None)

    try:
        _supervise(d)
    except Exception as exc:  # never leave a row stuck in "running"
        log.exception("download %s crashed", pk)
        Download.objects.filter(pk=pk, status=Download.RUNNING).update(
            status=Download.ERROR, error=f"Internal error: {exc}", finished_at=timezone.now(), pid=None)
    finally:
        live.clear_live(pk)


def _spawn(d, rate):
    rc = wget.write_wgetrc(d, rate)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "http_proxy", "https_proxy", "ftp_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "NO_PROXY")}
    env.update(LC_ALL="C", LANG="C", HOME=os.environ.get("TMPDIR", "/tmp"))
    try:
        proc = subprocess.Popen(
            wget.build_command(d, rc), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, start_new_session=True, close_fds=True)
    except Exception:
        os.unlink(rc)
        raise
    return proc, rc


def _supervise(d):
    pk = d.pk
    while True:
        d.refresh_from_db()
        if d.status != Download.RUNNING:
            return
        rate = wget.effective_rate(d)
        proc, rc = _spawn(d, rate)
        Download.objects.filter(pk=pk).update(pid=proc.pid)
        outcome = _monitor(d, proc, rate)
        try:
            os.unlink(rc)
        except OSError:
            pass
        size, total = _size(d.path), _total_from_log(d.log_path)
        if outcome == "restart":
            continue
        if outcome in ("pause", "cancel", "cancel_delete"):
            _checkpoint(pk, size, total)
            Download.objects.filter(pk=pk).update(pid=None)
            if outcome == "cancel_delete":
                _remove_files(d)
            return
        code = proc.returncode
        now = timezone.now()
        if code == 0:
            Download.objects.filter(pk=pk, status=Download.RUNNING).update(
                status=Download.COMPLETED, downloaded_bytes=size, total_bytes=total or size, exit_code=0, pid=None, finished_at=now, error="")
        else:
            tail = "\n".join(l for l in _log_tail(d.log_path, 1500).splitlines() if l.strip())[-800:]
            Download.objects.filter(pk=pk, status=Download.RUNNING).update(
                status=Download.ERROR, downloaded_bytes=size, total_bytes=total, exit_code=code, pid=None, finished_at=now,
                error=wget.describe_exit(code, tail))
        return


def _monitor(d, proc, rate):
    """Sample the file until wget exits or a control message / rule change stops it.
    Returns: 'exit' | 'pause' | 'cancel' | 'cancel_delete' | 'restart'."""
    pk, path, logp = d.pk, d.path, d.log_path
    last_size, last_t = _size(path), time.monotonic()
    speed, total = 0.0, None
    last_cp = last_rule = time.monotonic()
    while True:
        try:
            proc.wait(timeout=POLL)
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        size = _size(path)
        dt = max(now - last_t, 1e-6)
        inst = max(size - last_size, 0) / dt
        speed = inst if speed == 0 else ALPHA * inst + (1 - ALPHA) * speed
        if proc.poll() is not None:
            speed = 0
        last_size, last_t = size, now
        if total is None:
            total = _total_from_log(logp)
        try:
            live.set_live(pk, size, total, speed, proc.pid)
            ctl = live.pop_ctl(pk)
        except Exception:  # redis hiccup must not kill the download
            log.warning("redis unavailable for download %s", pk, exc_info=True)
            ctl = None
        if proc.poll() is not None:
            return "exit"
        if ctl in ("pause", "cancel", "cancel_delete"):
            terminate(proc)
            return ctl
        if now - last_cp >= CHECKPOINT_EVERY:
            _checkpoint(pk, size, total)
            last_cp = now
        if now - last_rule >= RULE_CHECK_EVERY:
            last_rule = now
            fresh = Download.objects.filter(pk=pk).values_list("status", "options").first()
            if fresh is None or fresh[0] != Download.RUNNING:  # changed behind our back (API without redis, reaper ...)
                terminate(proc)
                return "pause"
            d.options = fresh[1]
            if wget.effective_rate(d) != rate:
                log.info("download %s: speed limit changed, restarting wget with -c", pk)
                terminate(proc)
                return "restart"


def _remove_files(d):
    for p in (d.path,):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.remove(d.log_path)
    except OSError:
        pass
