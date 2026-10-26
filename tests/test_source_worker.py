"""Worker prompt/JSON dispatch and source fetch guards; no real agent or network."""
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from saishi_tong import agent_tools


class WorkerSourceTests(unittest.TestCase):
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
