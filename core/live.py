"""Live metrics in Redis.

The worker measures the size of the file being written every second (wget has no
RPC), stores speed / bytes in a short lived hash and appends to a capped series
used for the graphs. Postgres only receives checkpoints, so a thousand viewers
polling the dashboard never touch the database for speed numbers.
"""
import time

import redis
from django.conf import settings

SERIES_LEN = 600  # points kept per series
_client = None


def client():
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.WGP_REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
    return _client


def reset_client():
    global _client
    _client = None


def _k(*parts):
    return "wgp:" + ":".join(str(p) for p in parts)


# --- per download -----------------------------------------------------------
def set_live(pk, downloaded, total, speed, pid):
    r = client()
    key = _k("live", pk)
    pipe = r.pipeline()
    pipe.hset(key, mapping={"downloaded": downloaded, "total": total if total is not None else "", "speed": int(speed), "pid": pid or "", "ts": time.time()})
    pipe.expire(key, 20)
    pipe.rpush(_k("series", pk), f"{int(time.time())}:{int(speed)}:{downloaded}")
    pipe.ltrim(_k("series", pk), -SERIES_LEN, -1)
    pipe.expire(_k("series", pk), 7 * 86400)
    pipe.execute()


def get_live_many(pks):
    if not pks:
        return {}
    pipe = client().pipeline()
    for pk in pks:
        pipe.hgetall(_k("live", pk))
    return {pk: (h or None) for pk, h in zip(pks, pipe.execute())}


def is_alive(pk):
    return bool(client().exists(_k("live", pk)))


def clear_live(pk):
    client().delete(_k("live", pk))


def get_series(pk, n=300):
    return _parse(client().lrange(_k("series", pk), -n, -1))


def drop_series(pk):
    client().delete(_k("series", pk), _k("live", pk), _k("ctl", pk))


# --- control channel (API -> runner) ---------------------------------------
def send_ctl(pk, command):
    client().set(_k("ctl", pk), command, ex=120)


def pop_ctl(pk):
    r = client()
    key = _k("ctl", pk)
    pipe = r.pipeline()
    pipe.get(key)
    pipe.delete(key)
    return pipe.execute()[0]


# --- aggregated series -----------------------------------------------------
def push_aggregate(name, speed):
    r = client()
    key = _k("series", name)
    pipe = r.pipeline()
    pipe.rpush(key, f"{int(time.time())}:{int(speed)}:0")
    pipe.ltrim(key, -SERIES_LEN, -1)
    pipe.expire(key, 86400)
    pipe.execute()


def get_aggregate(name, n=300):
    return _parse(client().lrange(_k("series", name), -n, -1))


def _parse(rows):
    out = []
    for row in rows:
        try:
            ts, speed, size = row.split(":")
            out.append({"t": int(ts), "speed": int(speed), "bytes": int(size)})
        except ValueError:
            continue
    return out


def ping():
    try:
        return bool(client().ping())
    except redis.RedisError:
        return False
