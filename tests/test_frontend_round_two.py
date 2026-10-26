"""Discovery, repeated public filters and the separate admin surface."""
import json
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

import test_frontend_round_one as round_one
from saishi_tong import store, service


class RoundTwoTests(unittest.TestCase):
    setUp = round_one.RoundOneTests.setUp
    card = round_one.RoundOneTests.card
    def seed_matrix(self):
        for si, status in enumerate(store.CATALOG_STATUSES):
            for fi, mode in enumerate(store.CATALOG_FORMATS):
                self.card(f'{si}-{fi}', status=status, format=mode, deadline='')
        self.card('private-pending', status='报名中', luna_state='pending')
        self.card('private-draft', status='即将截止', publication_state='draft')

    def api(self, statuses=(), formats=(), **kwargs):
        fields = [('status', s) for s in statuses] + [('format', f) for f in formats]
        code, _, body = self.browser.request('/api/contests?' + urlencode(fields), **kwargs)
        self.assertEqual(code, 200)
        return json.loads(body)

    def test_multi_selection_matrix_and_private_visibility(self):
        self.seed_matrix()
        cases = [([], [], 16), (['报名中', '即将截止'], [], 8),
                 ([], ['线上'], 8), ([], ['线下'], 8),
                 ([], ['线上', '线下'], 12), ([], ['混合'], 4),
                 (['报名中', '已结束'], ['线下'], 4),
                 (['未知'], ['未知'], 1)]
        with patch.object(service, 'run_search', side_effect=AssertionError('local only')):
            for states, modes, count in cases:
                data = self.api(states, modes)
                self.assertEqual(data['total'], count, (states, modes))
                self.assertEqual(len({c['id'] for c in data['contests']}), count)
                self.assertFalse(any(c['id'].startswith('private') for c in data['contests']))
        # No-JS forms leave the old all checkbox checked: explicit tags win.
        data = self.api(['', '报名中', '报名中'], ['', '线上', '混合'])
        self.assertEqual(data['statuses'], ['报名中'])
        self.assertEqual(data['formats'], ['线上', '混合'])
        self.assertEqual(data['total'], 2)

    def test_get_form_and_json_use_identical_filter_semantics(self):
        self.seed_matrix()
        fields = [('status', '报名中'), ('status', '即将截止'), ('format', '线下')]
        expected = {c['id'] for c in self.api(['报名中', '即将截止'], ['线下'])['contests']}
        requests = [('/api/search', urlencode(fields).encode(), {'content-type': 'application/x-www-form-urlencoded'}),
                    ('/api/contests', {'status': ['报名中', '即将截止'], 'format': ['线下']}, {}),
                    ('/api/search', {'status': ['报名中', '即将截止'], 'format': '线下'}, {})]
        for path, body, headers in requests:
            code, _, result = self.browser.request(path, method='POST', data=body, headers=headers)
            self.assertEqual(code, 200)
            self.assertEqual({c['id'] for c in json.loads(result)['contests']}, expected)
        code, _, body = self.browser.request('/contests', method='POST', data=urlencode(fields).encode(),
                                             headers={'content-type': 'application/x-www-form-urlencoded'})
        self.assertEqual(code, 200)
        self.assertEqual(body.decode().count('class="contest-card'), 4)

    def test_invalid_filter_types_fail_closed(self):
        for field in ('status', 'format'):
            for value in (None, 1, {}, [1], ['不存在'], ['线上,线下'], [''] * 21):
                code, _, _ = self.browser.request('/api/contests', method='POST', data={field: value})
                self.assertEqual(code, 422, (field, value))

    def test_home_excludes_ended_unknown_but_directory_keeps_them(self):
        self.seed_matrix()
        code, _, body = self.browser.request('/')
        self.assertEqual(code, 200)
        links = [a['href'] for a in round_one.Links(body.decode()).links if a.get('href', '').startswith('/contests/')]
        self.assertEqual(len(links), 6)
        self.assertTrue(all(link.split('/')[-1].startswith(('0-', '1-')) for link in links))
        self.assertIn('8', body.decode())
        self.assertEqual(self.api()['total'], 16)

    def test_remove_one_chip_preserves_repeated_values_and_query(self):
        fields = [('q', 'AI&<>'), ('status', '报名中'), ('status', '即将截止'), ('format', '线上'), ('format', '线下')]
        code, _, body = self.browser.request('/contests?' + urlencode(fields))
        self.assertEqual(code, 200)
        chips = [a for a in round_one.Links(body.decode()).links if a.get('aria-label', '').startswith('移除')]
        self.assertEqual(len(chips), len(fields))
        for chip, removed in zip(chips, fields):
            remaining = fields.copy()
            remaining.remove(removed)
            self.assertEqual(parse_qs(urlsplit(chip['href']).query), parse_qs(urlencode(remaining)))
            self.assertEqual(urlsplit(chip['href']).fragment, 'directory-title')

    def test_admin_drawer_has_sticky_actions_and_private_workspace_hidden(self):
        code, _, body = self.browser.request('/admin')
        self.assertEqual(code, 200)
        html = body.decode()
        self.assertIn('<dialog id="admin-editor-panel"', html)
        self.assertIn('form="admin-editor"', html)
        self.assertIn('id="admin-view-jobs"', html)
        self.assertIn('id="admin-select-page"', html)
        self.assertEqual(self.browser.request('/api/admin/contests')[0], 401)
