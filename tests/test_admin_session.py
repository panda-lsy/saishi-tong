"""Offline signed-session lifecycle and request-boundary tests."""
import json
import os
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI
from saishi_tong.web import admin_session as admin
from tests._browser import Browser

TOKEN = "fixture-only-admin-credential-not-real-" + "x" * 32


class AdminSessionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SAISHI_ADMIN_TOKEN": TOKEN})
        self.env.start()
        self.now = 1000000
        self.svc = admin.AdminSessionService(clock=lambda: self.now)
        self.app = FastAPI()
        self.app.include_router(admin.router, prefix="/api/admin")
        self.app.state.admin_session_service = self.svc
        @self.app.api_route("/protected", methods=["GET", "POST"], dependencies=[Depends(admin.require_admin)])
        async def protected():
            return {"ok": True}
        self.browser = Browser(self.app)

    def tearDown(self):
        self.env.stop()

    def login(self):
        status, headers, body = self.browser.request("/api/admin/login", "POST", {"token": TOKEN})
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN, repr(headers) + body.decode() + repr(self.svc.sessions))
        return headers, json.loads(body)

    def test_refresh_signed_cookie_and_no_credential_leak(self):
        headers, payload = self.login()
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("SameSite=strict", headers["set-cookie"])
        self.assertNotIn("Secure", headers["set-cookie"])
        self.assertIn("Max-Age=28800", headers["set-cookie"])
        opaque = self.browser.cookies[admin.SESSION_COOKIE]
        self.assertEqual(opaque.count("."), 1)
        self.assertNotIn(opaque, self.svc.sessions)
        refreshed = Browser(self.app)
        refreshed.cookies = dict(self.browser.cookies)
        body = json.loads(refreshed.request("/api/admin/session")[2])
        self.assertEqual(body, payload)
        self.assertEqual(refreshed.request("/protected")[0], 200)
        self.assertEqual(payload["expires_at"], self.now + admin.SESSION_TTL)

    def test_csrf_origin_and_bearer_rules(self):
        _, payload = self.login()
        csrf = {"x-csrf-token": payload["csrf_token"]}
        self.assertEqual(self.browser.request("/protected", "POST")[0], 403)
        self.assertEqual(self.browser.request("/protected", "POST", headers={"x-csrf-token": "wrong"})[0], 403)
        self.assertEqual(self.browser.request("/protected", "POST", headers=csrf)[0], 200)
        for headers in ({**csrf, "origin": "https://evil.test"}, {**csrf, "sec-fetch-site": "same-site"}):
            self.assertEqual(self.browser.request("/protected", "POST", headers=headers)[0], 403)
        auth = {"authorization": "Bearer " + TOKEN}
        self.assertEqual(self.browser.request("/protected", "POST", headers=auth)[0], 200)
        self.assertEqual(self.browser.request("/protected", "POST", headers={**auth, "origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.browser.request("/api/admin/login", "POST", {"token": TOKEN}, {"origin": "null"})[0], 403)

    def test_logout_revocation_and_relogin_rotation(self):
        _, first = self.login()
        old = self.browser.cookies[admin.SESSION_COOKIE]
        _, second = self.login()
        self.assertIsNone(self.svc.resolve(old))
        self.assertNotEqual(first["csrf_token"], second["csrf_token"])
        current = self.browser.cookies[admin.SESSION_COOKIE]
        self.assertEqual(self.browser.request("/api/admin/logout", "POST")[0], 403)
        self.assertEqual(self.browser.request("/api/admin/logout", "POST", headers={"x-csrf-token": second["csrf_token"]})[0], 200)
        self.assertNotIn(admin.SESSION_COOKIE, self.browser.cookies)
        self.assertIsNone(self.svc.resolve(current))
        self.browser.cookies[admin.SESSION_COOKIE] = current
        self.assertEqual(self.browser.request("/protected")[0], 401)

    def test_expiry_token_rotation_and_tamper_fail_closed(self):
        self.login()
        old = self.browser.cookies[admin.SESSION_COOKIE]
        self.browser.cookies[admin.SESSION_COOKIE] = old[:-1] + ("0" if old[-1] != "0" else "1")
        self.assertEqual(self.browser.request("/protected")[0], 401)
        self.browser.cookies[admin.SESSION_COOKIE] = old
        os.environ["SAISHI_ADMIN_TOKEN"] = TOKEN + "rotate"
        self.assertEqual(self.browser.request("/protected")[0], 401)
        self.assertFalse(self.svc.sessions)
        os.environ["SAISHI_ADMIN_TOKEN"] = TOKEN
        self.login()
        self.now += admin.SESSION_TTL
        self.assertFalse(json.loads(self.browser.request("/api/admin/session")[2])["authenticated"])
        self.assertFalse(self.svc.sessions)

    def test_unconfigured_missing_wrong_and_bearer_login(self):
        self.assertEqual(self.browser.request("/protected")[0], 401)
        self.assertEqual(self.browser.request("/api/admin/login", "POST", {"token": "wrong"})[0], 403)
        self.assertEqual(self.browser.request("/api/admin/login", "POST")[0], 401)
        self.assertEqual(self.browser.request("/api/admin/login", "POST", headers={"authorization": "Bearer " + TOKEN})[0], 200)
        os.environ["SAISHI_ADMIN_TOKEN"] = ""
        for route, method in (("/protected", "GET"), ("/api/admin/login", "POST"), ("/api/admin/session", "GET")):
            self.assertEqual(self.browser.request(route, method)[0], 503)

    def test_https_and_nonloopback_http_never_downgrade_secure(self):
        for scheme, host in (("https", "127.0.0.1:8787"), ("https", "site.test"), ("http", "site.test"), ("http", "192.168.1.4")):
            self.browser = Browser(self.app, scheme=scheme, host=host)
            headers, _ = self.login()
            self.assertIn("Secure", headers["set-cookie"])

    def test_form_login_and_json_validation(self):
        from urllib.parse import urlencode
        status, _, raw = self.browser.request("/api/admin/login", "POST", urlencode({"token": TOKEN}).encode(),
                                             {"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertEqual(self.browser.request("/api/admin/login", "POST", [TOKEN])[0], 422)
        self.assertEqual(self.browser.request("/api/admin/login", "POST", {"token": "x" * 17000})[0], 413)

    def test_capacity_and_cleanup(self):
        with patch.object(admin, "MAX_SESSIONS", 1):
            self.login()
            other = Browser(self.app)
            self.assertEqual(other.request("/api/admin/login", "POST", {"token": TOKEN})[0], 503)
            self.now += admin.SESSION_TTL + 1
            self.assertEqual(other.request("/api/admin/login", "POST", {"token": TOKEN})[0], 200)


if __name__ == "__main__":
    unittest.main()
