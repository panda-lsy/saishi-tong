from _review_fixture import reviewed
"""First-round regression checks: synthetic credentials and temporary DB only."""
import importlib
import json
import os
import tempfile
from datetime import date
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from _browser import Browser
from saishi_tong import store
from saishi_tong.parse import ContestCard


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


class RoundOneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            'SAISHI_DB_PATH': str(Path(self.temp.name) / 'catalog.db'),
            'SAISHI_ADMIN_TOKEN': 'synthetic-round-one-admin',
        })
        env.start()
        self.addCleanup(env.stop)
        self.web = importlib.import_module('saishi_tong.web.app')
        self.browser = Browser(self.web.app)
        today = patch('saishi_tong.classify.current_date', return_value=date(2026, 10, 26))
        today.start(); self.addCleanup(today.stop)

    def card(self, key='visible', **changes):
        values = dict(name='测试开发者挑战赛', contest_name='测试开发者挑战赛', id=key,
                      source_url='https://example.test/' + key,
                      search_time='2026-10-22T00:00:00Z', luna_state='verified',
                      status='报名中', format='线上', deadline='2099-11-22',
                      signup_url='https://example.test/signup', summary='AI&<> 测试赛事')
        values.update(changes)
        card = ContestCard(**values)
        store.upsert_contests([reviewed(card) if card.luna_state == 'verified' else card])
        return card

    def test_server_keys_endpoint_removed_even_for_admin_without_disk_reads(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('no credential file reads')):
            for headers in ({}, {'authorization': 'Bearer synthetic-round-one-admin'}):
                code, _, body = self.browser.request('/api/admin/server-searchpro', headers=headers)
                self.assertEqual(code, 404)
                self.assertNotIn(b'secret_key', body)

    def test_public_visibility_matrix(self):
        visible = self.card()
        hidden = [self.card(state, luna_state=state) for state in ('pending', 'review', 'failed', 'stale', 'disabled')]
        hidden += [self.card('draft', publication_state='draft'), self.card('retired', publication_state='retired')]
        code, _, body = self.browser.request('/api/contests')
        self.assertEqual(code, 200)
        self.assertEqual([c['id'] for c in json.loads(body)['contests']], [visible.id])
        for card in hidden:
            self.assertEqual(self.browser.request('/contests/' + card.id)[0], 404)

    def test_applied_filter_links_preserve_other_fields(self):
        self.card()
        filters = {'q': 'AI&<>', 'status': '报名中', 'format': '线上'}
        code, _, body = self.browser.request('/contests?' + urlencode(filters))
        self.assertEqual(code, 200)
        html = body.decode()
        links = [a for a in Links(html).links if a.get('aria-label', '').startswith('移除')]
        self.assertEqual(len(links), 3)
        for anchor, field in zip(links, ('q', 'status', 'format')):
            query = {k: v[0] for k, v in parse_qs(urlsplit(anchor['href']).query).items()}
            self.assertNotIn(field, query)
            self.assertEqual(query, {k: v for k, v in filters.items() if k != field})
        self.assertIn('AI&amp;&lt;&gt;', html)

    def test_detail_signup_entry_handles_missing_and_ended(self):
        self.card('ended', status='已结束', deadline='2025-01-01')
        self.assertEqual(self.browser.request('/contests/ended')[0], 404)
        for key, changes, expected in [
            ('open', {}, '前往官方报名页'),
            ('missing', {'signup_url': ''}, '报名入口待核验'),
            ('unsafe', {'signup_url': 'javascript:alert(1)'}, '报名入口待核验'),
        ]:
            self.card(key, **changes)
            if key == 'unsafe':
                # Confirm the stored, sanitized facts, not the discarded URL.
                saved = store.get_contest(key, include_drafts=True)
                store.save_admin_contest({'human_reviewed': True,
                    'expected_fingerprint': saved['record_fingerprint']}, key)
            code, _, body = self.browser.request('/contests/' + key)
            self.assertEqual(code, 200)
            html = body.decode()
            self.assertIn(expected, html)
            buttons = [a for a in Links(html).links if 'signup-button' in a.get('class', '')]
            self.assertEqual(len(buttons), 0 if key in ('missing', 'unsafe') else 1)
            if buttons:
                self.assertEqual(buttons[0]['href'], 'https://example.test/signup')
                self.assertIn('noopener', buttons[0]['rel'])

    def test_settings_prioritizes_admin_and_collapses_local_keys(self):
        code, _, body = self.browser.request('/settings')
        self.assertEqual(code, 200)
        html = body.decode()
        self.assertLess(html.index('<section id="admin"'), html.index('<section id="local-keys"'))
        self.assertIn('<details class="advanced-settings">', html)
        self.assertNotIn('自动填入服务端', html)
        self.assertIn('href="/admin"', html)
        self.assertNotIn('id="admin-workspace"', html)
        self.assertNotIn('/static/admin.js', html)
        code, _, body = self.browser.request('/admin')
        self.assertEqual(code, 200)
        self.assertIn('人工确认后对游客可见', body.decode())
        self.assertIn('id="admin-workspace" hidden', body.decode())


if __name__ == '__main__':
    unittest.main()
