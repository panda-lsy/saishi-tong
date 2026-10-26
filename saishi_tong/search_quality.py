"""Conservative retrieval triage, independent of AI/human verification."""
import re
from urllib.parse import urlsplit
from .contest_scope import classify_scope


def assess(page):
    title = page.get('title', '')
    text = '\n'.join(page.get(k, '') for k in ('title', 'passage', 'content'))
    source=urlsplit(page.get('url',''))
    if classify_scope(page)=='out_of_scope':
        return {'kind':'收录范围外','retain':False,'reason':'本站仅收录开发、计算机类赛事；普通作文、文学及文化主题征文不收录，提到AI主题也不能替代技术参赛要求。'}
    if (source.hostname or '').lower() in {'modelscope.cn','www.modelscope.cn'} and re.match(r'^/(?:models|datasets|skills)/',source.path):
        return {'kind':'资源页面','retain':False,'reason':'模型、数据集或技能资源页，需找到具体赛事活动页再收录。'}
    if re.search(r'创作手记|参赛(?:作品|总结)|^\s*(?:魔搭|CSDN)?\s*(?:日报|周报)',title,re.I):
        return {'kind':'作品或资讯','retain':False,'reason':'参赛作品、创作复盘或资讯汇总，不能代替具体赛事公告。'}
    if (not re.search(r'黑客松|征文(?:大赛|比赛|启事)|挑战赛|竞赛|hackathon|competition|contest',title,re.I)
            and re.search(r'测评|选型攻略|横评|工具清单|深度解析|实战指南|实测分析|写小说|各写一版',title)):
        return {'kind':'教程或评测','retain':False,'reason':'工具评测或技术教程，尚无可指认的具体赛事公告。'}
    if re.search(r'是什么意思|汉语词语|词语解释|(?:征文|征稿).*(?:格式怎么写|怎么写.*范文)|(?:征文|征文启事)[的\s]*(?:范文|模板)|(?:征文|征文启事)的写作(?:方法|技巧)|^什么是征文', title):
        return {'kind': '非赛事', 'retain': False, 'reason': '词义解释或写作范文，不能指认具体赛事。'}
    if re.search(r'(?:赛事|比赛|黑客松|征文)(?:信息)?(?:汇总|大全|合集|导航|列表)|(?:本周|本月|近期).*(?:赛事|比赛).*盘点', title):
        return {'kind': '来源列表', 'retain': False, 'reason': '包含多场活动的列表页，需从具体活动链接收录。'}
    if re.search(r'(?<!未)(?<!尚未)落幕|(?<!未)收官|获奖名单|获奖公示|拿下.*奖|斩获.*奖|荣获|颁奖', title):
        return {'kind': '赛后报道', 'retain': True, 'reason': '赛后报道，可作历史线索；不代表正在报名。'}
    if re.search(r'报名|征稿|征集|投稿|参赛|竞赛规则|比赛规则|征文启事|邀请赛|黑客松|hackathon', text, re.I):
        return {'kind': '赛事线索', 'retain': True, 'reason': '发现活动或参赛线索，仍需核对具体赛事及规则。'}
    return {'kind': '待判断', 'retain': True, 'reason': '摘录不足以判断，保留来源待核，不自动补齐事实。'}
