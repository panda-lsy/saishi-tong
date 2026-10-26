"""Conservative, testable contest labels and safe source links."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def safe_source_url(url: str) -> str:
    if not isinstance(url, str):
        return ""
    url = url or ""
    if any(ord(c) < 32 or ord(c) == 127 for c in url) or chr(92) in url:
        return ""
    url = url.strip()
    if any(c.isspace() for c in url):
        return ""
    try:
        p = urlsplit(url)
        if (p.scheme.lower() not in {"http", "https"} or not p.hostname
                or p.username is not None or p.password is not None):
            return ""
        _ = p.port
        return url
    except ValueError:
        return ""


def normalized_url(url: str) -> str:
    url = safe_source_url(url)
    if not url:
        return ""
    p = urlsplit(url)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                             if not k.lower().startswith("utm_")
                             and k.lower() not in {"spm", "from", "ref"}))
    return urlunsplit(("", p.netloc.lower().removeprefix("www."),
                       p.path.rstrip("/"), query, "")).removeprefix("//")


def current_date() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def display_status(status: str, deadline: str, *, today: date | None = None) -> str:
    """Age a known registration date on read; never rewrite verified source facts.

    Do not infer a year, pick a date out of a range, or reopen a closed event.
    Explicit clock times with no source timezone remain date-level clues.
    """
    today = today or current_date()
    match = re.fullmatch(r'(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?', (deadline or '').strip())
    if not match or status == '已结束':
        return status
    try:
        end = date(*map(int, match.groups()))
    except ValueError:
        return status
    if end < today:
        return '已结束'
    if status in {'报名中', '即将截止'}:
        return '即将截止' if (end - today).days <= 7 else '报名中'
    return status


def parse_deadline(value: str, *, reference_year: int | None = None) -> date | None:
    """Do not guess a year (or roll a past date into next year)."""
    match = re.fullmatch(r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?", (value or "").strip())
    if not match:
        return None
    year, month, day = match.groups()
    if not year and reference_year is None:
        return None
    try:
        return date(int(year) if year else reference_year, int(month), int(day))
    except ValueError:
        return None


def classify_contest(title: str, snippet: str = "", deadline: str = "",
                     published: str = "", *, today: date | None = None) -> tuple[str, str]:
    """Labels are hints, not verified registration eligibility."""
    today = today or current_date()
    blob = title + chr(10) + snippet
    online = bool(re.search(r"线上|在线|远程|online|virtual|remote", blob, re.I))
    offline = bool(re.search(r"线下|现场|offline|in[- ]person", blob, re.I))
    mixed = bool(re.search(r"混合|hybrid", blob, re.I))
    event_format = "混合" if mixed or (online and offline) else "线上" if online else "线下" if offline else "未知"
    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", blob)]
    published_year = re.search(r"(?<!\d)(20\d{2})(?!\d)", published or "")
    reference_year = max(years) if years else int(published_year.group(1)) if published_year else None
    old_publication = published_year is not None and int(published_year.group(1)) < today.year
    if not deadline:
        from .parse import extract_fields
        deadline = extract_fields(title, snippet)[0]
    end = parse_deadline(deadline, reference_year=reference_year)
    ended = bool(re.search(r"已结束|已截止|报名结束|报名截止了|(?<!未)(?<!尚未)落幕|(?<!未)收官|获奖名单|获奖公示|比赛结果|报名已截止|征集结束|\bclosed\b|\bended\b", blob, re.I))
    if ended or (end is not None and end < today):
        status = "已结束"
    elif end is not None:
        status = "即将截止" if (end - today).days <= 7 else "报名中"
    elif reference_year is not None and reference_year < today.year:
        status = "已结束"
    elif reference_year is not None and reference_year > today.year:
        status = "未知"
    elif old_publication:
        status = "未知"
    elif deadline:
        status = "未知"
    elif re.search(r"报名中|正在报名|开放报名|报名开启|征集中|征稿中|火热报名|registration open|applications open", blob, re.I):
        status = "未知" if deadline else "报名中"
    else:
        status = "未知"
    return status, event_format


# These rules only shorten supplied source text; they never translate or invent names.
_CONTEST_WORD = r"(?:黑客松|征文大赛|征文比赛|开发者大赛|挑战赛|创新大赛|竞赛|大赛|比赛|征文|hackathon|challenge|competition|contest)"
_NEWS_ACTION = r"(?:正式启动|即将启动|启动|开启|开幕|圆满落幕|落幕|圆满收官|收官|获奖名单|获奖公示|报名通知|征集通知|报名开启|火热报名|报名中|招募|来了|官宣|揭晓|将在|将于|將在|將於|将办|將辦|携手|攜手|联手|聯手|舉辦|舉行|举办|举行|发布|发布会|成功举办|顺利举办|圆满举行)"
_GENERIC_NAME = re.compile(r"(?:征文|征文启事|征文比赛|大赛|比赛|竞赛|黑客松)(?:的(?:写作|格式|要求|意思))?")


def _name_piece(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if _GENERIC_NAME.fullmatch(value):
        return ""
    # Strip recognizable publisher suffixes, not arbitrary words within a name.
    value = re.split(r"\s[|｜]\s*|\s[-—_]\s*(?=[^\n]*(?:网|日报|新闻|CSDN|博客|腾讯|搜狐|新浪|澎湃|知乎))", value, maxsplit=1, flags=re.I)[0]
    value = re.sub(r"[_|｜-](?:CSDN博客|CSDN|[^_|｜]{1,12}(?:新闻网|日报|官网|博客))$", "", value, flags=re.I)
    value = re.sub(r"^(?:重磅|喜讯|快讯|通知|报名|活动预告|赛事速递)[！!：:丨|\s]+", "", value)
    # A lead-in announcement is not part of the formal name.
    value = re.sub(r"^(?:关于|邀你参加|欢迎参加|报名参加|聚焦|直击|祝贺|恭喜|见证)\s*", "", value)
    quoted = re.search(r"[《【“\"]([^》】”\"]{1,100}" + _CONTEST_WORD + r"[^》】”\"]{0,24})[》】”\"]", value, re.I)
    if quoted:
        value = quoted.group(1)
    else:
        value = re.split(_NEWS_ACTION, value, maxsplit=1, flags=re.I)[0]
        value = re.sub(r"(?:的通知|报名指南|报名须知|赛事介绍|参赛指南|活动介绍|总决赛|决赛结果|颁奖典礼)$", "", value)
        # Keep only a candidate ending in a contest noun (plus a source-stated year).
        match = re.search(r"^(.{2,100}?" + _CONTEST_WORD + r")(?:\s+20\d{2})?(?:[：:，,！!。；;].*)?$", value, re.I)
        if not match:
            return ""
        value = match.group(0) if re.fullmatch(r".+\s+20\d{2}", match.group(0)) else match.group(1)
    value = value.strip(" \t：:，,！!。；;《》【】“”\"'丨|—-")
    if (not 3 <= len(value) <= 120 or not re.search(_CONTEST_WORD, value, re.I)
            or re.search(_NEWS_ACTION + r"|报道|记者|获奖|荣获|斩获|\b(?:launched|announced|wins?|report)\b", value, re.I)):
        return ""
    return value


def heuristic_contest_name(title: str, excerpt: str = "") -> str:
    """A conservative display clue, never a model/official verification claim."""
    candidate = _name_piece(title)
    if candidate:
        return candidate
    # Headlines without a recognizable contest noun are not safe display names.
    # Prefer explicit quoted names in the excerpt over headline prose.
    for match in re.finditer(r"[《【“\"]([^》】”\"\n]{3,140})[》】”\"]", excerpt or ""):
        quoted = match.group(1).strip()
        # Explicit source-quoted English names may end in Series, not Hackathon.
        if (3 <= len(quoted) <= 120 and re.search(_CONTEST_WORD, quoted, re.I)
                and not _GENERIC_NAME.fullmatch(quoted)
                and not re.search(_NEWS_ACTION + r"|报道|记者|获奖|荣获|斩获|\b(?:launched|announced|wins?|report)\b", quoted, re.I)):
            return quoted
        candidate = _name_piece(quoted)
        if candidate:
            return candidate
    for match in re.finditer(r"(?:赛事名称|比赛名称|大赛名称|正式名称)[：:]\s*([^。；;\n]{3,140})", excerpt or ""):
        candidate = _name_piece(match.group(1))
        if candidate:
            return candidate
    return "待核验赛事"


def supported_contest_name(value: str, quotes: list[str]) -> bool:
    """Allow source-exact names, or punctuation/spacing-only Chinese normalization.

    English official names must be stated verbatim in evidence; do not let arbitrary
    translations, reordered tokens or new qualifiers pass as normalized names.
    """
    if not value or value == "待核验赛事" or not _name_piece(value):
        return False
    compact = lambda s: re.sub(r"[\s《》【】“”\"'·•：:（）()—–-]", "", s).casefold()
    for quote in quotes:
        normalized = re.sub(r"\s+", " ", value).strip()
        if normalized in re.sub(r"\s+", " ", quote):
            # Exact English names are valid just like exact Chinese names.
            if _name_piece(value) == value.strip():
                return True
        if re.search(r"[\u4e00-\u9fff]", value):
            candidates = [_name_piece(quote)]
            candidates.extend(_name_piece(m.group(1)) for m in re.finditer(
                r"[《【“\"]([^》】”\"\n]{3,140})[》】”\"]", quote))
            if any(candidate and compact(value) == compact(candidate) for candidate in candidates):
                return True
    return False


def enrich_card(card, *, today: date | None = None):
    card.source_url = safe_source_url(card.source_url)
    if not getattr(card, "raw_title", ""):
        card.raw_title = card.name
    if not getattr(card, "contest_name", ""):
        card.contest_name = heuristic_contest_name(card.raw_title or card.name,
                                                     card.source_excerpt or card.snippet)
    # Curated facts, Luna verification and OpenClaw 整理 (review) must not be replaced by heuristics.
    if not getattr(card, "curated", False) and getattr(card, "luna_state", "pending") not in {"verified", "stale", "review"} and not getattr(card, "human_reviewed_at", ""):
        card.status, card.format = classify_contest(card.name, card.source_excerpt or card.snippet, card.deadline,
                                                   card.published, today=today)
    return card
