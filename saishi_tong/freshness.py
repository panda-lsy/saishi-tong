"""Event-time gate, independent of webpage publication/search time or AI approval."""
import re
from datetime import date
from . import classify

def current_date():
    return classify.current_date()

display_status = classify.display_status

_DATE = r'20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?'
_REGISTRATION = r'(?:作品(?:提交|征集)|报名|投稿|征稿|征集|registration|submissions?)'
_RANGE = re.compile(
    _REGISTRATION + r'(?:时间|阶段|日期|周期|期)?[^\d\n。；]{0,12}('
    + _DATE + r')(?:\s+\d{1,2}:\d{2})?\s*(?:至|到|—|–|~|～|--|-)\s*('
    + _DATE + r')', re.I)
def _start_patterns(date_pattern):
    """Require opening words adjacent to the date, not a later deadline."""
    opening = r'(?:开放|开启|开始|启动)'
    separator = r'\s*(?:[:：]|为)?\s*'
    return [re.compile(pattern, re.I) for pattern in (
        _REGISTRATION + r'(?:正式)?(?:' + opening + r'|\s+opens?)(?:时间|日期)?' + separator + '(' + date_pattern + r')',
        opening + _REGISTRATION + r'(?:时间|日期)?' + separator + '(' + date_pattern + r')',
        _REGISTRATION + r'(?:自|将于|从|于)\s*(' + date_pattern + r')\s*(?:起|正式|即将)?' + opening,
        r'(' + date_pattern + r')\s*(?:起|正式|即将)?' + opening + _REGISTRATION,
        r'(' + date_pattern + r')\s*起' + _REGISTRATION,
        r'(' + date_pattern + r')\s*(?:正式)?' + _REGISTRATION + r'(?:正式)?' + opening,
    )]


_START_PATTERNS = _start_patterns(_DATE)
_SHORT_START_PATTERNS = _start_patterns(r'(?<![\d./年-])\d{1,2}月\d{1,2}日?')
_CLAUSE_DEADLINE = re.compile(
    r'(?:报名|投稿|征稿|征集|作品提交)?(?:截止|截至|结束|截稿)'
    r'(?:报名|投稿|时间|日期|日)?\s*(?:[:：]|为)?\s*(' + _DATE + r')', re.I)
_NOT_OPEN = re.compile(
    r'(?:报名|投稿|征稿|征集)(?:通道)?(?:尚未|暂未|还未|未|即将)(?:开始|开放|开启)'
    r'|(?:尚未|暂未|还未|未|即将)(?:开始|开放|开启)(?:报名|投稿|征稿|征集)'
    r'|\bregistration\s+(?:not\s+yet\s+open|opens?\s+soon)\b', re.I)
_OPEN = re.compile(
    r'正在报名|报名中|报名(?:已|现已)?(?:开放|开启|启动|开始)(?!\s*(?:时间|日期))|开放报名|火热报名'
    r'|征集中|征稿中|投稿中|正在(?:征集|征稿|招募)'
    r'|\bregistration\s+(?:is\s+)?open\b'
    r'|\b(?:applications|submissions)\s+(?:are\s+)?open\b', re.I)


def deadline_date(value):
    match = re.fullmatch(r'(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?', (value or '').strip())
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _opening_evidence(get, deadline):
    """Read only registration/submission dates, never the contest/finals dates.

    A range ending on another date belongs to another phase/edition; do not
    hide a currently open first round because a source also lists a later one.
    """
    fragments = [get(key) for key in ('timeline', 'source_excerpt', 'snippet', 'summary', 'raw_title', 'name')]
    fragments += [s.get('excerpt', '') for s in (get('sources', []) or []) if isinstance(s, dict)]
    starts, not_open, open_now, ambiguous_start = set(), False, False, False
    for fragment in fragments:
        if not isinstance(fragment, str):
            continue
        # A later phase may share a source with the current one. Only use a
        # clause tied to this deadline, or a clause without a stated deadline.
        for match in _RANGE.finditer(fragment):
            start, end = map(deadline_date, match.groups())
            if start and end == deadline and start <= end:
                starts.add(start)
        for clause in re.split(r'[\n。；;]', fragment):
            clause_deadlines = {deadline_date(m.group(1)) for m in _CLAUSE_DEADLINE.finditer(clause)}
            clause_deadlines.discard(None)
            if clause_deadlines and deadline not in clause_deadlines:
                continue
            clause_starts = set()
            for pattern in _START_PATTERNS:
                for match in pattern.finditer(clause):
                    start = deadline_date(match.group(1))
                    if start:
                        clause_starts.add(start)
            starts.update(clause_starts)
            # A dated announcement may stay online after it has opened.
            if not clause_starts and _NOT_OPEN.search(clause):
                not_open = True
            if not clause_starts and any(p.search(clause) for p in _SHORT_START_PATTERNS):
                ambiguous_start = True
            if _OPEN.search(clause):
                open_now = True
    return starts, not_open, open_now, ambiguous_start


def assess_event(card, *, today=None):
    get = card.get if isinstance(card, dict) else lambda key, default='': getattr(card, key, default)
    today = today or current_date()
    status, deadline = get('status'), get('deadline')
    current_status = display_status(status, deadline, today=today)
    if current_status == '已结束':
        return {'state': 'expired', 'reason': '赛事已结束或报名／征集截止日期已过。'}
    end = deadline_date(deadline)
    if end is None:
        return {'state': 'pending', 'reason': '缺少明确含年份的报名／征集截止日期，时效待核；不作为新赛事搜索结果。'}
    starts, not_open, open_now, ambiguous_start = _opening_evidence(get, end)
    if any(start > today for start in starts) or not_open:
        return {'state': 'pending', 'reason': '来源标明报名／征集尚未开放，暂不作为进行中的赛事搜索结果。'}
    if ambiguous_start:
        return {'state': 'pending', 'reason': '报名／征集开始日期缺少年份，时效待核；不作为进行中的赛事搜索结果。'}
    if current_status not in {'报名中', '即将截止'}:
        return {'state': 'pending', 'reason': '报名／征集状态仍不明确；仅有未来截止日期不能证明正在进行，留待核验。'}
    from .verification import human_reviewed
    reviewed_status = human_reviewed(card) or (
        get('luna_state') == 'verified' and get('luna_model') and get('luna_verified_at')
        and 'status' not in (get('uncertain_fields', []) or []))
    if not (reviewed_status or starts or open_now):
        return {'state': 'pending', 'reason': '尚无正在报名／征集的来源证据；未来截止日期仅为线索，留待核验。'}
    return {'state': 'current', 'reason': '来源提供的报名／征集截止日期未过，仍需核对报名规则。'}
