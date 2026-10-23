import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import TransactionTestCase, override_settings

from core import conf, live

TEST_REDIS = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


def make_blob(size, seed=1):
    out, h = bytearray(), hashlib.sha256(str(seed).encode()).digest()
    while len(out) < size:
        h = hashlib.sha256(h).digest()
        out += h
    return bytes(out[:size])


class FileServer:
    """Tiny HTTP server with Range support and optional throttling (sleep per 32 KB chunk)."""

    def __init__(self, files, delay=0.0, auth=None, ranges=True, status_map=None):
        self.files, self.delay, self.requests, self.auth, self.ranges = files, delay, [], auth, ranges
        self.status_map = status_map if status_map is not None else {}  # path -> forced HTTP status (e.g. 503)
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range")))
                forced = outer.status_map.get(self.path)
                if forced:
                    self.send_response(forced)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if outer.auth:
                    import base64
                    want = "Basic " + base64.b64encode(f"{outer.auth[0]}:{outer.auth[1]}".encode()).decode()
                    if self.headers.get("Authorization") != want:
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", 'Basic realm="t"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                data = outer.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, status = 0, 200
                m = re.match(r"bytes=(\d+)-", self.headers.get("Range") or "") if outer.ranges else None
                if m:
                    start, status = int(m.group(1)), 206
                    if start >= len(data):  # what real servers (nginx, apache) answer
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                self.send_response(status)
                self.send_header("Content-Length", str(len(data) - start))
                if outer.ranges:
                    self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                self.end_headers()
                try:
                    for i in range(start, len(data), 32768):
                        self.wfile.write(data[i:i + 32768])
                        if outer.delay:
                            time.sleep(outer.delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


class PanelTestCase(TransactionTestCase):
    """Real Redis (db 15) + real wget; downloads go to a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wgp-test-")
        self.dl_dir = os.path.join(self.tmp, "dl")
        os.makedirs(self.dl_dir)
        self._ov = override_settings(WGP_REDIS_URL=TEST_REDIS, WGP_DOWNLOAD_ROOT=self.dl_dir,
                                     WGP_LOG_DIR=os.path.join(self.tmp, "logs"), WGP_BLOCK_PRIVATE_ADDRESSES=False)
        self._ov.enable()
        live.reset_client()
        live.client().flushdb()
        conf.invalidate()

    def tearDown(self):
        self._ov.disable()
        live.client().flushdb()
        live.reset_client()
        conf.invalidate()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_user(self, name="alice", staff=False):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.create_user(name, password="pw-12345-xyz", is_staff=staff)

    def make_download(self, url, user=None, status="running", **kw):
        from core.models import Download
        from core.scheduler import next_position
        from core import wget
        name = kw.pop("filename", wget.filename_from_url(url))
        return Download.objects.create(owner=user, url=url, directory=self.dl_dir, filename=name, status=status,
                                       position=next_position(), **kw)
