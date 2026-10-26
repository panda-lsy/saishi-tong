"""Offline administrator/catalog tests; every fixture uses an isolated database."""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException

from saishi_tong import store
from saishi_tong.classify import enrich_card
from saishi_tong.parse import ContestCard, pages_to_cards
from saishi_tong.web.admin_api import authenticate_admin, router


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "isolated.db"
        self.env = patch.dict(os.environ, {"SAISHI_DB_PATH": str(self.path), "SAISHI_ADMIN_TOKEN": "test-admin-only"})
        self.env.start()
        self.app = FastAPI()
        self.app.include_router(router)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def payload(self, **extra):
        data = {"title": "测试策展赛事", "summary": "管理员确认的事实", "status": "报名中", "mode": "线上",
                "sources": [{"url": "https://example.test/official", "title": "官方来源", "excerpt": "已核对"}]}
        data.update(extra)
        return data

    def request(self, path="/api/admin/contests", method="GET", data=None, token="test-admin-only", form=False):
        async def run():
            body = (urlencode(data).encode() if form else json.dumps(data).encode()) if data is not None else b""
            headers = [(b"content-type", b"application/x-www-form-urlencoded" if form else b"application/json")]
            if token is not None:
                headers.append((b"authorization", ("Bearer " + token).encode()))
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                     "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                     "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("test", 80)}
            messages = []
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            async def send(message):
                messages.append(message)
            await self.app(scope, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return status, json.loads(raw)
        return asyncio.run(run())

    def test_auth_fail_closed_and_constant_time(self):
        for token, status in [(None, 401), ("wrong-sensitive-token", 403)]:
            actual, body = self.request(token=token)
            self.assertEqual(actual, status)
            self.assertNotIn("wrong-sensitive-token", json.dumps(body))
        with patch.dict(os.environ, {"SAISHI_ADMIN_TOKEN": ""}):
            self.assertEqual(self.request()[0], 503)
        with patch("saishi_tong.web.admin_api.hmac.compare_digest", return_value=True) as compare:
            authenticate_admin("test-admin-only")
            compare.assert_called_once_with(b"test-admin-only", b"test-admin-only")
        with self.assertRaises(HTTPException):
            authenticate_admin("坏令牌")

    def test_create_draft_edit_publish_and_hide_again(self):
        code, response = self.request(method="POST", data=self.payload())
        self.assertEqual(code, 201)
        card = response["contest"]
        cid = card["id"]
        self.assertTrue(card["curated"])
        self.assertEqual(card["record_origin"], "manual")
        self.assertTrue(card["search_time"])
        self.assertEqual(card["publication_state"], "draft")
        self.assertIsNone(store.get_contest(cid))
        self.assertEqual(store.list_contests(limit=6), [])
        self.assertEqual(store.count_contests(), 0)
        self.assertEqual(self.request()[1]["total"], 1)
        self.assertEqual(self.request("/api/admin/contests/" + cid)[0], 200)
        code, response = self.request("/api/admin/contests/" + cid, "PUT", {
            "source_url": "https://example.test/revised", "name": "改名", "organizer": "主办方", "prizes": "奖品"})
        self.assertEqual(code, 200)
        self.assertEqual(response["contest"]["id"], cid)
        self.assertEqual(response["contest"]["host"], "主办方")
        self.assertEqual(response["contest"]["prize"], "奖品")
        self.assertEqual(self.request("/api/admin/contests/" + cid + "/publish", "POST")[0], 200)
        self.assertEqual(store.get_contest(cid, include_drafts=True)["name"], "改名")
        self.assertIsNone(store.get_contest(cid), "publishing does not bypass verification")
        self.assertEqual(store.count_contests(), 0)
        self.request("/api/admin/contests/" + cid, "PUT", {"publication_state": "draft"})
        self.assertEqual(store.count_contests(), 0)

    def test_no_authorization_for_writes(self):
        for method, path in [("POST", "/api/admin/contests"), ("PUT", "/api/admin/contests/other"),
                             ("POST", "/api/admin/contests/other/publish"), ("GET", "/api/admin/contests/other")]:
            self.assertEqual(self.request(path, method, self.payload(), token=None)[0], 401)
            self.assertEqual(self.request(path, method, self.payload(), token="bad")[0], 403)
        self.assertEqual(store.count_contests(include_drafts=True), 0)

    def test_form_auth_without_credential_persistence(self):
        code, body = self.request(method="POST", data={"token": "test-admin-only", "name": "表单赛事",
                                "source_url": "https://example.test/form"}, token=None, form=True)
        self.assertEqual(code, 201)
        self.assertNotIn("test-admin-only", json.dumps(body))
        self.assertNotIn(b"test-admin-only", self.path.read_bytes())

    def test_validation_and_error_redaction(self):
        bad_values = [{"source_url": "javascript:alert(1)"}, {"signup_url": "file:///tmp/x"},
                      {"sources": [{"url": "https://user:pass@example.test"}]}, {"sources": []},
                      {"status": "开放"}, {"format": "else"}, {"publication_state": "public"},
                      {"name": "x" * 301}, {"name": ""}, {"search_time": "not-time"}, {"search_time": "2026-10-21"},
                      {"luna_state": "verified"}, {"id": "other"}, {"curated": False},
                      {"source_url": "https://example.test:bad"}]
        for change in bad_values:
            with self.subTest(change=change):
                code, body = self.request(method="POST", data=self.payload(**change))
                self.assertEqual(code, 422)
                self.assertNotIn("test-admin-only", json.dumps(body))
        self.assertEqual(store.count_contests(include_drafts=True), 0)
        self.assertEqual(self.request("/api/admin/contests/missing", "PUT", {"name": "update"})[0], 404)
        self.assertIsNone(store.get_contest("' OR 1=1 --", include_drafts=True))

    def test_additive_migration_retains_legacy_rows(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE contests (id TEXT PRIMARY KEY, name TEXT, source_url TEXT, search_time TEXT)")
        conn.execute("INSERT INTO contests VALUES (?, ?, ?, ?)", ("legacy", "真实旧记录", "https://example.test/old", "2026-10-01"))
        conn.commit()
        conn.close()
        store.initialize()
        store.initialize()
        card = store.get_contest("legacy", include_drafts=True)
        self.assertEqual(card["name"], "真实旧记录")
        self.assertEqual(card["search_time"], "2026-10-01")
        self.assertEqual(card["publication_state"], "published")
        self.assertEqual(card["sources"], [])
        self.assertEqual(card["luna_state"], "pending")
        self.assertFalse(card["curated"])
        self.assertEqual(store.count_contests(include_drafts=True), 1)
        self.assertEqual(store.count_contests(), 0)

    def test_json_roundtrip_and_corruption_is_safe(self):
        card = ContestCard(name="来源", source_url="https://example.test/json", search_time="2026-10-21",
                           sources=[{"url": "https://example.test/json", "title": "来源", "excerpt": "原文"}],
                           uncertain_fields=["prizes"], verification_notes=["待核对"])
        store.upsert_contests([card])
        got = store.get_contest(card.id, include_drafts=True)
        self.assertEqual(got["sources"], card.sources)
        self.assertEqual(got["uncertain_fields"], ["prizes"])
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("UPDATE contests SET sources = ?, uncertain_fields = ?, verification_notes = ?", ("broken", "{}", "null"))
        conn.close()
        got = store.get_contest(card.id, include_drafts=True)
        self.assertEqual(got["sources"], [])
        self.assertEqual(got["uncertain_fields"], [])
        self.assertEqual(got["verification_notes"], [])

    def test_manual_facts_survive_search_and_heuristic(self):
        saved = store.save_admin_contest(self.payload(publication_state="published", deadline="2024-01-01"))
        incoming = ContestCard(name="搜索不能覆盖", source_url=saved["source_url"], snippet="线下 已结束",
                               search_time="2026-10-22", source_excerpt="新检索材料")
        store.upsert_contests([incoming])
        got = store.get_contest(saved["id"], include_drafts=True)
        self.assertEqual(got["name"], saved["name"])
        self.assertEqual(got["summary"], saved["summary"])
        self.assertEqual(got["status"], "报名中")
        self.assertEqual(got["format"], "线上")
        self.assertEqual(got["source_excerpt"], "新检索材料")
        self.assertEqual(got["search_time"], "2026-10-22")
        self.assertEqual(incoming.id, saved["id"])
        self.assertEqual(enrich_card(ContestCard.fromdict(got)).status, "报名中")

    def test_verified_facts_survive_failed_or_plain_refresh(self):
        verified = ContestCard(name="核验名称", source_url="https://example.test/luna", summary="核验摘要",
                               luna_state="verified", luna_input_hash="old-hash", luna_model="luna",
                               status="报名中", format="线上")
        store.upsert_contests([verified])
        incoming = ContestCard(name="未核验", source_url=verified.source_url, luna_state="failed", status="已结束",
                               source_excerpt="changed", search_time="2026-10-22")
        store.upsert_contests([incoming])
        got = store.get_contest(verified.id, include_drafts=True)
        self.assertEqual(got["name"], "核验名称")
        self.assertEqual(got["luna_state"], "stale")
        self.assertEqual(got["luna_input_hash"], "old-hash")
        self.assertTrue(any("来源已变更" in note for note in got["verification_notes"]))
        self.assertIn("summary", got["uncertain_fields"])
        self.assertEqual(enrich_card(ContestCard.fromdict(got)).status, "报名中")
        self.assertEqual(got["source_excerpt"], "changed")
        self.assertEqual(got["status"], "报名中")

    def test_stale_refresh_keeps_last_facts_until_successful_reverification(self):
        from saishi_tong.luna import input_hash
        verified = ContestCard(name="原始标题", raw_title="原始标题", source_url="https://example.test/stale",
                               source_excerpt="旧片段", summary="上次核验事实", status="报名中", format="线上",
                               luna_model="test/old-model", luna_verified_at="2026-10-20T00:00:00Z",
                               luna_state="verified")
        verified.luna_input_hash = input_hash(verified)
        store.upsert_contests([verified])
        old_hash = verified.luna_input_hash
        for state in ("failed", "disabled", "pending"):
            fresh = ContestCard(name="原始标题", raw_title="原始标题", source_url=verified.source_url,
                                source_excerpt="新片段已结束", luna_state=state,
                                verification_notes=["本次未完成核验"], uncertain_fields=["prizes"])
            fresh.luna_input_hash = input_hash(fresh)
            store.upsert_contests([fresh])
            saved = store.get_contest(verified.id, include_drafts=True)
            self.assertEqual(saved["luna_state"], "stale")
            self.assertEqual(saved["summary"], "上次核验事实")
            self.assertEqual(saved["status"], "报名中")
            self.assertEqual(saved["luna_model"], "test/old-model")
            self.assertEqual(saved["luna_verified_at"], "2026-10-20T00:00:00Z")
            self.assertEqual(saved["luna_input_hash"], old_hash)
            self.assertIn("summary", saved["uncertain_fields"])
            self.assertIn("本次未完成核验", saved["verification_notes"])
        fresh.luna_state = "verified"
        fresh.summary = "新片段核验事实"
        fresh.luna_model = "test/new-model"
        fresh.luna_input_hash = input_hash(fresh)
        fresh.verification_notes = ["本次来源片段核验完成"]
        fresh.uncertain_fields = []
        store.upsert_contests([fresh])
        saved = store.get_contest(verified.id, include_drafts=True)
        self.assertEqual(saved["luna_state"], "verified")
        self.assertEqual(saved["summary"], "新片段核验事实")
        self.assertNotEqual(saved["luna_input_hash"], old_hash)
        self.assertFalse(any("来源已变更" in note for note in saved["verification_notes"]))

    def test_same_source_hash_retains_previous_successful_verification(self):
        from saishi_tong.luna import input_hash
        verified = ContestCard(name="不变的源标题", raw_title="不变的源标题",
                               source_url="https://example.test/unchanged", source_excerpt="未变的片段",
                               summary="上次事实", luna_state="verified", status="报名中", format="线上")
        verified.luna_input_hash = input_hash(verified)
        store.upsert_contests([verified])
        fresh = ContestCard(name=verified.name, raw_title=verified.raw_title, source_url=verified.source_url,
                            source_excerpt=verified.source_excerpt, search_time="later", query="new query",
                            luna_state="failed")
        store.upsert_contests([fresh])
        saved = store.get_contest(verified.id, include_drafts=True)
        self.assertEqual(saved["luna_state"], "verified")
        self.assertEqual(saved["summary"], "上次事实")
        self.assertFalse(any("来源已变更" in note for note in saved["verification_notes"]))

    def test_curated_facts_win_over_new_verified_results(self):
        saved = store.save_admin_contest(self.payload(publication_state="published"))
        incoming = ContestCard(name="模型试图改名", source_url=saved["source_url"],
                               summary="不能覆盖的模型结果", luna_state="verified",
                               source_excerpt="新来源材料", status="已结束", format="线下")
        store.upsert_contests([incoming])
        got = store.get_contest(saved["id"], include_drafts=True)
        self.assertTrue(got["curated"])
        self.assertEqual(got["summary"], saved["summary"])
        self.assertEqual(got["status"], "报名中")
        self.assertEqual(got["format"], "线上")
        self.assertNotEqual(got["luna_state"], "verified")

    def test_draft_never_republished_by_search(self):
        card = store.save_admin_contest(self.payload())
        store.upsert_contests([ContestCard(name="refresh", source_url=card["source_url"])])
        self.assertIsNone(store.get_contest(card["id"]))
        self.assertEqual(store.count_contests(), 0)
        self.assertEqual(store.get_contest(card["id"], include_drafts=True)["publication_state"], "draft")

    def test_parse_retains_source_material_and_default_lists_independent(self):
        a, b = ContestCard(name="a"), ContestCard(name="b")
        a.sources.append({"url": "https://example.test/a"})
        self.assertEqual(b.sources, [])
        card = pages_to_cards([{"Pages": [{"title": "赛事", "url": "https://example.test/raw", "passage": "原" * 9000}]}])[0]
        self.assertEqual(len(card.source_excerpt), 6000)
        self.assertEqual(len(card.snippet), 280)
        self.assertEqual(card.publication_state, "published")


if __name__ == "__main__":
    unittest.main()
