#!/usr/bin/env python3
"""End-to-end smoke test against a RUNNING stack (web + worker + beat).

    python deploy/smoke_test.py http://localhost:8000 admin 's3cret' http://host-serving-a-file/big.bin

Logs in, queues the URL, watches speed/progress through /api/snapshot/, pauses and resumes it,
waits for completion and prints the result. Exit code 0 only if the download completed.
"""
import http.cookiejar
import json
import sys
import time
import urllib.parse
import urllib.request

base, user, password, url = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3], sys.argv[4]
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def csrf():
    return next((c.value for c in jar if c.name == "csrftoken"), "")


def call(path, body=None):
    req = urllib.request.Request(base + path)
    if body is not None:
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-CSRFToken": csrf(), "Referer": base + "/"})
    try:
        with op.open(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read()[:500].decode(errors="replace"))
        raise


op.open(base + "/login/")
data = urllib.parse.urlencode({"username": user, "password": password, "csrfmiddlewaretoken": csrf()}).encode()
op.open(urllib.request.Request(base + "/login/", data=data, headers={"Referer": base + "/login/"}))
print("health:", json.loads(op.open(base + "/healthz").read()))
res = call("/api/add/", {"urls": [url]})
pk = res["created"][0]
print("queued download", pk)
paused = resumed = False
deadline = time.time() + 300
while time.time() < deadline:
    snap = call("/api/snapshot/")
    d = next(x for x in snap["downloads"] if x["id"] == pk)
    print(f"{d['status']:<9} {d['downloaded']:>10}/{d['total'] or '?':<10} {d['speed']:>9} B/s  scheduler_alive={snap['scheduler_alive']} series={len(snap['series'])}")
    if d["status"] == "running" and not paused and d["downloaded"] > 2_000_000:
        print(">> pause"); call("/api/action/", {"action": "pause", "ids": [pk]}); paused = True
    elif d["status"] == "paused" and not resumed:
        time.sleep(3); print(">> resume"); call("/api/action/", {"action": "resume", "ids": [pk]}); resumed = True
    elif d["status"] in ("completed", "error", "canceled"):
        print("FINAL:", d["status"], d["error"])
        sys.exit(0 if d["status"] == "completed" else 1)
    time.sleep(1)
print("timeout"); sys.exit(2)
