"""Read models shared by the browser API (/api/...) and the mobile API (/api/v1/...)."""
import os

from django.utils import timezone

from . import conf, live, retry, services
from .models import Download, SchedulerState


def row(d, lv):
    total = d.total_bytes
    downloaded, speed = d.downloaded_bytes, 0
    if d.status == Download.RUNNING and lv:
        downloaded = int(lv.get("downloaded") or downloaded)
        speed = int(lv.get("speed") or 0)
        if lv.get("total"):
            total = int(lv["total"])
    eta = None
    if speed > 0 and total and total > downloaded:
        eta = int((total - downloaded) / speed)
    return {
        "id": d.pk, "url": d.url, "filename": d.filename, "directory": d.directory, "status": d.status,
        "total": total, "downloaded": downloaded, "speed": speed, "eta": eta,
        "progress": round(downloaded * 100 / total, 1) if total else None,
        "error": d.error, "owner": d.owner.get_username() if d.owner else None,
        "position": d.position, "not_before": d.not_before.isoformat() if d.not_before else None,
        "created": d.created_at.isoformat(), "finished": d.finished_at.isoformat() if d.finished_at else None,
        "has_auth": bool(d.http_user),
        "retry_max": d.retry_max, "retry_count": d.retry_count, "retry_total": d.retry_total, "retry_limit": retry.limit_for(d),
    }


def build_snapshot(user, status=None, include_downloads=True):
    qs = services.visible(user).select_related("owner")
    counts = {s: 0 for s in (Download.QUEUED, Download.RUNNING, Download.PAUSED, Download.COMPLETED, Download.ERROR, Download.CANCELED)}
    for s, n in _count_by_status(qs):
        counts[s] = n
    rows, lives = [], {}
    try:
        series = live.get_aggregate("global" if user.is_staff else f"user:{user.pk}", 300)
        redis_ok = True
    except Exception:
        series, redis_ok = [], False
    active = list(qs.filter(status__in=Download.ACTIVE))
    finished = list(qs.filter(status__in=Download.FINISHED).order_by("-finished_at", "-id")[:200]) if include_downloads else []
    listing = active + finished
    try:
        lives = live.get_live_many([d.pk for d in listing if d.status == Download.RUNNING])
    except Exception:
        lives, redis_ok = {}, False
    rows = [row(d, lives.get(d.pk)) for d in listing]
    speed = sum(r["speed"] for r in rows)
    if status:
        rows = [r for r in rows if r["status"] == status]
    state = SchedulerState.objects.filter(id=1).first()
    last_tick = state.last_tick if state else None
    out = {
        "counts": counts, "speed": speed, "series": series,
        "max_downloads": conf.get("scheduler.max_downloads"), "redis": redis_ok,
        "delete_files_default": conf.get("files.delete_on_remove"),
        "scheduler_alive": bool(last_tick and (timezone.now() - last_tick).total_seconds() < 30),
        "server_time": timezone.now().isoformat(),
    }
    if include_downloads:
        out["downloads"] = rows
    return out


def _count_by_status(qs):
    from django.db.models import Count
    return qs.values_list("status").annotate(n=Count("id"))


def build_detail(user, pk):
    d = services.visible(user).filter(pk=pk).select_related("owner").first()
    if d is None:
        return None
    try:
        lv = live.get_live_many([pk])[pk]
        series = live.get_series(pk, 300)
    except Exception:
        lv, series = None, []
    log_text = ""
    try:
        with open(d.log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16384))
            log_text = fh.read().decode("utf-8", "replace")
    except OSError:
        pass
    return {"download": row(d, lv), "series": series, "log": log_text, "options": d.options,
            "exit_code": d.exit_code, "requeues": d.requeues}
