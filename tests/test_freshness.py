"""Registration-window regressions; all cases are synthetic and use fixed dates."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saishi_tong import online_search, store
from saishi_tong.freshness import assess_event


TODAY = date(2026, 10, 27)


class RegistrationWindowTests(unittest.TestCase):
    def state(self, **fields):
        return assess_event({'status': '报名中', 'deadline': '2026-11-20', 'source_excerpt': '正在报名', **fields}, today=TODAY)['state']

    def test_ended_and_unknown_dates_never_become_current(self):
        for fields, expected in [({'deadline': '2026-10-26'}, 'expired'),
                                 ({'status': '已结束'}, 'expired'),
                                 ({'deadline': ''}, 'pending'),
                                 ({'deadline': '11月20日'}, 'pending'),
                                 ({'deadline': '2026-02-30'}, 'pending'),
                                 ({'status': '未知'}, 'pending')]:
            with self.subTest(fields=fields):
                self.assertEqual(self.state(**fields), expected)

    def test_future_registration_range_is_pending(self):
        for text in ['报名：2026-11-01 至 2026-11-20',
                     '作品征集时间：2026年11月1日—2026年11月20日',
                     'Registration: 2026-11-01 - 2026-11-20']:
            with self.subTest(text=text):
                self.assertEqual(self.state(timeline=text), 'pending')

    def test_future_opening_is_detected_in_actual_source_fragments(self):
        for text in ['报名将于2026-11-01开放。', '报名开始时间：2026-11-01。',
                     '2026-11-01起开放报名。', '2026-11-01开放报名。', 'Registration opens 2026-11-01.',
                     '2026-11-01正式开放报名。', '2026-11-01报名启动，报名截止2026-11-20。',
                     '报名通道尚未开放。', '即将开放报名。', 'registration not yet open']:
            with self.subTest(text=text):
                self.assertEqual(self.state(sources=[{'excerpt': text}]), 'pending')

    def test_start_and_deadline_in_separate_sentences(self):
        text = '2026-11-01正式开放报名。报名截止2026-11-20。'
        self.assertEqual(self.state(source_excerpt=text), 'pending')

    def test_open_registration_deadline_is_not_parsed_as_start(self):
        for text in ['目前开放报名，报名截止2026-11-20。',
                     '开放报名：报名截止2026-11-20。',
                     '报名开始，报名截止2026-11-20。']:
            with self.subTest(text=text):
                self.assertEqual(self.state(source_excerpt=text), 'current')

    def test_opening_without_year_is_not_guessed(self):
        self.assertEqual(self.state(source_excerpt='11月1日正式开放报名。报名截止2026-11-20。'), 'pending')

    def test_known_past_opening_is_current_even_if_announcement_says_will_open(self):
        self.assertEqual(self.state(source_excerpt='报名将于2026-10-20开放。'), 'current')
        self.assertEqual(self.state(timeline='报名：2026-10-20 至 2026-11-20'), 'current')

    def test_competition_and_final_dates_do_not_close_current_registration(self):
        self.assertEqual(self.state(timeline='比赛：2026-12-01 至 2026-12-03\n决赛：2026-12-20'), 'current')

    def test_another_round_range_does_not_hide_current_round(self):
        self.assertEqual(self.state(timeline='报名：2026-10-20 至 2026-11-20\n下一期报名：2026-12-01 至 2026-12-31'), 'current')
        self.assertEqual(self.state(source_excerpt='第一期报名：2026-10-20 至 2026-11-20；第二期2026-12-01正式开放报名，报名截止2026-12-31。'), 'current')

    def test_opening_date_boundary(self):
        self.assertEqual(self.state(timeline='报名：2026-10-27 至 2026-11-20'), 'current')
        self.assertEqual(self.state(source_excerpt='2026-10-27报名启动。报名截止2026-11-20。'), 'current')

    def test_date_only_deadline_boundary(self):
        self.assertEqual(self.state(deadline='2026-10-27'), 'current')
        self.assertEqual(self.state(deadline='2026-10-26'), 'expired')

    def test_raw_future_deadline_without_open_evidence_is_pending(self):
        self.assertEqual(self.state(source_excerpt='报名截止2026-11-20'), 'pending')
        self.assertEqual(self.state(source_excerpt='报名开始时间待定，报名截止2026-11-20'), 'pending')

    def test_actual_ai_checked_status_can_supply_open_state_but_unknown_cannot(self):
        self.assertEqual(self.state(source_excerpt='',luna_state='verified',luna_model='actual-model',
                                    luna_verified_at='2026-10-27T00:00:00Z'), 'current')
        self.assertEqual(self.state(source_excerpt='',luna_state='verified',luna_model='actual-model',
                                    luna_verified_at='2026-10-27T00:00:00Z',uncertain_fields=['status']), 'pending')

    def test_public_catalog_hides_future_opening_without_changing_review_or_deleting(self):
        with tempfile.TemporaryDirectory() as temp, patch('saishi_tong.classify.current_date', return_value=TODAY):
            path = Path(temp) / 'catalog.db'
            card = store.save_admin_contest({'contest_name': '测试开发者黑客松',
                'source_url': 'https://example.test/registration', 'search_time': '2026-10-27T00:00:00Z',
                'status': '报名中', 'deadline': '2026-11-20',
                'timeline': '报名：2026-11-01 至 2026-11-20'}, path=path)
            reviewed = store.save_admin_contest({'expected_fingerprint': card['record_fingerprint'],
                'human_reviewed': True, 'publication_state': 'published'}, card['id'], path=path)
            self.assertEqual(store.search_contests(path=path), [])
            self.assertIsNone(store.get_contest(card['id'], path=path))
            private = store.get_contest(card['id'], include_drafts=True, path=path)
            self.assertEqual(private['human_reviewed_at'], reviewed['human_reviewed_at'])
            self.assertEqual(private['timeline'], '报名：2026-11-01 至 2026-11-20')
            self.assertFalse(private['deleted_at'])
            self.assertEqual(store.archive_ended(path=path), [])

    def test_online_results_respect_explicit_opening_and_actual_deadline(self):
        pages = [
            {'title': '星河开发者黑客松', 'url': 'https://example.test/future-one',
             'passage': '2026-11-01正式开放报名。报名截止2026-11-20。参赛者开发应用。'},
            {'title': '云海开发者黑客松', 'url': 'https://example.test/future-two',
             'passage': '2026-11-01报名启动，报名截止2026-11-20。参赛者开发应用。'},
            {'title': '晨光开发者黑客松', 'url': 'https://example.test/open',
             'passage': '目前开放报名，报名截止2026-11-20。参赛者开发应用。'},
        ]
        payload = json.dumps({'Query': '开发者黑客松'})
        with patch('saishi_tong.classify.current_date', return_value=TODAY), \
             patch.object(online_search, 'validate_envelope', return_value=(payload, 0, 'synthetic', 'AKIDSynthetic')), \
             patch.object(online_search, 'upstream', return_value={'Pages': pages}):
            result = online_search.search_results({})
        self.assertEqual({c.source_url for c in result['cards']}, {'https://example.test/open'})
        self.assertEqual({c.source_url for c in result['pending_cards']},
                         {'https://example.test/future-one', 'https://example.test/future-two'})
