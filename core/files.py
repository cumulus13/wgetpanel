"""Deleting downloaded files from disk, with a path guard."""
import logging
import os

from . import conf

log = logging.getLogger(__name__)


def _inside_allowed(directory: str) -> bool:
    real = os.path.realpath(directory)
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in conf.allowed_dirs())


def delete(download) -> bool:
    """Remove the data file and the wget log of ``download``. Returns True if the data file was removed.

    The *parent directory* is resolved (symlinks followed) and must lie inside an allowed download directory, then the
    entry itself is removed without following it, so a symlink can never make us delete something outside."""
    deleted = False
    try:
        if _inside_allowed(download.directory) and os.path.lexists(download.path) and not os.path.isdir(download.path):
            os.remove(download.path)
            deleted = True
        elif os.path.lexists(download.path):
            log.warning("refusing to delete %s: outside the allowed download directories", download.path)
    except OSError:
        log.warning("could not delete %s", download.path, exc_info=True)
    try:
        os.remove(download.log_path)
    except OSError:
        pass
    return deleted
