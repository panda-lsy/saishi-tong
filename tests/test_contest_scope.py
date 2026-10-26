"""A real event may still be outside the computing/developer catalog."""
import tempfile
from pathlib import Path
import unittest
from saishi_tong import store
from saishi_tong.contest_scope import classify_scope
from saishi_tong.parse import ContestCard
from saishi_tong.search_quality import assess

class ContestScopeTests(unittest.TestCase):
    def test_school_composition_is_out_even_with_ai_topic(self):
        page={'title':'第二十七届世界华人学生作文大赛征文启事','passage':'主题：数智之光，向善而行，人工智能改变生活。主办：中国侨联。'}
        self.assertEqual(classify_scope(page),'out_of_scope')
        self.assertFalse(assess(page)['retain'])
        for title in ['AI时代学生作文大赛','人工智能时代主题征文','“礼润扶风”廉洁主题征文启事','《鹿鸣》创刊600期全国征文启事']:
            self.assertEqual(classify_scope({'title':title}),'out_of_scope')

    def test_technical_essay_and_computing_contests_remain_in_scope(self):
        for title in ['元宝搜索开发者征文大赛','AI文化造物黑客松','全国青少年信息学竞赛','网络安全CTF挑战赛','计算机视觉算法挑战赛','软件开发技术征文']:
            self.assertEqual(classify_scope({'title':title}),'in_scope')

    def test_old_out_of_scope_row_cannot_be_reapproved_or_published(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'c.db'
            card=ContestCard(name='世界华人学生作文大赛',source_url='https://example.test/composition',publication_state='draft',search_time='2026-10-26T00:00:00Z')
            store.upsert_contests([card],path=path)
            c=store.get_contest(card.id,path,include_drafts=True)
            for payload in [{'human_reviewed':True},{'publication_state':'published'}]:
                with self.assertRaisesRegex(ValueError,'收录范围不符'):
                    store.save_admin_contest({**payload,'expected_fingerprint':c['record_fingerprint']},card.id,path=path)
            store.save_admin_contest({'human_reviewed':False,'publication_state':'draft'},card.id,path=path)
            self.assertEqual(store.list_contests(path=path),[])

    def test_new_manual_out_of_scope_draft_is_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(ValueError,'收录范围不符'):
                store.save_admin_contest({'contest_name':'全国学生作文大赛','source_url':'https://example.test/composition'},path=Path(t)/'c.db')

    def test_legacy_confirmed_out_of_scope_record_is_hidden_on_all_public_reads(self):
        from _review_fixture import reviewed
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'c.db'
            card=reviewed(ContestCard(name='世界华人学生作文大赛',source_url='https://example.test/composition',publication_state='published',search_time='2026-10-26T00:00:00Z'))
            store.upsert_contests([card],path=path)
            self.assertTrue(store.get_contest(card.id,path,include_drafts=True)['human_reviewed'])
            self.assertIsNone(store.get_contest(card.id,path))
            self.assertEqual(store.search_contests(path=path),[])
            self.assertEqual(store.list_contests(path=path),[])
            self.assertEqual(store.count_contests(path),0)
