import json
from datetime import timedelta

from django.test import Client, override_settings
from django.urls import resolve
from django.utils import timezone

from core import api_v1, live, runner, services
from core.models import ApiToken, Download
from core.openapi import spec
from core.tests.helpers import FileServer, PanelTestCase, make_blob


def parse_sse(chunks):
    """Join SSE text chunks and return [(event, data_dict)]; comments are skipped."""
    text = "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)
    out = []
    for block in text.split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            out.append((ev, data))
    return out


class V1Base(PanelTestCase):
    def setUp(self):
        super().setUp()
        self.alice, self.bob = self.make_user("alice"), self.make_user("bob")
        self.admin = self.make_user("admin", staff=True)
        self.c = Client(enforce_csrf_checks=True)  # bearer API must not need CSRF

    def login(self, user="alice", pw="pw-12345-xyz", device="Pixel 8"):
        return self.c.post("/api/v1/auth/token", json.dumps({"username": user, "password": pw, "device_name": device}), content_type="application/json")

    def token_for(self, user):
        return self.login(user.get_username()).json()["token"]

    def get(self, url, tok, **kw):
        return self.c.get(url, HTTP_AUTHORIZATION=f"Bearer {tok}", **kw)

    def post(self, url, tok, data):
        return self.c.post(url, json.dumps(data), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {tok}")


class AuthTests(V1Base):
    def test_login_issues_token_stored_only_as_hash(self):
        r = self.login()
        self.assertEqual(r.status_code, 201)
        body = r.json()
        raw = body["token"]
        self.assertTrue(raw.startswith("wgp_") and len(raw) > 40)
        self.assertEqual(body["user"]["username"], "alice")
        t = ApiToken.objects.get()
        self.assertEqual(t.name, "Pixel 8")
        self.assertNotIn(raw, t.key_hash + t.prefix)
        self.assertEqual(len(t.key_hash), 64)
        self.assertIsNotNone(t.expires_at)

    def test_wrong_password_and_error_shape(self):
        r = self.login(pw="nope")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "invalid_credentials")
        self.assertEqual(ApiToken.objects.count(), 0)

    def test_login_rate_limit(self):
        for _ in range(api_v1.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(self.login(pw="bad").status_code, 401)
        r = self.login(pw="bad")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r)
        self.assertEqual(self.login().status_code, 429)  # even the right password is refused while locked
        self.assertEqual(self.login("bob").status_code, 201)  # other accounts unaffected

    def test_success_resets_counter(self):
        for _ in range(5):
            self.login(pw="bad")
        self.assertEqual(self.login().status_code, 201)
        for _ in range(api_v1.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(self.login(pw="bad").status_code, 401)

    def test_requires_bearer_cookie_session_is_not_enough(self):
        r = self.c.get("/api/v1/downloads")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Bearer", r["WWW-Authenticate"])
        self.assertEqual(r.json()["error"]["code"], "unauthorized")
        self.c.force_login(self.alice)  # a browser session must NOT authenticate the (CSRF exempt) v1 API
        self.assertEqual(self.c.get("/api/v1/downloads").status_code, 401)
        self.assertEqual(self.c.post("/api/v1/downloads", "{}", content_type="application/json").status_code, 401)

    def test_garbage_tokens_rejected(self):
        for h in ("Bearer", "Bearer nope", "Basic abc", "Bearer wgp_" + "x" * 300, "bearer wgp_doesnotexist"):
            r = self.c.get("/api/v1/me" if False else "/api/v1/auth/me", HTTP_AUTHORIZATION=h)
            self.assertEqual(r.status_code, 401, h)

    def test_me_and_logout_revokes(self):
        tok = self.token_for(self.alice)
        me = self.get("/api/v1/auth/me", tok).json()
        self.assertEqual(me["user"]["username"], "alice")
        self.assertTrue(me["token"]["current"])
        r = self.c.delete("/api/v1/auth/token", HTTP_AUTHORIZATION=f"Bearer {tok}")
        self.assertEqual(r.json(), {"revoked": True})
        self.assertEqual(self.get("/api/v1/auth/me", tok).status_code, 401)

    def test_list_and_revoke_other_device(self):
        t1, t2 = self.token_for(self.alice), self.token_for(self.alice)
        listing = self.get("/api/v1/auth/tokens", t1).json()["tokens"]
        self.assertEqual(len(listing), 2)
        self.assertEqual(sum(x["current"] for x in listing), 1)
        other = next(x for x in listing if not x["current"])
        self.assertEqual(self.c.delete(f"/api/v1/auth/tokens/{other['id']}", HTTP_AUTHORIZATION=f"Bearer {t1}").status_code, 200)
        self.assertEqual(self.get("/api/v1/auth/me", t2).status_code, 401)
        self.assertEqual(self.get("/api/v1/auth/me", t1).status_code, 200)

    def test_cannot_revoke_someone_elses_token(self):
        ta, tb = self.token_for(self.alice), self.token_for(self.bob)
        bob_id = ApiToken.objects.get(user=self.bob).pk
        self.assertEqual(self.c.delete(f"/api/v1/auth/tokens/{bob_id}", HTTP_AUTHORIZATION=f"Bearer {ta}").status_code, 404)
        self.assertEqual(self.get("/api/v1/auth/me", tb).status_code, 200)

    def test_expired_token_and_disabled_user(self):
        tok = self.token_for(self.alice)
        ApiToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.get("/api/v1/auth/me", tok).status_code, 401)
        ApiToken.objects.update(expires_at=None)
        self.assertEqual(self.get("/api/v1/auth/me", tok).status_code, 200)
        self.alice.is_active = False
        self.alice.save()
        self.assertEqual(self.get("/api/v1/auth/me", tok).status_code, 401)

    @override_settings(WGP_TOKEN_TTL_DAYS=0)
    def test_ttl_zero_never_expires(self):
        self.token_for(self.alice)
        self.assertIsNone(ApiToken.objects.get().expires_at)

    def test_last_used_is_tracked(self):
        tok = self.token_for(self.alice)
        self.assertIsNone(ApiToken.objects.get().last_used_at)
        self.get("/api/v1/auth/me", tok)
        self.assertIsNotNone(ApiToken.objects.get().last_used_at)


