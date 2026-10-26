"""Offline unittest suite; no API or real credential file access."""
import asyncio
import importlib
import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse
from saishi_tong import service, store
from saishi_tong.classify import classify_contest, safe_source_url
from saishi_tong.parse import ContestCard, dedupe, pages_to_cards, closing_soon
from saishi_tong.report import cards_table

TODAY = date(2026, 10, 21)


class ClassificationTests(unittest.TestCase):
    def label(self, title="竞赛", snippet="", deadline="", published=""):
        return classify_contest(title, snippet, deadline, published, today=TODAY)

    def test_dates_and_formats(self):
        for deadline, expected in [("2026-10-20", "已结束"), ("2026-10-21", "即将截止"),
                                   ("2026年10月28日", "即将截止"), ("2026/11/22", "报名中")]:
            with self.subTest(deadline=deadline):
                self.assertEqual(self.label(deadline=deadline)[0], expected)
        for snippet, expected in [("线上征集", "线上"), ("现场线下比赛", "线下"),
                                  ("线上报名，线下决赛", "混合"), ("hybrid", "混合"), ("", "未知")]:
            self.assertEqual(self.label(snippet=snippet)[1], expected)

    def test_historical_ambiguous_and_invalid(self):
        self.assertEqual(self.label("2024 黑客松正在报名", "截止11月22日")[0], "已结束")
        self.assertEqual(self.label("征文正在报名", "截止11月22日", published="2024-10-01")[0], "已结束")
        self.assertEqual(self.label("竞赛报名中", "截止11月22日")[0], "未知")
        self.assertEqual(self.label(deadline="2026-02-30")[0], "未知")
        self.assertEqual(self.label("2026 报名中", deadline="2026-02-30")[0], "未知")
        self.assertEqual(self.label("2026 报名中", published="2024-01-01")[0], "未知")
        self.assertEqual(self.label("竞赛已结束", deadline="2026-11-22")[0], "已结束")
        self.assertEqual(self.label("2027 报名中")[0], "未知")

    def test_safe_sources_and_markdown(self):
        for url in ["javascript:alert(1)", "data:text/html,test", "//evil.test", "file:///x",
                    "https://a.test\n/x", "https://user:pass@a.test", "https://a.test:wrong"]:
            self.assertEqual(safe_source_url(url), "")
            card = ContestCard(name="test", source_url=url)
            self.assertNotIn(url, cards_table([card]))
        self.assertEqual(safe_source_url("https://a.test/article?id=1"), "https://a.test/article?id=1")

    def test_parse_and_dedupe(self):
        cards = pages_to_cards([{"Query": "test", "Pages": [
            {"title": "历史竞赛", "url": "javascript:alert(1)", "score": "bad"},
            {"title": "一", "url": "https://a.test/article?id=1"},
            {"title": "二", "url": "https://a.test/article?id=2"},
            {"title": "一副本", "url": "https://a.test/article?id=1&utm_source=test"},
        ]}])
        self.assertEqual(cards[0].source_url, "")
        self.assertEqual(len(dedupe(cards)), 3)
        old = ContestCard(name="2024竞赛", deadline="2024-11-22")
        self.assertEqual(closing_soon([old]), [])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SAISHI_DB_PATH": str(Path(self.temp.name) / "contests.db")})
        self.env.start()
        self.loader = patch.object(service, "load_env_files")
        self.loader.start()
        self.creds = patch.object(service, "credentials_present", return_value=True)
        self.creds.start()

    def tearDown(self):
        self.creds.stop()
        self.loader.stop()
        self.env.stop()
        self.temp.cleanup()

    def response(self, count=20):
        return [{"Query": "test", "Pages": [
            {"title": f"赛事{i}", "url": f"https://example.test/articles/{i}",
             "passage": "线上征文 截止2026-11-22", "score": i}
            for i in range(count)]}]

    def test_persist_all_stable_ids_and_sql(self):
        with patch.object(service, "search_many", return_value=self.response()):
            result = service.run_search("test", top=2)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["top"]), 2)
            self.assertEqual(len(result["cards"]), 20)
            self.assertEqual(store.count_contests(include_drafts=True), 20)
            old_ids = {c["id"] for c in result["cards"]}
            again = service.run_search("again", top=1)
        self.assertEqual({c["id"] for c in again["cards"]}, old_ids)
        self.assertEqual(store.count_contests(include_drafts=True), 20)
        self.assertEqual(store.count_contests(), 0, "offline search alone is not verification")
        self.assertIsNone(store.get_contest("' OR 1=1 --"))
        self.assertEqual(len(store.list_contests(include_drafts=True)), 20)
        self.assertTrue(all(isinstance(c, dict) and c["id"] and c["format"] and c["status"]
                            for c in store.list_contests(include_drafts=True)))

    def test_pack_enriches_when_storage_is_mocked(self):
        # Pack owns its labels even if a replacement storage implementation does not mutate cards.
        card = ContestCard(name="线上竞赛", deadline="2026-10-25", source_url="https://example.test/pack",
                           status="未知", format="未知")
        with patch.object(service, "search_many", return_value=[{"Pages": []}]), patch.object(
                service, "pages_to_cards", return_value=[card]), patch.object(
                service, "upsert_contests"), patch.object(service, "closing_soon", return_value=[card]), patch(
                "saishi_tong.classify.current_date", return_value=TODAY):
            result = service.run_search("test")
        for key in ["cards", "top", "closing"]:
            self.assertEqual(result[key][0]["status"], "即将截止")
            self.assertEqual(result[key][0]["format"], "线上")

    def test_failure_partial_and_empty(self):
        with patch.object(service, "search_many", return_value=[{"error": "DO_NOT_LEAK"}]):
            result = service.run_search("test")
            self.assertFalse(result["ok"])
            self.assertNotIn("DO_NOT_LEAK", json.dumps(result))
        with patch.object(service, "search_many", side_effect=RuntimeError("DO_NOT_LEAK")):
            result = service.run_search("test")
            self.assertFalse(result["ok"])
            self.assertNotIn("DO_NOT_LEAK", json.dumps(result))
        with patch.object(service, "search_many", return_value=self.response(1) + [{"error": "DO_NOT_LEAK"}]):
            result = service.run_search("test")
            self.assertTrue(result["ok"])
            self.assertEqual(result["failed_queries"], 1)
            self.assertIn("部分搜索失败", result["note"])
        with patch.object(service, "search_many", return_value=[{"Pages": []}]):
            result = service.run_search("test")
            self.assertTrue(result["ok"])
            self.assertEqual(result["result_count"], 0)
            self.assertIn("调用成功", result["note"])

    def test_malformed_and_nan_data(self):
        for responses in [[{}], [{"Error": {"Message": "DO_NOT_LEAK"}, "Pages": []}], [None]]:
            with patch.object(service, "search_many", return_value=responses):
                result = service.run_search("test")
                self.assertFalse(result["ok"])
                self.assertNotIn("DO_NOT_LEAK", json.dumps(result))
        with patch.object(service, "search_many", return_value=[{"Pages": ["[]", {
                "title": "NaN", "url": "https://example.test/nan", "score": "NaN"}]}]):
            result = service.run_search("test")
            self.assertTrue(result["ok"])
            self.assertEqual(result["top"][0]["score"], 0)
            json.dumps(result, allow_nan=False)

    def test_asgi_visitor_and_search_dispatch(self):
        web = importlib.import_module("saishi_tong.web.app")
        async def request(path, method="GET", body=b""):
            messages = []
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": method, "scheme": "http", "path": path,
                     "raw_path": path.encode(), "query_string": b"", "root_path": "",
                     "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
                     "client": ("127.0.0.1", 1), "server": ("test", 80)}
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            async def send(message):
                messages.append(message)
            await web.app(scope, receive, send)
            return next(m["status"] for m in messages if m["type"] == "http.response.start")
        with patch.object(web.TEMPLATES, "TemplateResponse", side_effect=lambda request, name, context, **kwargs: HTMLResponse("stub", status_code=kwargs.get("status_code", 200))):
            for path in ["/", "/contests", "/settings"]:
                self.assertEqual(asyncio.run(request(path)), 200)
            self.assertEqual(asyncio.run(request("/contests/missing")), 404)
            with patch.object(service, "run_search", side_effect=AssertionError("web must stay local")) as run:
                self.assertEqual(asyncio.run(request("/search", "POST", b"q=test&queries=1")), 200)
                self.assertEqual(asyncio.run(request("/api/search", "POST", b"q=test&queries=1")), 200)
                run.assert_not_called()
            with patch.dict(os.environ, {"SAISHI_ADMIN_TOKEN": "synthetic-admin"}):
                self.assertEqual(asyncio.run(request("/admin")), 200)
                self.assertEqual(asyncio.run(request("/admin", "POST", b"token=x")), 303)
            self.assertEqual(asyncio.run(request("/settings", "POST", b"key=x")), 405)

    def test_routes_contract_without_credentials(self):
        web = importlib.import_module("saishi_tong.web.app")
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
        with patch.object(service, "search_many", return_value=self.response(25)):
            service.run_search("seed")
        # Synthetic AI + human-reviewed fixture, separate from offline ingestion.
        with closing(store._connect()) as conn, conn:
            conn.execute("UPDATE contests SET luna_state = ?", ("verified",))
        for fixture in store.list_contests(include_drafts=True):
            store.save_admin_contest({"human_reviewed": True}, fixture["id"])
        def capture(request, name, context, **kwargs):
            return name, context
        with patch.object(web.TEMPLATES, "TemplateResponse", side_effect=capture), patch.object(
                service, "load_env_files", side_effect=AssertionError("visitor must not load credentials")):
            name, context = web.home(request)
            self.assertEqual(name, "index.html")
            self.assertTrue({"request", "version", "search_mode", "contest_count", "recent_contests"}.issubset(context))
            name, context = asyncio.run(web.contests(request))
            self.assertEqual(name, "contests.html")
            self.assertEqual(context["total"], 25)
            name, context = web.contest_detail(request, context["contests"][0]["id"])
            self.assertEqual(name, "detail.html")
            with self.assertRaises(HTTPException) as missing:
                web.contest_detail(request, "missing")
            self.assertEqual(missing.exception.status_code, 404)
            name, context = web.settings(request)
            self.assertEqual(name, "settings.html")
            self.assertEqual(context["role"], "settings")
            name, context = web.admin(request)
            self.assertEqual(name, "admin.html")
            self.assertEqual(context["role"], "admin")
            redirect = web.legacy_admin_post()
            self.assertEqual(redirect.status_code, 303)
            self.assertEqual(redirect.headers["location"], "/admin")
        with patch.object(service, "run_search", side_effect=AssertionError("web must stay local")) as run:
            self.assertEqual(asyncio.run(web.search_api(request)).status_code, 200)
            run.assert_not_called()


class CredentialLoaderTests(unittest.TestCase):
    def test_only_approved_path_and_names_existing_env_wins(self):
        # Synthetic values only; never inspect real credentials.
        content = "TENCENTCLOUD_SECRET_ID=fake-file-id\nTENCENTCLOUD_SECRET_KEY=fake-file-key\nOTHER=not-loaded"
        with patch.dict(os.environ, {"TENCENTCLOUD_SECRET_ID": "fake-existing"}, clear=True), patch.object(
                Path, "home", return_value=Path("/fake-home")), patch.object(
                Path, "read_text", autospec=True, return_value=content) as read:
            service.load_env_files()
            self.assertEqual(os.environ["TENCENTCLOUD_SECRET_ID"], "fake-existing")
            self.assertEqual(os.environ["TENCENTCLOUD_SECRET_KEY"], "fake-file-key")
            self.assertNotIn("OTHER", os.environ)
            self.assertEqual(read.call_args.args[0], Path("/fake-home/.config/saishi-tong/tencent.env"))
            self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
