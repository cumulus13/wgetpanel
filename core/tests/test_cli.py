"""wgp CLI against a real Django server (LiveServerTestCase), real wget, Redis and PostgreSQL."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import stat
import threading
from pathlib import Path

from django.db import connection
from django.test import LiveServerTestCase
from django.utils import timezone

from core import conf, runner
from core.models import ApiToken, Download, SpeedRule
from core.tests.helpers import FileServer, PanelTestCase, make_blob
from core.tests.test_notify import NtfyMock, run_notifications_inline

spec = importlib.util.spec_from_file_location("wgp", Path(__file__).resolve().parents[2] / "cli" / "wgp" / "wgp.py")
wgp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wgp)
PW = "pw-12345-xyz"


class CliBase(LiveServerTestCase, PanelTestCase):
    def setUp(self):
        PanelTestCase.setUp(self)
        self.cfg = os.path.join(self.tmp, "cli-config.json")
        os.environ["WGP_CONFIG"] = self.cfg
        for k in ("WGP_HOST", "WGP_TOKEN"):
            os.environ.pop(k, None)
        self.alice = self.make_user("alice")
        self.admin = self.make_user("root", staff=True)

    def tearDown(self):
        os.environ.pop("WGP_CONFIG", None)
        PanelTestCase.tearDown(self)

    def cli(self, *argv, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = wgp.main(list(argv))
            except SystemExit as exc:  # argparse usage errors exit(2), as a real CLI should
                code = exc.code
        if expect is not None:
            self.assertEqual(code, expect, f"wgp {' '.join(argv)} -> {code}\nstdout: {out.getvalue()}\nstderr: {err.getvalue()}")
        return out.getvalue(), err.getvalue(), code

    def login(self, user="alice"):
        return self.cli("login", self.live_server_url, "-u", user, "-p", PW, "--device", "pytest")

    def finished(self, name="f.bin", size=50_000, user=None, status="completed", **kw):
        d = self.make_download(f"http://h/{name}", user=user or self.alice, status=status, filename=name, **kw)
        open(d.path, "wb").write(b"z" * size)
        Download.objects.filter(pk=d.pk).update(total_bytes=size, downloaded_bytes=size)
        return d


class HelperTests(PanelTestCase):
    def test_parse_ids(self):
        self.assertEqual(wgp.parse_ids(["1", "3-5", "7,9", "#12", "3"]), [1, 3, 4, 5, 7, 9, 12])
        for bad in (["x"], ["5-3"], ["1-99999"]):
            with self.assertRaises(wgp.CliError):
                wgp.parse_ids(bad)

    def test_parse_retry(self):
        self.assertEqual([wgp.parse_retry(x) for x in ("always", "INF", "-1", "off", "0", "3", "default")], ["always", "always", "always", "off", "off", 3, None])
        with self.assertRaises(wgp.CliError):
            wgp.parse_retry("maybe")

    def test_parse_when(self):
        now = dt.datetime(2026, 10, 23, 12, 0).astimezone()
        self.assertEqual(wgp.parse_when(delay="2h30m", now=now), (now + dt.timedelta(hours=2, minutes=30)).isoformat(timespec="seconds"))
        self.assertEqual(wgp.parse_when(delay="1d", now=now), (now + dt.timedelta(days=1)).isoformat(timespec="seconds"))
        self.assertTrue(wgp.parse_when(at="13:30", now=now).startswith("2026-10-23T13:30"))
        self.assertTrue(wgp.parse_when(at="11:00", now=now).startswith("2026-10-24T11:00"))   # already passed today -> tomorrow
        self.assertTrue(wgp.parse_when(at="2027-01-02 03:04", now=now).startswith("2027-01-02T03:04"))
        self.assertIsNone(wgp.parse_when(now=now))
        for bad in ({"at": "tomorrow"}, {"delay": "soon"}, {"delay": ""} if False else {"delay": "5x"}):
            with self.assertRaises(wgp.CliError):
                wgp.parse_when(now=now, **bad)

    def test_parse_days(self):
        self.assertEqual(wgp.parse_days("mon-fri"), "0,1,2,3,4")
        self.assertEqual(wgp.parse_days("weekend"), "5,6")
        self.assertEqual(wgp.parse_days("sat,mon"), "5,0")
        self.assertEqual(wgp.parse_days("fri-mon"), "4,5,6,0")
        self.assertEqual(wgp.parse_days("all"), "")
        self.assertEqual(wgp.days_text("0,6"), "mon,sun")
        with self.assertRaises(wgp.CliError):
            wgp.parse_days("funday")

    def test_formatting(self):
        self.assertEqual((wgp.human_bytes(0), wgp.human_bytes(1536), wgp.human_bytes(5 * 1024 ** 3), wgp.human_bytes(None)), ("0 B", "1.5 KB", "5.0 GB", "-"))
        self.assertEqual((wgp.human_eta(59), wgp.human_eta(125), wgp.human_eta(7384), wgp.human_eta(None)), ("59s", "2m05s", "2h03m", "-"))
        self.assertEqual(wgp.content_disposition_name('attachment; filename="a b.iso"'), "a b.iso")
        self.assertEqual(wgp.content_disposition_name("attachment; filename*=UTF-8''na%C3%AFve.txt"), "naïve.txt")
        self.assertEqual(len(wgp.bar(50, 10)), 10)

    def test_downsample_and_graph(self):
        self.assertEqual(wgp.downsample([1, 2, 3, 4], 2), [1.5, 3.5])
        self.assertEqual(wgp.downsample([1, 2], 10), [1, 2])
        series = [{"t": 1000 + i, "speed": (i % 7) * 1024 * 1024, "bytes": 0} for i in range(300)]
        text = wgp.speed_graph(series, height=6, width=80, title="x")
        self.assertIn("MB/s", text)
        self.assertTrue(any(c in text for c in "┤┼┼│"), text)
        self.assertLessEqual(max(len(l) for l in text.splitlines()), 80 + 4)
        self.assertEqual(wgp.speed_graph([]), "(no speed data yet)")
        self.assertIn("B/s", wgp.speed_graph([{"t": 1, "speed": 0, "bytes": 0}] * 3))   # flat zero line must not crash

    def test_sse_parser(self):
        class R:
            raw = type("Raw", (), {})()
            def iter_content(self, chunk_size=1):
                yield b"id: 1\nevent: status\ndata: {\"id\": 3}\n\n: keep"
                yield b"-alive\n\nevent: progress\ndata: {\"speed\": 5}\n\n"
        self.assertEqual(list(wgp.sse_events(R())), [("status", {"id": 3}), ("keepalive", None), ("progress", {"speed": 5})])


class AuthCliTests(CliBase):
    def test_login_saves_private_config_and_commands_work(self):
        out, _, _ = self.login()
        self.assertIn("logged in", out)
        cfg = json.load(open(self.cfg))
        self.assertTrue(cfg["token"].startswith("wgp_"))
        self.assertEqual(cfg["host"], self.live_server_url)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
        out, _, _ = self.cli("whoami")
        self.assertIn("alice", out)
        out, _, _ = self.cli("tokens")
        self.assertIn("pytest", out)

    def test_wrong_password_and_not_logged_in_and_bad_host(self):
        _, err, code = self.cli("login", self.live_server_url, "-u", "alice", "-p", "nope", expect=None)
        self.assertNotEqual(code, 0)
        self.assertIn("Wrong username or password", err)
        self.assertFalse(os.path.exists(self.cfg))
        _, err, code = self.cli("list", "--host", self.live_server_url, expect=2)
        self.assertIn("not logged in", err)
        _, err, _ = self.cli("login", "localhost:8000", "-u", "a", "-p", "b", expect=2)
        self.assertIn("http://", err)
        _, err, _ = self.cli("list", "--host", "http://127.0.0.1:1", "--token", "wgp_x", expect=2)
        self.assertIn("cannot reach", err)

    def test_revoked_token_gives_clear_message(self):
        self.login()
        ApiToken.objects.all().delete()
        _, err, _ = self.cli("list", expect=2)
        self.assertIn("run  wgp login", err)

    def test_logout_revokes_on_server(self):
        self.login()
        out, _, _ = self.cli("logout")
        self.assertIn("revoked", out)
        self.assertEqual(ApiToken.objects.count(), 0)
        self.assertNotIn("token", json.load(open(self.cfg)))

    def test_tokens_revoke_and_env_overrides(self):
        self.login()
        tid = ApiToken.objects.get().pk
        os.environ.pop("WGP_CONFIG")
        os.environ["WGP_CONFIG"] = os.path.join(self.tmp, "empty.json")           # nothing saved ...
        os.environ["WGP_HOST"], os.environ["WGP_TOKEN"] = self.live_server_url, json.load(open(self.cfg))["token"]
        try:
            out, _, _ = self.cli("whoami")                                          # ... env is enough
            self.assertIn("alice", out)
            self.cli("tokens", "revoke", str(tid))
            self.cli("whoami", expect=2)
        finally:
            os.environ.pop("WGP_HOST"); os.environ.pop("WGP_TOKEN")

    def test_health_needs_no_login(self):
        out, _, _ = self.cli("health", "--host", self.live_server_url)
        self.assertIn("database=ok", out)


class DownloadCliTests(CliBase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_add_options_reach_the_server(self):
        out, _, _ = self.cli("add", "http://example.com/a.iso", "-o", "my.iso", "--limit", "500", "--tries", "3", "--timeout-net", "20", "--retry", "always",
                             "-H", "X-Token: abc", "--referer", "http://r/", "--insecure", "--in", "2h", "--auth-user", "bob", "--auth-password", "s3cret")
        self.assertIn("queued 1 download", out)
        d = Download.objects.get()
        self.assertEqual((d.filename, d.retry_max, d.http_user, d.http_password), ("my.iso", -1, "bob", "s3cret"))
        self.assertEqual(d.options, {"limit_rate": 512000, "tries": 3, "timeout": 20, "referer": "http://r/", "headers": ["X-Token: abc"], "check_certificate": False})
        self.assertGreater(d.not_before, timezone.now() + dt.timedelta(hours=1, minutes=55))
        self.assertLess(d.not_before, timezone.now() + dt.timedelta(hours=2, minutes=5))

    def test_retry_number_off_and_default(self):
        self.cli("add", "http://example.com/1", "--retry", "3")
        self.cli("add", "http://example.com/2", "--retry", "off")
        self.cli("add", "http://example.com/3")
        self.assertEqual(list(Download.objects.order_by("id").values_list("retry_max", flat=True)), [3, 0, None])

    def test_add_from_file_reports_bad_urls_and_exits_1(self):
        path = os.path.join(self.tmp, "urls.txt")
        open(path, "w").write("# comment\nhttp://example.com/ok1\n\nfile:///etc/passwd\nhttp://example.com/ok2\n")
        out, err, code = self.cli("add", "-f", path, expect=1)
        self.assertIn("queued 2", out)
        self.assertIn("Scheme must be", err)
        self.assertEqual(Download.objects.count(), 2)
        _, err, _ = self.cli("add", expect=2)
        self.assertIn("no URL", err)

    def test_list_show_and_json(self):
        a = self.finished("alpha.bin")
        self.make_download("http://h/beta", user=self.alice, status="queued", filename="beta.bin")
        out, _, _ = self.cli("list")
        self.assertIn("alpha.bin", out); self.assertIn("beta.bin", out); self.assertIn("completed", out); self.assertIn("100.0%", out)
        out, _, _ = self.cli("list", "--status", "queued", "-q")
        self.assertIn("beta.bin", out); self.assertNotIn("alpha.bin", out)
        data = json.loads(self.cli("list", "--json")[0])                         # --json also valid after the command
        self.assertEqual({d["filename"] for d in data["downloads"]}, {"alpha.bin", "beta.bin"})
        out, _, _ = self.cli("show", str(a.pk))
        self.assertIn("alpha.bin", out); self.assertIn("auto retry", out); self.assertIn("off", out)
        self.cli("show", "999999", expect=1)

    def test_show_displays_error_log_and_speed_graph_from_a_real_download(self):
        blob = make_blob(1_000_000)
        with FileServer({"/g.bin": blob}, delay=0.1) as srv:
            self.cli("add", srv.url("/g.bin"))
            d = Download.objects.get()
            Download.objects.filter(pk=d.pk).update(status="running")
            runner.run(d.pk)
        out, _, _ = self.cli("show", str(d.pk), "--log", "5")
        self.assertIn("completed", out)
        self.assertIn("speed (", out)                                            # asciichartpy graph header
        self.assertTrue(any(c in out for c in "┤┼"), out)
        self.assertIn("wget log (tail)", out)
        out, _, _ = self.cli("graph", str(d.pk))
        self.assertIn("B/s", out)
        self.assertTrue(any(c in out for c in "┤┼"), out)
        out, _, _ = self.cli("summary", "--no-graph")
        self.assertIn("1 completed", out)

    def test_actions_ids_ranges_and_messages(self):
        ids = [self.make_download(f"http://h/{i}", user=self.alice, status="queued", filename=f"f{i}.bin").pk for i in range(4)]
        out, _, _ = self.cli("pause", f"{ids[0]}-{ids[2]}")
        self.assertIn("3 download(s) paused", out)
        self.assertEqual(Download.objects.filter(status="paused").count(), 3)
        self.cli("resume", str(ids[0]))
        self.cli("bottom", str(ids[0]))
        queued = lambda: list(Download.objects.filter(status="queued").order_by("position", "id").values_list("pk", flat=True))
        self.assertEqual(queued(), [ids[3], ids[0]])          # only queued downloads are reordered
        self.cli("top", str(ids[0]))
        self.assertEqual(queued(), [ids[0], ids[3]])
        self.cli("down", str(ids[0]))
        self.assertEqual(queued(), [ids[3], ids[0]])
        self.cli("up", str(ids[0]))
        self.assertEqual(queued(), [ids[0], ids[3]])
        self.cli("cancel", str(ids[3]))
        out, _, _ = self.cli("retry", str(ids[3]))
        self.assertIn("re-queued", out)
        _, _, code = self.cli("pause", "424242", expect=1)                           # nothing affected -> exit 1

    def test_delete_files_flags(self):
        keep, gone, via_default = self.finished("keep.bin"), self.finished("gone.bin"), self.finished("default.bin")
        out, _, _ = self.cli("remove", str(gone.pk), "--delete-files")
        self.assertIn("files deleted: 1", out)
        self.assertFalse(os.path.exists(gone.path))
        self.cli("remove", str(keep.pk))                                             # server default: keep
        self.assertTrue(os.path.exists(keep.path))
        conf.set("files.delete_on_remove", True)
        self.cli("remove", str(via_default.pk))                                      # server default: delete
        self.assertFalse(os.path.exists(via_default.path))
        again = self.finished("again.bin")
        self.cli("remove", str(again.pk), "--keep-files")                            # explicit keep beats the setting
        self.assertTrue(os.path.exists(again.path))
        self.cli("remove", "1", "--keep-files", "--delete-files", expect=2)          # mutually exclusive

    def test_purge(self):
        a, b = self.finished("a.bin"), self.finished("b.bin", status="error")
        live_one = self.make_download("http://h/q", user=self.alice, status="queued")
        out, _, _ = self.cli("purge", "--delete-files")
        self.assertIn("2 download(s) purged", out)
        self.assertFalse(os.path.exists(a.path))
        self.assertEqual(list(Download.objects.values_list("pk", flat=True)), [live_one.pk])

    def test_get_downloads_the_file(self):
        d = self.finished("data.bin", size=300_000)
        dest = os.path.join(self.tmp, "out")
        out, _, _ = self.cli("get", str(d.pk), "-o", dest + os.sep)
        self.assertIn("saved", out)
        self.assertEqual(open(os.path.join(dest, "data.bin"), "rb").read(), b"z" * 300_000)
        self.assertEqual(os.listdir(dest), ["data.bin"])                              # no .part left behind
        _, err, _ = self.cli("get", str(d.pk), "-o", dest + os.sep, expect=1)
        self.assertIn("exists", err)
        self.cli("get", str(d.pk), "-o", os.path.join(dest, "renamed.bin"))
        self.assertTrue(os.path.exists(os.path.join(dest, "renamed.bin")))
        self.cli("get", str(d.pk), "-o", dest + os.sep, "--force")
        queued = self.make_download("http://h/q", user=self.alice, status="queued")
        _, err, _ = self.cli("get", str(queued.pk), "-o", dest + os.sep, expect=1)
        self.assertIn("No such completed download", err)

    def test_get_resumes_a_partial_file_and_restarts_when_the_file_changed(self):
        d = self.finished("big.bin", size=1_200_000)
        blob = make_blob(1_200_000)
        open(d.path, "wb").write(blob)
        dest = os.path.join(self.tmp, "resume") + os.sep
        os.makedirs(dest)
        api = wgp.Api(self.live_server_url, json.load(open(self.cfg))["token"])
        etag = api.request("HEAD", f"/api/v1/downloads/{d.pk}/file").headers["ETag"]
        part = os.path.join(dest, "big.bin.part")
        # (1) matching ETag: continue from the 700000 bytes we already have
        open(part, "wb").write(blob[:700_000]); open(part + ".etag", "w").write(etag)
        out, _, _ = self.cli("get", str(d.pk), "-o", dest)
        self.assertIn("resumed from 683.6 KB", out)
        self.assertEqual(open(os.path.join(dest, "big.bin"), "rb").read(), blob)
        self.assertEqual(sorted(os.listdir(dest)), ["big.bin"])                       # .part and .part.etag are gone
        # (2) the partial file belongs to an older version of the file: start over, never mix versions
        os.remove(os.path.join(dest, "big.bin"))
        open(part, "wb").write(b"OLD-VERSION-" * 10_000); open(part + ".etag", "w").write('"stale-etag"')
        out, _, _ = self.cli("get", str(d.pk), "-o", dest)
        self.assertNotIn("resumed", out)
        self.assertEqual(open(os.path.join(dest, "big.bin"), "rb").read(), blob)
        # (3) a partial file that is already complete is just renamed, no transfer
        os.remove(os.path.join(dest, "big.bin"))
        open(part, "wb").write(blob); open(part + ".etag", "w").write(etag)
        out, _, _ = self.cli("get", str(d.pk), "-o", dest)
        self.assertIn("already complete", out)
        self.assertEqual(open(os.path.join(dest, "big.bin"), "rb").read(), blob)
        # (4) --no-resume ignores the partial file; no sidecar = no resume either
        os.remove(os.path.join(dest, "big.bin"))
        open(part, "wb").write(blob[:500_000]); open(part + ".etag", "w").write(etag)
        out, _, _ = self.cli("get", str(d.pk), "-o", dest, "--no-resume")
        self.assertNotIn("resumed", out)
        self.assertEqual(open(os.path.join(dest, "big.bin"), "rb").read(), blob)
        os.remove(os.path.join(dest, "big.bin"))
        open(part, "wb").write(blob[:500_000])
        out, _, _ = self.cli("get", str(d.pk), "-o", dest)
        self.assertNotIn("resumed", out)
        self.assertEqual(open(os.path.join(dest, "big.bin"), "rb").read(), blob)

    def test_users_only_see_their_own(self):
        other = self.make_download("http://h/o", user=self.admin, status="queued", filename="secret.bin")
        out, _, _ = self.cli("list")
        self.assertNotIn("secret.bin", out)
        self.cli("show", str(other.pk), expect=1)
        self.cli("remove", str(other.pk), expect=1)
        self.assertTrue(Download.objects.filter(pk=other.pk).exists())


class LiveCliTests(CliBase):
    """watch / events / wait / add --wait: real SSE stream, a real download running in the background."""

    def setUp(self):
        super().setUp()
        self.login()

    def start_download(self, srv, path="/w.bin", **kw):
        out, _, _ = self.cli("add", srv.url(path), "--json", **kw)
        pk = json.loads(out)["created"][0]
        Download.objects.filter(pk=pk).update(status="running")
        errors = []

        def work():
            try:
                runner.run(pk)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()
        t = threading.Thread(target=work)
        return pk, t, errors

    def test_events_stream_snapshot_status_progress(self):
        with FileServer({"/w.bin": make_blob(600_000)}, delay=0.1) as srv:
            pk, t, errors = self.start_download(srv)
            t.start()
            out, _, _ = self.cli("events", "--json", "--max-seconds", "6", "--count", "12")
            t.join()
        kinds = [json.loads(l)["event"] for l in out.splitlines()]
        self.assertEqual(kinds[0], "snapshot")
        self.assertIn("progress", kinds)
        self.assertIn("status", kinds)
        self.assertFalse(errors)

    def test_watch_renders_table_graph_and_final_state(self):
        with FileServer({"/w.bin": make_blob(500_000)}, delay=0.1) as srv:
            pk, t, _ = self.start_download(srv)
            t.start()
            out, _, _ = self.cli("watch", "--max-seconds", "6", "--no-clear", "--interval", "0.5")
            t.join()
        self.assertIn("wgp watch", out)
        self.assertIn("w.bin", out)
        self.assertIn("total speed", out)
        self.assertIn("completed", out.split("wgp watch")[-1])                       # the last frame shows the finished download

    def test_add_wait_exit_codes(self):
        blob = make_blob(300_000)
        with FileServer({"/ok.bin": blob}, delay=0.05) as srv:
            # run the worker side while the CLI waits: dispatch happens by this helper instead of celery
            threading.Thread(target=self._run_when_queued, args=(1, "ok.bin"), daemon=True).start()
            out, _, code = self.cli("add", srv.url("/ok.bin"), "--wait", "--timeout-wait", "20", expect=None)
        self.assertEqual(code, 0, out)
        self.assertIn("completed", out)
        with FileServer({}) as srv:
            threading.Thread(target=self._run_when_queued, args=(2, "missing"), daemon=True).start()
            out, err, code = self.cli("add", srv.url("/missing"), "--wait", "--timeout-wait", "20", expect=None)
        self.assertEqual(code, 1)
        self.assertIn("error", out)

    def _run_when_queued(self, nth, name):
        import time
        try:
            for _ in range(100):
                d = Download.objects.filter(filename=name).first()
                if d:
                    break
                time.sleep(0.1)
            Download.objects.filter(pk=d.pk).update(status="running")
            time.sleep(1.0)                                                        # let the CLI subscribe before the first event
            runner.run(d.pk)
        finally:
            connection.close()

    def test_wait_times_out(self):
        d = self.make_download("http://h/x", user=self.alice, status="queued")
        out, err, code = self.cli("wait", str(d.pk), "--timeout-wait", "1.5", expect=None)
        self.assertEqual(code, 1)
        self.assertIn("timed out", err)


class AdminCliTests(CliBase):
    def test_settings_list_get_set_and_validation(self):
        self.login("root")
        out, _, _ = self.cli("settings", "-v")
        self.assertIn("retry.default_max", out); self.assertIn("files.delete_on_remove", out)
        self.assertEqual(self.cli("settings", "get", "scheduler.max_downloads")[0].strip(), "3")
        out, _, _ = self.cli("settings", "set", "retry.default_max", "-1", "files.delete_on_remove", "true", "scheduler.max_downloads", "7")
        self.assertIn("saved", out)
        self.assertEqual((conf.get("retry.default_max"), conf.get("files.delete_on_remove"), conf.get("scheduler.max_downloads")), (-1, True, 7))
        self.assertIn("(changed)", self.cli("settings")[0])
        conf.invalidate()
        self.cli("settings", "set", "security.allowed_download_dirs", "/a\\n/b")
        self.assertEqual(conf.get("security.allowed_download_dirs"), "/a\n/b")
        _, err, _ = self.cli("settings", "set", "scheduler.max_downloads", "-5", expect=1)
        self.assertIn("minimum", err)
        _, err, _ = self.cli("settings", "set", "nope", "1", expect=2)
        self.assertIn("unknown setting", err)
        self.cli("settings", "set", "scheduler.max_downloads", "abc", expect=2)
        self.cli("settings", "set", "scheduler.max_downloads", expect=2)

    def test_settings_and_rules_are_staff_only(self):
        self.login("alice")
        _, err, _ = self.cli("settings", expect=1)
        self.assertIn("Staff only", err)
        self.cli("rules", expect=1)
        self.cli("rules", "add", "x", "--from", "01:00", "--to", "02:00", "--limit", "1", expect=1)

    def test_rules_lifecycle(self):
        self.login("root")
        self.assertIn("no speed rules", self.cli("rules")[0])
        out, _, _ = self.cli("rules", "add", "Office", "--from", "09:00", "--to", "17:00", "--limit", "256", "--days", "mon-fri")
        self.assertIn("created", out)
        rule = SpeedRule.objects.get()
        self.assertEqual((rule.name, rule.limit_kbps, rule.days), ("Office", 256, "0,1,2,3,4"))
        out, _, _ = self.cli("rules")
        self.assertIn("Office", out); self.assertIn("mon,tue,wed,thu,fri", out); self.assertIn("256 KB/s", out)
        self.cli("rules", "edit", str(rule.pk), "--limit", "128", "--days", "weekend")
        rule.refresh_from_db()
        self.assertEqual((rule.limit_kbps, rule.days), (128, "5,6"))
        self.cli("rules", "disable", str(rule.pk))
        rule.refresh_from_db()
        self.assertFalse(rule.enabled)
        self.cli("rules", "enable", str(rule.pk))
        self.assertIn("Office", self.cli("rules", "--json")[0])
        self.cli("rules", "edit", str(rule.pk), expect=2)
        self.cli("rules", "add", "x", "--from", "99:99", "--to", "02:00", "--limit", "1", expect=1)
        self.cli("rules", "add", expect=2)
        self.cli("rules", "delete", str(rule.pk))
        self.assertEqual(SpeedRule.objects.count(), 0)
        self.cli("rules", "delete", str(rule.pk), expect=1)


class NotifyCliTests(CliBase):
    def test_notify_test_command(self):
        run_notifications_inline(self)
        ntfy = NtfyMock()
        self.addCleanup(ntfy.close)
        self.login("root")
        with self.settings(NTFY_URL="", NTFY_TOPIC="", GNTP_HOST=""):
            _, err, _ = self.cli("notify-test", "--global", expect=1)
            self.assertIn("No global notification transport configured", err)
        with self.settings(NTFY_URL=ntfy.url, NTFY_TOPIC="dl", NTFY_TOKEN="", NOTIFY_RETRY_INTERVAL=60, NOTIFY_EVENTS=["completed", "error", "retry"], GNTP_HOST=""):
            out, _, _ = self.cli("notify-test", "--global")
            self.assertIn("ntfy", out); self.assertIn("ok", out)
            self.assertIn("retry notices: at most every 60s per download", out)
            self.assertEqual(ntfy.messages[0]["json"]["title"], "wgetpanel test notification")
            ntfy.status = 401
            out, _, code = self.cli("notify-test", "--global", expect=1)
            self.assertIn("HTTP 401", out)
        self.login("alice")
        _, err, _ = self.cli("notify-test", "--global", expect=1)                      # staff only
        self.assertIn("Staff only", err)

    def test_show_includes_retry_totals(self):
        self.login("alice")
        d = self.make_download("http://h/x", user=self.alice, status="queued", retry_max=-1, retry_count=2, retry_total=9)
        out, _, _ = self.cli("show", str(d.pk), "--no-graph")
        self.assertIn("2 failed in a row, limit always; 9 retries in total", out)


class UserNotifyCliTests(CliBase):
    def setUp(self):
        super().setUp()
        run_notifications_inline(self)
        self.ntfy = NtfyMock()
        self.addCleanup(self.ntfy.close)
        self._ov_n = self.settings(NTFY_URL=self.ntfy.url, NTFY_TOPIC="admin", NTFY_TOKEN="", GNTP_HOST="", NOTIFY_EVENTS=["completed", "error", "paused", "retry"],
                                   NOTIFY_RETRY_INTERVAL=60, NOTIFY_GLOBAL_SCOPE="none", NOTIFY_USER_GNTP_ALLOW=[])
        self._ov_n.enable()
        self.addCleanup(self._ov_n.disable)
        self.login("alice")

    def test_show_set_rotate_and_test(self):
        out, _, _ = self.cli("notify")
        self.assertIn("ntfy  off", out); self.assertIn("(no target active)", out); self.assertIn("gntp  not enabled for users", out)
        out, _, _ = self.cli("notify", "set", "--ntfy", "on", "--events", "completed,error", "--retry-interval", "45")
        self.assertIn("ntfy  on", out)
        self.assertIn(f"subscribe in the ntfy app to: {self.ntfy.url}/wgetpanel-", out)
        self.assertIn("completed, error", out); self.assertIn("every 45s", out)
        topic = json.loads(self.cli("notify", "--json")[0])["ntfy"]["topic"]
        out, _, _ = self.cli("notify", "rotate-topic")
        self.assertNotIn(topic, out)
        topic = json.loads(self.cli("notify", "--json")[0])["ntfy"]["topic"]
        out, _, _ = self.cli("notify-test")
        self.assertIn("ntfy", out); self.assertIn("ok", out); self.assertIn("at most every 45s", out)
        self.assertEqual(self.ntfy.messages[-1]["json"]["topic"], topic)
        out, _, _ = self.cli("notify", "set", "--retry-interval", "default", "--events", "default")
        self.assertIn("server default", out)

    def test_errors_are_explained(self):
        _, err, _ = self.cli("notify", "set", "--gntp", "on", "--gntp-host", "127.0.0.1", expect=1)
        self.assertIn("NOTIFY_USER_GNTP_ALLOW", err)
        _, err, _ = self.cli("notify", "set", "--events", "bogus", expect=1)
        self.assertIn("unknown event", err)
        _, err, _ = self.cli("notify", "set", expect=2)
        self.assertIn("nothing to change", err)
        self.cli("notify", "set", "--ntfy", "maybe", expect=2)
        _, err, _ = self.cli("notify-test", expect=1)
        self.assertIn("no notification target enabled", err)
