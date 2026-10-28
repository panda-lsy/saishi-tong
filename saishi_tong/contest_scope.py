"""Product scope is separate from whether an event is real."""
import re
from dataclasses import asdict, is_dataclass

TECH=re.compile(r'黑客松|hackathon|开发者|编程|程序设计|软件开发|计算机|算法|信息学|网络安全|数据科学|数据挖掘|机器学习|深度学习|技术征文|\b(?:API|SDK|CTF|ICPC|ACM)\b|开源',re.I)
ORDINARY_TITLE=re.compile(r'学生作文|作文(?:大赛|比赛|征文)|诗歌(?:大赛|征集)|书法(?:大赛|比赛)|摄影(?:大赛|比赛)|文学征文|散文征文|廉洁|青春家书|主题征文|《[^》]+》创刊.*征文')
TECH_TASK=re.compile(r'开发(?:真实|实际|一个|可运行)?(?:应用|软件|程序|项目)|编写(?:代码|程序)|提交(?:源码|源代码|代码|技术文章)|(?:API|SDK).{0,30}(?:开发|调用)|(?:技术文章|技术实践).{0,30}(?:征文|参赛)|技术征文|(?:软件|程序|代码|计算机|鸿蒙|HarmonyOS|API|SDK).{0,20}开发.{0,20}(?:心得|经验|复盘|笔记|脚本)',re.I)
STRONG_TECH=re.compile(r'黑客松|hackathon|开发者|编程|程序设计|软件开发|计算机|算法|信息学|网络安全|数据科学|数据挖掘|机器学习|深度学习|技术征文|\b(?:API|SDK|CTF|ICPC|ACM)\b',re.I)
CREATIVE=re.compile(r'短视频|视频创作|影像|影视|绘画|摄影|书法|诗歌|文学|小说|音乐创作',re.I)
TECHNICAL_TITLE=re.compile(r'编程|程序设计|软件开发|计算机|算法|信息学|网络安全|数据科学|机器学习|深度学习|技术征文|\b(?:API|SDK|CTF|ICPC|ACM)\b',re.I)

def classify_scope(value):
    data=asdict(value) if is_dataclass(value) else value
    title=' '.join(str(data.get(k,'') or '') for k in ('contest_name','raw_title','name','title'))
    parts=[str(data.get(k,'') or '') for k in ('source_excerpt','snippet','summary','eligibility','passage','content')]
    parts.extend(str(s.get('excerpt','') or '') for s in (data.get('sources') or []) if isinstance(s,dict))
    text='\n'.join(parts)
    # A theme mentioning AI does not turn a school composition contest into CS.
    if re.search(r'学生作文|作文(?:大赛|比赛|征文)',title):return 'out_of_scope'
    if ORDINARY_TITLE.search(title) and not STRONG_TECH.search(title) and not TECH_TASK.search(text):return 'out_of_scope'
    if re.search(r'征文|征稿',title) and not STRONG_TECH.search(title) and not TECH_TASK.search(text) and re.search(r'文学|散文|诗歌|非遗|廉洁|家书|作者自行拟定',text):return 'out_of_scope'
    if CREATIVE.search(title) and not TECHNICAL_TITLE.search(title) and not TECH_TASK.search(text):return 'uncertain'
    if TECH.search(title) or TECH_TASK.search(text):return 'in_scope'
    return 'uncertain'
