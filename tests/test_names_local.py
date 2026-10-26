from _review_fixture import reviewed
"""Structured-name/search regression fixtures only; never write the real catalog."""
import os
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from saishi_tong import luna, luna_jobs, store
from saishi_tong.classify import heuristic_contest_name
from saishi_tong.parse import ContestCard, pages_to_cards


def model_result(c, name, quote=None):
    values = {key: "" for key in luna.TEXT_LIMITS}
    values.update({k: "未知" for k in luna.ENUMS})
    values.update(contest_name=name, status="未知", format="未知")
    return {"fields": values, "evidence": {"contest_name": [{"url": c.source_url,
        "quote": quote or c.source_excerpt}]}, "uncertain_fields": [], "verification_notes": []}


class NameTests(unittest.TestCase):
    def test_headline_is_not_display_name(self):
        for title, expected in [
            ("2026星河开发者大赛正式启动 - 测试新闻网", "2026星河开发者大赛"),
            ("重磅！星河创新大赛报名开启_CSDN博客", "星河创新大赛"),
            ("星河黑客松圆满落幕｜测试日报", "星河黑客松"),
            ("开发者齐聚一堂交流技术", "待核验赛事"),
            ("测试团队荣获多项大奖", "待核验赛事"),
            ("Aurora Hackathon 2026", "Aurora Hackathon 2026"),
        ]:
            with self.subTest(title=title):
                self.assertEqual(heuristic_contest_name(title), expected)
        title = "2026星河开发者大赛正式启动 - 测试新闻网"
        c = pages_to_cards([{"Pages": [{"title": title, "url": "https://example.test/name"}]}])[0]
        self.assertEqual(c.raw_title, title)
        self.assertEqual(c.name, title)
        self.assertEqual(c.to_dict()["contest_name"], "2026星河开发者大赛")

    def test_source_grounded_names_chinese_normalization_and_english(self):
        for quote, name in [
            ("正式名称：Aurora Developer Challenge 2026，现公布规则。", "Aurora Developer Challenge 2026"),
            ("星河 · 开发者大赛", "星河开发者大赛"),
            ("2026星河开发者大赛正式启动", "2026星河开发者大赛"),
            ("本届活动名为《星河 · 开发者大赛》，欢迎参与。", "星河开发者大赛"),
        ]:
            c = ContestCard(name="新闻标题", source_url="https://example.test/name", source_excerpt=quote)
            self.assertEqual(luna.validate_result(model_result(c, name), c)["contest_name"], name)
        c = ContestCard(name="新闻标题", source_url="https://example.test/name",
                        source_excerpt="Aurora Developer Challenge 2026。星河开发者大赛正式启动。")
        for invented in ("Aurora Global Developer Challenge 2026", "极光开发者挑战赛", "全国星河开发者大赛", c.source_excerpt):
            self.assertEqual(luna.validate_result(model_result(c, invented), c)["contest_name"], "")
        bad = model_result(c, "Aurora Developer Challenge 2026")
        bad["evidence"]["contest_name"][0]["url"] = "https://other.test/name"
        self.assertEqual(luna.validate_result(bad, c)["contest_name"], "")

    def test_missing_new_field_is_explicit_schema_error(self):
        c = ContestCard(name="测试大赛")
        result = model_result(c, "测试大赛")
        del result["fields"]["contest_name"]
        with self.assertRaises(luna.LunaError):
            luna.validate_result(result, c)

    def test_hash_upgrade_reexports_old_verified_without_fake_inference(self):
        c = ContestCard(name="星河大赛", raw_title="星河大赛", source_url="https://example.test/rules",
                        source_excerpt="星河大赛", summary="旧事实", luna_state="verified",
                        luna_model="test/old-model", luna_verified_at="2026-10-20T00:00:00Z")
        old = luna.source_input(c)
        old["prompt_version"] = "luna-source-v2"
        import hashlib
        c.luna_input_hash = hashlib.sha256(luna._json(old).encode()).hexdigest()
        before = asdict(c)
        self.assertEqual(len(luna_jobs.export_jobs([c])["jobs"]), 1)
        self.assertEqual(asdict(c), before)
        self.assertNotEqual(c.luna_input_hash, luna.input_hash(c))
        h = luna.input_hash(c)
        c.contest_name = "更改的显示名称"
        self.assertEqual(luna.input_hash(c), h)


class LocalCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "isolated.db"
        self.env = patch.dict(os.environ, {"SAISHI_DB_PATH": str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def add(self, i, **extra):
        values = dict(name=f"原始新闻{i}", contest_name=f"星河赛事{i}", raw_title=f"完整原题{i}",
                      source_url=f"https://example.test/{i}", search_time="2026-10-21T00:00:00Z",
                      summary="共享摘要", curated=True, luna_state="verified", status="报名中", format="线上")
        values.update(extra)
        c = ContestCard(**values)
        store.upsert_contests([reviewed(c) if c.luna_state == 'verified' else c])
        return c

    def test_search_all_fields_filters_and_no_hidden_cap(self):
        for i in range(130):
            self.add(i)
        self.add(131, publication_state="draft", contest_name="隐藏草稿")
        self.add(132, organizer="独有组织", status="已结束", format="线下")
        self.add(133, snippet="独有片段", format="混合")
        self.assertEqual(len(store.search_contests()), 132)
        self.assertEqual(len(store.search_contests(q="共享摘要")), 132)
        self.assertEqual(len(store.search_contests(q="完整原题129")), 1)
        self.assertEqual(len(store.search_contests(q="星河赛事129")), 1)
        self.assertEqual(len(store.search_contests(q="原始新闻129")), 1)
        self.assertEqual(len(store.search_contests(q="独有组织", status="已结束", format="线下")), 1)
        self.assertEqual(len(store.search_contests(q="独有片段", format="混合")), 1)
        self.assertEqual(store.search_contests(q="隐藏草稿"), [])
        self.assertEqual(len(store.search_contests(limit=2)), 2)
        self.assertEqual(store.search_contests(limit=0), [])
        self.assertEqual(len(store.search_contests(status="已结束", limit=1)), 1)
        self.assertEqual(store.count_contests(), 132)
        for kw in ({"status": "all"}, {"format": "线上,线下"}, {"limit": -1}):
            with self.assertRaises(ValueError):
                store.search_contests(**kw)

    def test_like_parameterization_and_literal_wildcards(self):
        self.add(1, summary="100%_! literal")
        self.add(2)
        for q in ("%", "_", "!", "100%_!"):
            self.assertEqual(len(store.search_contests(q=q)), 1)
        self.assertEqual(store.search_contests(q="' OR 1=1 --"), [])
        self.assertEqual(store.count_contests(), 2)

    def test_additive_legacy_read_preserves_evidence_and_provenance(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE contests(id TEXT PRIMARY KEY,name TEXT,raw_title TEXT,source_url TEXT,search_time TEXT,luna_state TEXT,luna_model TEXT)")
        conn.execute("INSERT INTO contests VALUES(?,?,?,?,?,?,?)", ("old", "旧标题", "星河创新大赛正式启动 - 测试新闻网",
                     "https://example.test/old", "2026-10-01", "verified", "test/old-model"))
        conn.commit(); conn.close()
        store.initialize(); store.initialize()
        self.assertIsNone(store.get_contest("old"))
        got = store.get_contest("old", include_drafts=True)
        self.assertEqual(got["contest_name"], "星河创新大赛")
        self.assertEqual(got["raw_title"], "星河创新大赛正式启动 - 测试新闻网")
        self.assertEqual(got["luna_state"], "verified")
        self.assertEqual(got["luna_model"], "test/old-model")
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("SELECT contest_name FROM contests").fetchone()[0], "")
        conn.close()

    def test_admin_name_curation_preserves_raw_title_and_sources(self):
        c = self.add(1, curated=False, contest_name="", raw_title="星河大赛正式启动")
        before = store.get_contest(c.id)
        saved = store.save_admin_contest({"contest_name": "星河大赛"}, c.id)
        self.assertEqual(saved["contest_name"], "星河大赛")
        self.assertEqual(saved["raw_title"], before["raw_title"])
        self.assertEqual(saved["source_url"], before["source_url"])
        incoming = ContestCard(name="模型新闻", contest_name="其他大赛", source_url=c.source_url, luna_state="verified")
        store.upsert_contests([incoming])
        self.assertEqual(store.get_contest(c.id, include_drafts=True)["contest_name"], "星河大赛")
        created = store.save_admin_contest({"contest_name": "人工确认赛事", "source_url": "https://example.test/manual"})
        self.assertEqual(created["name"], "人工确认赛事")
        self.assertEqual(created["raw_title"], "")
        self.assertEqual(created["publication_state"], "draft")
        legacy = ContestCard(name="原始媒体报道", source_url="https://example.test/legacy-edit", search_time="2026-10-21T00:00:00Z")
        conn = store._connect()
        legacy.id = store.stable_id(legacy)
        with conn:
            store._write(conn, legacy)
        conn.close()
        changed = store.save_admin_contest({"name": "改后的正式名称"}, legacy.id)
        self.assertEqual(changed["raw_title"], "原始媒体报道")
        self.assertEqual(changed["contest_name"], "改后的正式名称")

    def test_old_verified_name_survives_upgrade_when_disabled(self):
        c = self.add(1, curated=False, contest_name="旧核验大赛", luna_state="verified", luna_model="test/old-model",
                     luna_verified_at="2026-10-20T00:00:00Z", luna_input_hash="old-prompt-hash", summary="旧事实")
        incoming = ContestCard(name=c.name, raw_title=c.raw_title, source_url=c.source_url)
        cards, _, _ = luna.process_cards([incoming], enabled=False)
        store.upsert_contests(cards)
        got = store.get_contest(c.id, include_drafts=True)
        self.assertEqual(got["contest_name"], "旧核验大赛")
        self.assertEqual(got["summary"], "旧事实")
        self.assertEqual(got["luna_state"], "stale")
        self.assertEqual(got["luna_model"], "test/old-model")
        self.assertEqual(got["luna_verified_at"], "2026-10-20T00:00:00Z")
        self.assertEqual(got["luna_input_hash"], "old-prompt-hash")
        self.assertIn("contest_name", got["uncertain_fields"])


if __name__ == "__main__":
    unittest.main()