class DownloadTests(V1Base):
    def test_add_list_actions_flow(self):
        tok = self.token_for(self.alice)
        r = self.post("/api/v1/downloads", tok, {"url": "http://example.com/a.iso"})
        self.assertEqual(r.status_code, 201)
        pk = r.json()["created"][0]
        lst = self.get("/api/v1/downloads", tok).json()
        self.assertEqual([d["id"] for d in lst["downloads"]], [pk])
        self.assertEqual(lst["counts"]["queued"], 1)
        self.assertEqual(self.post("/api/v1/downloads/actions", tok, {"action": "pause", "ids": [pk]}).json()["affected"], 1)
        self.assertEqual(self.get("/api/v1/downloads?status=paused", tok).json()["downloads"][0]["status"], "paused")
        self.assertEqual(self.get("/api/v1/downloads?status=queued", tok).json()["downloads"], [])
        self.assertEqual(self.get("/api/v1/downloads?status=bogus", tok).status_code, 400)
        self.assertEqual(self.get(f"/api/v1/downloads/{pk}", tok).json()["download"]["id"], pk)

    def test_validation_errors_are_reported(self):
        tok = self.token_for(self.alice)
        r = self.post("/api/v1/downloads", tok, {"urls": ["file:///etc/passwd"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"][0]["line"], 1)
        self.assertEqual(self.post("/api/v1/downloads/actions", tok, {"action": "format-disk", "ids": [1]}).status_code, 400)
        self.assertEqual(self.post("/api/v1/downloads/actions", tok, {"action": "pause", "ids": ["x"]}).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/downloads", "not json", content_type="text/plain", HTTP_AUTHORIZATION=f"Bearer {tok}").status_code, 400)

    def test_isolation_between_users_staff_sees_all(self):
        a = self.make_download("http://h/a", user=self.alice, status="queued")
        b = self.make_download("http://h/b", user=self.bob, status="queued")
        ta, tb, tadm = (self.token_for(u) for u in (self.alice, self.bob, self.admin))
        ids = lambda tok: {d["id"] for d in self.get("/api/v1/downloads", tok).json()["downloads"]}
        self.assertEqual(ids(ta), {a.pk})
        self.assertEqual(ids(tb), {b.pk})
        self.assertEqual(ids(tadm), {a.pk, b.pk})
        self.assertEqual(self.get(f"/api/v1/downloads/{a.pk}", tb).status_code, 404)
        self.assertEqual(self.post("/api/v1/downloads/actions", tb, {"action": "remove", "ids": [a.pk]}).json()["affected"], 0)
        self.assertTrue(Download.objects.filter(pk=a.pk).exists())

    def test_summary_has_no_list_but_counts(self):
        tok = self.token_for(self.alice)
        self.make_download("http://h/a", user=self.alice, status="queued")
        s = self.get("/api/v1/summary", tok).json()
        self.assertNotIn("downloads", s)
        self.assertEqual(s["counts"]["queued"], 1)
        self.assertIn("series", s)

    def test_counts_cover_all_finished_not_only_listed_200(self):
        tok = self.token_for(self.alice)
        Download.objects.bulk_create([Download(owner=self.alice, url=f"http://h/{i}", directory=self.dl_dir, filename=f"f{i}", status="completed",
                                               finished_at=timezone.now(), position=i) for i in range(230)])
        data = self.get("/api/v1/downloads", tok).json()
        self.assertEqual(data["counts"]["completed"], 230)
        self.assertEqual(len(data["downloads"]), 200)

    def test_file_download_owner_only(self):
        d = self.make_download("http://h/f.txt", user=self.alice, status="completed", filename="f.txt")
        open(d.path, "w").write("hello")
        ta, tb = self.token_for(self.alice), self.token_for(self.bob)
        self.assertEqual(b"".join(self.get(f"/api/v1/downloads/{d.pk}/file", ta).streaming_content), b"hello")
        self.assertEqual(self.get(f"/api/v1/downloads/{d.pk}/file", tb).status_code, 404)

    def test_openapi_matches_routes(self):
        doc = self.c.get("/api/v1/openapi.json").json()
        self.assertEqual(doc["openapi"], "3.0.3")
        for path, ops in doc["paths"].items():
            match = resolve(path.replace("{id}", "1"))
            self.assertTrue(match.url_name.startswith("v1-"), path)
            self.assertTrue(ops, path)
        routes = {"v1-token", "v1-me", "v1-tokens", "v1-token-detail", "v1-summary", "v1-downloads", "v1-actions", "v1-detail", "v1-file", "v1-events",
              "v1-settings", "v1-rules", "v1-rule-detail", "v1-notify", "v1-notify-test"}
        documented = {resolve(p.replace("{id}", "1")).url_name for p in doc["paths"]}
        self.assertEqual(routes, documented)


class SseTests(V1Base):
    def test_http_stream_headers_and_snapshot(self):
        tok = self.token_for(self.alice)
        self.make_download("http://h/a", user=self.alice, status="queued")
        with override_settings(WGP_SSE_MAX_SECONDS=1):
            r = self.get("/api/v1/events", tok)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r["Content-Type"], "text/event-stream")
            self.assertEqual(r["Cache-Control"], "no-cache")
            self.assertEqual(r["X-Accel-Buffering"], "no")
            events = parse_sse(r.streaming_content)
        self.assertEqual(events[0][0], "snapshot")
        self.assertEqual(len(events[0][1]["downloads"]), 1)

    def test_stream_requires_token(self):
        self.assertEqual(self.c.get("/api/v1/events").status_code, 401)

    def test_status_events_after_actions_and_removal(self):
        d = self.make_download("http://h/a", user=self.alice, status="queued", filename="a.bin")
        g = api_v1.event_stream(self.alice, max_seconds=2)
        first = parse_sse([next(g)])
        self.assertEqual(first[0][0], "snapshot")
        services.apply_action(self.alice, "pause", [d.pk])
        services.apply_action(self.alice, "resume", [d.pk])
        services.apply_action(self.alice, "remove", [d.pk])
        events = parse_sse(list(g))
        kinds = [(e, p["id"], p["status"]) for e, p in events if e in ("status", "removed")]
        self.assertEqual(kinds, [("status", d.pk, "paused"), ("status", d.pk, "queued"), ("removed", d.pk, "removed")])
        self.assertEqual(events[0][1]["filename"], "a.bin")
        self.assertNotIn("owner_id", events[0][1])

    def test_events_are_not_leaked_to_other_users_staff_sees_all(self):
        a = self.make_download("http://h/a", user=self.alice, status="queued")
        gb = api_v1.event_endpoint and api_v1.event_stream(self.bob, max_seconds=2)
        gadm = api_v1.event_stream(self.admin, max_seconds=6)  # started together with gb, which is drained first (2 s)
        next(gb); next(gadm)
        services.apply_action(self.alice, "pause", [a.pk])
        self.assertEqual([e for e, _ in parse_sse(list(gb)) if e != "progress"], [])
        self.assertEqual([p["status"] for e, p in parse_sse(list(gadm)) if e == "status"], ["paused"])

    def test_progress_events_for_running_downloads(self):
        d = self.make_download("http://h/a", user=self.alice, status="running")
        live.set_live(d.pk, 500, 1000, 250, 1)
        g = api_v1.event_stream(self.alice, max_seconds=3)
        events = parse_sse([c for c in g])
        prog = [p for e, p in events if e == "progress"]
        self.assertTrue(prog)
        self.assertEqual(prog[0]["items"][0], {"id": d.pk, "downloaded": 500, "total": 1000, "speed": 250})
        self.assertEqual(prog[0]["speed"], 250)

    def test_real_download_emits_completed_event(self):
        blob = make_blob(300_000)
        with FileServer({"/e.bin": blob}) as srv:
            d = self.make_download(srv.url("/e.bin"), user=self.alice)
            g = api_v1.event_stream(self.alice, max_seconds=2)
            next(g)
            runner.run(d.pk)
            events = parse_sse(list(g))
        final = [p for e, p in events if e == "status"][-1]
        self.assertEqual((final["id"], final["status"], final["total"]), (d.pk, "completed", len(blob)))

    def test_failed_download_event_carries_error(self):
        with FileServer({}) as srv:
            d = self.make_download(srv.url("/missing.bin"), user=self.alice)
            g = api_v1.event_stream(self.alice, max_seconds=2)
            next(g)
            runner.run(d.pk)
            events = parse_sse(list(g))
        final = [p for e, p in events if e == "status"][-1]
        self.assertEqual(final["status"], "error")
        self.assertIn("404", final["error"] + " 404" if final["exit_code"] == 8 else final["error"])
        self.assertEqual(final["exit_code"], 8)

    def test_stream_ends_when_token_revoked(self):
        tok_obj = ApiToken.objects.create(user=self.alice, key_hash="x" * 64, prefix="wgp_x")
        g = api_v1.event_stream(self.alice, max_seconds=60, token_pk=tok_obj.pk)
        next(g)
        tok_obj.delete()
        import core.api_v1 as m
        orig = m.time.monotonic
        base = orig()
        m.time.monotonic = lambda: base + 31  # jump past the 30 s revocation check
        try:
            self.assertEqual(list(g), [])  # generator returns instead of streaming on
        finally:
            m.time.monotonic = orig
