import json

from django.test import Client

from core import conf, live, services
from core.models import Download, SpeedRule
from core.tests.helpers import PanelTestCase


class ApiTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        self.alice, self.bob = self.make_user("alice"), self.make_user("bob")
        self.admin = self.make_user("admin", staff=True)
        self.ca, self.cb, self.cadmin = Client(), Client(), Client()
        self.ca.force_login(self.alice); self.cb.force_login(self.bob); self.cadmin.force_login(self.admin)

    def post(self, c, url, data):
        return c.post(url, json.dumps(data), content_type="application/json")

    def test_anonymous_is_rejected(self):
        c = Client()
        self.assertEqual(c.get("/api/snapshot/").status_code, 401)
        self.assertEqual(self.post(c, "/api/add/", {"urls": ["http://x/y"]}).status_code, 401)
        self.assertEqual(c.get("/").status_code, 302)

    def test_csrf_enforced(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.alice)
        r = c.post("/api/add/", json.dumps({"urls": ["http://example.com/a"]}), content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_add_validates_and_reports_per_line(self):
        r = self.post(self.ca, "/api/add/", {"urls": "http://example.com/ok.iso\nfile:///etc/passwd\nhttp://u:p@example.com/x"})
        body = r.json()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(body["created"]), 1)
        self.assertEqual({e["line"] for e in body["errors"]}, {2, 3})

    def test_add_all_invalid_is_400(self):
        r = self.post(self.ca, "/api/add/", {"urls": ["ftp:/bad"]})
        self.assertEqual(r.status_code, 400)

    def test_ssrf_guard_on_by_default_setting(self):
        with self.settings(WGP_BLOCK_PRIVATE_ADDRESSES=True):
            conf.invalidate()
            r = self.post(self.ca, "/api/add/", {"urls": ["http://127.0.0.1:8000/admin", "http://169.254.169.254/latest/meta-data/"]})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(Download.objects.count(), 0)

    def test_directory_traversal_rejected(self):
        r = self.post(self.ca, "/api/add/", {"urls": ["http://example.com/a"], "directory": "../../etc"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("outside", r.json()["errors"][0]["error"])

    def test_filename_uniqueness_for_same_target(self):
        for _ in range(3):
            self.post(self.ca, "/api/add/", {"urls": ["http://example.com/same.iso"]})
        names = sorted(Download.objects.values_list("filename", flat=True))
        self.assertEqual(names, ["same (1).iso", "same (2).iso", "same.iso"])

    def test_filename_only_with_single_url(self):
        r = self.post(self.ca, "/api/add/", {"urls": ["http://example.com/a", "http://example.com/b"], "filename": "x.bin"})
        self.assertEqual(r.status_code, 400)
        r = self.post(self.ca, "/api/add/", {"urls": ["http://example.com/a"], "filename": "../x.bin"})
        self.assertEqual(Download.objects.get().filename, "x.bin")

    def test_queue_cap_per_user(self):
        conf.set("limits.max_queue_per_user", 2)
        r = self.post(self.ca, "/api/add/", {"urls": ["http://example.com/1", "http://example.com/2", "http://example.com/3"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Queue limit", r.json()["errors"][0]["error"])

    def test_password_not_in_api_and_encrypted(self):
        self.post(self.ca, "/api/add/", {"urls": ["http://example.com/p"], "http_user": "u", "http_password": "topsecret"})
        d = Download.objects.get()
        self.assertNotIn("topsecret", d.http_password_enc)
        self.assertEqual(d.http_password, "topsecret")
        snap = self.ca.get("/api/snapshot/").content.decode()
        self.assertNotIn("topsecret", snap)
        self.assertNotIn("topsecret", self.ca.get(f"/api/download/{d.pk}/").content.decode())

    def test_users_are_isolated_staff_sees_all(self):
        a = self.make_download("http://h/a", user=self.alice, status="queued")
        b = self.make_download("http://h/b", user=self.bob, status="queued")
        ids = lambda c: {x["id"] for x in c.get("/api/snapshot/").json()["downloads"]}
        self.assertEqual(ids(self.ca), {a.pk})
        self.assertEqual(ids(self.cb), {b.pk})
        self.assertEqual(ids(self.cadmin), {a.pk, b.pk})
        self.assertEqual(self.cb.get(f"/api/download/{a.pk}/").status_code, 404)
        r = self.post(self.cb, "/api/action/", {"action": "remove", "ids": [a.pk]})
        self.assertEqual(r.json()["affected"], 0)
        self.assertTrue(Download.objects.filter(pk=a.pk).exists())

    def test_actions_state_machine(self):
        d = self.make_download("http://h/a", user=self.alice, status="queued")
        act = lambda a, **kw: self.post(self.ca, "/api/action/", {"action": a, "ids": [d.pk], **kw}).json()["affected"]
        self.assertEqual(act("pause"), 1); d.refresh_from_db(); self.assertEqual(d.status, "paused")
        self.assertEqual(act("pause"), 0)
        self.assertEqual(act("resume"), 1); d.refresh_from_db(); self.assertEqual(d.status, "queued")
        self.assertEqual(act("cancel"), 1); d.refresh_from_db(); self.assertEqual(d.status, "canceled")
        self.assertEqual(act("retry"), 1); d.refresh_from_db(); self.assertEqual(d.status, "queued")
        self.assertEqual(act("remove"), 1); self.assertFalse(Download.objects.filter(pk=d.pk).exists())

    def test_running_pause_sends_control_message(self):
        d = self.make_download("http://h/a", user=self.alice, status="running")
        self.post(self.ca, "/api/action/", {"action": "pause", "ids": [d.pk]})
        self.assertEqual(live.pop_ctl(d.pk), "pause")

    def test_unknown_action_and_bad_ids(self):
        self.assertEqual(self.post(self.ca, "/api/action/", {"action": "rm -rf", "ids": [1]}).status_code, 400)
        self.assertEqual(self.post(self.ca, "/api/action/", {"action": "pause", "ids": ["x"]}).status_code, 400)

    def test_purge_only_own_finished(self):
        mine = self.make_download("http://h/m", user=self.alice, status="completed")
        other = self.make_download("http://h/o", user=self.bob, status="completed")
        run = self.make_download("http://h/r", user=self.alice, status="running")
        self.post(self.ca, "/api/action/", {"action": "purge"})
        self.assertEqual(set(Download.objects.values_list("pk", flat=True)), {other.pk, run.pk})

    def test_snapshot_merges_live_values(self):
        d = self.make_download("http://h/a", user=self.alice, status="running")
        live.set_live(d.pk, 500, 1000, 250, 99)
        row = self.ca.get("/api/snapshot/").json()["downloads"][0]
        self.assertEqual((row["downloaded"], row["total"], row["speed"], row["progress"], row["eta"]), (500, 1000, 250, 50.0, 2))

    def test_settings_and_rules_staff_only(self):
        for url in ("/api/settings/", "/api/rules/", "/settings/", "/rules/"):
            self.assertEqual(self.ca.get(url).status_code, 403, url)
            self.assertEqual(self.cadmin.get(url).status_code, 200, url)
        self.assertEqual(self.post(self.ca, "/api/settings/", {"scheduler.max_downloads": 99}).status_code, 403)

    def test_settings_validation(self):
        r = self.post(self.cadmin, "/api/settings/", {"scheduler.max_downloads": 7, "ui.theme": "neon"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ui.theme", r.json()["errors"])
        self.assertEqual(conf.get("scheduler.max_downloads"), 7)  # valid keys are still applied
        r = self.post(self.cadmin, "/api/settings/", {"scheduler.max_downloads": -1})
        self.assertEqual(r.status_code, 400)

    def test_rules_crud(self):
        r = self.post(self.cadmin, "/api/rules/", {"name": "Work", "start": "09:00", "end": "17:00", "days": "0,1,2", "limit_kbps": 512})
        rule = r.json()["rules"][0]
        self.assertEqual((rule["name"], rule["limit_kbps"], rule["days"]), ("Work", 512, "0,1,2"))
        self.post(self.cadmin, "/api/rules/", {"id": rule["id"], "name": "Work", "start": "08:00", "end": "17:00", "limit_kbps": 100})
        self.assertEqual(SpeedRule.objects.get().limit_kbps, 100)
        self.assertEqual(self.post(self.cadmin, "/api/rules/", {"name": "x", "start": "99:99"}).status_code, 400)
        self.post(self.cadmin, "/api/rules/", {"delete": rule["id"]})
        self.assertEqual(SpeedRule.objects.count(), 0)

    def test_file_download_only_completed_and_owner(self):
        d = self.make_download("http://h/f.txt", user=self.alice, status="completed", filename="f.txt")
        open(d.path, "w").write("hello")
        self.assertEqual(b"".join(self.ca.get(f"/api/download/{d.pk}/file/").streaming_content), b"hello")
        self.assertEqual(self.cb.get(f"/api/download/{d.pk}/file/").status_code, 404)
        Download.objects.filter(pk=d.pk).update(status="running")
        self.assertEqual(self.ca.get(f"/api/download/{d.pk}/file/").status_code, 404)

    def test_healthz(self):
        r = Client().get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"database": "ok", "redis": "ok"})

    def test_pages_render(self):
        for url in ("/", ):
            self.assertEqual(self.ca.get(url).status_code, 200)
