"""Review/visibility and wrongful-auto-merge regressions; isolated fixtures only."""
from contextlib import closing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saishi_tong import curation, store
from saishi_tong.parse import ContestCard
from saishi_tong.verification import describe
from test_openclaw_curation import call, ADMIN
import test_openclaw_curation as tc


class ReviewStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'c.db'
        env = patch.dict(os.environ, {'SAISHI_DB_PATH': str(self.path), 'SAISHI_ADMIN_TOKEN': ADMIN})
        env.start(); self.addCleanup(env.stop)
        from saishi_tong.web.app import app
        self.app = app

    def card(self, key='one', **extra):
        data = dict(name='示例黑客松', contest_name='示例黑客松',
                    source_url='https://example.test/' + key,
                    search_time='2026-10-23T00:00:00Z', deadline='2026-11-22',
                    status='报名中', format='线上', curated=True)
        data.update(extra)
        card = ContestCard(**data)
        store.upsert_contests([card])
        return store.get_contest(card.id, include_drafts=True)

    def test_manual_review_groups_and_public_catalog_without_model(self):
        card = self.card()
        marked = store.save_admin_contest({'human_reviewed': True}, card['id'])
        self.assertEqual(marked['verification_state'], 'manual')
        self.assertEqual(marked['luna_state'], 'pending')
        self.assertEqual(marked['luna_model'], '')
        self.assertTrue(marked['human_reviewed_at'])
        self.assertEqual(store.list_admin_contests(verification='verified')['total'], 1)
        self.assertEqual(store.list_admin_contests(verification='unverified')['total'], 0)
        self.assertEqual(store.count_contests(), 1)
        self.assertEqual(store.list_contests()[0]['id'], card['id'])
        self.assertEqual(store.search_contests(q='示例')[0]['verification_label'], '人工校对')
        code, response = call(self.app, '/api/contests', token=None)
        self.assertEqual(code, 200)
        self.assertIn('人工校对', str(response))

    def test_ai_only_requires_human_and_is_prioritized_without_auto_approval(self):
        plain = self.card('plain', search_time='2099-01-01')
        ai = self.card('ai', luna_state='verified', luna_model='synthetic-model')
        self.assertIsNone(store.get_contest(ai['id']))
        listing = store.list_admin_contests(verification='awaiting')
        self.assertEqual(listing['contests'][0]['id'], ai['id'])
        self.assertEqual(listing['counts'], {'verified': 0, 'awaiting': 1, 'unverified': 1})
        self.assertEqual(listing['contests'][0]['verification_label'], 'AI核验完成·待人工确认')
        self.assertFalse(listing['contests'][0]['human_reviewed'])
        j = curation.create_job(scope='unverified')
        self.assertEqual(j['items'][0]['contest_id'], plain['id'])

    def test_ai_and_manual_combined_and_noop_save_publish(self):
        card = self.card(luna_state='verified', luna_model='synthetic-model', luna_verified_at='2026-10-23T00:00:00Z')
        marked = store.save_admin_contest({'human_reviewed': True}, card['id'])
        self.assertEqual(marked['verification_state'], 'ai_manual')
        for payload in ({'publication_state': 'published'}, {'contest_name': marked['contest_name'], 'sources': marked['sources']}):
            result = store.save_admin_contest(payload, card['id'])
            self.assertEqual(result['verification_state'], 'ai_manual')
            self.assertEqual(result['human_reviewed_at'], marked['human_reviewed_at'])
        self.assertEqual(store.list_admin_contests(verification='verified')['counts'], {'verified': 1, 'awaiting': 0, 'unverified': 0})

    def test_review_is_explicit_and_cannot_forge_ai_metadata(self):
        card = self.card()
        self.assertFalse(store.save_admin_contest({'publication_state': 'published'}, card['id'])['human_reviewed'])
        endpoint = '/api/admin/contests/' + card['id']
        self.assertEqual(call(self.app, endpoint, 'PUT', {'human_reviewed': True}, token=None)[0], 401)
        for body in ({'human_reviewed': 'true'}, {'luna_state': 'verified'}, {'human_reviewed_at': 'forged'}):
            self.assertEqual(call(self.app, endpoint, 'PUT', body)[0], 422)
        self.assertEqual(call(self.app, endpoint, 'PUT', {'human_reviewed': True, 'expected_fingerprint': store.get_contest(card['id'],include_drafts=True)['record_fingerprint']})[1]['contest']['verification_state'], 'manual')
        self.assertEqual(store.get_contest(card['id'])['luna_model'], '')

    def test_edit_invalidates_current_ai_and_manual_until_reaffirmed(self):
        card = self.card(luna_state='verified', luna_model='synthetic-model', luna_verified_at='2026-10-23T00:00:00Z')
        store.save_admin_contest({'human_reviewed': True}, card['id'])
        changed = store.save_admin_contest({'deadline': '2026-11-23'}, card['id'])
        self.assertEqual(changed['luna_state'], 'stale')
        self.assertFalse(changed['human_reviewed'])
        self.assertEqual(changed['luna_model'], 'synthetic-model')
        self.assertIsNone(store.get_contest(card['id']))
        marked = store.save_admin_contest({'human_reviewed': True}, card['id'])
        self.assertEqual(marked['verification_state'], 'manual')
        cleared = store.save_admin_contest({'human_reviewed': False}, card['id'])
        self.assertFalse(cleared['human_reviewed'])

    def test_drafts_stay_private_after_manual_review_and_batch_skips_checked(self):
        card = self.card(publication_state='draft')
        store.save_admin_contest({'human_reviewed': True}, card['id'])
        self.assertIsNone(store.get_contest(card['id']))
        self.assertEqual(store.list_admin_contests(verification='verified')['total'], 1)
        with self.assertRaises(curation.CurationError): curation.create_job(scope='unverified')

    def test_distinct_events_sharing_listing_are_not_duplicate_identity(self):
        listing = {'url': 'https://modelscope.cn/events', 'title': '赛事列表', 'excerpt': '多场赛事'}
        first = self.card('first', contest_name='一万个回答·AI文化造物黑客松', sources=[listing], luna_state='verified')
        second = self.card('second', contest_name='群核科技 Aholo 全球3D AI黑客松', sources=[listing])
        self.assertEqual(store.find_duplicate_contests(second), [])
        self.assertIsNone(store.get_contest(first['id']))
        self.assertEqual(store.get_contest(first['id'],include_drafts=True)['luna_state'], 'verified')

    def test_sources_only_merge_preserves_verified_facts_and_draft_state(self):
        a = self.card('a', luna_state='verified', publication_state='draft')
        b = self.card('b', eligibility='尚未核实的新资格', luna_state='review')
        result = store.merge_contests(a['id'], [b['id']], sources_only=True,
            expected_fingerprints={c['id']: store.record_fingerprint(c) for c in (a,b)})
        self.assertEqual(result['contest']['luna_state'], 'verified')
        self.assertEqual(result['contest']['eligibility'], '')
        self.assertEqual(result['contest']['publication_state'], 'draft')

    def test_keep_separate_blocks_future_jobs_and_inverse_merge(self):
        a, b = self.card('a', luna_state='verified'), self.card('b')
        j = curation.create_job(scope='selected', contest_ids=[b['id']])
        claimed = curation.claim_job()
        r = tc.CurationTests.result(self)
        r['merge_with'] = [a['id']]
        curation.commit_result(j['id'], b['id'], claimed['items'][0]['fingerprint'], r, model='synthetic-model')
        curation.keep_separate(j['id'], b['id'])
        curation.finish_job(j['id'])
        self.assertEqual(store.find_duplicate_contests(store.get_contest(a['id'],include_drafts=True)), [])
        next_job = curation.create_job(scope='selected', contest_ids=[b['id']])
        claimed = curation.claim_job()
        out = curation.commit_result(next_job['id'], b['id'], claimed['items'][0]['fingerprint'], r, model='synthetic-model')
        self.assertFalse(out.get('auto_merged'))
        self.assertEqual(out['merge_ids'], [])
        a, b = [store.get_contest(c['id'], include_drafts=True) for c in (a,b)]
        with self.assertRaisesRegex(ValueError, 'records_kept_separate'):
            store.merge_contests(a['id'],[b['id']],expected_fingerprints={c['id']:store.record_fingerprint(c) for c in (a,b)})

    def test_worker_field_changes_clear_manual_review_without_fabrication(self):
        card = self.card()
        store.save_admin_contest({'human_reviewed': True}, card['id'])
        j = curation.create_job(scope='selected', contest_ids=[card['id']])
        c = curation.claim_job()
        curation.commit_result(j['id'], card['id'], c['items'][0]['fingerprint'], tc.CurationTests.result(self), model='synthetic-model')
        updated = store.get_contest(card['id'], include_drafts=True)
        self.assertEqual(updated['verification_state'], 'ai')
        self.assertFalse(updated['human_reviewed'])

    def test_review_marker_cannot_survive_unchecked_fact_write(self):
        card = self.card()
        store.save_admin_contest({'human_reviewed': True}, card['id'])
        record = store._as_card(store.get_contest(card['id'],include_drafts=True))
        record.organizer = '不同事实'
        with closing(store._connect()) as conn, conn: store._write(conn,record)
        self.assertFalse(store.get_contest(card['id'],include_drafts=True)['human_reviewed'])

    def test_legacy_stale_review_is_not_public_or_counted_as_verified(self):
        card = self.card()
        store.save_admin_contest({'human_reviewed': True}, card['id'])
        self.assertIsNotNone(store.get_contest(card['id']))
        # Simulate an older import/direct SQLite edit bypassing the normal writer.
        with closing(store._connect()) as conn, conn:
            conn.execute('UPDATE contests SET organizer=? WHERE id=?', ('变更后的主办方', card['id']))
        self.assertEqual(store.search_contests(), [])
        self.assertIsNone(store.get_contest(card['id']))
        self.assertEqual(store.list_admin_contests(verification='verified')['total'], 0)
        pending = store.list_admin_contests(verification='unverified')
        self.assertEqual(pending['total'], 1)
        self.assertFalse(pending['contests'][0]['human_reviewed'])
        self.assertEqual(curation.create_job(scope='unverified')['items'][0]['contest_id'], card['id'])

    def test_human_confirmation_rejects_stale_or_missing_snapshot(self):
        card = self.card()
        url = '/api/admin/contests/' + card['id']
        self.assertEqual(call(self.app,url,'PUT',{'human_reviewed': True})[0],409)
        store.save_admin_contest({'organizer': '后台新事实'},card['id'])
        self.assertEqual(call(self.app,url,'PUT',{'human_reviewed': True,
                         'expected_fingerprint': card['record_fingerprint']})[0],409)
        self.assertFalse(store.get_contest(card['id'],include_drafts=True)['human_reviewed'])

    def test_three_groups_distinguish_parsing_completed_ai_and_human(self):
        parsed=self.card('parsed',luna_state='review',record_origin='source_intake',luna_model='synthetic-model',luna_verified_at='2026-10-24T00:00:00Z')
        ai=self.card('processed',luna_state='review',ai_reviewed_at='2026-10-24T00:00:00Z')
        manual=self.card('manual')
        store.save_admin_contest({'human_reviewed':True},manual['id'])
        counts=store.list_admin_contests()['counts']
        self.assertEqual(counts,{'unverified':1,'awaiting':1,'verified':1})
        self.assertEqual(store.list_admin_contests(verification='awaiting')['contests'][0]['id'],ai['id'])
        self.assertEqual(store.list_admin_contests(verification='unverified')['contests'][0]['id'],parsed['id'])
        self.assertEqual(store.count_contests(),1)

    def test_completed_needs_review_moves_to_manual_queue_and_edit_invalidates_it(self):
        card=self.card('needs-review')
        job=curation.create_job(scope='selected',contest_ids=[card['id']]);claimed=curation.claim_job()
        result=tc.CurationTests.result(self);result['verdict']='needs_review'
        curation.commit_result(job['id'],card['id'],claimed['items'][0]['fingerprint'],result,model='synthetic-model')
        self.assertEqual(store.list_admin_contests(verification='awaiting')['total'],1)
        self.assertIsNone(store.get_contest(card['id']))
        marked=store.save_admin_contest({'human_reviewed':True},card['id'])
        self.assertEqual(marked['verification_state'],'ai_manual')
        changed=store.save_admin_contest({'deadline':'2026-12-01'},card['id'])
        self.assertEqual(changed['luna_state'],'stale')
        self.assertEqual(changed['ai_reviewed_at'],'')
        self.assertFalse(changed['human_reviewed'])
