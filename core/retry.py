"""Auto retry policy (pure functions, unit tested)."""
import re
from dataclasses import dataclass

from . import conf

STATUS_RE = re.compile(r"awaiting response\.\.\.\s+(\d{3})")
TRANSIENT_HTTP = {408, 425, 429}


@dataclass(frozen=True)
class Plan:
    retry: bool
    delay: int = 0
    count: int = 0  # consecutive failures including this one
    limit: int = 0  # -1 = forever


def http_status(log_tail: str):
    found = STATUS_RE.findall(log_tail or "")
    return int(found[-1]) if found else None


def is_transient(exit_code: int, log_tail: str) -> bool:
    """wget exit 4 = network failure (refused, timeout, reset ...); exit 8 = server error response, retry only 5xx / 408 / 425 / 429.
    Never retried: disk errors (3), TLS (5), authentication (6), protocol (7), 4xx such as 404."""
    if exit_code == 4:
        return True
    if exit_code == 8:
        status = http_status(log_tail)
        return status is not None and (status >= 500 or status in TRANSIENT_HTTP)
    return False


def limit_for(download) -> int:
    return download.retry_max if download.retry_max is not None else conf.get("retry.default_max")


def delay_for(count_before: int) -> int:
    return min(conf.get("retry.delay_seconds") * (2 ** min(count_before, 30)), conf.get("retry.max_delay_seconds"))


def plan(download, exit_code: int, log_tail: str, progressed: bool) -> Plan:
    limit = limit_for(download)
    if limit == 0 or not is_transient(exit_code, log_tail):
        return Plan(False, limit=limit)
    before = 0 if progressed else download.retry_count
    if limit != -1 and before >= limit:
        return Plan(False, count=before, limit=limit)
    return Plan(True, delay=delay_for(before), count=before + 1, limit=limit)


def parse_retry(value):
    """API input -> retry_max. None / 'default' = use the setting, 'off' = 0, 'always' / 'infinite' = -1."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "default")):
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("off", "none", "no", "false"):
            return 0
        if v in ("always", "infinite", "inf", "forever", "unlimited"):
            return -1
        value = v
    if isinstance(value, bool):
        raise ValueError("retry must be a number, 'always' or 'off'")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("retry must be a number, 'always' or 'off'") from exc
    if not -1 <= n <= 1000:
        raise ValueError("retry must be between -1 (forever) and 1000")
    return n
