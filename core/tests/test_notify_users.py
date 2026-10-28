"""Per-user notifications: private ntfy topics, optional GNTP, routing isolation, API, web page."""
import json
import time

from django.test import Client, override_settings

from core import conf, notify, notify_prefs, runner, services, tasks
from core.models import Download, UserNotify
from core.tests.helpers import FileServer, PanelTestCase, make_blob
from core.tests.test_notify import GntpMock, NtfyMock, run_notifications_inline


class UserNotifyBase(PanelTestCase):
    def setUp(self):
        super().setUp()
        self.sent_payloads = []
        run_notifications_inline(self)

        def record(payload, only=None):  # run inline, but remember what would have gone through the broker
            self.sent_payloads.append(json.loads(json.dumps(payload)))
            return tasks.send_notification.apply(args=(payload, only))
        tasks.send_notification.delay.side_effect = record
        self.ntfy = NtfyMock()
        self.addCleanup(self.ntfy.close)
        self._ov_notify = override_settings(NTFY_URL=self.ntfy.url, NTFY_TOPIC="admin-topic", NTFY_TOKEN="tk_server_secret", NTFY_USER="", GNTP_HOST="",
                                     NOTIFY_EVENTS=["completed", "error", "paused", "retry"], NOTIFY_RETRY_INTERVAL=60, NOTIFY_PANEL_URL="",
                                     NOTIFY_GLOBAL_SCOPE="all", NOTIFY_USER_GNTP_ALLOW=[], NTFY_USER_TOPIC_PREFIX="wgetpanel", NOTIFY_TIMEOUT=5)
        self._ov_notify.enable()
        self.alice, self.bob = self.make_user("alice"), self.make_user("bob")
        self.root = self.make_user("root", staff=True)

    def tearDown(self):
        self._ov_notify.disable()
        super().tearDown()

    def enable_ntfy(self, user):
        return notify_prefs.update(user, {"ntfy_enabled": True})["ntfy"]["topic"]

    def topics(self):
        return [m["json"]["topic"] for m in self.ntfy.messages]

    def done(self, user, name="x.bin", status="completed", **kw):
        return self.make_download(f"http://h/{name}", user=user, status=status, filename=name, **kw)


class PrefsTests(UserNotifyBase):
    def test_defaults_and_availability(self):
        d = notify_prefs.to_dict(self.alice)
        self.assertEqual((d["enabled"], d["ntfy"]["available"], d["ntfy"]["enabled"], d["ntfy"]["topic"]), (True, True, False, ""))
        self.assertEqual((d["gntp"]["available"], d["events"], d["retry_interval"], d["active"]), (False, None, None, False))
        self.assertEqual(d["defaults"], {"events": ["completed", "error", "paused", "retry"], "retry_interval": 60})
        with self.settings(NTFY_URL=""):
            self.assertFalse(notify_prefs.to_dict(self.alice)["ntfy"]["available"])

    def test_topic_is_generated_unguessable_unique_and_rotatable(self):
        ta, tb = self.enable_ntfy(self.alice), self.enable_ntfy(self.bob)
        for t in (ta, tb):
            self.assertRegex(t, r"^wgetpanel-[A-Za-z0-9_-]{16}$")
        self.assertNotEqual(ta, tb)
        d = notify_prefs.to_dict(self.alice)
        self.assertEqual(d["ntfy"]["subscribe_url"], f"{self.ntfy.url}/{ta}")
        self.assertEqual(notify_prefs.update(self.alice, {"ntfy_enabled": True})["ntfy"]["topic"], ta)   # enabling again keeps it
        rotated = notify_prefs.update(self.alice, {"rotate_topic": True})["ntfy"]["topic"]
        self.assertNotEqual(rotated, ta)
        self.assertNotEqual(self.enable_ntfy(self.make_user("carol")), rotated)

    def test_ntfy_needs_a_server(self):
        with self.settings(NTFY_URL=""):
            with self.assertRaises(ValueError) as cm:
                notify_prefs.update(self.alice, {"ntfy_enabled": True})
            self.assertIn("NTFY_URL", str(cm.exception))
        self.assertFalse(UserNotify.objects.get(user=self.alice).ntfy_enabled)

    def test_validation_is_all_or_nothing(self):
        notify_prefs.update(self.alice, {"retry_interval": 30})
        for bad in ({"nope": 1}, {"enabled": "yes"}, {"events": ["completed", "bogus"]}, {"retry_interval": 999999}, {"retry_interval": "x"},
                    {"events": ["x"], "retry_interval": 5}, {"gntp_port": 0}):
            with self.assertRaises(ValueError, msg=bad):
                notify_prefs.update(self.alice, {**bad, **({"enabled": False} if "enabled" not in bad else {})})
        p = UserNotify.objects.get(user=self.alice)
        self.assertEqual((p.enabled, p.retry_interval), (True, 30))                                           # nothing half-applied

    def test_events_and_interval_normalisation(self):
        d = notify_prefs.update(self.alice, {"events": ["success", "FAILED", "pause", "success"], "retry_interval": "0"})
        self.assertEqual((d["events"], d["retry_interval"]), (["completed", "error", "paused"], 0))
        d = notify_prefs.update(self.alice, {"events": "default", "retry_interval": None})
        self.assertEqual((d["events"], d["retry_interval"]), (None, None))
        self.assertEqual(notify_prefs.update(self.alice, {"events": "completed,error"})["events"], ["completed", "error"])


