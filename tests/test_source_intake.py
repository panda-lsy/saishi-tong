from _review_fixture import reviewed
"""Manual source jobs: synthetic model responses, no network/credential reads."""
from contextlib import closing
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saishi_tong import source_intake as sources, store, curation
from saishi_tong.classify import display_status
from saishi_tong.parse import ContestCard
from test_openclaw_curation import call, ADMIN, WORKER


class SourceIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'c.db'
        env = patch.dict(os.environ, {'SAISHI_DB_PATH': str(self.path), 'SAISHI_ADMIN_TOKEN': ADMIN, 'SAISHI_WORKER_TOKEN': WORKER})
        env.start(); self.addCleanup(env.stop)
        from saishi_tong.web.app import app
        self.app = app

    def result(self, url='https://modelscope.cn/events/123/summary'):
        values = dict(contest_name='2026 示例开发者挑战赛', organizer='示例主办', deadline='2026-11-22',
                      summary='真实来源中的赛事简介', timeline='征集至2026-11-22', format='线上', status='报名中')
        return {'source_url': url, 'verdict': 'needs_review', 'fields': values,
                'sources': [{'url': url, 'title': '2026 示例开发者挑战赛', 'excerpt': '示例开发者挑战赛，2026-11-22截止。', 'official': True}],
                'evidence': {k: [url] for k in values}, 'notes': ['合成模型测试响应'], 'uncertain_fields': []}

    def create_claim(self, **kwargs):
        job = sources.create_job(**kwargs)
        claimed = sources.claim('jarvis')
        self.assertEqual(job['id'], claimed['job_id'])
        return job, claimed

    def test_admin_and_worker_roles_and_no_network(self):
        for path in ['/api/admin/openclaw/sources', '/api/admin/openclaw/jobs']:
            self.assertEqual(call(self.app, path, token=None)[0], 401)
        endpoint = '/api/admin/openclaw/source-jobs'
        self.assertEqual(call(self.app, endpoint, 'POST', {}, token=WORKER)[0], 403)
        self.assertEqual(call(self.app, '/api/worker/openclaw/sources/claim', 'POST', {}, token=ADMIN)[0], 401)
        with patch('urllib.request.urlopen', side_effect=AssertionError('website cannot fetch sources')):
            code, data = call(self.app, endpoint, 'POST', {'source_id': 'csdn', 'limit': 1})
        self.assertEqual(code, 201)
        self.assertNotIn('claim_key', data['job'])
        self.assertEqual(call(self.app, '/api/worker/openclaw/claim', 'POST', {}, token=WORKER)[0], 204)
        self.assertEqual(call(self.app, '/api/worker/openclaw/sources/claim', 'POST', {}, token=WORKER)[0], 200)

    def test_source_inputs_reject_unsafe_urls_and_bad_limits(self):
        bad = ['http://127.0.0.1/a', 'https://[::1]/', 'http://localhost/', 'file:///a',
               'https://u:p@example.test/a', 'https://example.test/?token=abc', 'http://metadata.internal/', 'http://a.test:22/']
        for url in bad:
            with self.assertRaises(curation.CurationError): sources.create_job('url', url)
        for value in (None, [], 2, 'missing'):
            with self.assertRaises(curation.CurationError): sources.create_job(value)
        for value in (0, 6, True, '1'):
            with self.assertRaises(curation.CurationError): sources.create_job(limit=value)
        with self.assertRaises(curation.CurationError): sources.create_job('modelscope', 'https://example.test/a')

    def test_exclusive_claim_and_duplicate_enqueue(self):
        job, claim = self.create_claim()
        self.assertIsNone(sources.claim('jarvis'))
        self.assertIsNone(sources.claim('other'))
        with self.assertRaises(curation.CurationError): sources.create_job()
        with self.assertRaises(curation.CurationError): sources.heartbeat(job['id'], 'wrong')
        self.assertEqual(sources.heartbeat(job['id'], claim['claim_key'])['status'], 'running')

    def test_platform_detail_subdomains_are_accepted_without_suffix_spoofing(self):
        job = sources.create_job('csdn', 'https://builderx.csdn.net/activity-site/yuanbao/index', 1)
        self.assertEqual(job['source_id'], 'csdn')
        with self.assertRaises(curation.CurationError):
            sources.create_job('csdn', 'https://evilcsdn.net/a')
        with self.assertRaises(curation.CurationError):
            sources.create_job('csdn', 'https://csdn.net.example.test/a')

    def test_atomic_private_draft_import_and_idempotent_replay(self):
        job, claim = self.create_claim()
        payload = {'contests': [self.result()]}
        output = sources.commit(job['id'], claim['claim_key'], payload, 'openai/gpt-6-luna')
        self.assertEqual(output['status'], 'done')
        self.assertEqual(len(output['results']), 1)
        self.assertEqual(store.list_contests(), [])
        card = store.get_contest(output['results'][0]['id'], include_drafts=True)
        self.assertEqual(card['publication_state'], 'draft')
        self.assertEqual(card['luna_state'], 'review')
        self.assertEqual(card['luna_model'], 'openclaw/saishi:openai/gpt-6-luna')
        self.assertTrue(card['search_time'] and card['luna_verified_at'] and card['sources'])
        self.assertEqual(sources.list_jobs()[0]['results'][0]['search_time'], card['search_time'])
        self.assertEqual(sources.commit(job['id'], claim['claim_key'], payload, 'openai/gpt-6-luna')['results'], output['results'])

    def test_missing_deadline_stays_private_and_ended_source_is_skipped(self):
        job, claim = self.create_claim(limit=2)
        unknown = self.result('https://modelscope.cn/events/unknown')
        unknown['fields'].update(deadline='', timeline='', status='未知')
        unknown['evidence'].pop('deadline', None)
        unknown['sources'][0]['excerpt'] = '星河开发者挑战赛为面向开发者的技术赛事；报名时间尚未公布。'
        ended = self.result('https://modelscope.cn/events/ended')
        ended['fields'].update(deadline='2026-01-02', status='已结束')
        ended['sources'][0]['excerpt'] = '星河开发者挑战赛，报名截止2026-01-02。'
        payload = {'contests': [unknown, ended]}
        with patch('saishi_tong.classify.current_date', return_value=date(2026, 10, 26)):
            receipt = sources.commit(job['id'], claim['claim_key'], payload, 'openai/gpt-6-luna')
        self.assertEqual(len(receipt['results']), 1); self.assertEqual(receipt['skipped'], 1)
        draft = store.get_contest(receipt['results'][0]['id'], include_drafts=True)
        self.assertEqual(draft['deadline'], ''); self.assertEqual(draft['publication_state'], 'draft')
        self.assertIn('不作为新赛事搜索结果', ' '.join(draft['verification_notes']))
        self.assertEqual(store.list_contests(), [])

    def test_real_but_non_computing_contest_is_not_imported(self):
        job,claim=self.create_claim()
        result=self.result()
        result['fields']['contest_name']='世界华人学生作文大赛'
        result['sources'][0]['title']='世界华人学生作文大赛'
        output=sources.commit(job['id'],claim['claim_key'],{'contests':[result]},'synthetic/model')
        self.assertEqual(output['results'],[]);self.assertEqual(output['skipped'],1)
        self.assertEqual(store.list_contests(include_drafts=True),[])

    def test_invalid_second_record_rolls_back_all_inserts(self):
        job, claim = self.create_claim()
        invalid = self.result('https://example.test/other'); invalid['fields'] = []
        with self.assertRaises(curation.CurationError):
            sources.commit(job['id'], claim['claim_key'], {'contests': [self.result(), invalid]}, 'openai/gpt-6-luna')
        self.assertEqual(store.list_contests(include_drafts=True), [])

    def test_missing_model_or_agent_failure_never_imports(self):
        for result, model in [({'contests': [self.result()]}, ''), ({'error': 'not_available'}, 'openai/gpt-6-luna')]:
            job, claim = self.create_claim()
            receipt = sources.commit(job['id'], claim['claim_key'], result, model)
            self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(store.list_contests(include_drafts=True), [])

    def test_existing_curated_verified_and_retired_rows_are_untouched(self):
        for retired in (False, True):
            url = 'https://modelscope.cn/events/' + ('retired' if retired else 'existing')
            card = ContestCard(name='原赛事', contest_name='管理员正式名称', source_url=url, curated=True,
                               luna_state='verified', publication_state='retired' if retired else 'published')
            store.upsert_contests([card])
            with closing(store._connect()) as conn: before = conn.execute('SELECT * FROM contests WHERE id=?', (card.id,)).fetchone()
            job, claim = self.create_claim()
            receipt = sources.commit(job['id'], claim['claim_key'], {'contests': [self.result(url)]}, 'openai/gpt-6-luna')
            self.assertEqual(receipt['skipped'], 1)
            with closing(store._connect()) as conn: self.assertEqual(tuple(before), tuple(conn.execute('SELECT * FROM contests WHERE id=?', (card.id,)).fetchone()))

    def test_cancelled_or_reclaimed_jobs_reject_late_results(self):
        job, claim = self.create_claim()
        sources.cancel_job(job['id'])
        with self.assertRaises(curation.CurationError): sources.commit(job['id'], claim['claim_key'], {'contests': []}, 'openai/gpt-6-luna')
        job, claim = self.create_claim()
        with closing(sources._conn()) as conn, conn:
            conn.execute("UPDATE source_intake_jobs SET heartbeat_at='2000-01-01' WHERE id=?", (job['id'],))
        renewed = sources.claim()
        self.assertNotEqual(renewed['claim_key'], claim['claim_key'])
        with self.assertRaises(curation.CurationError): sources.commit(job['id'], claim['claim_key'], {'contests': []}, 'openai/gpt-6-luna')

    def test_api_bad_result_fails_task_without_retry_loop(self):
        job, claim = self.create_claim()
        code, _ = call(self.app, '/api/worker/openclaw/sources/' + job['id'] + '/result', 'POST',
                       {'claim_key': claim['claim_key'], 'model': 'openai/gpt-6-luna', 'result': {'contests': [{'source_url': 'javascript:alert(1)'}]}}, token=WORKER)
        self.assertEqual(code, 422)
        self.assertEqual(sources.list_jobs()[0]['status'], 'failed')
        self.assertEqual(sources.list_jobs()[0]['model'], 'openai/gpt-6-luna')
        self.assertEqual(store.list_contests(include_drafts=True), [])

    def test_missing_primary_link_is_explicit_and_retains_actual_model(self):
        job, claim = self.create_claim()
        result = self.result()
        result['source_url'] = 'https://modelscope.cn/events/123/detail'
        code, _ = call(self.app, '/api/worker/openclaw/sources/' + job['id'] + '/result', 'POST',
                       {'claim_key': claim['claim_key'], 'model': 'openai/gpt-6-luna', 'result': {'contests':[result]}}, token=WORKER)
        self.assertEqual(code, 422)
        record = sources.list_jobs()[0]
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['model'], 'openai/gpt-6-luna')
        self.assertIn('source_evidence_required', record['error'])

    def test_worker_source_protocol_calls_agent_and_commits_draft(self):
        import importlib, sys
        from types import SimpleNamespace
        with patch.dict(sys.modules, {'fcntl': SimpleNamespace()}):
            worker = importlib.import_module('saishi_tong.openclaw_worker')
        sources.create_job()
        code, job = call(self.app, '/api/worker/openclaw/sources/claim', 'POST', {}, token=WORKER)
        self.assertEqual(code, 200)
        app = self.app
        class Remote:
            def post(self, path, data):
                return call(app, '/api/worker/openclaw' + path, 'POST', data, token=WORKER)
        with patch.object(worker, 'run_agent', return_value=({'contests': [self.result()]}, 'openai/gpt-6-luna', '')) as run:
            worker.process_source(Remote(), job)
        run.assert_called_once()
        self.assertEqual(sources.list_jobs()[0]['status'], 'done')
        self.assertEqual(len(store.list_contests(include_drafts=True)), 1)

    def test_worker_initial_heartbeat_failure_is_visible_without_model_call(self):
        import importlib, sys
        from types import SimpleNamespace
        with patch.dict(sys.modules, {'fcntl': SimpleNamespace()}):
            worker = importlib.import_module('saishi_tong.openclaw_worker')
        sources.create_job()
        _, job = call(self.app, '/api/worker/openclaw/sources/claim', 'POST', {}, token=WORKER)
        app = self.app
        class Remote:
            def post(self, path, data):
                if path.endswith('/heartbeat'): return 503, None
                return call(app, '/api/worker/openclaw' + path, 'POST', data, token=WORKER)
        with patch.object(worker, 'run_agent') as run:
            worker.process_source(Remote(), job)
        run.assert_not_called()
        self.assertEqual(sources.list_jobs()[0]['status'], 'failed')


