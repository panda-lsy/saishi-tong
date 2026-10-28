"""Recoverable deletion and source-backed offline geography; isolated fixtures."""
from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from saishi_tong import store,curation,online_search
from saishi_tong.parse import ContestCard
from saishi_tong.verification import facts_hash
from tests._browser import Browser
from test_admin_session import TOKEN


class DeleteLocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'c.db'
        env=patch.dict(os.environ,{'SAISHI_DB_PATH':str(self.path),'SAISHI_ADMIN_TOKEN':TOKEN})
        env.start();self.addCleanup(env.stop)
        from saishi_tong.web.app import app
        self.browser=Browser(app)

    def card(self,key='a',**changes):
        c=ContestCard(name='线下开发赛'+key,contest_name='线下开发赛'+key,
                      source_url='https://example.test/'+key,search_time='2026-10-24T00:00:00Z',
                      curated=True,format='线下',status='报名中',location_scope='未知',deadline='2099-11-22')
        for k,v in changes.items():setattr(c,k,v)
        store.upsert_contests([c]);store.save_admin_contest({'human_reviewed':True},c.id)
        return store.get_contest(c.id,include_drafts=True)

    def test_delete_hides_every_surface_restore_preserves_all_facts(self):
        original=self.card()
        result=store.change_deleted_state(original['id'],original['record_fingerprint'])
        self.assertTrue(result['deleted_at'])
        self.assertIsNone(store.get_contest(original['id'],include_drafts=True))
        self.assertEqual(store.list_contests(),[]);self.assertEqual(store.search_contests(),[])
        self.assertEqual(store.list_admin_contests()['total'],0);self.assertEqual(store.count_contests(include_drafts=True),0)
        trash=store.list_deleted_contests()[0]
        self.assertEqual(trash['source_url'],original['source_url']);self.assertEqual(trash['search_time'],original['search_time'])
        store.change_deleted_state(original['id'],trash['record_fingerprint'],restore=True)
        self.assertEqual(store.get_contest(original['id'],include_drafts=True),original)
        with closing(store._connect()) as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT action FROM contest_deletion_audit ORDER BY id')],['delete','restore'])

    def test_cookie_csrf_delete_and_restore_and_stale_guard(self):
        original=self.card();url='/api/admin/contests/'+original['id'];body={'expected_fingerprint':original['record_fingerprint']}
        self.assertEqual(self.browser.request(url,'DELETE',body)[0],401)
        raw=self.browser.request('/api/admin/login','POST',{'token':TOKEN})[2]
        csrf={'x-csrf-token':json.loads(raw)['csrf_token']}
        self.assertEqual(self.browser.request(url,'DELETE',body)[0],403)
        self.assertEqual(self.browser.request(url,'DELETE',{'expected_fingerprint':'0'*64},csrf)[0],409)
        self.assertEqual(self.browser.request(url,'DELETE',body,csrf)[0],200)
        self.assertEqual(self.browser.request(url,'GET')[0],404)
        trash=json.loads(self.browser.request('/api/admin/trash')[2])['contests'][0]
        self.assertEqual(self.browser.request(url+'/restore','POST',body,csrf)[0],409)
        self.assertEqual(self.browser.request(url+'/restore','POST',{'expected_fingerprint':trash['record_fingerprint']},csrf)[0],200)

    def test_worker_archives_ended_rows_recoverably_and_keeps_current_rows(self):
        ended = self.card('expired', deadline='2026-10-25')
        current = self.card('current', deadline='2026-11-30')
        ended_id = ended['id']; current_id = current['id']
        with patch('saishi_tong.classify.current_date', return_value=__import__('datetime').date(2026, 10, 26)):
            self.assertEqual(store.archive_ended(path=self.path), [ended_id])
            self.assertIsNone(store.get_contest(ended_id, path=self.path))
            self.assertEqual(next(row for row in store.list_deleted_contests(self.path) if row['id'] == ended_id)['deadline'], '2026-10-25')
            self.assertEqual(store.get_contest(current_id, path=self.path)['deadline'], '2026-11-30')
        with closing(store._connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT action FROM contest_deletion_audit WHERE contest_id=?", (ended_id,)).fetchone()[0], 'auto_expire')

    def test_deleted_record_cannot_be_reimported_or_curated(self):
        original=self.card();store.change_deleted_state(original['id'],original['record_fingerprint'])
        incoming=store._as_card(original);incoming.summary='不得覆盖';incoming.id=''
        store.upsert_contests([incoming])
        self.assertEqual(store.list_deleted_contests()[0]['summary'],original['summary'])
        self.assertEqual(online_search.import_admin_results([store._as_card(original)])['skipped'],1)
        with self.assertRaises(curation.CurationError):curation.create_job(scope='selected',contest_ids=[original['id']])

    def test_offline_region_multi_selection_and_unknown_are_explicit(self):
        cn=self.card('cn',location_scope='国内')
        foreign=self.card('foreign',format='混合',location_scope='国外')
        unknown=self.card('unknown')
        pure=self.card('online',format='线上',location_scope='国内')
        self.assertEqual([r['id'] for r in store.search_contests(format='线下',location='国内')],[cn['id']])
        self.assertEqual({r['id'] for r in store.search_contests(format='线下',location=['国内','国外'])},{cn['id'],foreign['id']})
        self.assertEqual([r['id'] for r in store.search_contests(format=['线上','线下'],location='未知')],[unknown['id']])
        with self.assertRaises(ValueError):store.search_contests(format='线上',location='国内')
        with self.assertRaises(ValueError):store.search_contests(format='线下',location='月球')

    def test_geography_form_api_and_remove_link_preserve_other_filters(self):
        self.card('cn',location_scope='国内');self.card('foreign',location_scope='国外')
        query=urlencode([('q','开发赛'),('status','报名中'),('format','线下'),('location','国内'),('location','国外')])
        code,_,body=self.browser.request('/api/contests?'+query)
        self.assertEqual(code,200);data=json.loads(body);self.assertEqual(data['total'],2);self.assertEqual(data['locations'],['国内','国外'])
        code,_,html=self.browser.request('/contests?'+query)
        self.assertEqual(code,200);self.assertIn('移除地区筛选：国内',html.decode())
        self.assertIn('location=%E5%9B%BD%E5%A4%96',html.decode())
        code,_,body=self.browser.request('/api/contests','POST',{'format':['线下'],'location':['国内']})
        self.assertEqual(json.loads(body)['total'],1)
        self.assertEqual(self.browser.request('/api/contests?location='+urlencode({'x':'国内'})[2:])[0],422)

    def test_additive_geography_migration_preserves_valid_human_confirmation(self):
        c=ContestCard(name='已人工确认的旧赛',id='old',contest_name='已人工确认的旧赛',source_url='https://example.test/old',
                      status='报名中',curated=True,search_time='2026-10-24T00:00:00Z',deadline='2099-11-22',human_reviewed_at='2026-10-23T00:00:00Z')
        c.human_reviewed_hash=facts_hash(c,include_location=False)
        old_columns=[n for n in store.COLUMNS if n not in {'location_scope','deleted_at','ai_reviewed_at'}]
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute('CREATE TABLE contests ('+', '.join(store._column_sql(n) for n in old_columns)+')')
            values=asdict(c)
            conn.execute('INSERT INTO contests ('+','.join(old_columns)+') VALUES ('+','.join('?' for _ in old_columns)+')',[store._encode(n,values[n]) for n in old_columns])
        upgraded=store.get_contest('old')
        self.assertTrue(upgraded['human_reviewed']);self.assertEqual(upgraded['human_reviewed_at'],c.human_reviewed_at)
        self.assertEqual(upgraded['location_scope'],'未知')