class RoutingTests(UserNotifyBase):
    def test_each_user_gets_only_their_own_events_on_their_own_topic(self):
        ta, tb = self.enable_ntfy(self.alice), self.enable_ntfy(self.bob)
        a, b = self.done(self.alice, "alice.bin"), self.done(self.bob, "bob.bin")
        notify.dispatch("completed", a.pk)
        self.assertEqual(sorted(self.topics()), sorted(["admin-topic", ta]))
        notify.dispatch("completed", b.pk)
        by_topic = {}
        for m in self.ntfy.messages:
            by_topic.setdefault(m["json"]["topic"], []).append(m["json"]["title"])
        self.assertEqual(by_topic[ta], ["Download complete: alice.bin"])                   # nothing of bob's on alice's topic
        self.assertEqual(by_topic[tb], ["Download complete: bob.bin"])
        self.assertEqual(sorted(by_topic["admin-topic"]), ["Download complete: alice.bin", "Download complete: bob.bin"])
        self.assertTrue(all(m["auth"] == "Bearer tk_server_secret" for m in self.ntfy.messages))      # published with the SERVER's credentials

    def test_owner_name_only_on_the_global_channel(self):
        ta = self.enable_ntfy(self.alice)
        notify.dispatch("completed", self.done(self.alice).pk)
        msgs = {m["json"]["topic"]: m["json"]["message"] for m in self.ntfy.messages}
        self.assertIn("by alice", msgs["admin-topic"])
        self.assertNotIn("by alice", msgs[ta])

    def test_global_scope_none_keeps_other_users_out_of_the_admin_channel(self):
        ta = self.enable_ntfy(self.alice)
        with self.settings(NOTIFY_GLOBAL_SCOPE="none"):
            notify.dispatch("completed", self.done(self.alice, "a.bin").pk)
            notify.dispatch("completed", self.done(self.bob, "b.bin").pk)            # bob has no target of his own
        self.assertEqual(self.topics(), [ta])

    def test_user_without_targets_or_with_master_switch_off_gets_nothing(self):
        ta = self.enable_ntfy(self.alice)
        with self.settings(NOTIFY_GLOBAL_SCOPE="none"):
            notify_prefs.update(self.alice, {"enabled": False})
            self.assertFalse(notify.dispatch("completed", self.done(self.alice).pk))
            notify_prefs.update(self.alice, {"enabled": True, "ntfy_enabled": False})
            self.assertFalse(notify.dispatch("completed", self.done(self.alice, "y.bin").pk))
        self.assertEqual(self.ntfy.messages, [])

    def test_per_user_events(self):
        ta = self.enable_ntfy(self.alice)
        notify_prefs.update(self.alice, {"events": ["error"]})
        with self.settings(NOTIFY_GLOBAL_SCOPE="none"):
            notify.dispatch("completed", self.done(self.alice, "ok.bin").pk)
            notify.dispatch("error", self.done(self.alice, "bad.bin", status="error", error="Network failure (exit 4)").pk)
        self.assertEqual([m["json"]["title"] for m in self.ntfy.messages], ["Download failed: bad.bin"])

    def test_retry_interval_is_per_target_global_stays_throttled(self):
        ta = self.enable_ntfy(self.alice)
        notify_prefs.update(self.alice, {"retry_interval": 0})                            # alice wants every retry, the admin channel 60 s
        d = self.done(self.alice, "r.bin", status="queued")
        for n in (1, 2, 3):
            notify.dispatch("retry", d.pk, reason="Network failure (exit 4)", delay=3, count=n, limit=-1)
        self.assertEqual(self.topics().count(ta), 3)
        self.assertEqual(self.topics().count("admin-topic"), 1)

    def test_user_retry_interval_never_and_default(self):
        ta = self.enable_ntfy(self.alice)
        d = self.done(self.alice, "r.bin", status="queued")
        notify_prefs.update(self.alice, {"retry_interval": -1})
        with self.settings(NOTIFY_GLOBAL_SCOPE="none"):
            notify.dispatch("retry", d.pk, reason="x", delay=1, count=1, limit=3)
            self.assertEqual(self.ntfy.messages, [])
            notify_prefs.update(self.alice, {"retry_interval": None})                      # back to the server default (60 s)
            notify.dispatch("retry", d.pk, reason="x", delay=1, count=2, limit=3)
            notify.dispatch("retry", d.pk, reason="x", delay=1, count=3, limit=3)
        self.assertEqual(self.topics(), [ta])

    def test_final_event_resets_each_targets_throttle_window(self):
        ta = self.enable_ntfy(self.alice)
        d = self.done(self.alice, "r.bin", status="queued")
        notify.dispatch("retry", d.pk, reason="x", delay=1, count=1, limit=3)
        notify.dispatch("retry", d.pk, reason="x", delay=1, count=2, limit=3)
        self.assertEqual(len(self.ntfy.messages), 2)                                       # one per target, second retry suppressed on both
        Download.objects.filter(pk=d.pk).update(status="completed")
        notify.dispatch("completed", d.pk)
        notify.dispatch("retry", d.pk, reason="x", delay=1, count=1, limit=3)
        self.assertEqual(len(self.ntfy.messages), 2 + 2 + 2)

    def test_real_download_goes_to_the_owners_topic(self):
        ta = self.enable_ntfy(self.alice)
        with FileServer({"/r.bin": make_blob(300_000)}) as srv:
            d = self.make_download(srv.url("/r.bin"), user=self.alice, filename="r.bin")
            runner.run(d.pk)
        self.assertEqual(sorted(self.topics()), sorted(["admin-topic", ta]))

    def test_pause_by_the_user_notifies_only_that_user(self):
        ta, tb = self.enable_ntfy(self.alice), self.enable_ntfy(self.bob)
        d = self.done(self.alice, "p.bin", status="queued")
        services.apply_action(self.alice, "pause", [d.pk])
        self.assertEqual(sorted(self.topics()), sorted(["admin-topic", ta]))
        self.assertNotIn(tb, self.topics())

    def test_no_secret_ever_enters_the_broker_payload(self):
        ta = self.enable_ntfy(self.alice)
        notify.dispatch("completed", self.done(self.alice).pk)
        raw = json.dumps(self.sent_payloads)
        self.assertEqual({p["target"] for p in self.sent_payloads}, {"g", f"u{self.alice.pk}"})
        for secret in ("tk_server_secret", ta, "password"):
            self.assertNotIn(secret, raw)

    def test_disabling_after_queueing_drops_the_message_instead_of_sending(self):
        ta = self.enable_ntfy(self.alice)
        msg = notify.build_message("completed", self.done(self.alice), show_owner=False)
        msg["target"] = f"u{self.alice.pk}"
        notify_prefs.update(self.alice, {"ntfy_enabled": False})
        self.assertEqual(notify.deliver(msg), {})
        self.assertEqual(self.ntfy.messages, [])