class DeadlineAgingTests(unittest.TestCase):
    def test_known_date_ages_but_does_not_reopen_or_guess(self):
        self.assertEqual(display_status('报名中', '2026-11-22', today=date(2026, 11, 15)), '即将截止')
        self.assertEqual(display_status('报名中', '2026-11-22', today=date(2026, 11, 23)), '已结束')
        self.assertEqual(display_status('已结束', '2027-11-22', today=date(2026, 11, 1)), '已结束')
        for deadline in ('11月22日', '2026-02-30', '2026-10-20 至 2026-11-22', '待定'):
            self.assertEqual(display_status('报名中', deadline, today=date(2026, 12, 1)), '报名中')

    def test_verified_curated_aging_is_read_only_and_fingerprint_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'c.db'
            card = ContestCard(name='测试挑战赛', source_url='https://example.test/aging',
                               contest_name='测试挑战赛', status='报名中', deadline='2026-11-22',
                               curated=True, luna_state='verified')
            store.upsert_contests([reviewed(card)], path)
            original = store.get_contest(card.id, path, include_drafts=True)
            before = store.record_fingerprint(original)
            with patch('saishi_tong.classify.current_date', return_value=date(2026, 11, 23)):
                self.assertIsNone(store.get_contest(card.id, path))
                self.assertEqual(store.get_contest(card.id, path, include_drafts=True)['status'], '报名中')
                self.assertEqual(store.search_contests(status=['报名中', '即将截止'], path=path), [])
                self.assertEqual(store.search_contests(status='已结束', path=path), [])
                self.assertEqual(store.record_fingerprint(store.get_contest(card.id, path, include_drafts=True)), before)
