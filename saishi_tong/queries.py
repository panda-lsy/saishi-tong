"""Expand a natural-language constraint into multiple SearchPro queries."""

from __future__ import annotations

import re
from datetime import date
from typing import List

# CN + EN contest keywords for Yuanbao multi-query recall
_CN_TEMPLATES = [
    "{topic} {year} 官方 报名入口 竞赛规则 截止时间",
    "{topic} {year} 参赛资格 赛程 主办方 官方公告",
    "{topic} {year} CSDN 征文大赛 活动规则 奖励",
    "{topic} {year} 腾讯云 阿里云 华为云 黑客松 报名 规则",
    "{topic} {year} 天池 比赛 官方 参赛要求 奖金",
    "{topic} {year} 开发者竞赛 官网 报名截止",
]

_EN_TEMPLATES = [
    "{topic} {year} hackathon official rules eligibility schedule registration",
    "{topic} {year} developer contest official deadline prizes",
]

_DEFAULT_TOPIC = "AI 大模型 开源"


def _extract_topic(user_text: str) -> str:
    """Soft topic for templates only — never destroy the raw user string."""
    text = (user_text or "").strip()
    if not text:
        return _DEFAULT_TOPIC
    text = re.sub(
        r"^(帮我|请|查找|搜索|找一下|找|列出|汇总)[，,\s]*",
        "",
        text,
    )
    # Keep first ~40 chars as topic seed if long
    if len(text) > 40:
        return text[:40].strip()
    return text.strip() or _DEFAULT_TOPIC


def build_queries(user_text: str, extra: List[str] | None = None, limit: int = 6) -> List[str]:
    """Build a de-duplicated list of SearchPro Query strings.

    Always puts the raw user text first when present.
    """
    queries: List[str] = []
    raw = (user_text or "").strip()
    if raw:
        queries.append(raw)
    topic = _extract_topic(user_text)
    for tpl in _CN_TEMPLATES + _EN_TEMPLATES:
        q = tpl.format(topic=topic, year=date.today().year)
        if q not in queries:
            queries.append(q)
    if extra:
        for e in extra:
            e = (e or "").strip()
            if e and e not in queries:
                queries.append(e)
    return queries[: max(1, limit)]