class UserGntpTests(UserNotifyBase):
    def setUp(self):
        super().setUp()
        self.gntp = GntpMock()
        self.addCleanup(self.gntp.close)
        self._ov2 = override_settings(NOTIFY_USER_GNTP_ALLOW=["127.0.0.0/8"], NOTIFY_GLOBAL_SCOPE="none")
        self._ov2.enable()

    def tearDown(self):
        self._ov2.disable()
        super().tearDown()

    def enable(self, user, **extra):
        return notify_prefs.update(user, {"gntp_host": "127.0.0.1", "gntp_port": self.gntp.port, "gntp_enabled": True, **extra})

    def notifies(self):
        return [p for p in self.gntp.packets if p.startswith("GNTP/1.0 NOTIFY")]

    def test_disabled_unless_admin_lists_networks(self):
        with self.settings(NOTIFY_USER_GNTP_ALLOW=[]):
            self.assertFalse(notify_prefs.to_dict(self.alice)["gntp"]["available"])
            with self.assertRaises(ValueError) as cm:
                notify_prefs.update(self.alice, {"gntp_host": "127.0.0.1"})
            self.assertIn("NOTIFY_USER_GNTP_ALLOW", str(cm.exception))
        self.assertTrue(notify_prefs.to_dict(self.alice)["gntp"]["available"])

    def test_host_must_resolve_inside_the_allowed_networks(self):
        for bad in ("8.8.8.8", "169.254.169.254", "10.0.0.1", "no-such-host.invalid"):
            with self.assertRaises(ValueError, msg=bad):
                notify_prefs.update(self.alice, {"gntp_host": bad})
        self.assertEqual(notify_prefs.update(self.alice, {"gntp_host": "127.0.0.5"})["gntp"]["host"], "127.0.0.5")
        with self.assertRaises(ValueError):
            notify_prefs.update(self.alice, {"gntp_enabled": True, "gntp_host": ""})          # enabling needs a host

    def test_delivery_to_the_users_receiver_with_hashed_password(self):
        self.enable(self.alice, gntp_password="s3cret-pw")
        p = UserNotify.objects.get(user=self.alice)
        self.assertNotIn("s3cret-pw", p.gntp_password_enc)                                  # encrypted at rest
        self.assertEqual(p.gntp_password, "s3cret-pw")
        self.assertTrue(notify_prefs.to_dict(self.alice)["gntp"]["has_password"])
        self.assertNotIn("s3cret-pw", json.dumps(notify_prefs.to_dict(self.alice)))          # never returned by the API
        notify.dispatch("completed", self.done(self.alice, "g.bin").pk)
        n = self.notifies()[0]
        self.assertIn("Notification-Title: Download complete: g.bin", n)
        self.assertTrue(any(h in n.splitlines()[0] for h in ("MD5:", "SHA1:", "SHA256:", "SHA512:")))
        self.assertTrue(all("s3cret-pw" not in x for x in self.gntp.packets))
        self.assertNotIn("s3cret-pw", json.dumps(self.sent_payloads))

    def test_other_users_events_do_not_reach_it(self):
        self.enable(self.alice)
        notify.dispatch("completed", self.done(self.bob, "b.bin").pk)
        self.assertEqual(self.gntp.packets, [])

    def test_rechecked_at_send_time_when_the_admin_narrows_the_allow_list(self):
        self.enable(self.alice)
        with self.settings(NOTIFY_USER_GNTP_ALLOW=["192.168.99.0/24"]):
            self.assertFalse(notify.dispatch("completed", self.done(self.alice, "c.bin").pk))
        self.assertEqual(self.gntp.packets, [])                                              # no connection was even attempted
        self.assertTrue(notify.dispatch("completed", self.done(self.alice, "d.bin").pk))

    def test_clear_password_and_port_validation(self):
        self.enable(self.alice, gntp_password="x")
        self.assertFalse(notify_prefs.update(self.alice, {"clear_gntp_password": True})["gntp"]["has_password"])
        for port in (0, 70000, "abc"):
            with self.assertRaises(ValueError):
                notify_prefs.update(self.alice, {"gntp_port": port})


