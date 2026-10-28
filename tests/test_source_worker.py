"""Worker prompt/JSON dispatch and source fetch guards; no real agent or network."""
import argparse
import importlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from saishi_tong import agent_tools


class WorkerSourceTests(unittest.TestCase):
    def test_search_results_preserve_both_provider_excerpts_and_legacy_snippet(self):
        standard = '标准摘要' + '标' * 1300
        dynamic = '动态摘要中的报名截止日期' + '动' * 1300
        response = {'Pages': [json.dumps({'title': '开发者比赛', 'url': 'https://example.test/event',
                                        'passage': standard, 'content': dynamic}),
                              {'title': '极速版结果', 'url': 'https://example.test/lite', 'passage': '仅标准摘要'},
                              {'title': '只有动态摘要', 'url': 'https://example.test/dynamic', 'content': '动态证据'}]}
        pages = agent_tools._pages(response)
        self.assertEqual(pages[0]['snippet'], standard[:1200])
        self.assertEqual(pages[0]['passage'], standard[:1200])
        self.assertEqual(pages[0]['content'], dynamic[:1200])
        self.assertEqual(pages[1]['content'], '')
        self.assertEqual(pages[2]['snippet'], '动态证据')
        self.assertEqual(pages[2]['content'], '动态证据')

    def test_documented_freshness_forms_validate_calendar_bounds(self):
        today = date(2026, 10, 27)
        for value in ['', 'd', 'd1', 'd7', 'd30', 'm', 'm12', 'y', 'y5',
                      '1970-01-01', '2026-10-27', '2026-10-01,2026-10-27',
                      '2026-10-27,2026-10-01']:
            with self.subTest(value=value): self.assertEqual(agent_tools._freshness(value, today=today), value)
        for value in ['w1', 'd0', 'd31', 'm0', 'm13', 'y0', 'y6', 'D7',
                      '1969-12-31', '2026-10-28', '2026-02-30',
                      '2026-1-01', '2026-10-01,2026-10-28', '2026-10-01,', 'd1,m1']:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                agent_tools._freshness(value, today=today)

    def test_invalid_freshness_fails_before_loading_credentials_or_calling_search(self):
        with patch.object(agent_tools, '_load_cam') as load, \
             patch('saishi_tong.client.search_pro') as search, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                agent_tools.searchpro_main(['开发者比赛', '--freshness', 'w1'])
        self.assertEqual(result.exception.code, 2)
        load.assert_not_called()
        search.assert_not_called()

    def test_valid_freshness_is_sent_once_without_rewriting(self):
        response={'Pages': [{'title':'赛事', 'url':'https://example.test/a',
                             'passage':'标准摘要', 'content':'动态摘要'}]}
        output=io.StringIO()
        with patch.object(agent_tools, '_load_cam') as load, \
             patch('saishi_tong.client.search_pro', return_value=response) as search, redirect_stdout(output):
            code=agent_tools.searchpro_main(['开发者比赛', '--freshness', 'd7'])
        self.assertEqual(code, 0)
        load.assert_called_once_with()
        search.assert_called_once_with('开发者比赛', cnt=10, mode=0, freshness='d7')
        self.assertEqual(json.loads(output.getvalue())['results'][0]['content'], '动态摘要')

    def test_lite_mode_is_explicit_and_sends_one_request_without_unsupported_fields(self):
        output=io.StringIO()
        with patch.object(agent_tools, '_load_cam') as load, \
             patch('saishi_tong.client.search_pro', return_value={'Pages': [], 'Version': 'lite'}) as search, \
             redirect_stdout(output):
            code=agent_tools.searchpro_main(['开发者比赛', '--lite', '--freshness', 'd7'])
        self.assertEqual(code,0)
        load.assert_called_once_with()
        search.assert_called_once_with('开发者比赛',cnt=None,mode=None,freshness='d7')
        self.assertEqual(json.loads(output.getvalue())['version'],'lite')

    def test_sdk_serializes_lite_request_without_null_mode_or_count(self):
        from saishi_tong.client import search_pro
        captured=[]
        class FakeClient:
            def SearchPro(self, request):
                captured.append(request._serialize())
                return SimpleNamespace(to_json_string=lambda: json.dumps({'Pages': []}))
        search_pro('示例查询', cnt=None, mode=None, freshness='d7', client=FakeClient())
        self.assertEqual(captured,[{'Query':'示例查询','Freshness':'d7'}])

    def test_lite_rejects_explicit_count_before_loading_credentials(self):
        with patch.object(agent_tools, '_load_cam') as load, \
             patch('saishi_tong.client.search_pro') as search, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                agent_tools.searchpro_main(['开发者比赛', '--lite', '--cnt', '10'])
        self.assertEqual(result.exception.code,2)
        load.assert_not_called()
        search.assert_not_called()

    def test_default_search_behavior_and_version_allowlist(self):
        for version in ['standard','flagship','untrusted-version',{'unexpected':'value'},None]:
            with self.subTest(version=version):
                output=io.StringIO()
                with patch.object(agent_tools, '_load_cam'), \
                     patch('saishi_tong.client.search_pro',return_value={'Pages':[],'Version':version}) as search, \
                     redirect_stdout(output):
                    code=agent_tools.searchpro_main(['开发者比赛'])
                self.assertEqual(code,0)
                search.assert_called_once_with('开发者比赛',cnt=10,mode=0,freshness=None)
                data=json.loads(output.getvalue())
                self.assertEqual(data.get('version'),version if version in ('standard','flagship') else None)

    def test_cli_gateway_and_embedded_replies_keep_actual_model(self):
        with patch.dict(sys.modules, {'fcntl': SimpleNamespace()}):
            worker = importlib.import_module('saishi_tong.openclaw_worker')
        verdict = {'verdict': 'needs_review', 'fields': {}}
        for wrapped, provider, model in [(True, 'openai', 'gpt-6-luna'), (False, 'other-provider', 'actual-fallback')]:
            reply = {'meta': {'agentMeta': {'provider': provider, 'model': model}},
                     'payloads': [{'text': '```json\n'+json.dumps(verdict)+'\n```'}]}
            data, actual, error = worker.decode_agent_reply({'result': reply, 'status': 'ok'} if wrapped else reply)
            self.assertEqual(data, verdict)
            self.assertEqual(actual, provider+'/'+model)
            self.assertEqual(error, '')

    def test_cli_empty_or_malformed_reply_never_claims_success(self):
        with patch.dict(sys.modules, {'fcntl': SimpleNamespace()}):
            worker = importlib.import_module('saishi_tong.openclaw_worker')
        for reply in [None, {'status':'error'}, {'status':[]}, {'payloads':[None, {'text':None}], 'meta':None}]:
            data, _, error = worker.decode_agent_reply(reply)
            self.assertIsNone(data)
            self.assertTrue(error)

    def test_source_prompt_and_result_dispatch(self):
        with patch.dict(sys.modules, {'fcntl': SimpleNamespace()}):
            worker = importlib.import_module('saishi_tong.openclaw_worker')
        item = {'source': {'source_url': 'https://modelscope.cn/events', 'item_limit': 3}}
        message = worker.build_message(item, '2026-10-23')
        self.assertIn('手动来源解析', message)
        self.assertIn('2026-10-23', message)
        self.assertIn('item_limit', message)
        self.assertEqual(worker.extract_json('```json\n{"contests":[]}\n```'), {'contests': []})
        self.assertIsNone(worker.extract_json('仅给建议，没有解析结果'))
        # Existing contest curation continues accepting its original JSON format.
        self.assertEqual(worker.extract_json('{"verdict":"needs_review","fields":{}}')['verdict'], 'needs_review')

    def test_private_targets_and_redirects_are_blocked(self):
        for address in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1']:
            with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', (address, 80))]):
                with self.assertRaises(ValueError): agent_tools._public_fetch_url('https://example.test/a')
                with self.assertRaises(ValueError): agent_tools._PublicRedirect().redirect_request(None, None, 302, '', {}, 'https://example.test/a')
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('1.1.1.1', 443))]):
            agent_tools._public_fetch_url('https://example.test/a')

    def test_event_detail_links_survive_plain_text_extraction(self):
        title, text = agent_tools._text('<title>活动</title><a href="/events/123/summary">AI 开发者挑战赛</a><a href="/topics/42">征文比赛</a>')
        self.assertIn('/events/123/summary', text)
        self.assertIn('/topics/42', text)

    def test_submission_action_preserves_actual_destination(self):
        # CSDN's actual action label is just “去投稿”; plain text alone loses
        # the destination and previously left the parser without an entry URL.
        _, text = agent_tools._text('<a href="https://activity.csdn.net/writing?id=11260">去投稿</a><a href="/submit/entry">提交作品</a>')
        self.assertIn('[去投稿](https://activity.csdn.net/writing?id=11260)', text)
        self.assertIn('[提交作品](/submit/entry)', text)
