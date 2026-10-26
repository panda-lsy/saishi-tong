"""OpenClaw 整理 queue: admin enqueue -> worker claim -> validated write-back."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from saishi_tong import curation, store
from saishi_tong.parse import ContestCard

WORKER = "w" * 40
ADMIN = "test-admin-only"


def call(app, path, method="GET", data=None, token=ADMIN):
    async def run():
        body = json.dumps(data).encode() if data is not None else b""
        headers = [(b"content-type", b"application/json")]
        if token:
            headers.append((b"authorization", ("Bearer " + token).encode()))
        path_only, _, qs = path.partition("?")
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": path_only, "raw_path": path_only.encode(), "query_string": qs.encode(),
                 "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("test", 80)}
        messages = []
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(message):
            messages.append(message)
        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, (json.loads(raw) if raw else None)
    return asyncio.run(run())


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "c.db"
        self.env = patch.dict(os.environ, {"SAISHI_DB_PATH": str(self.path), "SAISHI_ADMIN_TOKEN": ADMIN,
                                           "SAISHI_WORKER_TOKEN": WORKER})
        self.env.start()
        cards = [ContestCard(name="2026 示例黑客松 报名开启", source_url="https://news.example.test/a1",
                             snippet="示例黑客松报名开启", search_time="2026-10-20T10:00:00+08:00"),
                 ContestCard(name="示例黑客松2026 启动", source_url="https://news.example.test/a2",
                             snippet="示例黑客松2026", search_time="2026-10-19T10:00:00+08:00")]
        store.upsert_contests(cards)
        from saishi_tong.web.app import app
        self.app = app
        self.ids = [c["id"] for c in store.list_contests(include_drafts=True)]

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def result(self, **extra):
        data = {"verdict": "verified",
                "fields": {"contest_name": "2026 示例黑客松", "summary": "面向开发者的黑客松",
                           "organizer": "示例基金会", "timeline": "报名：2026-10-01 至 2026-11-20",
                           "eligibility": "全球开发者", "prizes": "一等奖 10 万元", "signup_url": "https://hack.example.test/signup",
                           "deadline": "2026-11-20", "status": "报名中", "format": "线上"},
                "evidence": {k: ["https://hack.example.test/"] for k in
                             ("contest_name", "summary", "organizer", "timeline", "eligibility", "prizes",
                              "signup_url", "deadline", "status", "format")},
                "sources": [{"url": "https://hack.example.test/", "title": "官网", "excerpt": "示例黑客松官网", "official": True}],
                "uncertain_fields": [], "notes": ["官网核对"], "merge_with": []}
        data.update(extra)
        return data

    def test_removed_oauth_and_worker_auth(self):
        self.assertEqual(call(self.app, "/auth/openai/login")[0], 404)
        self.assertEqual(call(self.app, "/api/worker/openclaw/claim", "POST", {}, token=None)[0], 401)
        self.assertEqual(call(self.app, "/api/worker/openclaw/claim", "POST", {}, token=ADMIN)[0], 401)
        self.assertEqual(call(self.app, "/api/admin/openclaw/jobs", token=None)[0], 401)
        self.assertEqual(call(self.app, "/api/worker/openclaw/claim", "POST", {}, token=WORKER)[0], 204)

    def test_end_to_end_verified_and_merge_proposal(self):
        status, body = call(self.app, "/api/admin/openclaw/jobs", "POST", {"scope": "unverified", "limit": 1})
        self.assertEqual(status, 201, body)
        target = body["job"]["items"][0]["contest_id"]
        other = next(i for i in self.ids if i != target)
        # duplicate enqueue of the same contest is refused while active
        self.assertEqual(call(self.app, "/api/admin/openclaw/jobs", "POST", {"scope": "selected", "contest_ids": [target]})[0], 409)
        status, job = call(self.app, "/api/worker/openclaw/claim", "POST", {}, token=WORKER)
        self.assertEqual(status, 200)
        item = job["items"][0]
        self.assertNotIn("TENCENT", json.dumps(job))
        status, out = call(self.app, f"/api/worker/openclaw/jobs/{job['job_id']}/items/{target}", "POST",
                           {"fingerprint": item["fingerprint"], "model": "openai/gpt-6-luna",
                            "result": self.result(merge_with=[other])}, token=WORKER)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["verdict"], "verified")
        card = store.get_contest(target, include_drafts=True)
        self.assertEqual(card["luna_state"], "verified")
        self.assertEqual(card["organizer"], "示例基金会")
        self.assertTrue(card["luna_model"].startswith("openclaw/saishi"))
        self.assertIn("https://hack.example.test/", [s["url"] for s in card["sources"]])
        self.assertEqual(card["publication_state"], "published")
        call(self.app, f"/api/worker/openclaw/jobs/{job['job_id']}/finish", "POST", {"status": "done"}, token=WORKER)
        status, listing = call(self.app, "/api/admin/openclaw/jobs")
        self.assertEqual(listing["jobs"][0]["status"], "done")
        self.assertEqual(listing["jobs"][0]["items"][0]["merge_ids"], [other])
        status, preview = call(self.app, f"/api/admin/openclaw/jobs/{job['job_id']}/items/{target}/merge-preview")
        self.assertEqual(status, 200, preview)
        status, merged = call(self.app, f"/api/admin/openclaw/jobs/{job['job_id']}/items/{target}/merge", "POST",
                              {"duplicate_ids": [other], "expected_fingerprints": preview["fingerprints"]})
        self.assertEqual(status, 200, merged)
        self.assertEqual(store.get_contest(other, include_drafts=True)["merged_into"], target)

    def proposal(self):
        target, other = self.ids
        job = curation.create_job(scope='selected', contest_ids=[target])
        claimed = curation.claim_job()
        curation.commit_result(job['id'], target, claimed['items'][0]['fingerprint'], self.result(merge_with=[other]))
        curation.finish_job(job['id'])
        return job['id'], target, other

    def test_preview_fingerprints_reject_edits_and_no_preview(self):
        job, target, other = self.proposal()
        path = f'/api/admin/openclaw/jobs/{job}/items/{target}'
        self.assertEqual(call(self.app, path + '/merge-preview', token=None)[0], 401)
        self.assertEqual(call(self.app, path + '/merge', 'POST', {'duplicate_ids': [other]})[0], 422)
        code, preview = call(self.app, path + '/merge-preview', 'POST', {'duplicate_ids': [other]})
        self.assertEqual(code, 200)
        self.assertEqual(preview['survivor_id'], target)
        self.assertEqual(len(preview['contests']), 2)
        self.assertNotIn('TENCENT', json.dumps(preview))
        store.save_admin_contest({'organizer': '人工更正主办'}, other)
        code, _ = call(self.app, path + '/merge', 'POST', {'duplicate_ids': [other], 'expected_fingerprints': preview['fingerprints']})
        self.assertEqual(code, 409)
        self.assertFalse(curation.get_job(job)['items'][0]['merged'])
        self.assertEqual(store.get_contest(other, include_drafts=True)['publication_state'], 'published')

    def test_keep_separate_is_persistent_and_prevents_old_confirmation(self):
        job, target, other = self.proposal()
        preview = curation.preview_merge(job, target)
        curation.keep_separate(job, target)
        item = curation.get_job(job)['items'][0]
        self.assertEqual(item['merge_ids'], [])
        self.assertIn('保留独立', item['summary'])
        with self.assertRaises(curation.CurationError):
            curation.merge_proposal(job, target, [other], expected_fingerprints=preview['fingerprints'])
        self.assertEqual(store.get_contest(other, include_drafts=True)['merged_into'], '')

    def test_concurrent_keep_separate_and_failed_merge_roll_back_atomically(self):
        job, target, other = self.proposal()
        preview = curation.preview_merge(job, target)
        # The proposal flag is updated in the same transaction as the records.
        with patch.object(store, '_check_fingerprint', side_effect=ValueError('record_changed')):
            with self.assertRaises(curation.CurationError):
                curation.merge_proposal(job, target, [other], expected_fingerprints=preview['fingerprints'])
        self.assertFalse(curation.get_job(job)['items'][0]['merged'])
        original = store.merge_contests
        def concurrent_decision(*args, **kwargs):
            curation.keep_separate(job, target)
            return original(*args, **kwargs)
        with patch.object(store, 'merge_contests', side_effect=concurrent_decision):
            with self.assertRaises(curation.CurationError):
                curation.merge_proposal(job, target, [other], expected_fingerprints=preview['fingerprints'])
        self.assertEqual(store.get_contest(other, include_drafts=True)['merged_into'], '')

    def test_batch_cooldown_avoids_repeating_failed_item_but_allows_explicit_retry(self):
        job = curation.create_job(scope='unverified', limit=1)
        first = job['items'][0]['contest_id']
        curation.claim_job()
        curation._set_item(job['id'], first, status='failed', summary='测试失败')
        curation.finish_job(job['id'])
        next_job = curation.create_job(scope='unverified', limit=1)
        self.assertNotEqual(next_job['items'][0]['contest_id'], first)
        retry = curation.create_job(scope='selected', contest_ids=[first])
        self.assertEqual(retry['items'][0]['contest_id'], first)

    def test_unbacked_fields_are_flagged_not_guessed(self):
        job = curation.create_job(scope="selected", contest_ids=[self.ids[0]])
        claimed = curation.claim_job()
        res = self.result()
        res["evidence"] = {"contest_name": ["https://hack.example.test/"]}
        res["sources"][0]["official"] = False
        res["fields"]["deadline"] = "2025-01-01"
        out = curation.commit_result(job["id"], self.ids[0], claimed["items"][0]["fingerprint"], res)
        card = store.get_contest(self.ids[0], include_drafts=True)
        self.assertEqual(card["organizer"], "")
        self.assertIn("organizer", card["uncertain_fields"])
        self.assertIn("signup_url", card["uncertain_fields"])
        self.assertEqual(out["verdict"], "needs_review")  # no official source
        self.assertEqual(card["luna_state"], "review")
        self.assertEqual(card["status"], "未知")  # unbacked -> unknown; display must not re-guess

    def test_stale_fingerprint_rejected_and_ended_status(self):
        job = curation.create_job(scope="selected", contest_ids=[self.ids[0]])
        claimed = curation.claim_job()
        with self.assertRaises(curation.CurationError):
            curation.commit_result(job["id"], self.ids[0], "0" * 64, self.result())
        res = self.result()
        res["fields"]["deadline"] = "2025-11-20"
        curation.commit_result(job["id"], self.ids[0], claimed["items"][0]["fingerprint"], res)
        self.assertEqual(store.get_contest(self.ids[0], include_drafts=True)["status"], "已结束")

    def test_review_state_keeps_agent_format(self):
        job = curation.create_job(scope="selected", contest_ids=[self.ids[0]])
        claimed = curation.claim_job()
        res = self.result()
        res["sources"][0]["official"] = False  # -> needs_review
        res["fields"]["format"] = "线下"
        curation.commit_result(job["id"], self.ids[0], claimed["items"][0]["fingerprint"], res)
        card = store.get_contest(self.ids[0], include_drafts=True)
        self.assertEqual(card["luna_state"], "review")
        self.assertEqual(card["format"], "线下")

    def test_same_worker_resumes_running_job(self):
        job = curation.create_job(scope="selected", contest_ids=[self.ids[0]])
        first = curation.claim_job("jarvis-mini")
        self.assertEqual(first["job_id"], job["id"])
        self.assertIsNone(curation.claim_job("other"))
        again = curation.claim_job("jarvis-mini")
        self.assertEqual(again["job_id"], job["id"])
        self.assertEqual(len(again["items"]), 1)


    def test_dedupe_before_verify_auto_merges_into_existing_verified(self):
        """Same official URL as an already-verified row → auto-merge, one survivor."""
        existing = ContestCard(
            name="已有核验赛", contest_name="已有核验赛", id="",
            source_url="https://official.example.test/same",
            source_excerpt="已有核验赛官网", search_time="2026-10-20T10:00:00+08:00",
            organizer="基金会", timeline="报名：2026-10-01", deadline="2026-11-20",
            status="报名中", format="线上", luna_state="verified",
            publication_state="published",
            sources=[{"url": "https://official.example.test/same", "title": "官网", "excerpt": "已有"}])
        existing.id = store.stable_id(existing)
        store.upsert_contests([existing])
        pending = ContestCard(
            name="重复待核验", contest_name="重复待核验",
            source_url="https://news.example.test/dup-pending",
            snippet="重复待核验", search_time="2026-10-21T10:00:00+08:00")
        store.upsert_contests([pending])
        pending_id = store.stable_id(pending)
        job = curation.create_job(scope="selected", contest_ids=[pending_id])
        claimed = curation.claim_job()
        item = next(i for i in claimed["items"] if i["contest"]["id"] == pending_id)
        res = self.result()
        res["fields"]["contest_name"] = "已有核验赛"
        res["sources"] = [{"url": "https://official.example.test/same", "title": "官网",
                           "excerpt": "已有核验赛", "official": True}]
        res["evidence"] = {k: ["https://official.example.test/same"] for k in res["fields"]}
        out = curation.commit_result(job["id"], pending_id, item["fingerprint"], res)
        self.assertTrue(out.get("auto_merged"))
        active = store.list_admin_contests(verification="awaiting", path=self.path, page_size=50)
        self.assertEqual(store.list_admin_contests(verification="verified", path=self.path)["total"], 0)
        verified_ids = {c["id"] for c in active["contests"] if c["luna_state"] == "verified"}
        self.assertEqual(len(verified_ids), 1)
        self.assertIn(out["survivor_id"], verified_ids)

if __name__ == "__main__":
    unittest.main()
