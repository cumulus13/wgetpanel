"""Status change events (Redis pub/sub) consumed by the SSE stream of the mobile API.

Every state transition calls ``emit``; failures are swallowed because a missing event only
delays a notification, it must never break a download.
"""
import json
import logging
import time

from . import live
from .models import Download

log = logging.getLogger(__name__)
CHANNEL = "wgp:events"


def _publish(payload):
    try:
        live.client().publish(CHANNEL, json.dumps(payload))
    except Exception:
        log.warning("could not publish event", exc_info=True)


def emit(pk):
    """Publish the current state of download ``pk``."""
    d = Download.objects.filter(pk=pk).values(
        "id", "owner_id", "status", "filename", "error", "downloaded_bytes", "total_bytes", "exit_code").first()
    if d is None:
        return
    _publish({"type": "status", "id": d["id"], "owner_id": d["owner_id"], "status": d["status"], "filename": d["filename"],
              "error": (d["error"] or "").split("\n")[0][:300], "downloaded": d["downloaded_bytes"], "total": d["total_bytes"],
              "exit_code": d["exit_code"], "ts": time.time()})


def emit_many(pks):
    for pk in pks:
        emit(pk)


def emit_removed(pk, owner_id):
    _publish({"type": "removed", "id": pk, "owner_id": owner_id, "status": "removed", "ts": time.time()})
