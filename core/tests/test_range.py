import hashlib
import json
import os

from django.test import Client

from core import filesend
from core.tests.helpers import PanelTestCase, make_blob


class ParseRangeTests(PanelTestCase):
    def test_parse(self):
        p = filesend.parse_range
        self.assertEqual(p("bytes=0-99", 1000), (0, 99))
        self.assertEqual(p("bytes=500-", 1000), (500, 999))
        self.assertEqual(p("bytes=-100", 1000), (900, 999))
        self.assertEqual(p("bytes=-5000", 1000), (0, 999))             # suffix longer than the file = whole file
        self.assertEqual(p("bytes=900-5000", 1000), (900, 999))        # end beyond EOF is clamped
        self.assertEqual(p("bytes = 5 - 9", 1000), (5, 9))             # spaces tolerated
        for unsat in ("bytes=1000-", "bytes=2000-3000", "bytes=-0"):
            self.assertEqual(p(unsat, 1000), "unsatisfiable", unsat)
        self.assertEqual(p("bytes=0-", 0), "unsatisfiable")
        for ignored in (None, "", "bytes=", "bytes=-", "items=0-5", "bytes=5-2", "bytes=0-1,5-6", "bytes=a-b", "garbage"):
            self.assertIsNone(p(ignored, 1000), ignored)


class RangeEndpointTests(PanelTestCase):
    SIZE = 1_000_003  # not a multiple of the chunk size

    def setUp(self):
        super().setUp()
        self.user, self.other = self.make_user("alice"), self.make_user("bob")
        self.blob = make_blob(self.SIZE)
        self.d = self.make_download("http://h/data.bin", user=self.user, status="completed", filename="data.bin")
        open(self.d.path, "wb").write(self.blob)
        self.c = Client()
        r = self.c.post("/api/v1/auth/token", json.dumps({"username": "alice", "password": "pw-12345-xyz"}), content_type="application/json")
        self.tok = r.json()["token"]
        self.url = f"/api/v1/downloads/{self.d.pk}/file"

    def get(self, **headers):
        return self.c.get(self.url, HTTP_AUTHORIZATION=f"Bearer {self.tok}", **headers)

    def body(self, r):
        return b"".join(r.streaming_content)

    def test_full_download_advertises_ranges(self):
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Accept-Ranges"], "bytes")
        self.assertEqual(r["Content-Length"], str(self.SIZE))
        self.assertTrue(r["ETag"].startswith('"') and r["Last-Modified"])
        self.assertIn('filename="data.bin"', r["Content-Disposition"])
        self.assertEqual(hashlib.sha256(self.body(r)).digest(), hashlib.sha256(self.blob).digest())

    def test_partial_ranges_return_exact_bytes(self):
        cases = {"bytes=0-0": (0, 0), "bytes=10-99": (10, 99), "bytes=262143-262144": (262143, 262144),     # across a chunk boundary
                 "bytes=500000-": (500000, self.SIZE - 1), "bytes=-1000": (self.SIZE - 1000, self.SIZE - 1),
                 f"bytes={self.SIZE - 1}-": (self.SIZE - 1, self.SIZE - 1), "bytes=999000-9999999": (999000, self.SIZE - 1)}
        for header, (a, b) in cases.items():
            r = self.get(HTTP_RANGE=header)
            self.assertEqual(r.status_code, 206, header)
            self.assertEqual(r["Content-Range"], f"bytes {a}-{b}/{self.SIZE}", header)
            self.assertEqual(r["Content-Length"], str(b - a + 1), header)
            self.assertEqual(self.body(r), self.blob[a:b + 1], header)

    def test_unsatisfiable_is_416_with_size(self):
        for header in (f"bytes={self.SIZE}-", "bytes=99999999-", "bytes=-0"):
            r = self.get(HTTP_RANGE=header)
            self.assertEqual(r.status_code, 416, header)
            self.assertEqual(r["Content-Range"], f"bytes */{self.SIZE}")

    def test_unusable_headers_fall_back_to_the_whole_file(self):
        for header in ("bytes=0-1,5-6", "items=0-5", "bytes=9-2", "junk"):
            r = self.get(HTTP_RANGE=header)
            self.assertEqual((r.status_code, r["Content-Length"]), (200, str(self.SIZE)), header)

    def test_if_range(self):
        etag = self.get()["ETag"]
        r = self.get(HTTP_RANGE="bytes=100-199", HTTP_IF_RANGE=etag)
        self.assertEqual((r.status_code, self.body(r)), (206, self.blob[100:200]))
        r = self.get(HTTP_RANGE="bytes=100-199", HTTP_IF_RANGE='"old-version"')       # file changed -> everything again
        self.assertEqual((r.status_code, r["Content-Length"]), (200, str(self.SIZE)))
        modified = self.get()["Last-Modified"]
        self.assertEqual(self.get(HTTP_RANGE="bytes=0-9", HTTP_IF_RANGE=modified).status_code, 206)

    def test_etag_changes_when_the_file_changes(self):
        before = self.get()["ETag"]
        open(self.d.path, "ab").write(b"more")
        self.assertNotEqual(self.get()["ETag"], before)

    def test_head_has_headers_and_no_body(self):
        r = self.c.head(self.url, HTTP_AUTHORIZATION=f"Bearer {self.tok}")
        self.assertEqual((r.status_code, r["Content-Length"], r["Accept-Ranges"]), (200, str(self.SIZE), "bytes"))
        self.assertIn("data.bin", r["Content-Disposition"])
        self.assertEqual(b"".join(r.streaming_content) if r.streaming else r.content, b"")
        r = self.c.head(self.url, HTTP_AUTHORIZATION=f"Bearer {self.tok}", HTTP_RANGE="bytes=10-19")
        self.assertEqual((r.status_code, r["Content-Length"], r["Content-Range"]), (206, "10", f"bytes 10-19/{self.SIZE}"))

    def test_resume_by_pieces_reassembles_identically(self):
        got, pos = b"", 0
        while pos < self.SIZE:
            r = self.get(HTTP_RANGE=f"bytes={pos}-{pos + 299_999}")
            self.assertEqual(r.status_code, 206)
            got += self.body(r)
            pos = len(got)
        self.assertEqual(got, self.blob)

    def test_access_control_is_unchanged(self):
        self.assertEqual(self.c.get(self.url, HTTP_RANGE="bytes=0-9").status_code, 401)
        tok_b = self.c.post("/api/v1/auth/token", json.dumps({"username": "bob", "password": "pw-12345-xyz"}), content_type="application/json").json()["token"]
        self.assertEqual(self.c.get(self.url, HTTP_AUTHORIZATION=f"Bearer {tok_b}", HTTP_RANGE="bytes=0-9").status_code, 404)
        self.assertEqual(self.c.head(self.url, HTTP_AUTHORIZATION=f"Bearer {tok_b}").status_code, 404)
        from core.models import Download
        Download.objects.filter(pk=self.d.pk).update(status="running")
        self.assertEqual(self.get(HTTP_RANGE="bytes=0-9").status_code, 404)

    def test_web_endpoint_supports_ranges_too(self):
        web = Client()
        web.force_login(self.user)
        r = web.get(f"/api/download/{self.d.pk}/file/", HTTP_RANGE="bytes=-10")
        self.assertEqual((r.status_code, b"".join(r.streaming_content)), (206, self.blob[-10:]))
        self.assertEqual(web.head(f"/api/download/{self.d.pk}/file/").status_code, 200)
