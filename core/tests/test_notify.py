import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from unittest import mock

from django.test import Client, override_settings

from core import conf, notify, runner, services, tasks
from core.models import Download
from core.tests.helpers import FileServer, PanelTestCase, make_blob


def run_notifications_inline(testcase):
    """Make ``send_notification.delay`` execute in-process (celery's task_always_eager does not reach us through the
    namespaced django settings, and we must not leave test tasks in the real broker)."""
    patcher = mock.patch.object(tasks.send_notification, "delay", side_effect=lambda payload, only=None: tasks.send_notification.apply(args=(payload, only)))
    patcher.start()
    testcase.addCleanup(patcher.stop)


class NtfyMock:
    def __init__(self, status=200):
        self.messages, self.status, self.script = [], status, []   # script: statuses for the next requests, then ``status``
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.messages.append({"path": self.path, "auth": self.headers.get("Authorization"), "json": json.loads(body or b"{}"),
                                       "ctype": self.headers.get("Content-Type")})
                self.send_response(outer.script.pop(0) if outer.script else outer.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class GntpMock:
    """Minimal GNTP receiver: records every packet and answers -OK (or -ERROR)."""

    def __init__(self, ok=True):
        self.packets, self.ok = [], ok
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(0.4)
            data = b""
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
            text = data.decode("utf-8", "replace")
            self.packets.append(text)
            action = "NOTIFY" if text.startswith("GNTP/1.0 NOTIFY") else "REGISTER"
            if self.ok:
                conn.sendall(f"GNTP/1.0 -OK NONE\r\nResponse-Action: {action}\r\n\r\n".encode())
            else:
                conn.sendall(b"GNTP/1.0 -ERROR NONE\r\nError-Code: 401\r\nError-Description: Not authorized\r\n\r\n")
            conn.close()

    def close(self):
        self._stop = True
        self.sock.close()


class NotifyBase(PanelTestCase):
    def setUp(self):
        super().setUp()
        run_notifications_inline(self)
        self.ntfy = NtfyMock()
        self.addCleanup(self.ntfy.close)
        self._ov2 = override_settings(NTFY_URL=self.ntfy.url, NTFY_TOPIC="dl", NTFY_TOKEN="tk_secret", NTFY_USER="", GNTP_HOST="",
                                      NOTIFY_EVENTS=["completed", "error", "paused", "retry"], NOTIFY_RETRY_INTERVAL=60, NOTIFY_PANEL_URL="https://panel.example.com",
                                      NOTIFY_TIMEOUT=5)
        self._ov2.enable()
        conf.set("retry.delay_seconds", 1); conf.set("retry.max_delay_seconds", 2)

    def tearDown(self):
        self._ov2.disable()
        super().tearDown()

    def titles(self):
        return [m["json"]["title"] for m in self.ntfy.messages]

    def bodies(self):
        return [m["json"]["message"] for m in self.ntfy.messages]


class NtfyTests(NotifyBase):
    def test_nothing_is_sent_without_configuration(self):
        with self.settings(NTFY_URL="", NTFY_TOPIC="", GNTP_HOST=""):
            d = self.make_download("http://h/x.bin", status="completed")
            self.assertFalse(notify.dispatch("completed", d.pk))
        self.assertEqual(self.ntfy.messages, [])

    def test_completed_message_format_and_auth(self):
        blob = make_blob(1_500_000)
        user = self.make_user("alice")
        with FileServer({"/x.bin": blob}) as srv:
            d = self.make_download(srv.url("/x.bin") + "?sig=SECRETTOKEN123&e=99", user=user, filename="x.bin")
            runner.run(d.pk)
        self.assertEqual(len(self.ntfy.messages), 1)
        m = self.ntfy.messages[0]
        self.assertEqual(m["path"], "/")                                   # JSON publish goes to the ROOT url, not /topic
        self.assertEqual(m["ctype"], "application/json")
        self.assertEqual(m["auth"], "Bearer tk_secret")
        j = m["json"]
        self.assertEqual((j["topic"], j["title"], j["priority"], j["tags"], j["click"]), ("dl", "Download complete: x.bin", 3, ["white_check_mark"], "https://panel.example.com/"))
        self.assertIn("1.4 MB", j["message"])
        self.assertIn("by alice", j["message"])
        self.assertIn("from 127.0.0.1", j["message"])
        self.assertNotIn("SECRETTOKEN123", json.dumps(j))                  # signed URLs never leave the server in a notification

    def test_basic_auth_and_unicode(self):
        with self.settings(NTFY_TOKEN="", NTFY_USER="bob", NTFY_PASSWORD="pw"):
            d = self.make_download("http://h/x", status="completed", filename="Café – 映画.mp4")
            notify.dispatch("completed", d.pk)
        import base64
        self.assertEqual(self.ntfy.messages[0]["auth"], "Basic " + base64.b64encode(b"bob:pw").decode())
        self.assertEqual(self.ntfy.messages[0]["json"]["title"], "Download complete: Café – 映画.mp4")

    def test_error_message_has_reason_http_status_and_high_priority(self):
        with FileServer({}) as srv:
            d = self.make_download(srv.url("/missing.bin"))
            runner.run(d.pk)
        j = self.ntfy.messages[0]["json"]
        self.assertEqual((j["title"], j["priority"], j["tags"]), ("Download failed: missing.bin", 4, ["x"]))
        self.assertIn("Server issued an error response (exit 8)", j["message"])
        self.assertIn("404", j["message"])

    def test_paused_reports_progress_and_only_for_real_pauses(self):
        user = self.make_user()
        d = self.make_download("http://h/p.bin", user=user, status="queued", filename="p.bin", total_bytes=1000, downloaded_bytes=250)
        services.apply_action(user, "pause", [d.pk])
        services.apply_action(user, "pause", [d.pk])                       # already paused: nothing happens, nothing is sent
        self.assertEqual(self.titles(), ["Download paused: p.bin"])
        self.assertIn("25.0%", self.bodies()[0])

    def test_events_filter_and_aliases(self):
        user = self.make_user()
        d = self.make_download("http://h/c.bin", user=user, status="queued")
        services.apply_action(user, "cancel", [d.pk])                      # "canceled" is not enabled by default
        self.assertEqual(self.ntfy.messages, [])
        with self.settings(NOTIFY_EVENTS=["success", "failed", "pause", "cancel", "bogus"]):
            self.assertEqual(notify.enabled_events(), {"completed", "error", "paused", "canceled"})
            e = self.make_download("http://h/c2.bin", user=user, status="queued")
            services.apply_action(user, "cancel", [e.pk])
        self.assertEqual(self.titles(), ["Download canceled: c2.bin"])

    def test_failure_is_reported_per_transport(self):
        self.ntfy.status = 500
        with self.settings(GNTP_HOST=""):
            failed = notify.deliver({"kind": "completed", "title": "t", "body": "b", "click": "", "priority": 3, "tags": [], "sticky": False, "gntp_priority": 0})
        self.assertIn("ntfy", failed)
        self.assertIn("HTTP 500", failed["ntfy"])
        self.assertEqual(notify.deliver({"kind": "completed", "title": "t", "body": "b", "click": "", "priority": 3, "tags": [], "sticky": False, "gntp_priority": 0}, only=["gntp"]), {})


class RetryThrottleTests(NotifyBase):
    """The requested behaviour: retry notices follow their own interval, not the retry delay."""

    def retry(self, pk, count=1):
        return notify.dispatch("retry", pk, reason="Network failure (exit 4)", delay=3, count=count, limit=-1)

    def test_first_retry_at_once_then_one_per_interval_with_skipped_count(self):
        with self.settings(NOTIFY_RETRY_INTERVAL=2):
            d = self.make_download("http://h/r.bin", status="queued", retry_count=1)
            sent = [self.retry(d.pk, n) for n in (1, 2, 3)]                 # three retries within the window (retry delay 3 s)
            self.assertEqual(sent, [True, False, False])
            self.assertEqual(len(self.ntfy.messages), 1)
            self.assertNotIn("since the last notice", self.bodies()[0])
            time.sleep(2.2)
            self.assertTrue(self.retry(d.pk, 4))                            # window over, a retry happens -> notice
            self.assertEqual(len(self.ntfy.messages), 2)
            self.assertIn("2 more retries since the last notice", self.bodies()[1])
            self.assertIn("Attempt 4 of unlimited failed: Network failure (exit 4)", self.bodies()[1])
            self.assertIn("Next try in 3s", self.bodies()[1])
            self.assertFalse(self.retry(d.pk, 5))                           # a new window started

    def test_no_retry_no_notice_even_when_interval_passed(self):
        with self.settings(NOTIFY_RETRY_INTERVAL=1):
            d = self.make_download("http://h/r.bin", status="queued")
            self.retry(d.pk)
            time.sleep(1.2)
            self.assertEqual(len(self.ntfy.messages), 1)                    # nothing is sent just because time passed

    def test_interval_zero_every_retry_and_minus_one_never(self):
        d = self.make_download("http://h/r.bin", status="queued")
        with self.settings(NOTIFY_RETRY_INTERVAL=0):
            self.assertEqual([self.retry(d.pk) for _ in range(3)], [True] * 3)
        self.assertEqual(len(self.ntfy.messages), 3)
        with self.settings(NOTIFY_RETRY_INTERVAL=-1):
            self.assertFalse(self.retry(d.pk))
        self.assertEqual(len(self.ntfy.messages), 3)

    def test_throttle_is_per_download(self):
        a, b = self.make_download("http://h/a", status="queued", filename="a.bin"), self.make_download("http://h/b", status="queued", filename="b.bin")
        self.assertEqual([self.retry(a.pk), self.retry(a.pk), self.retry(b.pk)], [True, False, True])
        self.assertEqual(sorted(self.titles()), ["Retrying download: a.bin", "Retrying download: b.bin"])

    def test_other_events_are_never_throttled_and_final_message_closes_the_series(self):
        d = self.make_download("http://h/x.bin", status="completed", retry_total=3, filename="x.bin")
        self.retry(d.pk); self.retry(d.pk)
        self.assertEqual(len(self.ntfy.messages), 1)
        notify.dispatch("completed", d.pk)
        notify.dispatch("completed", d.pk)
        self.assertEqual(self.titles()[1:], ["Download complete: x.bin"] * 2)
        self.assertIn("Succeeded after 3 automatic retries", self.bodies()[1])
        self.assertTrue(self.retry(d.pk))                                    # window was cleared by the final event

    def test_real_run_503_retry_retry_retry_error(self):
        """Retry delay 1 s, retry notices every 60 s: 3 automatic retries produce ONE retry notice, then the final error."""
        with FileServer({"/f.bin": b"x"}, status_map={"/f.bin": 503}) as srv:
            d = self.make_download(srv.url("/f.bin"), retry_max=3, filename="f.bin")
            for _ in range(4):
                Download.objects.filter(pk=d.pk).update(status="running")
                runner.run(d.pk)
        d.refresh_from_db()
        self.assertEqual((d.status, d.retry_total), ("error", 3))
        self.assertEqual(self.titles(), ["Retrying download: f.bin", "Download failed: f.bin"])
        self.assertIn("Attempt 1 of 3 failed", self.bodies()[0])
        self.assertIn("Gave up after 3 automatic retries", self.bodies()[1])
        self.assertIn("503", self.bodies()[1])


class GntpTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        run_notifications_inline(self)
        self.gntp = GntpMock()
        self.addCleanup(self.gntp.close)
        self._ov2 = override_settings(NTFY_URL="", NTFY_TOPIC="", GNTP_HOST="127.0.0.1", GNTP_PORT=self.gntp.port, GNTP_PASSWORD="",
                                      GNTP_APP_NAME="wgetpanel-test", NOTIFY_EVENTS=["completed", "error", "paused", "retry"],
                                      NOTIFY_RETRY_INTERVAL=60, NOTIFY_PANEL_URL="", NOTIFY_TIMEOUT=5)
        self._ov2.enable()

    def tearDown(self):
        self._ov2.disable()
        super().tearDown()

    def notifies(self):
        return [p for p in self.gntp.packets if p.startswith("GNTP/1.0 NOTIFY")]

    def test_register_then_notify_with_fields(self):
        d = self.make_download("http://h/x.bin", status="error", filename="x.bin", error="Network failure (exit 4)\nlog")
        notify.dispatch("error", d.pk)
        self.assertTrue(self.gntp.packets[0].startswith("GNTP/1.0 REGISTER"))
        self.assertIn("Application-Name: wgetpanel-test", self.gntp.packets[0])
        for kind in notify.KINDS:
            self.assertIn(f"Notification-Name: {kind}", self.gntp.packets[0])
        n = self.notifies()[0]
        self.assertIn("Notification-Name: error", n)
        self.assertIn("Notification-Title: Download failed: x.bin", n)
        self.assertIn("Network failure (exit 4)", n)
        self.assertIn("Notification-Sticky: True", n)                        # errors stay on screen
        self.assertIn("Notification-Priority: 1", n)

    def test_password_is_hashed_not_sent(self):
        with self.settings(GNTP_PASSWORD="s3cret"):
            d = self.make_download("http://h/x.bin", status="completed")
            notify.dispatch("completed", d.pk)
        first_line = self.notifies()[0].splitlines()[0]
        self.assertTrue(any(h in first_line for h in ("MD5:", "SHA1:", "SHA256:", "SHA512:")), first_line)
        self.assertTrue(all("s3cret" not in p for p in self.gntp.packets))

    def test_retry_throttle_applies_to_gntp_too(self):
        d = self.make_download("http://h/x.bin", status="queued")
        for n in (1, 2, 3):
            notify.dispatch("retry", d.pk, reason="r", delay=3, count=n, limit=5)
        self.assertEqual(len(self.notifies()), 1)

    def test_server_refusal_is_a_failure(self):
        self.gntp.ok = False
        failed = notify.deliver({"kind": "completed", "title": "t", "body": "b", "click": "", "priority": 3, "tags": [], "sticky": False, "gntp_priority": 0})
        self.assertIn("gntp", failed)
        self.assertIn("refused", failed["gntp"])

    def test_unreachable_server_is_a_failure_not_a_crash(self):
        with self.settings(GNTP_PORT=1):
            failed = notify.deliver({"kind": "completed", "title": "t", "body": "b", "click": "", "priority": 3, "tags": [], "sticky": False, "gntp_priority": 0})
            self.assertIn("gntp", failed)
            d = self.make_download("http://h/x.bin", status="completed")
            self.assertTrue(notify.dispatch("completed", d.pk) in (True, False))  # queueing must never raise


class BothAndEndpointTests(NotifyBase):
    def test_both_transports_receive_the_same_event(self):
        gntp = GntpMock()
        self.addCleanup(gntp.close)
        with self.settings(GNTP_HOST="127.0.0.1", GNTP_PORT=gntp.port):
            d = self.make_download("http://h/x.bin", status="completed", filename="x.bin")
            notify.dispatch("completed", d.pk)
        self.assertEqual(self.titles(), ["Download complete: x.bin"])
        self.assertTrue(any("Notification-Title: Download complete: x.bin" in p for p in gntp.packets))

    def test_one_failing_transport_does_not_block_the_other(self):
        gntp = GntpMock(ok=False)
        self.addCleanup(gntp.close)
        with self.settings(GNTP_HOST="127.0.0.1", GNTP_PORT=gntp.port):
            failed = notify.deliver({"kind": "completed", "title": "t", "body": "b", "click": "", "priority": 3, "tags": [], "sticky": False, "gntp_priority": 0})
        self.assertEqual(list(failed), ["gntp"])
        self.assertEqual(len(self.ntfy.messages), 1)

    def test_failed_delivery_is_retried_only_for_the_transport_that_failed(self):
        """ntfy answers 500 once, GNTP is fine: ntfy must be tried again, GNTP must not get a duplicate."""
        gntp = GntpMock()
        self.addCleanup(gntp.close)
        self.ntfy.script = [500]
        with self.settings(GNTP_HOST="127.0.0.1", GNTP_PORT=gntp.port):
            d = self.make_download("http://h/x.bin", status="completed", filename="x.bin")
            notify.dispatch("completed", d.pk)
        self.assertEqual(len(self.ntfy.messages), 2)                                          # first attempt 500, retry 200
        self.assertEqual(len([p for p in gntp.packets if p.startswith("GNTP/1.0 NOTIFY")]), 1)

    def test_gives_up_after_max_retries(self):
        self.ntfy.status = 500
        d = self.make_download("http://h/x.bin", status="completed", filename="x.bin")
        notify.dispatch("completed", d.pk)
        self.assertEqual(len(self.ntfy.messages), 1 + tasks.send_notification.max_retries)    # 1 try + 3 retries, then dropped

    def test_global_test_endpoint_is_staff_only_and_reports_per_transport(self):
        self.make_user("bob"); self.make_user("root", staff=True)
        c = Client()

        def tok(name):
            return c.post("/api/v1/auth/token", json.dumps({"username": name, "password": "pw-12345-xyz"}), content_type="application/json").json()["token"]

        def post(name, **body):
            return c.post("/api/v1/notify/test", json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {tok(name)}")

        self.assertEqual(post("bob", target="global").status_code, 403)
        r = post("root", target="global")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"], {"ntfy": "ok"})
        self.assertEqual((r.json()["retry_interval"], r.json()["target"]), (60, "global"))
        self.assertEqual(self.titles(), ["wgetpanel test notification"])
        self.ntfy.status = 403
        self.assertIn("HTTP 403", post("root", target="global").json()["results"]["ntfy"])
        with self.settings(NTFY_URL="", NTFY_TOPIC=""):
            r = post("root", target="global")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "not_configured"))
