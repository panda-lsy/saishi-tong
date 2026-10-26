from _review_fixture import reviewed
"""Offline web isolation checks. Fixtures live only in temporary databases."""
import asyncio
import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse

from saishi_tong import client, service, store
from saishi_tong.parse import ContestCard
from saishi_tong.web import config


async def asgi_request(app, url, method="GET", fields=None, headers=(), json_body=None):
    url = urlsplit(url)
    body = json.dumps(json_body).encode() if json_body is not None else urlencode(fields or {}, doseq=True).encode()
    kind = b"application/json" if json_body is not None else b"application/x-www-form-urlencoded"
    messages = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": url.path,
             "raw_path": url.path.encode(), "query_string": url.query.encode(), "root_path": "",
             "headers": [(b"content-type", kind), *headers],
             "client": ("127.0.0.1", 1), "server": ("test", 80)}
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    return start["status"], b"".join(item.get("body", b"") for item in messages), dict(start["headers"])


class LocalWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"SAISHI_DB_PATH": str(Path(self.temp.name) / "local.db"),
                                      "SAISHI_ADMIN_TOKEN": "synthetic-admin"})
        env.start()
        self.addCleanup(env.stop)
        self.web = importlib.import_module("saishi_tong.web.app")
        for target in ("saishi_tong.service.run_search", "saishi_tong.service.load_env_files",
                       "saishi_tong.client.search_many", "saishi_tong.client.get_client"):
            p = patch(target, side_effect=AssertionError("web must stay local"))
            p.start()
            self.addCleanup(p.stop)
        cards = [ContestCard(name="离线黑客松", luna_state="verified", status="报名中", format="线上", snippet="线上征集", summary="本地目录记录",
                             source_url="https://example.test/local", search_time="2026-10-21T12:00:00Z",
                             deadline="2099-11-22"),
                 ContestCard(name="离线历史征文", luna_state="verified", source_url="https://example.test/history",
                             search_time="2025-01-01T12:00:00Z", deadline="2025-01-10")]
        store.upsert_contests([reviewed(c) for c in cards])
        self.card_id = cards[0].id
        self.draft = store.save_admin_contest({"name": "不可见草稿", "publication_state": "draft",
                                               "source_url": "https://example.test/draft"})

    def request(self, path, **kwargs):
        return asyncio.run(asgi_request(self.web.app, path, **kwargs))

    def test_get_post_api_search_are_local_and_ignore_all_browser_keys(self):
        for path in ("/api/search", "/api/contests"):
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    fields = {"q": "黑客松", "format": "线上", "status": "报名中",
                              "TENCENTCLOUD_SECRET_ID": "synthetic-browser-id",
                              "TENCENTCLOUD_SECRET_KEY": "synthetic-browser-key",
                              "queries": "not-an-integer", "dry_run": "false"}
                    url = path + "?" + urlencode(fields) if method == "GET" else path
                    code, body, headers = self.request(url, method=method, fields=fields,
                        headers=[(b"x-tencent-secret-id", b"synthetic-header-id"),
                                 (b"x-tencent-secret-key", b"synthetic-header-key")])
                    self.assertEqual(code, 200)
                    result = json.loads(body)
                    self.assertEqual(result["search_mode"], "local")
                    self.assertEqual(result["total"], 1)
                    self.assertEqual(result["contests"][0]["id"], self.card_id)
                    self.assertEqual(result["contests"][0]["source_url"], "https://example.test/local")
                    self.assertEqual(result["contests"][0]["search_time"], "2026-10-21T12:00:00Z")
                    self.assertNotIn(b"synthetic-browser", body)
                    self.assertNotIn(b"synthetic-header", body)
                    self.assertEqual(headers[b"cache-control"], b"no-store")

    def test_without_keys_json_partial_and_duplicate_keys_still_local(self):
        self.assertEqual(json.loads(self.request("/api/contests")[1])["total"], 2)
        for fields in ({"TENCENTCLOUD_SECRET_ID": "partial"},
                       {"TENCENTCLOUD_SECRET_ID": ["one", "two"], "TENCENTCLOUD_SECRET_KEY": "key"}):
            self.assertEqual(self.request("/api/search", method="POST", fields=fields)[0], 200)
        code, body, _ = self.request("/api/search", method="POST", json_body={"q": "黑客松"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["total"], 1)
        self.assertEqual(json.loads(self.request("/api/search?q=no-such-contest")[1])["total"], 0)
        self.assertEqual(self.request("/contests/" + self.draft["id"])[0], 404)

    def test_html_contexts_and_settings_never_include_credentials(self):
        def capture(request, name, context, **kwargs):
            self.assertNotIn("credentials", context)
            self.assertNotIn("presets", context)
            self.assertNotIn("synthetic-admin", repr(context))
            return HTMLResponse(name)
        with patch.object(self.web.TEMPLATES, "TemplateResponse", side_effect=capture) as render:
            for path in ("/", "/contests?q=" + urlencode({"x": "黑客松"})[2:], "/search", "/settings"):
                self.assertEqual(self.request(path)[0], 200)
            self.assertEqual(set(render.call_args.args[2]), {"request", "version", "role"})
            for path in ("/search", "/contests"):
                self.assertEqual(self.request(path, method="POST", fields={"q": "黑客松"})[0], 200)
                name, context = render.call_args.args[1:3]
                self.assertEqual(name, "contests.html")
                self.assertEqual(context["total"], 1)
                self.assertTrue({"q", "status", "format", "status_options", "format_options", "search_mode"} <= context.keys())

    def test_health_redirect_and_admin_api_stays_protected(self):
        code, body, headers = self.request("/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["search_mode"], "local")
        self.assertNotIn(b"credentials", body)
        for method in ("GET", "POST"):
            code, body, headers = self.request("/admin?token=synthetic-admin", method=method,
                                              fields={"token": "synthetic-admin"})
            self.assertEqual(code, 200 if method == "GET" else 303)
            if method == "POST":
                self.assertEqual(headers[b"location"], b"/admin")
            self.assertNotIn(b"synthetic-admin", body)
        self.assertEqual(self.request("/api/admin/contests?token=synthetic-admin")[0], 401)
        self.assertEqual(self.request("/api/admin/contests", headers=[(b"authorization", b"Bearer wrong")])[0], 403)
        code, body, _ = self.request("/api/admin/contests", headers=[(b"authorization", b"Bearer synthetic-admin")])
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["total"], 3)
        self.assertNotIn(b"synthetic-admin", body)
        with patch.dict(os.environ, {"SAISHI_ADMIN_TOKEN": ""}):
            self.assertEqual(self.request("/api/admin/contests")[0], 503)

    def test_removed_oauth_routes_are_404(self):
        for path in ("/auth/openai/login", "/auth/openai/session", "/auth/openai/callback", "/oauth-setup"):
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(self.request("/auth/openai/logout", method="POST")[0], 404)

    def test_invalid_json_filters_do_not_echo_secrets(self):
        code, body, headers = self.request("/api/search", method="POST",
                                           json_body={"q": ["synthetic-oauth-secret"]})
        self.assertEqual(code, 422)
        self.assertNotIn(b"synthetic-oauth-secret", body)
        self.assertIn(b"content-security-policy", headers)
        for route in self.web.app.routes:
            self.assertNotIn("sync", getattr(route, "path", "").lower())


class WebConfigTests(unittest.TestCase):
    def test_allowlisted_paths_and_no_shell_execution_or_cam_loading(self):
        opened = []
        def fake_open(path, *args, **kwargs):
            opened.append(path)
            if path.name == "tencent.env":
                return io.StringIO("TENCENTCLOUD_SECRET_ID=must-not-load\nTENCENTCLOUD_SECRET_KEY=must-not-load\n"
                                   "export SAISHI_ADMIN_TOKEN='synthetic-admin-file'\nUNRELATED=ignored\n")
            raise AssertionError("unexpected config file")
        with patch.dict(os.environ, {"TENCENTCLOUD_SECRET_ID": "inherited",
                                      "TENCENTCLOUD_SECRET_KEY": "inherited"}, clear=True), \
             patch.object(Path, "home", return_value=Path("/fake-home")), \
             patch.object(Path, "open", fake_open):
            config.load_web_config()
            self.assertEqual(os.environ["SAISHI_ADMIN_TOKEN"], "synthetic-admin-file")
            self.assertNotIn("OPENAI_OAUTH_CLIENT_ID", os.environ)
            self.assertNotIn("UNRELATED", os.environ)
            self.assertFalse(any(name in os.environ for name in config.CAM_KEYS))
            self.assertEqual(opened, [Path("/fake-home/.config/saishi-tong/tencent.env")])


if __name__ == "__main__":
    unittest.main()
