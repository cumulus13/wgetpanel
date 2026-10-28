import json
import os
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from core import conf, retry, runner, services, tasks
from core.models import Download, SpeedRule
from core.tests.helpers import FileServer, PanelTestCase, make_blob


class RetryPolicyTests(PanelTestCase):
    def test_parse_retry(self):
        for raw, want in ((None, None), ("", None), ("default", None), ("off", 0), ("none", 0), (0, 0), ("always", -1), ("infinite", -1),
                          ("INF", -1), (-1, -1), (3, 3), ("5", 5)):
            self.assertEqual(retry.parse_retry(raw), want, raw)
        for bad in ("many", 1001, -2, True, 1.5e9):
            with self.assertRaises(ValueError, msg=bad):
                retry.parse_retry(bad)

    def test_transient_classification(self):
        ok = "HTTP request sent, awaiting response... {} {}"
        self.assertTrue(retry.is_transient(4, ""))
        for code in (500, 502, 503, 504, 429, 408, 425):
            self.assertTrue(retry.is_transient(8, ok.format(code, "x")), code)
        for code in (400, 401, 403, 404, 410):
            self.assertFalse(retry.is_transient(8, ok.format(code, "x")), code)
        self.assertFalse(retry.is_transient(8, ""))
        for exit_code in (1, 2, 3, 5, 6, 7):
            self.assertFalse(retry.is_transient(exit_code, ""), exit_code)

    def test_plan_backoff_limit_and_progress_reset(self):
        conf.set("retry.delay_seconds", 10); conf.set("retry.max_delay_seconds", 50)
        d = self.make_download("http://h/x", retry_max=3)
        p = retry.plan(d, 4, "", progressed=False)
        self.assertEqual((p.retry, p.delay, p.count, p.limit), (True, 10, 1, 3))
        d.retry_count = 2
        p = retry.plan(d, 4, "", progressed=False)
        self.assertEqual((p.retry, p.delay, p.count), (True, 40, 3))
        d.retry_count = 3
        self.assertFalse(retry.plan(d, 4, "", progressed=False).retry)          # exhausted
        p = retry.plan(d, 4, "", progressed=True)                               # progress -> counter restarts
        self.assertEqual((p.retry, p.delay, p.count), (True, 10, 1))
        d.retry_max, d.retry_count = -1, 20
        p = retry.plan(d, 4, "", progressed=False)
        self.assertEqual((p.retry, p.delay), (True, 50))                        # capped, never gives up
        d.retry_max = 0
        self.assertFalse(retry.plan(d, 4, "", progressed=False).retry)

    def test_per_download_overrides_setting(self):
        conf.set("retry.default_max", 5)
        d = self.make_download("http://h/x")
        self.assertEqual(retry.limit_for(d), 5)
        d.retry_max = 0
        self.assertEqual(retry.limit_for(d), 0)


class RetryRunTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        conf.set("retry.delay_seconds", 5); conf.set("retry.max_delay_seconds", 30)

    def fail_once(self, srv, d):
        Download.objects.filter(pk=d.pk).update(status="running")
        runner.run(d.pk)
        d.refresh_from_db()
        return d

    def test_503_is_retried_n_times_then_fails(self):
        with FileServer({"/a.bin": b"x"}, status_map={"/a.bin": 503}) as srv:
            d = self.make_download(srv.url("/a.bin"), retry_max=2)
            d = self.fail_once(srv, d)
            self.assertEqual((d.status, d.retry_count), ("queued", 1))
            self.assertTrue(d.error.startswith("Auto retry 1/2 in 5s after: Server issued an error response (exit 8)"), d.error)
            self.assertIn("503", d.error)
            self.assertGreater(d.not_before, timezone.now() + timedelta(seconds=2))
            d = self.fail_once(srv, d)
            self.assertEqual((d.status, d.retry_count), ("queued", 2))
            d = self.fail_once(srv, d)
            self.assertEqual(d.status, "error")
            self.assertEqual(d.exit_code, 8)

    def test_always_never_gives_up_and_backoff_is_capped(self):
        delays = []
        with FileServer({"/a.bin": b"x"}, status_map={"/a.bin": 503}) as srv:
            d = self.make_download(srv.url("/a.bin"), retry_max=-1)
            for _ in range(5):
                before = timezone.now()
                d = self.fail_once(srv, d)
                self.assertEqual(d.status, "queued")
                delays.append(round((d.not_before - before).total_seconds()))
        self.assertEqual(d.retry_count, 5)
        self.assertEqual([x // 5 * 5 for x in delays], [5, 10, 20, 30, 30])  # 5,10,20 then capped at 30 (±1 s timing slack)
        self.assertIn("5/∞", d.error)

    def test_404_is_never_retried_even_when_always(self):
        with FileServer({}) as srv:
            d = self.make_download(srv.url("/missing"), retry_max=-1)
            d = self.fail_once(srv, d)
        self.assertEqual((d.status, d.exit_code, d.retry_count), ("error", 8, 0))

    def test_wrong_password_is_never_retried(self):
        with FileServer({"/a.bin": b"x"}, auth=("bob", "right")) as srv:
            d = self.make_download(srv.url("/a.bin"), retry_max=-1, http_user="bob")
            d.http_password = "wrong"
            d.save()
            d = self.fail_once(srv, d)
        self.assertEqual((d.status, d.exit_code), ("error", 6))

    def test_connection_refused_uses_setting_and_per_download_off(self):
        conf.set("retry.default_max", 1)
        url = "http://127.0.0.1:1/x.bin"
        a = self.make_download(url, options={"tries": 1}, filename="a.bin")
        b = self.make_download(url, options={"tries": 1}, retry_max=0, filename="b.bin")
        a, b = self.fail_once(None, a), self.fail_once(None, b)
        self.assertEqual((a.status, a.retry_count), ("queued", 1))   # from the setting
        self.assertEqual(b.status, "error")                          # explicit "off"

    def test_recovers_when_server_comes_back(self):
        blob = make_blob(400_000)
        with FileServer({"/r.bin": blob}, status_map={"/r.bin": 503}) as srv:
            d = self.make_download(srv.url("/r.bin"), retry_max=3)
            d = self.fail_once(srv, d)
            self.assertEqual(d.status, "queued")
            srv.status_map.clear()
            d = self.fail_once(srv, d)
        self.assertEqual(d.status, "completed", d.error)
        self.assertEqual(d.error, "")
        self.assertEqual(open(d.path, "rb").read(), blob)

    def test_scheduler_waits_for_not_before_then_starts(self):
        from core import scheduler
        conf.set("scheduler.max_downloads", 3)
        d = self.make_download("http://h/x", status="queued", retry_max=-1,
                               not_before=timezone.now() + timedelta(seconds=60))
        self.assertEqual(scheduler.tick(), [])
        Download.objects.filter(pk=d.pk).update(not_before=timezone.now() - timedelta(seconds=1))
        self.assertEqual(scheduler.tick(), [d.pk])

    def test_manual_retry_resets_counter(self):
        user = self.make_user()
        d = self.make_download("http://h/x", user=user, status="error", retry_count=7, retry_max=3)
        services.apply_action(user, "retry", [d.pk])
        d.refresh_from_db()
        self.assertEqual((d.status, d.retry_count), ("queued", 0))

    def test_create_with_retry_validates(self):
        user = self.make_user()
        ok, errs = services.create_downloads(user, ["http://example.com/a"], retry_max="always")
        self.assertEqual((len(ok), ok[0].retry_max, errs), (1, -1, []))
        bad, errs = services.create_downloads(user, ["http://example.com/b"], retry_max="sometimes")
        self.assertEqual((bad, len(errs)), ([], 1))
        self.assertIn("retry", errs[0]["error"])


class DeleteFilesTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.c = Client()
        self.c.force_login(self.user)

    def with_file(self, status="completed", name="f.bin"):
        d = self.make_download(f"http://h/{name}", user=self.user, status=status, filename=name)
        open(d.path, "wb").write(b"data")
        return d

    def act(self, action, ids, **kw):
        return self.c.post("/api/action/", json.dumps({"action": action, "ids": ids, **kw}), content_type="application/json").json()

    def test_remove_with_and_without_deleting_file(self):
        keep, gone = self.with_file(name="keep.bin"), self.with_file(name="gone.bin")
        r = self.act("remove", [keep.pk], delete_files=False)
        self.assertEqual((r["affected"], r["files_deleted"]), (1, 0))
        self.assertTrue(os.path.exists(keep.path))
        r = self.act("remove", [gone.pk], delete_files=True)
        self.assertEqual((r["affected"], r["files_deleted"]), (1, 1))
        self.assertFalse(os.path.exists(gone.path))

    def test_server_setting_is_the_default_and_request_overrides_it(self):
        conf.set("files.delete_on_remove", True)
        a, b = self.with_file(name="a.bin"), self.with_file(name="b.bin")
        self.act("remove", [a.pk])                                  # omitted -> setting -> deleted
        self.assertFalse(os.path.exists(a.path))
        self.act("remove", [b.pk], delete_files=False)              # explicit false wins
        self.assertTrue(os.path.exists(b.path))

    def test_cancel_queued_and_purge_delete_files(self):
        q, c = self.with_file(status="queued", name="q.bin"), self.with_file(name="c.bin")
        self.assertEqual(self.act("cancel", [q.pk], delete_files=True)["files_deleted"], 1)
        self.assertFalse(os.path.exists(q.path))
        r = self.act("purge", [], delete_files=True)
        self.assertEqual(r["affected"], 2)          # the canceled row and the completed one
        self.assertEqual(r["files_deleted"], 1)     # q.bin was already deleted by the cancel above
        self.assertFalse(os.path.exists(c.path))

    def test_running_download_reports_pending_deletion(self):
        d = self.with_file(status="running", name="r.bin")
        r = self.act("cancel", [d.pk], delete_files=True)
        self.assertEqual((r["files_deleted"], r["files_pending"]), (0, 1))

    def test_never_deletes_outside_allowed_directories(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        victim = os.path.join(outside, "precious.txt")
        open(victim, "w").write("keep me")
        d = Download.objects.create(owner=self.user, url="http://h/p", directory=outside, filename="precious.txt", status="completed")
        self.assertEqual(self.act("remove", [d.pk], delete_files=True)["files_deleted"], 0)
        self.assertTrue(os.path.exists(victim))

    def test_symlinks_cannot_be_used_to_escape(self):
        outside = os.path.join(self.tmp, "outside2")
        os.makedirs(outside)
        target = os.path.join(outside, "target.txt")
        open(target, "w").write("keep me")
        # (1) a link INSIDE the download dir pointing outside: only the link is removed
        link = os.path.join(self.dl_dir, "link.bin")
        os.symlink(target, link)
        a = self.make_download("http://h/l", user=self.user, status="completed", filename="link.bin")
        self.assertEqual(self.act("remove", [a.pk], delete_files=True)["files_deleted"], 1)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.exists(target))
        # (2) a directory inside the download dir that is a symlink to an outside directory
        os.symlink(outside, os.path.join(self.dl_dir, "escape"))
        b = Download.objects.create(owner=self.user, url="http://h/t", directory=os.path.join(self.dl_dir, "escape"), filename="target.txt", status="completed")
        self.assertEqual(self.act("remove", [b.pk], delete_files=True)["files_deleted"], 0)
        self.assertTrue(os.path.exists(target))

    def test_retention_cleanup_deletes_files_only_when_enabled(self):
        conf.set("history.retention_days", 7)
        old = timezone.now() - timedelta(days=10)
        a = self.with_file(name="a.bin"); Download.objects.filter(pk=a.pk).update(finished_at=old)
        self.assertEqual(tasks.maintenance(), 1)
        self.assertTrue(os.path.exists(a.path))                     # default: rows removed, files stay
        conf.set("files.delete_on_retention", True)
        b = self.with_file(name="b.bin"); Download.objects.filter(pk=b.pk).update(finished_at=old)
        self.assertEqual(tasks.maintenance(), 1)
        self.assertFalse(os.path.exists(b.path))

    def test_snapshot_exposes_default(self):
        self.assertFalse(self.c.get("/api/snapshot/").json()["delete_files_default"])
        conf.set("files.delete_on_remove", True)
        self.assertTrue(self.c.get("/api/snapshot/").json()["delete_files_default"])


class AdminV1Tests(PanelTestCase):
    def setUp(self):
        super().setUp()
        self.user, self.admin = self.make_user("bob"), self.make_user("root", staff=True)
        self.c = Client(enforce_csrf_checks=True)
        self.tu = self.login("bob")
        self.ta = self.login("root")

    def login(self, name):
        return self.c.post("/api/v1/auth/token", json.dumps({"username": name, "password": "pw-12345-xyz"}), content_type="application/json").json()["token"]

    def call(self, method, url, tok, data=None):
        kw = {"HTTP_AUTHORIZATION": f"Bearer {tok}"}
        if data is not None:
            kw.update(data=json.dumps(data), content_type="application/json")
        return getattr(self.c, method)(url, **kw)

    def test_staff_only(self):
        for method, url in (("get", "/api/v1/settings"), ("get", "/api/v1/rules"), ("post", "/api/v1/settings"), ("delete", "/api/v1/rules/1")):
            self.assertEqual(self.call(method, url, self.tu, {} if method == "post" else None).status_code, 403, url)
        self.assertEqual(self.call("get", "/api/v1/settings", self.ta).status_code, 200)

    def test_settings_schema_and_update(self):
        data = self.call("get", "/api/v1/settings", self.ta).json()
        keys = {s["key"] for s in data["schema"]}
        self.assertTrue({"retry.default_max", "files.delete_on_remove", "scheduler.max_downloads"} <= keys)
        spec = next(s for s in data["schema"] if s["key"] == "retry.default_max")
        self.assertEqual((spec["min"], spec["type"]), (-1, "int"))
        r = self.call("post", "/api/v1/settings", self.ta, {"retry.default_max": -1, "files.delete_on_remove": True, "scheduler.max_downloads": 4})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((conf.get("retry.default_max"), conf.get("files.delete_on_remove"), conf.get("scheduler.max_downloads")), (-1, True, 4))

    def test_settings_validation_reports_each_problem_and_saves_valid_keys(self):
        r = self.call("post", "/api/v1/settings", self.ta, {"scheduler.max_downloads": 9, "ui.theme": "neon", "retry.default_max": -5, "nope": 1})
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(set(body["errors"]), {"ui.theme", "retry.default_max"})
        self.assertEqual(body["unknown"], ["nope"])
        self.assertEqual(conf.get("scheduler.max_downloads"), 9)

    def test_rules_crud(self):
        r = self.call("post", "/api/v1/rules", self.ta, {"name": "Work", "start": "09:00", "end": "17:00", "days": [0, 1, 2], "limit_kbps": 512})
        self.assertEqual(r.status_code, 201)
        rule = r.json()["rule"]
        self.assertEqual((rule["days"], rule["limit_kbps"]), ("0,1,2", 512))
        r = self.call("patch", f"/api/v1/rules/{rule['id']}", self.ta, {"limit_kbps": 128, "enabled": False})
        self.assertEqual((r.json()["rule"]["limit_kbps"], r.json()["rule"]["enabled"], r.json()["rule"]["name"]), (128, False, "Work"))
        self.assertEqual(self.call("post", "/api/v1/rules", self.ta, {"start": "99:99"}).status_code, 400)
        self.assertEqual(self.call("patch", f"/api/v1/rules/{rule['id']}", self.ta, {"end": "nope"}).status_code, 400)
        self.assertEqual(len(self.call("get", "/api/v1/rules", self.ta).json()["rules"]), 1)
        self.assertEqual(self.call("delete", f"/api/v1/rules/{rule['id']}", self.ta).status_code, 200)
        self.assertEqual(self.call("delete", f"/api/v1/rules/{rule['id']}", self.ta).status_code, 404)
        self.assertEqual(SpeedRule.objects.count(), 0)

    def test_v1_add_accepts_retry_and_reports_it(self):
        r = self.call("post", "/api/v1/downloads", self.tu, {"url": "http://example.com/a.iso", "retry": "always"})
        self.assertEqual(r.status_code, 201)
        row = self.call("get", f"/api/v1/downloads/{r.json()['created'][0]}", self.tu).json()["download"]
        self.assertEqual((row["retry_max"], row["retry_limit"], row["retry_count"]), (-1, -1, 0))
        bad = self.call("post", "/api/v1/downloads", self.tu, {"url": "http://example.com/b.iso", "retry": "x"})
        self.assertEqual(bad.status_code, 400)

    def test_v1_remove_reports_files_deleted(self):
        d = self.make_download("http://h/f.bin", user=self.user, status="completed", filename="f.bin")
        open(d.path, "wb").write(b"x")
        r = self.call("post", "/api/v1/downloads/actions", self.tu, {"action": "remove", "ids": [d.pk], "delete_files": True})
        self.assertEqual((r.json()["affected"], r.json()["files_deleted"]), (1, 1))
        self.assertFalse(os.path.exists(d.path))


class OddUrlTests(PanelTestCase):
    NAME = "Shaun.The.Sheep.The.Beast.Of.Mossy.Bottom.2026.720p.WEBRip.x264.AAC-[YTS.GG - YTS.BZ].mp4"

    def test_url_with_spaces_and_signed_query_downloads_end_to_end(self):
        blob = make_blob(200_000)
        with FileServer({"/ff_get/" + self.NAME: blob}) as srv:
            user = self.make_user()
            raw = srv.url("/ff_get/" + self.NAME) + "?st=YnkyCld1SIGv_qUyifhp7g&e=1792835346"
            created, errors = services.create_downloads(user, [raw])
            self.assertEqual(errors, [])
            d = created[0]
            self.assertEqual(d.filename, self.NAME)
            self.assertIn("%20", d.url)
            self.assertNotIn(" ", d.url)
            Download.objects.filter(pk=d.pk).update(status="running")
            runner.run(d.pk)
        d.refresh_from_db()
        self.assertEqual(d.status, "completed", d.error)
        self.assertEqual(open(d.path, "rb").read(), blob)

    def test_api_accepts_the_url_from_the_bug_report(self):
        user = self.make_user()
        c = Client()
        c.force_login(user)
        raw = "https://rd20.seedr.cc/ff_get/87348/6012415599/" + self.NAME + "?st=YnkyCld1SIGv_qUyifhp7g&e=1792835346"
        with self.settings(WGP_BLOCK_PRIVATE_ADDRESSES=False):
            conf.invalidate()
            r = c.post("/api/add/", json.dumps({"urls": raw}), content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Download.objects.get().filename, self.NAME)
