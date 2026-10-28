"""Browser-signed online boundary: synthetic signed envelopes and provider responses."""
from contextlib import closing
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from saishi_tong import online_search as online,store
from saishi_tong.parse import ContestCard
from tests._browser import Browser
from test_admin_session import TOKEN

IDENTITY='AKIDsyntheticBrowserOnly00000000'


def envelope(query='中文黑客松',stamp=None):
    stamp=int(time.time()) if stamp is None else stamp
    day=datetime.fromtimestamp(stamp,timezone.utc).date().isoformat()
    return {'payload':json.dumps({'Query':query,'Cnt':10,'Mode':0,'Freshness':'m3'},ensure_ascii=False),
            'timestamp':stamp,'authorization':f'TC3-HMAC-SHA256 Credential={IDENTITY}/{day}/wsa/tc3_request, SignedHeaders=content-type;host;x-tc-action, Signature='+ 'a'*64}


def response():
    return {'Pages':[json.dumps({'title':'2026 示例黑客松报名','url':'https://example.test/online',
              'passage':'主办：测试基金会。正在报名，报名截止2026-11-22。线上举办。'},ensure_ascii=False)]}


class OnlineSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'c.db'
        env=patch.dict(os.environ,{'SAISHI_DB_PATH':str(self.path),'SAISHI_ADMIN_TOKEN':TOKEN})
        env.start();self.addCleanup(env.stop)
        from saishi_tong.web.app import app
        self.browser=Browser(app)
        from saishi_tong.web.online_api import OnlineLimits
        app.state.online_limits=OnlineLimits()
        self.app=app

    def test_anonymous_online_results_are_private_without_any_database_write(self):
        with patch.object(online,'upstream',return_value=response()),patch.object(store,'_connect',side_effect=AssertionError('guest online must not access DB')):
            code,headers,raw=self.browser.request('/api/online-search','POST',envelope())
        self.assertEqual(code,200)
        data=json.loads(raw)
        self.assertEqual(data['scope'],'private_preview');self.assertIsNone(data['admin_import'])
        self.assertEqual(data['total'],1)
        card=data['cards'][0]
        self.assertEqual(card['luna_state'],'pending');self.assertEqual(card['publication_state'],'draft')
        self.assertTrue(card['source_url'] and card['search_time'])
        self.assertNotIn(IDENTITY,raw.decode());self.assertFalse(self.path.exists())
        self.assertIn('no-store',headers['cache-control'])

    def test_admin_cookie_csrf_and_private_pending_import(self):
        code,_,raw=self.browser.request('/api/admin/login','POST',{'token':TOKEN})
        self.assertEqual(code,200);csrf={'x-csrf-token':json.loads(raw)['csrf_token']}
        with patch.object(online,'upstream',return_value=response()) as upstream:
            self.assertEqual(self.browser.request('/api/online-search','POST',envelope())[0],403)
            upstream.assert_not_called()
            code,_,raw=self.browser.request('/api/online-search','POST',envelope(),csrf)
        self.assertEqual(code,200);data=json.loads(raw)
        self.assertEqual(data['admin_import']['imported'],1)
        card=store.list_admin_contests(verification='unverified')['contests'][0]
        self.assertEqual(card['luna_state'],'pending');self.assertFalse(card['human_reviewed'])
        self.assertEqual(store.list_admin_contests(verification='awaiting')['total'],0)
        self.assertEqual(store.list_contests(),[])

    def test_same_origin_required_before_provider_call(self):
        with patch.object(online,'upstream') as upstream:
            self.assertEqual(self.browser.request('/api/online-search','POST',envelope(),{'origin':'https://evil.test'})[0],403)
            upstream.assert_not_called()

    def test_explicit_signature_only_and_static_errors(self):
        bad=[{**envelope(),'SecretKey':'sensitive-fixture'}, {**envelope(),'endpoint':'https://example.test'},
             {**envelope(),'timestamp':True},envelope(stamp=int(time.time())-600),
             {**envelope(),'authorization':'invalid-sensitive-fixture'},envelope(query=IDENTITY)]
        for body in bad:
            with self.subTest(body_keys=list(body)),patch.object(online,'upstream') as upstream:
                code,_,raw=self.browser.request('/api/online-search','POST',body)
                self.assertEqual(code,422);upstream.assert_not_called()
                self.assertNotIn('sensitive-fixture',raw.decode());self.assertNotIn(IDENTITY,raw.decode())

    def test_signed_body_constraints(self):
        for changes in [{'Cnt':50},{'Mode':2},{'Freshness':'y2'},{'Query':''},{'Query':'a'*301},{'SecretKey':'never-transmit'}]:
            body=envelope();payload=json.loads(body['payload']);payload.update(changes);body['payload']=json.dumps(payload)
            with self.assertRaises(online.OnlineError):online.validate_envelope(body)

    def test_lite_signature_omits_unsupported_mode_and_count(self):
        body=envelope();payload=json.loads(body['payload']);payload.pop('Cnt');payload.pop('Mode')
        body['payload']=json.dumps(payload)
        self.assertEqual(online.validate_envelope(body)[0],body['payload'])
        for field,value in [('Cnt',10),('Mode',0)]:
            partial={**body,'payload':json.dumps({**payload,field:value})}
            with self.assertRaises(online.OnlineError):online.validate_envelope(partial)

    def test_provider_receives_exact_signed_bytes_and_fixed_target(self):
        recorded=[]
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps({'Response':response()}).encode()
        class Opener:
            def open(self,request,timeout):recorded.append(request);return Reply()
        body=envelope()
        with patch('urllib.request.build_opener',return_value=Opener()):online.search(body)
        sent=recorded[0]
        self.assertEqual(sent.full_url,'https://wsa.tencentcloudapi.com/')
        self.assertEqual(sent.data,body['payload'].encode())
        self.assertEqual(sent.get_header('Authorization'),body['authorization'])
        self.assertEqual(sent.get_header('X-tc-action'),'SearchPro')
        self.assertEqual(sent.get_header('X-tc-version'),'2025-05-08')

    def test_provider_failure_is_not_success_or_a_draft(self):
        with patch.object(online,'upstream',side_effect=online.OnlineError('search_auth_failed')):
            code,_,raw=self.browser.request('/api/online-search','POST',envelope())
        self.assertEqual(code,502);self.assertIn('密钥',raw.decode())
        self.assertFalse(self.path.exists())

    def test_official_error_codes_are_distinct_and_raw_messages_are_never_reflected(self):
        request_id='11111111-2222-3333-4444-555555555555'
        for error,expected in [('ResourceNotFound','尚未开通'),('ResourceUnavailable','资源当前不可用'),
                               ('RequestLimitExceeded','请求频率受限'),('InvalidParameter','未接受本次参数')]:
            class Reply:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self,n):return json.dumps({'Response':{'Error':{'Code':error,'Message':IDENTITY},'RequestId':request_id}}).encode()
            class Opener:
                def open(self,request,timeout):return Reply()
            with self.subTest(error=error),patch('urllib.request.build_opener',return_value=Opener()):
                code,_,raw=self.browser.request('/api/online-search','POST',envelope())
                self.assertEqual(code,502)
                self.assertIn(expected,raw.decode());self.assertIn(request_id,raw.decode())
                self.assertNotIn(IDENTITY,raw.decode())
        self.assertFalse(self.path.exists())

    def test_single_site_filters_authoritative_results_outside_domain_before_import(self):
        urls=['https://csdn.net/a','https://blog.csdn.net/b',
              'https://csdn.net.attacker.example/c','https://news.example/d']
        pages=[{'title':f'2026 星河开发者黑客松{i}','url':url,
                'passage':'正在报名，报名截止2026-11-20。线上开发技术比赛。'} for i,url in enumerate(urls)]
        body=envelope();payload=json.loads(body['payload']);payload['Site']='csdn.net';body['payload']=json.dumps(payload)
        with patch('saishi_tong.classify.current_date',return_value=__import__('datetime').date(2026,10,27)), \
             patch.object(online,'upstream',return_value={'Pages':pages,'Version':'flagship'}):
            result=online.search_results(body)
        self.assertEqual({c.source_url for c in result['cards']},set(urls[:2]))
        self.assertEqual({x['source_url'] for x in result['excluded']},set(urls[2:]))
        self.assertTrue(all(x['kind']=='来源站点不符' for x in result['excluded']))
        self.assertEqual(result['service_version'],'flagship')
        self.assertEqual(result['counts']['returned'],4)
        self.assertEqual(result['counts']['excluded'],2)

    def test_admin_single_site_and_uncertain_scope_import_only_private_drafts(self):
        self.browser.request('/api/admin/login','POST',{'token':TOKEN})
        _,_,session=self.browser.request('/api/admin/session')
        csrf={'x-csrf-token':json.loads(session)['csrf_token']}
        pages=[{'title':'2026 开发者黑客松','url':'https://blog.csdn.net/test/code',
                'passage':'正在报名，报名截止2026-11-20，提交代码完成软件开发。'},
               {'title':'2026 AI视频创作大赛','url':'https://csdn.net/test/video',
                'passage':'正在报名，报名截止2026-11-20，提交AI生成短视频。'},
               {'title':'2026 开发者黑客松','url':'https://csdn.net.attacker.example/event',
                'passage':'正在报名，报名截止2026-11-20，提交代码。'}]
        body=envelope();payload=json.loads(body['payload']);payload['Site']='csdn.net';body['payload']=json.dumps(payload)
        with patch('saishi_tong.classify.current_date',return_value=__import__('datetime').date(2026,10,27)), \
             patch.object(online,'upstream',return_value={'Pages':pages}):
            code,_,raw=self.browser.request('/api/online-search','POST',body,csrf)
        self.assertEqual(code,200);result=json.loads(raw)
        self.assertEqual(result['admin_import']['imported'],2)
        self.assertEqual(result['counts']['pending'],1)
        self.assertEqual(result['counts']['excluded'],1)
        self.assertEqual([c['source_url'] for c in result['cards']],['https://blog.csdn.net/test/code'])
        drafts=store.list_admin_contests(verification='unverified')['contests']
        self.assertEqual({c['source_url'] for c in drafts}, {pages[0]['url'], pages[1]['url']})
        self.assertTrue(all(c['publication_state']=='draft' and not c['human_reviewed'] for c in drafts))
        self.assertEqual(store.search_contests(),[])

    def test_unverified_ai_creative_event_stays_private_without_technical_task(self):
        pages=[{'title':'2026 AI 视频创作大赛','url':'https://example.test/video',
                'passage':'正在报名，报名截止2026-11-20，作品为AI生成短视频。'},
               {'title':'2026 开发者黑客松','url':'https://example.test/code',
                'passage':'正在报名，报名截止2026-11-20，提交代码完成软件开发。'}]
        with patch('saishi_tong.classify.current_date',return_value=__import__('datetime').date(2026,10,27)), \
             patch.object(online,'upstream',return_value={'Pages':pages}):
            result=online.search_results(envelope())
        self.assertEqual([c.source_url for c in result['cards']],['https://example.test/code'])
        self.assertEqual([c.source_url for c in result['pending_cards']],['https://example.test/video'])
        self.assertIn('技术参赛任务尚不明确',result['pending_cards'][0].verification_notes[-1])

    def test_provider_null_pages_means_empty_success_without_guest_database_write(self):
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return b'{"Response":{"Pages":null,"RequestId":"00000000-0000-0000-0000-000000000000"}}'
        class Opener:
            def open(self,request,timeout):return Reply()
        with patch('urllib.request.build_opener',return_value=Opener()),patch.object(store,'_connect',side_effect=AssertionError('empty visitor search must not write DB')):
            code,_,raw=self.browser.request('/api/online-search','POST',envelope())
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)['total'],0)
        self.assertFalse(self.path.exists())

    def test_result_redaction_and_unsafe_source_rejection(self):
        data={'Pages':[{'title':IDENTITY,'url':'https://example.test/safe','passage':IDENTITY},
                       {'title':'本机','url':'http://127.0.0.1/'}, {'title':'凭据','url':'https://example.test/?token=sensitive'}]}
        with patch.object(online,'upstream',return_value=data):result=online.search_results(envelope())
        self.assertEqual(len(result['cards']),0);self.assertEqual(len(result['pending_cards']),1)
        self.assertNotIn(IDENTITY,repr(result))

    def test_duplicate_retired_and_existing_records_remain_unchanged(self):
        with patch.object(online,'upstream',return_value=response()):cards=online.search(envelope())
        first=online.import_admin_results(cards)
        old=store.get_contest(first['imported_ids'][0],include_drafts=True)
        self.assertEqual(online.import_admin_results(cards),{'imported_ids':[],'imported':0,'skipped':1})
        self.assertEqual(store.get_contest(old['id'],include_drafts=True),old)
        with closing(store._connect()) as conn,conn:conn.execute("UPDATE contests SET publication_state='retired' WHERE id=?",(old['id'],))
        self.assertEqual(online.import_admin_results(cards)['skipped'],1)
        self.assertEqual(store.get_contest(old['id'],include_drafts=True)['publication_state'],'retired')

    def test_online_relay_rate_and_concurrency_are_bounded(self):
        from saishi_tong.web.online_api import OnlineLimits
        now=[1000];limits=OnlineLimits(clock=lambda:now[0])
        for _ in range(8):limits.enter('fixture');limits.leave()
        with self.assertRaises(HTTPException):limits.enter('fixture')
        now[0]+=61
        limits.enter('fixture');limits.leave()
        for i in range(8):limits.enter('parallel'+str(i))
        with self.assertRaises(HTTPException):limits.enter('other')

    def test_admin_import_excludes_non_contest_and_returns_accounting(self):
        self.browser.request('/api/admin/login','POST',{'token':TOKEN})
        _,_,session=self.browser.request('/api/admin/session')
        csrf={'x-csrf-token':json.loads(session)['csrf_token']}
        pages=response()['Pages']+[{'title':'征文是什么意思','url':'https://example.test/meaning'},
                                   {'title':'本月赛事汇总','url':'https://example.test/list'}]
        with patch.object(online,'upstream',return_value={'Pages':pages}):
            code,_,raw=self.browser.request('/api/online-search','POST',envelope(),csrf)
        self.assertEqual(code,200);data=json.loads(raw)
        self.assertEqual(data['counts']['returned'],3);self.assertEqual(data['counts']['excluded'],2)
        self.assertEqual(data['admin_import']['imported'],1)
        self.assertEqual(store.list_admin_contests(verification='unverified')['total'],1)
        self.assertEqual({c['kind'] for c in data['excluded']},{'非赛事','来源列表'})
        self.assertTrue(all(c['source_url'] and c['search_time'] for c in data['excluded']))

    def test_missing_event_deadline_is_admin_only_pending_not_a_new_result(self):
        page={'title':'2026 星河开发者黑客松','url':'https://example.test/events/star',
              'passage':'面向开发者的线上技术比赛，主办方为星河实验室。'}
        with patch.object(online,'upstream',return_value={'Pages':[page]}):
            code,_,raw=self.browser.request('/api/online-search','POST',envelope())
        self.assertEqual(code,200);guest=json.loads(raw)
        self.assertEqual(guest['total'],0);self.assertEqual(guest['counts']['pending'],1)
        self.assertEqual(guest['cards'],[]);self.assertFalse(self.path.exists())
        self.browser.request('/api/admin/login','POST',{'token':TOKEN})
        _,_,session=self.browser.request('/api/admin/session')
        csrf={'x-csrf-token':json.loads(session)['csrf_token']}
        with patch.object(online,'upstream',return_value={'Pages':[page]}):
            code,_,raw=self.browser.request('/api/online-search','POST',envelope(),csrf)
        admin=json.loads(raw)
        self.assertEqual(admin['total'],0);self.assertEqual(admin['admin_import']['imported'],1)
        draft=store.list_admin_contests(verification='unverified')['contests'][0]
        self.assertEqual(draft['publication_state'],'draft');self.assertIn('时效待核', ' '.join(draft['verification_notes']))

    def test_online_future_opening_stays_private_pending(self):
        page={'title':'2026 星河开发者黑客松','url':'https://example.test/events/not-open',
              'passage':'报名开始时间：2026-11-01。报名截止2026-11-20。线上开发技术比赛。'}
        with patch('saishi_tong.classify.current_date',return_value=__import__('datetime').date(2026,10,27)), \
             patch.object(online,'upstream',return_value={'Pages':[page]}):
            code,_,raw=self.browser.request('/api/online-search','POST',envelope())
        self.assertEqual(code,200)
        result=json.loads(raw)
        self.assertEqual(result['cards'],[])
        self.assertEqual(result['counts']['pending'],1)
        self.assertFalse(self.path.exists())

    def test_future_deadline_alone_does_not_prove_online_registration_is_open(self):
        page={'title':'2026 星河开发者黑客松','url':'https://example.test/events/deadline-only',
              'passage':'报名截止2026-11-20。线上开发技术比赛。'}
        with patch('saishi_tong.classify.current_date',return_value=__import__('datetime').date(2026,10,27)), \
             patch.object(online,'upstream',return_value={'Pages':[page]}):
            code,_,raw=self.browser.request('/api/online-search','POST',envelope())
        self.assertEqual(code,200)
        result=json.loads(raw)
        self.assertEqual(result['cards'],[])
        self.assertEqual(result['counts']['pending'],1)
        self.assertFalse(self.path.exists())
