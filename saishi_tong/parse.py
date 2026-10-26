"""Parse SearchPro Pages (JSON strings) into contest cards + dedupe."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from .classify import enrich_card, normalized_url, safe_source_url

# Heuristic extractors for Chinese contest pages
_DEADLINE_RE = re.compile(
    r"(?:截止|报名截止|截稿|DDL|deadline)[^\d]{0,8}"
    r"(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}[-/.月]\d{1,2}日?)",
    re.I,
)
_PRIZE_RE = re.compile(
    r"(?:奖金|奖池|一等奖|总奖金|prize)[^\d￥¥$]{0,6}"
    r"([￥¥$]?\s?\d[\d,]*(?:\.\d+)?\s?(?:万|万元|元|W|k|K)?)",
    re.I,
)
_HOST_RE = re.compile(
    r"(?:联合主办(?:单位|机构|方)?|主办(?:单位|机构|方)?|发起单位|organized by|hosted by)"
    r"\s*[：:]\s*([^\n。；;|]{2,180})",
    re.I,
)

_DATE_TEXT = r"(?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}日?"
_DEADLINE_PATTERNS = [
    # Require a registration/submission cue, not an assessment/award date.
    re.compile(r"(?:报名|投稿|交稿|征稿|征文|作品(?:提交|征集)|提交作品|征集)(?:时间|日期)?\s*(?:截止(?:时间|日期)?(?:为|至|到)?|截至)\s*[：:]?\s*(" + _DATE_TEXT + r")", re.I),
    re.compile(r"(" + _DATE_TEXT + r")\s*(?:\d{1,2}[：:]\d{2}\s*)?(?:结束(?:征稿|征文|报名|投稿|征集)|(?:征稿|征文|报名|投稿|征集)截止|截稿)", re.I),
    re.compile(r"(?:于|请于|须于)\s*(" + _DATE_TEXT + r")\s*(?:前|之前)\s*(?:发送|投递|提交|投稿|报名|寄送)", re.I),
]


def _deadline(title: str, text: str) -> str:
    blob = f"{title}\n{text}"
    matches = []
    for pattern in _DEADLINE_PATTERNS + [_DEADLINE_RE]:
        for match in pattern.finditer(blob):
            context = blob[max(0, match.start()-16):match.start()]
            if re.search(r"(?:评审|颁奖|公布|公示|决赛)\s*(?:时间|日期)?\s*$", context):
                continue
            value = match.group(1).strip()
            from .classify import parse_deadline
            # Validate month/day without assigning a missing year to the fact.
            if parse_deadline(value, reference_year=2000) is not None:
                matches.append(value)
    # Several distinct submission/registration dates need human interpretation.
    from .classify import parse_deadline
    unique = {parse_deadline(v, reference_year=2000) for v in matches}
    return matches[0] if len(unique) == 1 else ""


def _host(text: str) -> str:
    match = _HOST_RE.search(text)
    if match:
        value = re.split(r"[,，](?=\s*(?:承办|协办|支持|报名|征稿|活动))|\s+(?=(?:承办|协办|支持)(?:单位|方)?\s*[：:])|[（(](?=以下简称)", match.group(1))[0].strip()
        if not re.search(r"主办者|某个|某一|例如|XXXX|征文的|主办方为", value, re.I):
            return value
    # Subject before 主办; don't turn text *after* 主办 into an organization.
    match = re.search(r"(?:^|[\n。；;，,])\s*(?:本(?:次|届)(?:活动|大赛|赛事)[，,]?\s*)?由\s*([^\n。；;]{2,160}?)\s*(?:联合)?主办(?=[，,。；;\s]|$)", text)
    return match.group(1).strip() if match and not re.search(r"某个|某一|例如|XXXX", match.group(1), re.I) else ""


@dataclass
class ContestCard:
    name: str
    deadline: str = ""
    prize: str = ""
    host: str = ""
    source_url: str = ""
    site: str = ""
    snippet: str = ""
    score: float = 0.0
    published: str = ""
    search_time: str = ""
    query: str = ""
    raw_title: str = ""
    contest_name: str = ""
    id: str = ""
    status: str = "未知"
    format: str = "未知"
    summary: str = ""
    organizer: str = ""
    timeline: str = ""
    eligibility: str = ""
    prizes: str = ""
    signup_url: str = ""
    sources: list[dict] = field(default_factory=list)
    uncertain_fields: list[str] = field(default_factory=list)
    verification_notes: list[str] = field(default_factory=list)
    luna_model: str = ""
    luna_verified_at: str = ""
    luna_input_hash: str = ""
    luna_state: str = "pending"
    human_reviewed_at: str = ""
    human_reviewed_hash: str = ""
    ai_reviewed_at: str = ""
    location_scope: str = "未知"
    deleted_at: str = ""
    source_excerpt: str = ""
    publication_state: str = "published"
    record_origin: str = "search"
    curated: bool = False
    merged_into: str = ""

    @classmethod
    def fromdict(cls, value: dict) -> "ContestCard":
        """Deserialize trusted card-shaped data without coercing arbitrary objects.

        Source dictionaries have an explicit public evidence allowlist; extension
        keys (including transport credentials) cannot leak into persisted cards.
        Errors are static, never a repr of the rejected value.
        """
        if not isinstance(value, dict):
            raise ValueError("invalid_card")
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in value.items() if k in names}
        for key, item in data.items():
            if key == "sources":
                if not isinstance(item, list) or len(item) > 500:
                    raise ValueError("invalid_card_sources")
                cleaned = []
                for source in item:
                    if not isinstance(source, dict):
                        raise ValueError("invalid_card_sources")
                    entry = {}
                    for part in ("url", "title", "excerpt"):
                        text = source.get(part, "")
                        if not isinstance(text, str):
                            raise ValueError("invalid_card_sources")
                        entry[part] = text
                    if not safe_source_url(entry["url"]):
                        raise ValueError("invalid_card_sources")
                    cleaned.append(entry)
                data[key] = cleaned
            elif key in {"uncertain_fields", "verification_notes"}:
                if not isinstance(item, list) or len(item) > 1000 or any(not isinstance(s, str) for s in item):
                    raise ValueError("invalid_card_list")
                data[key] = list(item)
            elif key == "curated":
                if not isinstance(item, bool):
                    raise ValueError("invalid_card_boolean")
            elif key == "score":
                if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
                    raise ValueError("invalid_card_score")
            elif not isinstance(item, str):
                raise ValueError("invalid_card_text")
        data.setdefault("name", "")
        for key in ("source_url", "signup_url"):
            if data.get(key) and not safe_source_url(data[key]):
                raise ValueError("invalid_card_url")
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(enrich_card(self))


def _now_iso() -> str:
    # Always label Asia/Shanghai for owner (UTC+8), even if Jarvis clock is UTC.
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        now = datetime.now(timezone.utc).astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S %z") + " Asia/Shanghai"


def _parse_page(page: Any) -> Optional[Dict[str, Any]]:
    if page is None:
        return None
    if isinstance(page, dict):
        return page
    if isinstance(page, str):
        try:
            value = json.loads(page)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return {"passage": page, "title": page[:80]}
    return None


def _norm_url(url: str) -> str:
    return normalized_url(url)

def _norm_title(title: str) -> str:
    t = re.sub(r"\s+", "", (title or "").lower())
    t = re.sub(r"[【】\[\]（）()·|｜\-—_]", "", t)
    return t[:80]


def extract_fields(title: str, text: str) -> Tuple[str, str, str]:
    blob = f"{title}\n{text}"
    deadline = _deadline(title, text)
    prize = ""
    host = ""
    m = _PRIZE_RE.search(blob)
    if m:
        prize = m.group(1).strip()
    host = _host(blob)
    return deadline, prize, host


def pages_to_cards(
    responses: Iterable[Dict[str, Any]],
    search_time: Optional[str] = None,
) -> List[ContestCard]:
    st = search_time or _now_iso()
    cards: List[ContestCard] = []
    for resp in responses:
        query = str(resp.get("Query") or "")
        pages = resp.get("Pages") or []
        for page in pages:
            obj = _parse_page(page)
            if not obj:
                continue
            title = str(obj.get("title") or "").strip()
            url = safe_source_url(str(obj.get("url") or ""))
            standard = str(obj.get("passage") or "").strip()
            dynamic = str(obj.get("content") or "").strip()
            # Retain both supplied excerpts, with a bounded share for each.
            passage = (standard[:3000] + "\n" + dynamic[:3000]).strip() if dynamic and dynamic != standard else (standard or dynamic)[:6000]
            site = str(obj.get("site") or "").strip()
            published = str(obj.get("date") or "").strip()
            try:
                score = float(obj.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if not math.isfinite(score):
                score = 0.0
            deadline, prize, host = extract_fields(title, passage)
            if not title and not url:
                continue
            cards.append(
                enrich_card(ContestCard(
                    name=title or url,
                    deadline=deadline,
                    prize=prize,
                    host=host,
                    source_url=url,
                    site=site,
                    snippet=passage[:280],
                    score=score,
                    published=published,
                    search_time=st,
                    query=query,
                    raw_title=title,
                    source_excerpt=passage[:6000],
                    sources=[{"url": url, "title": title, "excerpt": passage[:4000]}] if url else [],
                ))
            )
    return cards


def dedupe(cards: List[ContestCard]) -> List[ContestCard]:
    """Prefer higher score; key by URL then normalized title."""
    best: Dict[str, ContestCard] = {}
    order: List[str] = []
    for c in cards:
        key = _norm_url(c.source_url) or f"t:{_norm_title(c.name)}"
        if not key:
            continue
        prev = best.get(key)
        if prev is None:
            best[key] = c
            order.append(key)
        else:
            # merge richer fields, keep higher score
            if c.score > prev.score:
                merged = c
                if not merged.deadline:
                    merged.deadline = prev.deadline
                if not merged.prize:
                    merged.prize = prev.prize
                if not merged.host:
                    merged.host = prev.host
                best[key] = merged
            else:
                if not prev.deadline and c.deadline:
                    prev.deadline = c.deadline
                if not prev.prize and c.prize:
                    prev.prize = c.prize
                if not prev.host and c.host:
                    prev.host = c.host
    return [best[k] for k in order]


def closing_soon(cards: List[ContestCard], limit: int = 10) -> List[ContestCard]:
    """Only upcoming deadlines belong in the closing-soon list."""
    upcoming = [c for c in cards if enrich_card(c).status == "即将截止"]
    return sorted(upcoming, key=lambda c: c.deadline)[:limit]


def top_n(cards: List[ContestCard], n: int = 15) -> List[ContestCard]:
    return sorted(cards, key=lambda c: c.score, reverse=True)[:n]