class ApiTests(UserNotifyBase):
    def setUp(self):
        super().setUp()
        self.c = Client(enforce_csrf_checks=True)

    def tok(self, name):
        return self.c.post("/api/v1/auth/token", json.dumps({"username": name, "password": "pw-12345-xyz"}), content_type="application/json").json()["token"]

    def call(self, method, url, tok, data=None):
        kw = {"HTTP_AUTHORIZATION": f"Bearer {tok}"}
        if data is not None:
            kw.update(data=json.dumps(data), content_type="application/json")
        return getattr(self.c, method)(url, **kw)

    def test_v1_get_update_and_isolation(self):
        ta, tb = self.tok("alice"), self.tok("bob")
        self.assertEqual(self.c.get("/api/v1/notify").status_code, 401)
        r = self.call("post", "/api/v1/notify", ta, {"ntfy_enabled": True, "events": ["success"], "retry_interval": 120})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ntfy"]["enabled"] and d["active"])
        self.assertEqual((d["events"], d["retry_interval"]), (["completed"], 120))
        self.assertEqual(self.call("get", "/api/v1/notify", ta).json()["ntfy"]["topic"], d["ntfy"]["topic"])
        other = self.call("get", "/api/v1/notify", tb).json()
        self.assertEqual((other["ntfy"]["enabled"], other["ntfy"]["topic"], other["events"]), (False, "", None))   # bob sees nothing of alice's

    def test_v1_errors(self):
        ta = self.tok("alice")
        for body in ({"bogus": 1}, {"events": ["bogus"]}, {"retry_interval": -5}, {"enabled": "yes"}, {"gntp_host": "127.0.0.1"}):
            r = self.call("post", "/api/v1/notify", ta, body)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_notify_settings"), body)
        self.assertEqual(self.c.post("/api/v1/notify", "x", content_type="text/plain", HTTP_AUTHORIZATION=f"Bearer {ta}").status_code, 400)

    def test_v1_test_endpoint_me_and_global(self):
        ta, tr = self.tok("alice"), self.tok("root")
        r = self.call("post", "/api/v1/notify/test", ta, {})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "no_target"))
        self.call("post", "/api/v1/notify", ta, {"ntfy_enabled": True})
        r = self.call("post", "/api/v1/notify/test", ta, {})
        self.assertEqual((r.status_code, r.json()["results"], r.json()["target"]), (200, {"ntfy": "ok"}, "me"))
        mine = self.call("get", "/api/v1/notify", ta).json()["ntfy"]["topic"]
        self.assertEqual(self.topics(), [mine])                                              # the test went to HER topic only
        self.assertEqual(self.call("post", "/api/v1/notify/test", ta, {"target": "global"}).status_code, 403)
        self.assertEqual(self.call("post", "/api/v1/notify/test", tr, {"target": "global"}).json()["results"], {"ntfy": "ok"})
        self.assertEqual(self.call("post", "/api/v1/notify/test", ta, {"target": "elsewhere"}).status_code, 400)

    def test_web_page_and_session_endpoints(self):
        anon = Client()
        self.assertEqual(anon.get("/notifications/").status_code, 302)
        self.assertEqual(anon.get("/api/notify/").status_code, 401)
        web = Client(enforce_csrf_checks=True)
        web.force_login(self.alice)
        self.assertEqual(web.get("/notifications/").status_code, 200)
        self.assertEqual(web.post("/api/notify/", json.dumps({"ntfy_enabled": True}), content_type="application/json").status_code, 403)   # CSRF
        csrf = Client()
        csrf.force_login(self.alice)
        r = csrf.post("/api/notify/", json.dumps({"ntfy_enabled": True, "retry_interval": 90}), content_type="application/json")
        self.assertEqual((r.status_code, r.json()["retry_interval"]), (200, 90))
        r = csrf.post("/api/notify/", json.dumps({"events": ["nope"]}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("unknown event", r.json()["error"])
        r = csrf.post("/api/notify/test/", json.dumps({"target": "me"}), content_type="application/json")
        self.assertEqual((r.status_code, r.json()["results"]), (200, {"ntfy": "ok"}))
        r = csrf.post("/api/notify/test/", json.dumps({"target": "global"}), content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_deleting_a_user_removes_the_settings(self):
        self.enable_ntfy(self.bob)
        self.bob.delete()
        self.assertFalse(UserNotify.objects.filter(user_id=self.bob.pk).exists())
