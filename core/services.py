"""Business logic used by the API views (kept free of HTTP concerns)."""
import os

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import conf, events, files, live, notify, retry, wget
from .models import Download
from .scheduler import next_position

MAX_URLS_PER_REQUEST = 100


def create_downloads(user, urls, directory=None, filename=None, options=None, not_before=None, http_user="", http_password="", retry_max=None):
    """Returns (created, errors). Valid lines are created even if others fail."""
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        return [], [{"line": 0, "url": "", "error": "No URL given"}]
    if len(urls) > MAX_URLS_PER_REQUEST:
        return [], [{"line": 0, "url": "", "error": f"At most {MAX_URLS_PER_REQUEST} URLs per request"}]
    errors, created = [], []
    try:
        retry_max = retry.parse_retry(retry_max)
    except ValueError as exc:
        return [], [{"line": 0, "url": "", "error": str(exc)}]
    try:
        directory = wget.validate_directory(directory)
        options = wget.validate_options(options)
        if filename:
            filename = wget.safe_filename(filename)
            if len(urls) > 1:
                raise wget.ValidationError("A file name can only be used with a single URL")
            if not filename:
                raise wget.ValidationError("Invalid file name")
    except wget.ValidationError as exc:
        return [], [{"line": 0, "url": "", "error": str(exc)}]

    cap = conf.get("limits.max_queue_per_user")
    if cap and Download.objects.filter(owner=user, status__in=Download.ACTIVE).count() + len(urls) > cap:
        return [], [{"line": 0, "url": "", "error": f"Queue limit reached ({cap} unfinished downloads per user)"}]

    taken = set(Download.objects.filter(status__in=Download.ACTIVE).values_list("directory", "filename"))
    taken = {os.path.join(a, b) for a, b in taken}
    with transaction.atomic():
        pos = next_position()
        for i, url in enumerate(urls, 1):
            try:
                url = wget.validate_url(url)  # stored in its normalized form
            except wget.ValidationError as exc:
                errors.append({"line": i, "url": url[:200], "error": str(exc)})
                continue
            name = filename or wget.filename_from_url(url)
            name = wget.unique_name(directory, name, taken)
            taken.add(os.path.join(directory, name))
            d = Download(owner=user, url=url, directory=directory, filename=name, options=options,
                         http_user=http_user or "", position=pos, not_before=not_before, retry_max=retry_max)
            d.http_password = http_password
            d.save()
            pos += 1
            created.append(d)
    events.emit_many([d.pk for d in created])
    return created, errors


def visible(user):
    qs = Download.objects.all()
    return qs if user.is_staff else qs.filter(owner=user)


def _kill_if_running(d, command):
    if d.status == Download.RUNNING:
        try:
            live.send_ctl(d.pk, command)
        except Exception:
            pass  # the runner notices the status change within a few seconds anyway


