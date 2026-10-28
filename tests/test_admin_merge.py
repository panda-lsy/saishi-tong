from contextlib import closing
"""Admin verification/merge tests: synthetic fixtures, temporary databases only."""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from saishi_tong import luna, store
from saishi_tong.parse import ContestCard


class AdminMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "isolated.db"
        self.env = patch.dict(os.environ, {"SAISHI_DB_PATH": str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def card(self, key="one", **changes):
        card = ContestCard(name="测试开发者挑战赛", contest_name="测试开发者挑战赛",
            raw_title="测试开发者挑战赛启动报道", id=key,
            source_url="https://example.test/" + key,
            source_excerpt="测试开发者挑战赛。线上举办。主办：测试协会。奖金1000元。报名截止2026-11-22。",
            search_time="2026-10-22T00:00:00Z")
        for name, value in changes.items():
            setattr(card, name, value)
        return card

    def save(self, *cards):
        store.upsert_contests(list(cards), path=self.path)
        return [store.get_contest(c.id, path=self.path, include_drafts=True) for c in cards]

    def fingerprints(self, *ids):
        return {cid: store.record_fingerprint(store.get_contest(cid, path=self.path, include_drafts=True)) for cid in ids}

    def verify(self, snapshot):
        card = ContestCard.fromdict(copy.deepcopy(snapshot))
        values = {key: "" for key in luna.TEXT_LIMITS}
        values.update({k: "未知" for k in luna.ENUMS})
        values.update(contest_name="测试开发者挑战赛", organizer="测试协会", prizes="1000元",
                      deadline="2026-11-22", status="未知", format="线上")
        evidence = {k: [{"url": card.source_url, "quote": card.source_excerpt}]
                    for k, value in values.items() if value and value != "未知"}
        return luna._apply(card, {"fields": values, "evidence": evidence,
            "uncertain_fields": [], "verification_notes": []}, "test/offline-model")

    def audit(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM contest_merge_audit ORDER BY id")]

    def test_tabs_pagination_and_curated_is_not_verified(self):
        states = ["verified", "pending", "disabled", "failed", "stale", "curated"]
        self.save(*(self.card(str(i), luna_state=states[i % 6], curated=i % 6 == 5) for i in range(66)))
        first = store.list_admin_contests(path=self.path)
        self.assertEqual((first["total"], first["page_size"], first["pages"]), (66, 15, 5))
        self.assertEqual(len(first["contests"]), 15)
        self.assertEqual(first["counts"], {"verified": 0, "awaiting": 11, "unverified": 55})
        second = store.list_admin_contests(page=2, path=self.path)
        self.assertFalse({c["id"] for c in first["contests"]} & {c["id"] for c in second["contests"]})
        verified = store.list_admin_contests(verification="verified", path=self.path)
        self.assertTrue(all(c["luna_state"] == "verified" for c in verified["contests"]))
        unverified = store.list_admin_contests(verification="unverified", page_size=100, path=self.path)
        self.assertEqual(unverified["total"], 55)
        self.assertEqual(len(unverified["contests"]), 50)
        self.assertEqual(unverified["page_size"], 50)
        self.assertEqual(store.list_admin_contests(page=999999999999999999, path=self.path)["contests"], [])

    def test_tab_query_is_bound_and_wildcards_literal(self):
        self.save(self.card("a", summary="literal_%"), self.card("b", summary="unrelated"))
        self.assertEqual(store.list_admin_contests(q="_%", path=self.path)["total"], 1)
        self.assertEqual(store.list_admin_contests(q="' OR 1=1 --", path=self.path)["total"], 0)
        for kwargs in ({"page": 0}, {"page": True}, {"page_size": -1}, {"verification": "curated"}):
            with self.assertRaises(ValueError):
                store.list_admin_contests(path=self.path, **kwargs)

    def test_duplicate_url_alias_suggests_different_names_without_merging(self):
        first = self.card("a", contest_name="Sea × OpenAI Codex 黑客松（曼谷）",
                          source_url="https://new.qq.com/rain/a/20261021A09JHF00")
        second = self.card("b", contest_name="Sea OpenAI Hackathon",
                           source_url="https://news.qq.com/rain/a/20261021A09JHF00")
        a, b = self.save(first, second)
        candidates = store.find_duplicate_contests(a, path=self.path)
        self.assertEqual([c["id"] for c in candidates], ["b"])
        self.assertIn("canonical_source_url_match", candidates[0]["reasons"])
        self.assertGreaterEqual(candidates[0]["score"], .99)
        self.assertEqual(store.count_contests(path=self.path, include_drafts=True), 2)
        self.assertEqual(self.audit(), [])
        self.assertNotEqual(store._source_identity("https://a.test/index.html"),
                            store._source_identity("https://b.test/index.html"))
        self.assertNotEqual(store._source_identity(first.source_url),
                            store._source_identity("https://news.qq.com/rain/a/OTHER"))

    def test_duplicate_exact_source_hash_and_name_host_date(self):
        first = self.card("a", organizer="协会", deadline="2026-11-22")
        a = self.save(first)[0]
        same = copy.deepcopy(first)
        same.id = "external"
        candidates = store.find_duplicate_contests(same, path=self.path)
        self.assertIn("source_input_hash_match", candidates[0]["reasons"])
        second = self.card("b", organizer="协会", deadline="2026年11月22日")
        self.save(second)
        candidates = store.find_duplicate_contests(a, path=self.path)
        self.assertIn("formal_name_exact", candidates[0]["reasons"])
        self.assertIn("organizer_match", candidates[0]["reasons"])
        self.assertIn("timeline_date_match", candidates[0]["reasons"])

    def test_same_host_different_contests_or_editions_not_duplicates(self):
        a = self.save(self.card("a", organizer="共同主办", deadline="2026-11-22"))[0]
        self.save(self.card("b", contest_name="机器人摄影征文大赛", organizer="共同主办", deadline="2026-11-22"),
                  self.card("c", organizer="共同主办", deadline="2025-11-22"),
                  self.card("d", contest_name="另一个不同比赛", source_url="https://other.test/index.html"))
        self.assertEqual(store.find_duplicate_contests(a, path=self.path), [])

    def test_verification_updates_facts_but_preserves_retrieval_snapshot(self):
        old = self.save(self.card())[0]
        verified = self.verify(old)
        updated = store.commit_admin_verification("one", verified, store.record_fingerprint(old), path=self.path)
        self.assertEqual(updated["luna_state"], "verified")
        self.assertEqual(updated["organizer"], "测试协会")
        self.assertEqual(updated["contest_name"], "测试开发者挑战赛")
        self.assertEqual(updated["luna_input_hash"], luna.input_hash(ContestCard.fromdict(old)))
        for key in ("id", "source_url", "sources", "raw_title", "search_time", "source_excerpt", "name"):
            self.assertEqual(updated[key], old[key])
        self.assertEqual(store.list_admin_contests(verification="verified", path=self.path)["total"], 0)

    def test_explicit_curated_verification_keeps_manual_nonempty_facts(self):
        old = self.save(self.card(curated=True, organizer="人工主办", summary="人工摘要", format="线下"))[0]
        verified = self.verify(old)
        updated = store.commit_admin_verification("one", asdict(verified), store.record_fingerprint(old), path=self.path)
        self.assertEqual(updated["luna_state"], "verified")
        self.assertEqual(updated["organizer"], "人工主办")
        self.assertEqual(updated["summary"], "人工摘要")
        self.assertEqual(updated["format"], "线下")
        self.assertEqual(updated["prizes"], "1000元")
        self.assertIn("organizer", updated["uncertain_fields"])
        self.assertTrue(any("保留管理员" in n for n in updated["verification_notes"]))

    def test_failed_verification_or_wrong_snapshot_never_writes_verified(self):
        old = self.save(self.card())[0]
        expected = store.record_fingerprint(old)
        failed = copy.deepcopy(old)
        failed["luna_state"] = "failed"
        with self.assertRaisesRegex(ValueError, "^verification_incomplete$"):
            store.commit_admin_verification("one", failed, expected, path=self.path)
        verified = self.verify(old)
        verified.luna_input_hash = "0" * 64
        with self.assertRaisesRegex(ValueError, "^verification_source_changed$"):
            store.commit_admin_verification("one", verified, expected, path=self.path)
        self.assertEqual(store.record_fingerprint(store.get_contest("one", path=self.path, include_drafts=True)), expected)

    def test_fingerprint_full_record_stable_and_changed_after_concurrent_edit(self):
        old = self.save(self.card())[0]
        before = copy.deepcopy(old)
        expected = store.record_fingerprint(old)
        self.assertEqual(old, before)
        self.assertEqual(expected, store.record_fingerprint(dict(reversed(list(old.items())))))
        for key, change in (("search_time", "later"), ("raw_title", "new raw"),
                            ("summary", "new summary"), ("publication_state", "draft"),
                            ("status", "已结束"), ("format", "线下"),
                            ("verification_notes", ["new note"])):
            changed = copy.deepcopy(old)
            changed[key] = change
            self.assertNotEqual(expected, store.record_fingerprint(changed))
        verified = self.verify(old)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE contests SET summary = 'concurrent edit' WHERE id = 'one'")
        with self.assertRaisesRegex(ValueError, "^record_changed$"):
            store.commit_admin_verification("one", verified, expected, path=self.path)
        self.assertEqual(store.get_contest("one", path=self.path, include_drafts=True)["summary"], "concurrent edit")

    def test_merge_sources_conflicts_manual_values_retire_and_audit(self):
        a, b = self.save(self.card("a", curated=True, organizer="人工主办", summary="主摘要", publication_state="draft"),
                         self.card("b", organizer="来源主办", summary="备摘要", eligibility="学生"))
        b_original = copy.deepcopy(b)
        result = store.merge_contests("a", ["b"], expected_fingerprints=self.fingerprints("a", "b"), path=self.path)
        merged = result["contest"]
        self.assertEqual(result["retired_ids"], ["b"])
        self.assertEqual(merged["publication_state"], "published")
        self.assertEqual(merged["organizer"], "人工主办")
        self.assertEqual(merged["summary"], "主摘要")
        self.assertEqual(merged["eligibility"], "学生")
        self.assertEqual(len(merged["sources"]), 2)
        self.assertIn("organizer", merged["uncertain_fields"])
        retired = store.get_contest("b", path=self.path, include_drafts=True)
        b_original.update(publication_state="retired", merged_into="a")
        self.assertEqual({k:v for k,v in retired.items() if k != "record_fingerprint"},
                         {k:v for k,v in b_original.items() if k != "record_fingerprint"})
        self.assertIsNone(store.get_contest("b", path=self.path))
        self.assertEqual([c["id"] for c in store.list_contests(path=self.path, include_drafts=True)], ["a"])
        self.assertEqual([c["id"] for c in store.search_contests(path=self.path)], [])  # pending merge ≠ verified public
        self.assertEqual(store.get_contest("a", path=self.path, include_drafts=True)["id"], "a")
        self.assertEqual(store.list_admin_contests(path=self.path)["total"], 1)
        self.assertEqual(store.find_duplicate_contests(merged, path=self.path), [])
        audit = self.audit()[0]
        self.assertEqual(audit["id"], result["audit_id"])
        self.assertEqual(json.loads(audit["previous_states"]), {"a": "draft", "b": "published"})
        self.assertEqual(json.loads(audit["before_records"])[1]["publication_state"], "published")
        self.assertIn("organizer", {x["field"] for x in json.loads(audit["conflicts"])["conflicts"]})
        self.assertEqual(audit["after_fingerprint"], store.record_fingerprint(merged))

    def test_merge_deduplicates_alias_source_urls_and_preserves_excerpts(self):
        a, b = self.save(self.card("a", source_url="https://new.qq.com/rain/a/ARTICLE"),
                         self.card("b", source_url="https://news.qq.com/rain/a/ARTICLE", source_excerpt="第二段来源文本"))
        merged = store.merge_contests("a", ["b"], expected_fingerprints=self.fingerprints("a", "b"), path=self.path)["contest"]
        self.assertEqual(len(merged["sources"]), 1)
        self.assertIn("第二段来源文本", merged["sources"][0]["excerpt"])
        self.assertIn(a["source_excerpt"], merged["sources"][0]["excerpt"])

    def test_merged_new_material_never_claims_original_run_verified_it(self):
        a, b = self.save(self.card("a"), self.card("b", source_excerpt="新来源内容", eligibility="新资格"))
        verified = self.verify(a)
        result = store.merge_contests("a", ["b"], verified_card=verified,
                                     expected_fingerprints=self.fingerprints("a", "b"), path=self.path)
        # eligibility filled from unverified donor → stale; hash still the pre-merge verified snapshot
        self.assertEqual(result["contest"]["luna_state"], "stale")
        self.assertEqual(result["contest"]["luna_input_hash"], verified.luna_input_hash)
        self.assertEqual(result["contest"]["luna_model"], "test/offline-model")
        self.assertEqual(result["contest"]["eligibility"], "新资格")

    def test_merge_transaction_rolls_back_target_retirements_and_audit(self):
        self.save(self.card("a"), self.card("b"), self.card("c"))
        expected = self.fingerprints("a", "b", "c")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER block_audit BEFORE INSERT ON contest_merge_audit BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.merge_contests("a", ["b", "c"], expected_fingerprints=expected, path=self.path)
        self.assertEqual(self.fingerprints("a", "b", "c"), expected)
        self.assertEqual(store.count_contests(path=self.path, include_drafts=True), 3)
        self.assertEqual(self.audit(), [])

    def test_stale_merge_fingerprint_rejects_all_writes(self):
        self.save(self.card("a"), self.card("b"))
        expected = self.fingerprints("a", "b")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE contests SET source_excerpt = 'changed source' WHERE id = 'b'")
        with self.assertRaisesRegex(ValueError, "^record_changed$"):
            store.merge_contests("a", ["b"], expected_fingerprints=expected, path=self.path)
        self.assertEqual(store.count_contests(path=self.path, include_drafts=True), 2)
        self.assertEqual(self.audit(), [])

    def test_invalid_merge_and_missing_records_are_static_errors(self):
        self.save(self.card("a"), self.card("b"))
        for duplicate_ids in ([], ["a"], ["b", "b"], [str(i) for i in range(21)], "b"):
            with self.assertRaisesRegex(ValueError, "^invalid_merge_ids$"):
                store.merge_contests("a", duplicate_ids, path=self.path)
        with self.assertRaisesRegex(ValueError, "^fingerprint_required$"):
            store.merge_contests("a", ["b"], path=self.path)
        with self.assertRaisesRegex(LookupError, "^contest_not_found$"):
            store.merge_contests("a", ["missing"], expected_fingerprints={"a": "0"*64, "missing": "0"*64}, path=self.path)
        self.assertEqual(self.audit(), [])

    def test_search_cannot_revive_retired_and_prior_audit_survives(self):
        self.save(self.card("a"), self.card("b"), self.card("c"))
        store.merge_contests("a", ["b"], expected_fingerprints=self.fingerprints("a", "b"), path=self.path)
        before = store.get_contest("b", path=self.path, include_drafts=True)
        first_audit = self.audit()[0]
        store.upsert_contests([self.card("b", summary="new search cannot replace", publication_state="published")], path=self.path)
        self.assertEqual(store.get_contest("b", path=self.path, include_drafts=True), before)
        self.assertIsNone(store.get_contest("b", path=self.path))
        with self.assertRaisesRegex(ValueError, "^contest_retired$"):
            store.save_admin_contest({"publication_state": "published"}, "b", path=self.path)
        store.merge_contests("a", ["c"], expected_fingerprints=self.fingerprints("a", "c"), path=self.path)
        self.assertEqual(len(self.audit()), 2)
        self.assertEqual(self.audit()[0], first_audit)
        self.assertEqual(store.count_contests(path=self.path, include_drafts=True), 1)

    def test_additive_migration_preserves_all_66_ids(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE contests(id TEXT PRIMARY KEY, name TEXT, source_url TEXT, search_time TEXT)")
            conn.executemany("INSERT INTO contests VALUES (?, ?, ?, ?)",
                [(str(i), "legacy fixture", "https://example.test/" + str(i), "2026-10-01") for i in range(66)])
        store.initialize(self.path)
        store.initialize(self.path)
        self.assertEqual({c["id"] for c in store.list_contests(path=self.path, include_drafts=True)}, {str(i) for i in range(66)})
        self.assertTrue(all(c["merged_into"] == "" for c in store.list_contests(path=self.path, include_drafts=True)))
        self.assertEqual(self.audit(), [])

    def test_fromdict_type_safety_and_source_key_allowlist(self):
        original = asdict(self.card())
        original["sources"] = [{"url": "https://example.test/evidence", "title": "source", "excerpt": "text",
                                "transport_token": "discard-marker"}]
        original["access_token"] = "discard-marker"
        result = ContestCard.fromdict(original)
        self.assertNotIn("discard-marker", json.dumps(asdict(result)))
        for key, value in (("sources", "{}"), ("sources", [{"url": "javascript:x"}]),
                           ("verification_notes", [{}]), ("uncertain_fields", "summary"),
                           ("summary", []), ("curated", "false"), ("score", float("inf"))):
            bad = asdict(self.card())
            bad[key] = value
            with self.assertRaises(ValueError):
                ContestCard.fromdict(bad)



    def test_pick_merge_canonical_prefers_verified(self):
        weak = self.card("weak", luna_state="review", organizer="弱主办")
        strong = self.card("strong", luna_state="verified", organizer="强主办",
                           summary="完整摘要", timeline="报名：2026-11-01", eligibility="公开")
        self.save(weak, strong)
        cards = [store._as_card(store.get_contest(cid, path=self.path, include_drafts=True))
                 for cid in ("weak", "strong")]
        self.assertEqual(store.pick_merge_canonical(cards), "strong")

    def test_merge_keeps_verified_when_facts_unchanged(self):
        a = self.card("a", luna_state="verified", luna_input_hash="x" * 64,
                      organizer="主办A", summary="摘要A", eligibility="学生",
                      verification_notes=["已核验"])
        b = self.card("b", luna_state="verified", luna_input_hash="y" * 64,
                      organizer="主办A", summary="摘要A", eligibility="学生")
        self.save(a, b)
        result = store.merge_contests("a", ["b"], expected_fingerprints=self.fingerprints("a", "b"), path=self.path)
        self.assertEqual(result["contest"]["luna_state"], "verified")
        self.assertEqual(result["survivor_id"], "a")
        self.assertEqual(store.list_admin_contests(verification="verified", path=self.path)["total"], 0)
        self.assertIsNone(store.get_contest("b", path=self.path))  # retired hidden from public

    def test_merge_demotes_when_unverified_fact_filled(self):
        a, b = self.save(self.card("a", luna_state="verified", luna_input_hash="x" * 64, eligibility=""),
                         self.card("b", eligibility="仅学生", source_excerpt="新来源内容", luna_state="pending"))
        result = store.merge_contests("a", ["b"], expected_fingerprints=self.fingerprints("a", "b"), path=self.path)
        self.assertEqual(result["contest"]["eligibility"], "仅学生")
        self.assertEqual(result["contest"]["luna_state"], "stale")

    def test_merge_keeps_verified_when_only_sources_added(self):
        a, b = self.save(self.card("a", luna_state="verified", luna_input_hash="x" * 64),
                         self.card("b", source_excerpt="新来源内容", luna_state="pending"))
        result = store.merge_contests("a", ["b"], expected_fingerprints=self.fingerprints("a", "b"), path=self.path)
        self.assertEqual(result["contest"]["luna_state"], "verified")

    def test_merge_does_not_revive_legacy_only_prize_or_host(self):
        a, b = self.save(self.card("a", curated=True, luna_state="verified", luna_input_hash="x" * 64,
                                  organizer="", host="", prizes="", prize=""),
                         self.card("b", curated=True, organizer="", host="旧标题误提取主办", prizes="", prize="91万"))
        merged = store.merge_contests("a", ["b"], expected_fingerprints=self.fingerprints("a", "b"), path=self.path)["contest"]
        self.assertEqual((merged["organizer"], merged["host"], merged["prizes"], merged["prize"]), ("", "", "", ""))
        self.assertEqual(merged["luna_state"], "verified")
        self.assertEqual(json.loads(self.audit()[0]["conflicts"])["filled"], [])

    def test_merge_updates_legacy_mirrors_when_modern_facts_are_filled(self):
        self.save(self.card("a", organizer="", host="", prizes="", prize=""),
                  self.card("b", organizer="来源主办", host="过时主办", prizes="一等奖1000元\n二等奖500元", prize="错误金额"))
        merged = store.merge_contests("a", ["b"], expected_fingerprints=self.fingerprints("a", "b"), path=self.path)["contest"]
        self.assertEqual(merged["host"], "来源主办")
        self.assertEqual(merged["prize"], "一等奖1000元")

if __name__ == "__main__":
    unittest.main()
