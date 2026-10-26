"""Real public excerpts as regression inputs; never query or modify the live DB."""
from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from saishi_tong import online_search
from saishi_tong.classify import classify_contest, heuristic_contest_name
from saishi_tong.parse import extract_fields, pages_to_cards
from saishi_tong.search_quality import assess
from test_online_search import envelope

FIXTURE=Path(__file__).parent/'fixtures/online-search-20261025.json'


class SearchQualityTests(unittest.TestCase):
    def test_real_search_batches_apply_technical_scope_as_well_as_page_quality(self):
        samples=json.loads(FIXTURE.read_text(encoding='utf-8-sig'))
        self.assertEqual(len(samples),20)
        for query,expected in [('征文',0),('黑客松 征文 比赛',10)]:
            pages=[r['page'] for r in samples if r['Query']==query]
            with patch.object(online_search,'upstream',return_value={'Pages':pages}):
                result=online_search.search_results(envelope(query))
            self.assertEqual(len(result['cards']),expected)
            self.assertEqual(result['counts']['returned'],10)
            self.assertEqual(result['counts']['excluded'],10-expected)
            self.assertEqual(result['counts']['invalid'],0)
            self.assertTrue(all(c.luna_state=='pending' and not c.human_reviewed_at for c in result['cards']))
            self.assertFalse(any(c.host in {'者后','腾讯新闻','中国日报网','百问网','中国作家网'} or c.host.startswith('方:') for c in result['cards']))
            if query!='征文':
                ended=next(c for c in result['cards'] if '鼓楼江岸' in c.raw_title)
                self.assertEqual(ended.status,'已结束')

    def test_negative_explanation_and_list_not_a_contest(self):
        for title in ['征文是什么意思','征文(汉语词语)','征文的格式怎么写给个范文','征文启事模板','本月黑客松比赛汇总']:
            with self.subTest(title=title):self.assertFalse(assess({'title':title})['retain'])
        self.assertEqual(assess({'title':'黑客松比赛汇总'})['kind'],'来源列表')
        self.assertEqual(heuristic_contest_name('征文是什么意思','要求标题可用“征文启事”四字。'),'待核验赛事')

    def test_sparse_real_events_and_not_ended_are_preserved(self):
        for title in ['星河黑客松','晨光征文启事','2026未知赛事','跨国比赛尚未落幕']:
            self.assertTrue(assess({'title':title})['retain'])
        self.assertNotEqual(classify_contest('星河黑客松尚未落幕',today=date(2026,10,25))[0],'已结束')

    def test_modelscope_resources_and_submission_notes_are_not_events(self):
        for path in ['/models/vllm/example','/datasets/lab/data','/skills/lab/tool']:
            self.assertFalse(assess({'title':'AI黑客松相关资源','url':'https://modelscope.cn'+path})['retain'])
        for title in ['魔搭日报 9.21','亲爱的她创作手记','黑客松参赛总结']:
            self.assertFalse(assess({'title':title,'url':'https://modelscope.cn/learn/123'})['retain'])
        self.assertTrue(assess({'title':'星河黑客松报名','url':'https://modelscope.cn/events/123'})['retain'])
        self.assertFalse(assess({'title':'AI搜索横评：五大工具实测','url':'https://blog.csdn.net/example'})['retain'])
        self.assertTrue(assess({'title':'星河黑客松：AI工具实战指南','url':'https://blog.csdn.net/example'})['retain'])

    def test_organizer_requires_source_evidence_not_publisher(self):
        for text,expected in [('主办：星河基金会。','星河基金会'),('主办方:开猿spirit|协办方:其他单位','开猿spirit'),
                              ('主办:甲协会 乙协会 承办:丙组织 协办:丁组织','甲协会 乙协会'),
                              ('由甲协会、乙协会联合主办，丙承办。','甲协会、乙协会'),
                              ('活动主办者后，作者可以参加比赛。',''),('发起一场AI共创大赛「CCE」。','')]:
            self.assertEqual(extract_fields('',text)[2],expected)
        c=pages_to_cards([{'Pages':[{'title':'星河黑客松','url':'https://example.test/a','site':'新闻网站'}]}])[0]
        self.assertEqual(c.host,'')

    def test_submission_date_and_conflicts_not_award_date(self):
        for text,expected in [('2026年11月30日结束征稿','2026年11月30日'),('请于2026-11-22前发送作品','2026-11-22'),
            ('报名截止时间：2026年11月22日','2026年11月22日'),('颁奖截止2026-11-20',''),
            ('报名截止2026-02-30',''),('截稿11月22日','11月22日'),
            ('报名截止2026-11-20，作品提交截止2026-11-22','')]:
            with self.subTest(text=text):self.assertEqual(extract_fields('',text)[0],expected)

    def test_dynamic_excerpt_is_retained_and_used_beyond_card_snippet(self):
        page={'title':'星河黑客松','url':'https://example.test/a','passage':'介绍文字。'*100,
              'content':'主办方：星河基金会。报名截止2026-11-22。线上举行。赛事圆满落幕。','score':.93}
        with patch.object(online_search,'upstream',return_value={'Pages':[page]}):
            result=online_search.search_results(envelope())
        c=result['cards'][0]
        self.assertIn(page['content'],c.source_excerpt)
        self.assertIn(page['content'],c.sources[0]['excerpt'])
        self.assertEqual(c.host,'星河基金会');self.assertEqual(c.deadline,'2026-11-22')
        self.assertEqual(c.status,'已结束');self.assertEqual(c.score,.93)

    def test_all_discarded_and_invalid_is_a_successful_empty_search(self):
        pages=[{'title':'征文是什么意思','url':'https://example.test/a'},'not-json',{'url':'http://127.0.0.1'}]
        with patch.object(online_search,'upstream',return_value={'Pages':pages}):result=online_search.search_results(envelope())
        self.assertEqual(result['counts'],{'returned':3,'retained':0,'excluded':1,'invalid':2,'duplicates':0})
        self.assertTrue(result['excluded'][0]['search_time'])

    def test_score_orders_only_retained_candidates_and_stays_finite(self):
        pages=[{'title':title,'url':'https://example.test/'+str(i),'score':score} for i,(title,score) in enumerate([
            ('星河黑客松',.2),('征文是什么意思',1),('晨光征文启事',.8),('未知比赛',float('nan')),('公开大赛',10**500)])]
        with patch.object(online_search,'upstream',return_value={'Pages':pages}):result=online_search.search_results(envelope())
        self.assertEqual([c.score for c in result['cards']],[.8,.2,0,0])

    def test_direction_options_allowlist_preserves_signed_payload(self):
        for site in ['csdn.net','modelscope.cn']:
            body=envelope();payload=json.loads(body['payload']);payload.update(Site=site,Freshness='d30')
            body['payload']=json.dumps(payload)
            self.assertEqual(online_search.validate_envelope(body)[0],body['payload'])
        for changes in [{'Site':'http://127.0.0.1'},{'Site':'evil.test'},{'Site':[]},{'Freshness':[]}, {'Endpoint':'https://evil.test'}]:
            body=envelope();payload=json.loads(body['payload']);payload.update(changes);body['payload']=json.dumps(payload)
            with self.assertRaises(online_search.OnlineError):online_search.validate_envelope(body)