def apply_action(user, action, ids, delete_files=None, stats=None):
    """Returns number of downloads affected.

    ``delete_files``: True / False, or None = use setting files.delete_on_remove (cancel, remove, purge only).
    ``stats`` (optional dict) receives ``files_deleted`` (removed now) and ``files_pending`` (running downloads:
    the worker removes the file as soon as wget has stopped)."""
    stats = stats if stats is not None else {}
    stats.setdefault("files_deleted", 0)
    stats.setdefault("files_pending", 0)
    if delete_files is None:
        delete_files = conf.get("files.delete_on_remove")
    qs = visible(user)
    n = 0
    if action == "purge":
        gone = qs.filter(status__in=Download.FINISHED)
        if ids:
            gone = gone.filter(pk__in=ids)
        for d in gone:
            if delete_files:
                stats["files_deleted"] += _delete_file(d)
            live.drop_series(d.pk)
        removed = list(gone.values_list("pk", "owner_id"))
        n = len(removed)
        gone.delete()
        for pk, owner in removed:
            events.emit_removed(pk, owner)
        return n
    touched = []
    for d in qs.filter(pk__in=ids):
        if action == "pause" and d.status in (Download.RUNNING, Download.QUEUED):
            was = d.status
            Download.objects.filter(pk=d.pk).update(status=Download.PAUSED)
            if was == Download.RUNNING:
                _kill_if_running(d, "pause")
            n += 1
            touched.append(d.pk)
            notify.dispatch("paused", d.pk)
        elif action == "resume" and d.status == Download.PAUSED:
            Download.objects.filter(pk=d.pk).update(status=Download.QUEUED, not_before=None)
            n += 1
            touched.append(d.pk)
        elif action == "retry" and d.status in (Download.ERROR, Download.CANCELED):
            Download.objects.filter(pk=d.pk).update(status=Download.QUEUED, error="", exit_code=None, finished_at=None,
                                                    requeues=0, retry_count=0, position=next_position(), not_before=None)
            n += 1
            touched.append(d.pk)
        elif action == "cancel" and d.status in Download.ACTIVE:
            was = d.status
            Download.objects.filter(pk=d.pk).update(status=Download.CANCELED, finished_at=timezone.now(), pid=None)
            if was == Download.RUNNING:
                _kill_if_running(d, "cancel_delete" if delete_files else "cancel")
                stats["files_pending"] += bool(delete_files)
            elif delete_files:
                stats["files_deleted"] += _delete_file(d)
            n += 1
            touched.append(d.pk)
            notify.dispatch("canceled", d.pk)
        elif action == "remove":
            if d.status == Download.RUNNING:
                _kill_if_running(d, "cancel_delete" if delete_files else "cancel")
                stats["files_pending"] += bool(delete_files)
            elif delete_files:
                stats["files_deleted"] += _delete_file(d)
            live.drop_series(d.pk)
            pk, owner = d.pk, d.owner_id
            d.delete()
            events.emit_removed(pk, owner)
            n += 1
            continue
    if action in ("top", "bottom", "up", "down"):
        n = _reorder(qs, action, ids)
    elif action != "remove":
        events.emit_many(touched)
    return n


def _delete_file(d):
    return files.delete(d)


def _reorder(qs, action, ids):
    queued = list(qs.filter(status=Download.QUEUED).order_by("position", "id"))
    order = [d for d in queued]
    sel = [d for d in order if d.pk in set(ids)]
    if not sel:
        return 0
    rest = [d for d in order if d.pk not in set(ids)]
    if action == "top":
        order = sel + rest
    elif action == "bottom":
        order = rest + sel
    else:
        step = -1 if action == "up" else 1
        order = order[:]
        idxs = sorted((order.index(d) for d in sel), reverse=(step == 1))
        for i in idxs:
            j = i + step
            if 0 <= j < len(order) and order[j] not in sel:
                order[i], order[j] = order[j], order[i]
    base = min(d.position for d in queued)
    with transaction.atomic():
        for offset, d in enumerate(order):
            if d.position != base + offset:
                Download.objects.filter(pk=d.pk).update(position=base + offset)
    return len(sel)


def completed_file(user, pk):
    """(real_path, file_name) of a completed download the user may read, else None."""
    d = visible(user).filter(pk=pk, status=Download.COMPLETED).first()
    if d is None or not os.path.isfile(d.path):
        return None
    real = os.path.realpath(d.path)
    if not any(real.startswith(r.rstrip(os.sep) + os.sep) for r in conf.allowed_dirs()):
        return None
    return real, d.filename


# ---------------------------------------------------------------- speed rules (shared by the web UI and /api/v1)
def rule_dict(r):
    return {"id": r.pk, "name": r.name, "enabled": r.enabled, "days": r.days, "start": r.start.strftime("%H:%M"),
            "end": r.end.strftime("%H:%M"), "limit_kbps": r.limit_kbps, "active_now": r.is_active()}


def parse_rule(data, partial=False):
    """Validated model fields from request data. ``partial``: only the keys present (PATCH)."""
    from datetime import datetime

    out = {}
    try:
        if not partial or "name" in data:
            out["name"] = str(data.get("name") or "Rule")[:100]
        if not partial or "enabled" in data:
            out["enabled"] = bool(data.get("enabled", True))
        if not partial or "days" in data:
            days = data.get("days") or ""
            if isinstance(days, (list, tuple)):
                days = ",".join(str(d) for d in days)
            out["days"] = ",".join(str(int(x)) for x in str(days).split(",") if x.strip() != "" and 0 <= int(x) <= 6)
        for key, default in (("start", "00:00"), ("end", "23:59")):
            if not partial or key in data:
                out[key] = datetime.strptime(data.get(key, default), "%H:%M").time()
        if not partial or "limit_kbps" in data:
            out["limit_kbps"] = max(0, int(data.get("limit_kbps") or 0))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid rule") from exc
    return out
