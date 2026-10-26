import unittest
from saishi_tong.classify import heuristic_contest_name

class HeadlineFallbackTests(unittest.TestCase):
    def test_report_verbs_do_not_become_formal_names(self):
        for title in ['Sea与OpenAI将在曼谷办Codex黑客松','蝦皮購物母公司Sea攜手OpenAI在台舉辦黑客松']:
            self.assertEqual(heuristic_contest_name(title),'待核验赛事')

    def test_source_quoted_official_english_series_is_allowed(self):
        excerpt='Sea与OpenAI合作举办的“Sea x OpenAI Regional Codex Hackathon Series”将首次在泰国落地。'
        self.assertEqual(heuristic_contest_name('Sea与OpenAI将在曼谷办Codex黑客松',excerpt),'Sea x OpenAI Regional Codex Hackathon Series')
