import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from . import conf, events, files, live, notify, runner, scheduler
from .models import Download

log = logging.getLogger(__name__)


@shared_task(name="core.tasks.run_download", bind=True, acks_late=False)
def run_download(self, pk):
    runner.run(pk)


def dispatch(ids):
    for pk in ids:
        try:
            run_download.apply_async((pk,))
        except Exception:  # broker down: the reaper puts it back in the queue
            log.exception("could not dispatch download %s", pk)


@shared_task(name="core.tasks.scheduler_tick")
def scheduler_tick():
    ids = scheduler.tick()
    events.emit_many(ids)
    dispatch(ids)
    try:
        scheduler.record_metrics()
    except Exception:
        log.warning("metrics unavailable", exc_info=True)
    return len(ids)


@shared_task(name="core.tasks.reap_stale")
def reap_stale():
    return scheduler.reap_stale()


@shared_task(name="core.tasks.maintenance")
def maintenance():
    days = conf.get("history.retention_days")
    if not days:
        return 0
    cutoff = timezone.now() - timedelta(days=days)
    old = list(Download.objects.filter(status__in=Download.FINISHED, finished_at__lt=cutoff))
    delete = conf.get("files.delete_on_retention")
    for d in old:
        if delete:
            files.delete(d)
        live.drop_series(d.pk)
    Download.objects.filter(pk__in=[d.pk for d in old]).delete()
    return len(old)


@shared_task(name="core.tasks.send_notification", bind=True, max_retries=3)
def send_notification(self, payload, only=None):
    """Deliver one notification. Only the transports that failed are retried (no duplicates on the others)."""
    failed = notify.deliver(payload, only)
    if failed:
        if self.request.retries < self.max_retries:
            raise self.retry(args=(payload, sorted(failed)), countdown=15 * 2 ** self.request.retries)
        log.error("notification %r dropped after retries: %s", payload.get("title"), failed)
