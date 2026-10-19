import hashlib
import threading
import time

from django.db import connection

from core import conf, live, runner, services, wget
from core.models import Download
from core.tests.helpers import FileServer, PanelTestCase, make_blob


def sha(b):
    return hashlib.sha256(b).hexdigest()


class RunnerTests(PanelTestCase):
    def test_download_completes_and_is_identical(self):
        blob = make_blob(1_500_000)
        with FileServer({"/a.bin": blob}) as srv:
            d = self.make_download(srv.url("/a.bin"))
            runner.run(d.pk)
        d.refresh_from_db()
        self.assertEqual(d.status, "completed", d.error)
        self.assertEqual(d.total_bytes, len(blob))
        self.assertEqual(d.downloaded_bytes, len(blob))
        self.assertEqual(d.exit_code, 0)
        self.assertEqual(sha(open(d.path, "rb").read()), sha(blob))
        self.assertIn("Length: 1500000", open(d.log_path).read())  # exact wget output is kept

    def test_rerun_on_already_complete_file_is_completed(self):
        # e.g. the worker died after wget had finished: the reaper requeues, wget -c gets 416 and must report success
        blob = make_blob(500_000)
        with FileServer({"/done.bin": blob}) as srv:
            d = self.make_download(srv.url("/done.bin"))
            runner.run(d.pk)
            Download.objects.filter(pk=d.pk).update(status="running", finished_at=None)
            runner.run(d.pk)
            self.assertTrue(any(r for _, r in srv.requests), "second run must ask for a range")
        d.refresh_from_db()
        self.assertEqual(d.status, "completed", d.error)
        self.assertEqual(d.downloaded_bytes, len(blob))
        self.assertEqual(sha(open(d.path, "rb").read()), sha(blob))

    def test_404_becomes_error_with_log(self):
        with FileServer({}) as srv:
            d = self.make_download(srv.url("/missing.bin"))
            runner.run(d.pk)
        d.refresh_from_db()
        self.assertEqual(d.status, "error")
        self.assertEqual(d.exit_code, 8)
        self.assertIn("404", d.error)

    def test_connection_refused_is_error(self):
        d = self.make_download("http://127.0.0.1:1/x.bin", options={"tries": 1})
        runner.run(d.pk)
        d.refresh_from_db()
        self.assertEqual(d.status, "error")
        self.assertEqual(d.exit_code, 4)

    def test_speed_series_recorded_from_file_size(self):
        blob = make_blob(1_200_000)
        with FileServer({"/s.bin": blob}, delay=0.1) as srv:  # 32KB/0.1s ≈ 320KB/s -> ~4s
            d = self.make_download(srv.url("/s.bin"))
            runner.run(d.pk)
        series = live.get_series(d.pk)
        self.assertGreaterEqual(len(series), 3)
        speeds = [p["speed"] for p in series]
        self.assertGreater(max(speeds), 50_000)
        self.assertLess(max(speeds), 3_000_000)
        sizes = [p["bytes"] for p in series]
        self.assertEqual(sizes, sorted(sizes))  # monotonic
        self.assertEqual(sizes[-1], len(blob))

    def test_pause_then_resume_continues_with_range(self):
        blob = make_blob(2_000_000)
        with FileServer({"/p.bin": blob}, delay=0.1) as srv:
            user = self.make_user()
            d = self.make_download(srv.url("/p.bin"), user=user)

            def pause_soon():
                time.sleep(2.5)
                services.apply_action(user, "pause", [d.pk])
                connection.close()

            t = threading.Thread(target=pause_soon)
            t.start()
            runner.run(d.pk)
            t.join()
            d.refresh_from_db()
            self.assertEqual(d.status, "paused")
            self.assertIsNone(d.pid)
            partial = d.downloaded_bytes
            self.assertTrue(0 < partial < len(blob), partial)
            services.apply_action(user, "resume", [d.pk])
            Download.objects.filter(pk=d.pk).update(status="running")  # what the scheduler does
            runner.run(d.pk)
            ranges = [r for _, r in srv.requests if r]
        d.refresh_from_db()
        self.assertEqual(d.status, "completed", d.error)
        self.assertTrue(ranges, "second run must use a Range request")
        self.assertEqual(sha(open(d.path, "rb").read()), sha(blob))

    def test_resume_against_server_without_range_support_restarts_cleanly(self):
        blob = make_blob(1_500_000)
        with FileServer({"/n.bin": blob}, delay=0.1, ranges=False) as srv:
            user = self.make_user()
            d = self.make_download(srv.url("/n.bin"), user=user)

            def pause_soon():
                time.sleep(1.5)
                services.apply_action(user, "pause", [d.pk])
                connection.close()

            t = threading.Thread(target=pause_soon)
            t.start()
            runner.run(d.pk)
            t.join()
            d.refresh_from_db()
            self.assertEqual(d.status, "paused")
            partial = open(d.path, "rb").read()
            self.assertTrue(0 < len(partial) < len(blob))
            Download.objects.filter(pk=d.pk).update(status="running")
            runner.run(d.pk)
        d.refresh_from_db()
        # a server that ignores Range makes "resume" a restart from zero; the result must still be byte-identical
        self.assertEqual(d.status, "completed", d.error)
        self.assertEqual(open(d.path, "rb").read(), blob)

    def test_cancel_with_delete_removes_file(self):
        blob = make_blob(2_000_000)
        with FileServer({"/c.bin": blob}, delay=0.1) as srv:
            user = self.make_user()
            d = self.make_download(srv.url("/c.bin"), user=user)

            def cancel_soon():
                time.sleep(1.5)
                services.apply_action(user, "cancel", [d.pk], delete_files=True)
                connection.close()

            t = threading.Thread(target=cancel_soon)
            t.start()
            runner.run(d.pk)
            t.join()
        d.refresh_from_db()
        self.assertEqual(d.status, "canceled")
        import os
        self.assertFalse(os.path.exists(d.path))

    def test_speed_rule_change_restarts_wget_with_resume(self):
        from core.models import SpeedRule
        import datetime as dt
        blob = make_blob(3_000_000)
        old = runner.RULE_CHECK_EVERY
        runner.RULE_CHECK_EVERY = 1.0
        try:
            with FileServer({"/r.bin": blob}, delay=0.05) as srv:
                d = self.make_download(srv.url("/r.bin"))

                def add_rule():
                    time.sleep(1.5)
                    SpeedRule.objects.create(name="slow", start=dt.time(0, 0), end=dt.time(23, 59), limit_kbps=4096)
                    connection.close()

                t = threading.Thread(target=add_rule)
                t.start()
                runner.run(d.pk)
                t.join()
                reqs = list(srv.requests)
        finally:
            runner.RULE_CHECK_EVERY = old
        d.refresh_from_db()
        self.assertEqual(d.status, "completed", d.error)
        self.assertGreaterEqual(len(reqs), 2, "wget should have been restarted")
        self.assertTrue(any(r for _, r in reqs[1:]), "restart must resume with Range")
        self.assertEqual(sha(open(d.path, "rb").read()), sha(blob))

    def test_limit_rate_is_applied(self):
        blob = make_blob(600_000)
        with FileServer({"/l.bin": blob}) as srv:
            d = self.make_download(srv.url("/l.bin"), options={"limit_rate": 150_000})
            t0 = time.time()
            runner.run(d.pk)
            took = time.time() - t0
        d.refresh_from_db()
        self.assertEqual(d.status, "completed", d.error)
        self.assertGreater(took, 2.5)  # 600KB at 150KB/s ≈ 4s (wget's first burst is not throttled)

    def test_basic_auth_via_wgetrc(self):
        blob = make_blob(1000)
        with FileServer({"/auth.bin": blob}, auth=("bob", "p4ss")) as srv:
            ok = self.make_download(srv.url("/auth.bin"), http_user="bob", filename="ok.bin")
            ok.http_password = "p4ss"
            ok.save()
            runner.run(ok.pk)
            bad = self.make_download(srv.url("/auth.bin"), http_user="bob", filename="bad.bin")
            bad.http_password = "wrong"
            bad.save()
            runner.run(bad.pk)
        ok.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(ok.status, "completed", ok.error)
        self.assertEqual(bad.status, "error")
        self.assertEqual(bad.exit_code, 6)
