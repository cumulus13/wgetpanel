"""Serving a finished file with HTTP Range support (RFC 9110): resumable downloads for the CLI and the mobile app.

Single ranges only (``bytes=500-999``, ``bytes=500-``, ``bytes=-500``); several ranges in one header are answered with the
whole file, which the RFC allows. ``If-Range`` (ETag or Last-Modified) makes sure a resume never glues two different
versions of a file together. HEAD gives size, ETag and file name without a body.
"""
import os
import re

from django.http import HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header, http_date

CHUNK = 256 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def etag_for(st):
    return f'"{st.st_size:x}-{st.st_mtime_ns:x}"'


def parse_range(header, size):
    """None = ignore the header (send everything), 'unsatisfiable' = 416, else (start, end) inclusive."""
    if not header:
        return None
    spec = header.replace(" ", "")
    m = RANGE_RE.match(spec)
    if not m:  # includes multiple ranges ("bytes=0-1,5-6") and other units
        return None
    first, last = m.groups()
    if first == "" and last == "":
        return None
    if first == "":  # suffix: the last N bytes
        n = int(last)
        if n == 0 or size == 0:
            return "unsatisfiable"
        return max(size - n, 0), size - 1
    start = int(first)
    if last and int(last) < start:
        return None  # syntactically invalid range: ignore it
    if start >= size:
        return "unsatisfiable"
    end = min(int(last), size - 1) if last else size - 1
    return start, end


def _iter_file(path, start, length):
    with open(path, "rb") as fh:
        fh.seek(start)
        left = length
        while left > 0:
            chunk = fh.read(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk


def serve(request, path, filename):
    st = os.stat(path)
    size = st.st_size
    etag, modified = etag_for(st), http_date(st.st_mtime)
    rng = parse_range(request.headers.get("Range"), size)
    if_range = (request.headers.get("If-Range") or "").strip()
    if rng and if_range and if_range not in (etag, modified):
        rng = None  # the file changed since the client started: send it all again
    headers = {"Accept-Ranges": "bytes", "ETag": etag, "Last-Modified": modified,
               "Content-Disposition": content_disposition_header(True, filename), "Content-Type": "application/octet-stream"}
    if rng == "unsatisfiable":
        resp = HttpResponse(status=416)
        resp["Content-Range"] = f"bytes */{size}"
        for k, v in headers.items():
            resp[k] = v
        return resp
    status, start, length = 200, 0, size
    if rng:
        start, end = rng
        status, length = 206, end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        resp = HttpResponse(status=status)
    else:
        resp = StreamingHttpResponse(_iter_file(path, start, length), status=status)
    resp["Content-Length"] = str(length)
    for k, v in headers.items():
        resp[k] = v
    return resp
