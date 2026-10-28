from _review_fixture import reviewed
"""Synthetic credentials only; never read the owner's real key file."""
import asyncio,importlib,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor
from saishi_tong import client

class CredentialTests(unittest.TestCase):
    def test_explicit_pair_wins_and_partial_never_falls_back(self):
        with patch.dict(os.environ,{client.ENV_ID:'server-id',client.ENV_KEY:'server-key'}), patch('tencentcloud.common.credential.Credential') as credential, patch('tencentcloud.wsa.v20250508.wsa_client.WsaClient',return_value='synthetic-client'):
            before=dict(os.environ)
            self.assertEqual(client.get_client(secret_id='user-id',secret_key='user-key'),'synthetic-client')
            credential.assert_called_with('user-id','user-key')
            client.get_client();credential.assert_called_with('server-id','server-key')
            for pair in [('user-id',None),(None,'user-key'),('','')]:
                with self.assertRaises(client.MissingCredentials):client.get_client(secret_id=pair[0],secret_key=pair[1])
            self.assertEqual(dict(os.environ),before)

    def test_concurrent_pairs_do_not_mutate_defaults(self):
        with patch.dict(os.environ,{client.ENV_ID:'server-id',client.ENV_KEY:'server-key'}), patch('tencentcloud.common.credential.Credential') as credential, patch('tencentcloud.wsa.v20250508.wsa_client.WsaClient'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda n:client.get_client(secret_id='id'+str(n),secret_key='key'+str(n)),[1,2]))
            self.assertEqual({tuple(c.args) for c in credential.call_args_list},{('id1','key1'),('id2','key2')})
            self.assertEqual(os.environ[client.ENV_ID],'server-id')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'SAISHI_DB_PATH':str(Path(self.temp.name)/'contests.db'),'SAISHI_ADMIN_TOKEN':'synthetic-admin'})
        self.env.start()
        self.web=importlib.import_module('saishi_tong.web.app')
    def tearDown(self):self.env.stop();self.temp.cleanup()

    async def request(self,path='/api/search',fields=None,extra_headers=(),method='POST'):
        body=urlencode(fields or {}, doseq=True).encode()
        messages=[]
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'','root_path':'','headers':[(b'content-type',b'application/x-www-form-urlencoded'),*extra_headers],'client':('127.0.0.1',1),'server':('test',80)}
        async def receive():return {'type':'http.request','body':body,'more_body':False}
        async def send(message):messages.append(message)
        await self.web.app(scope,receive,send)
        start=next(x for x in messages if x['type']=='http.response.start')
        return start['status'],b''.join(x.get('body',b'') for x in messages),dict(start['headers'])

    def test_web_ignores_browser_credentials_and_uses_local_database(self):
        from saishi_tong.parse import ContestCard
        from saishi_tong import service,store
        store.upsert_contests([reviewed(ContestCard(name='本地赛事',status='报名中',luna_state='verified',source_url='https://example.test/local',search_time='2026-10-21T12:00:00Z',deadline='2099-11-22'))])
        variants=[({'q':'本地','TENCENTCLOUD_SECRET_ID':'synthetic-user-id','TENCENTCLOUD_SECRET_KEY':'synthetic-user-key'},[]),
                  ({'q':'本地'},[(b'x-tencent-secret-id',b'header-id'),(b'x-tencent-secret-key',b'header-key')]),
                  ({'q':'本地','TENCENTCLOUD_SECRET_ID':'partial'},[]),
                  ({'q':'本地','TENCENTCLOUD_SECRET_ID':['one','two'],'TENCENTCLOUD_SECRET_KEY':'key'},[])]
        with patch.object(service,'run_search',side_effect=AssertionError('web must stay local')),patch.object(client,'search_many',side_effect=AssertionError('web must stay local')):
            for fields,extra_headers in variants:
                code,body,headers=asyncio.run(self.request(fields=fields,extra_headers=extra_headers))
                self.assertEqual(code,200)
                self.assertNotIn(b'synthetic-user',body)
                self.assertNotIn(b'header-key',body)
                self.assertEqual(headers[b'cache-control'],b'no-store')
                import json
                self.assertEqual(json.loads(body)['total'],1)
                self.assertEqual(json.loads(body)['search_mode'],'local')

    def test_obsolete_query_count_and_credentials_are_ignored(self):
        code,body,_=asyncio.run(self.request(fields={'queries':'synthetic-sensitive-invalid-int','TENCENTCLOUD_SECRET_ID':'user-id','TENCENTCLOUD_SECRET_KEY':'user-key'}))
        self.assertEqual(code,200)
        self.assertNotIn(b'synthetic-sensitive',body)
        self.assertNotIn(b'user-key',body)

    def test_admin_page_does_not_authorize_api(self):
        for headers in [[],[(b'authorization',b'Bearer wrong')],[(b'authorization',b'Bearer synthetic-admin')]]:
            code,body,response_headers=asyncio.run(self.request(path='/admin',method='GET',extra_headers=headers))
            self.assertEqual(code,200)
            self.assertIn(b'id="admin-login-panel"',body)
            self.assertNotIn(b'synthetic-admin',body)
        code,body,_=asyncio.run(self.request(path='/api/admin/contests',method='GET'))
        self.assertEqual(code,401)
        with patch.dict(os.environ,{'SAISHI_ADMIN_TOKEN':''}):
            code,body,_=asyncio.run(self.request(path='/api/admin/contests',method='GET'))
            self.assertEqual(code,503)

if __name__=='__main__':unittest.main()
