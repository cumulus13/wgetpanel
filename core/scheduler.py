"""Queue scheduler: starts queued downloads until ``max_downloads`` is reached."""
import logging
import os
import signal
from datetime import timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from . import conf, events, live
from .models import Download, SchedulerState

log = logging.getLogger(__name__)
MAX_REQUEUES = 5


def next_position():
    return (Download.objects.aggregate(m=Max("position"))["m"] or 0) + 1


def tick():
    """Claim queued downloads. Returns the list of ids to hand to Celery.

    The singleton SchedulerState row is locked for the whole transaction, so
    concurrent ticks (several beat / worker processes) are serialised."""
    now = timezone.now()
    limit = conf.get("scheduler.max_downloads")
    with transaction.atomic():
        state = SchedulerState.lock()
        state.last_tick = now
        state.save(update_fields=["last_tick"])
        running = Download.objects.filter(status=Download.RUNNING).count()
        slots = 10**6 if limit == 0 else limit - running
        if slots <= 0:
            return []
        ids = list(
            Download.objects.filter(status=Download.QUEUED)
            .filter(Q(not_before__isnull=True) | Q(not_before__lte=now))
            .order_by("position", "id")
            .values_list("id", flat=True)[:slots]
        )
        if ids:
            Download.objects.filter(id__in=ids).update(status=Download.RUNNING, dispatched_at=now, pid=None, error="")
    return ids


def record_metrics():
    """Aggregate live speeds into the global / per user graph series."""
    rows = list(Download.objects.filter(status=Download.RUNNING).values_list("id", "owner_id"))
    lives = live.get_live_many([r[0] for r in rows])
    total, per_user = 0, {}
    for pk, owner in rows:
        h = lives.get(pk)
        sp = int(h["speed"]) if h and h.get("speed") else 0
        total += sp
        per_user[owner] = per_user.get(owner, 0) + sp
    live.push_aggregate("global", total)
    for owner, sp in per_user.items():
        live.push_aggregate(f"user:{owner}", sp)
    return total


def _pid_is_ours(pid, pk):
    """True if /proc says pid is a wget started for download pk (guards against PID reuse)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return f"wgp-{pk}-".encode() in fh.read()
    except OSError:
        return False


def reap_stale(grace_seconds=30):
    """Downloads marked running but without a heartbeat: the worker died (or the broker lost
    the task). Kill an orphaned wget, put the download back in the queue; ``-c`` resumes it."""
    cutoff = timezone.now() - timedelta(seconds=grace_seconds)
    requeued = 0
    for d in Download.objects.filter(status=Download.RUNNING, dispatched_at__lt=cutoff):
        if live.is_alive(d.pk):
            continue
        if d.pid and _pid_is_ours(d.pid, d.pk):
            try:
                os.killpg(d.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if d.requeues >= MAX_REQUEUES:
            Download.objects.filter(pk=d.pk, status=Download.RUNNING).update(
                status=Download.ERROR, error="Worker kept dying while running this download", finished_at=timezone.now(), pid=None)
        else:
            n = Download.objects.filter(pk=d.pk, status=Download.RUNNING).update(
                status=Download.QUEUED, pid=None, requeues=d.requeues + 1)
            requeued += n
            log.warning("download %s had no heartbeat, requeued", d.pk)
        events.emit(d.pk)
    return requeued
