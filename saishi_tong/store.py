"""SQLite catalog with additive migrations and protected, stable curation records."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
import re
import sqlite3
import uuid
from difflib import SequenceMatcher
from dataclasses import MISSING, asdict, fields
from datetime import datetime
from pathlib import Path

from .classify import enrich_card, normalized_url, safe_source_url
from .parse import ContestCard, _norm_title, _now_iso
from .verification import VERIFIED_SQL, REVIEW_GROUP_SQL, describe, facts_hash, human_reviewed, is_verified

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "contests.db"
FIELD_DEFS = {f.name: f for f in fields(ContestCard)}
COLUMNS = list(FIELD_DEFS)
JSON_COLUMNS = {"sources", "uncertain_fields", "verification_notes"}
# Raw retrieval evidence can advance without replacing curated/verified facts.
SEARCH_MATERIAL = {"search_time", "query", "raw_title", "source_excerpt", "score", "site"}


def database_path() -> Path:
    return Path(os.environ.get("SAISHI_DB_PATH", str(DEFAULT_DB))).expanduser()


def stable_id(card: ContestCard) -> str:
    identity = normalized_url(card.source_url) or "title:" + _norm_title(card.name)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _default(name):
    f = FIELD_DEFS[name]
    if f.default is not MISSING:
        return f.default
    return f.default_factory() if f.default_factory is not MISSING else ""


def _encode(name, value):
    if name in JSON_COLUMNS:
        return json.dumps(value if isinstance(value, list) else [], ensure_ascii=False, allow_nan=False)
    return int(bool(value)) if name == "curated" else value


def _column_sql(name):
    kind = "REAL" if name == "score" else "INTEGER" if name == "curated" else "TEXT"
    default = _encode(name, _default(name))
    literal = str(default) if isinstance(default, (int, float)) else "'" + str(default).replace("'", "''") + "'"
    return '"' + name + '" ' + kind + (" PRIMARY KEY" if name == "id" else " DEFAULT " + literal)


def _connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path) if path is not None else database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS contests (" + ", ".join(_column_sql(n) for n in COLUMNS) + ")")
        existing = {r[1] for r in conn.execute("PRAGMA table_info(contests)")}
        for name in COLUMNS:
            if name not in existing:
                try:
                    conn.execute("ALTER TABLE contests ADD COLUMN " + _column_sql(name))
                except sqlite3.OperationalError as exc:
                    # Another request may have performed this exact additive migration.
                    if "duplicate column name" not in str(exc).lower():
                        raise
        if 'ai_reviewed_at' not in existing and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='curation_items'").fetchone():
            conn.execute("UPDATE contests SET ai_reviewed_at=luna_verified_at WHERE luna_state IN ('verified','review') AND luna_model != '' AND luna_verified_at != '' AND id IN (SELECT contest_id FROM curation_items WHERE status='done')")
        if 'location_scope' not in existing:
            # The additive unknown field does not revoke a valid prior human
            # confirmation or grant a new one. Preserve its original timestamp.
            for row in conn.execute("SELECT * FROM contests WHERE human_reviewed_at != ''").fetchall():
                card=_decode(row)
                if card.human_reviewed_hash == facts_hash(card):
                    continue  # Another request already migrated this exact proof.
                if card.human_reviewed_hash == facts_hash(card,include_location=False):
                    conn.execute('UPDATE contests SET human_reviewed_hash=? WHERE id=?',(facts_hash(card),card.id))
                else:
                    conn.execute("UPDATE contests SET human_reviewed_at='',human_reviewed_hash='' WHERE id=?",(card.id,))
        conn.execute("""CREATE TABLE IF NOT EXISTS contest_deletion_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,contest_id TEXT NOT NULL,
            action TEXT NOT NULL,acted_at TEXT NOT NULL,before_fingerprint TEXT NOT NULL,after_fingerprint TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS contest_merge_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merged_at TEXT NOT NULL,
            target_id TEXT NOT NULL,
            source_ids TEXT NOT NULL,
            previous_states TEXT NOT NULL,
            conflicts TEXT NOT NULL,
            before_records TEXT NOT NULL,
            after_fingerprint TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS contest_keep_separate (
            first_id TEXT NOT NULL, second_id TEXT NOT NULL, decided_at TEXT NOT NULL,
            job_id TEXT NOT NULL, PRIMARY KEY(first_id,second_id))""")
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def initialize(path: Path | None = None) -> None:
    _connect(path).close()


def _decode(row) -> ContestCard:
    data = {name: dict(row).get(name, _default(name)) for name in COLUMNS}
    for name in JSON_COLUMNS:
        try:
            value = json.loads(data[name]) if isinstance(data[name], str) else data[name]
            data[name] = value if isinstance(value, list) else []
        except (ValueError, TypeError):
            data[name] = []
    data["sources"] = [s for s in data["sources"] if isinstance(s, dict) and safe_source_url(s.get("url", ""))]
    for name in ("uncertain_fields", "verification_notes"):
        data[name] = [s for s in data[name] if isinstance(s, str)]
    data["curated"] = data["curated"] in (True, 1, "1")
    for name in COLUMNS:
        if data[name] is None:
            data[name] = _default(name)
    # Old/corrupt JSON cannot introduce arbitrary source keys or scalar objects.
    data["sources"] = [{k: s.get(k, "") for k in ("url", "title", "excerpt")}
                       for s in data["sources"]
                       if all(isinstance(s.get(k, ""), str) for k in ("url", "title", "excerpt"))]
    for name in COLUMNS:
        if name not in JSON_COLUMNS and name not in {"curated", "score"} and not isinstance(data[name], str):
            data[name] = _default(name)
    if not isinstance(data["score"], (float, int)):
        data["score"] = 0.0
    # URL validation is deliberately tolerant for legacy rows on read.
    for name in ("source_url", "signup_url"):
        data[name] = safe_source_url(data[name])
    return ContestCard.fromdict(data)


def _write(conn, card):
    if card.luna_state in {'pending','stale','failed','disabled'}:
        card.ai_reviewed_at = ''
    if card.human_reviewed_at and not human_reviewed(card):
        card.human_reviewed_at = card.human_reviewed_hash = ''
        card.verification_notes.append('事实或主来源已变化，原人工校对已失效；请重新校对。')
    names = ", ".join('"' + n + '"' for n in COLUMNS)
    updates = ", ".join('"' + n + '" = excluded."' + n + '"' for n in COLUMNS if n != "id")
    values = asdict(card)
    conn.execute("INSERT INTO contests (" + names + ") VALUES (" + ", ".join("?" for _ in COLUMNS)
                 + ") ON CONFLICT(id) DO UPDATE SET " + updates,
                 [_encode(n, values[n]) for n in COLUMNS])


def _existing(conn, card):
    row = conn.execute("SELECT * FROM contests WHERE id = ?", (card.id or stable_id(card),)).fetchone()
    if row is not None:
        return _decode(row)
    # Manual records retain their id even when their source URL is edited.
    identity = normalized_url(card.source_url)
    if identity:
        for candidate in conn.execute("SELECT * FROM contests"):
            if normalized_url(candidate["source_url"]) == identity:
                return _decode(candidate)
    return None


def upsert_contests(cards: list[ContestCard], path: Path | None = None) -> list[ContestCard]:
    conn = _connect(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for index, card in enumerate(cards):
                enrich_card(card)
                old = _existing(conn, card)
                if old:
                    if old.publication_state == "retired" or old.merged_into or old.deleted_at:
                        # A later offline search must not revive or rewrite a tombstone.
                        cards[index] = old
                        card.id = old.id
                        continue
                    card.id = old.id
                    has_verified_facts = old.luna_state in {"verified", "stale"}
                    protected = old.curated or human_reviewed(old) or (has_verified_facts and card.luna_state != "verified")
                    if protected:
                        # Compare immutable extraction inputs BEFORE restoring old facts.
                        # The old hash/model/time describe the last successful run only.
                        from .luna import FACT_FIELDS, input_hash
                        source_changed = has_verified_facts and input_hash(card) != old.luna_input_hash
                        new_notes = list(card.verification_notes)
                        new_uncertain = list(card.uncertain_fields)
                        for name in COLUMNS:
                            if name not in SEARCH_MATERIAL:
                                setattr(card, name, getattr(old, name))
                        if source_changed:
                            card.luna_state = "stale"
                            note = "来源已变更，本次未完成核验；保留事实来自上次核验，模型、时间与哈希均为上次记录。"
                            card.verification_notes = list(dict.fromkeys(old.verification_notes + new_notes + [note]))
                            card.uncertain_fields = sorted(set(old.uncertain_fields + new_uncertain + list(FACT_FIELDS)))
                    else:
                        card.publication_state = old.publication_state
                        card.record_origin = old.record_origin
                else:
                    card.id = card.id or stable_id(card)
                _write(conn, card)
    finally:
        conn.close()
    return cards


def _card_dict(row, *, current=False) -> dict:
    from .classify import display_status
    data = describe(enrich_card(_decode(row)))
    data['record_fingerprint'] = record_fingerprint(data)
    if current:
        data['status'] = display_status(data['status'], data['deadline'])
    return data


def list_contests(limit: int | None = None, *, include_drafts: bool = False,
                  path: Path | None = None) -> list[dict]:
    """Public catalog lists verified published contests only.

    include_drafts=True is for internal/admin helpers (non-retired, not merged);
    it does not apply the verified filter.
    """
    conn = _connect(path)
    try:
        if include_drafts:
            sql = "SELECT * FROM contests WHERE publication_state != 'retired' AND merged_into = '' AND deleted_at = ''"
        else:
            sql = "SELECT * FROM contests WHERE publication_state = 'published' AND deleted_at = '' AND " + VERIFIED_SQL
        sql += " ORDER BY search_time DESC, score DESC, id"
        rows = conn.execute(sql + (" LIMIT ?" if limit is not None else ""),
                            (max(0, limit),) if limit is not None else ()).fetchall()
        return [_card_dict(row, current=not include_drafts) for row in rows]
    finally:
        conn.close()


CATALOG_STATUSES = ("报名中", "即将截止", "已结束", "未知")
CATALOG_FORMATS = ("线上", "线下", "混合", "未知")
CATALOG_LOCATIONS = ('国内','国外','未知')


def catalog_filter_values(value, choices) -> list[str]:
    """Normalize single/repeated filter values without accepting invented tags."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or len(values) > 20:
        raise ValueError("筛选字段无效")
    if any(not isinstance(v, str) or v.strip() not in ("", *choices) for v in values):
        raise ValueError("筛选字段无效")
    selected = {v.strip() for v in values if v.strip()}
    return [choice for choice in choices if choice in selected]


def search_contests(*, q: str = "", status: str | list[str] = "", format: str | list[str] = "",
                    location: str | list[str] = "", limit: int | None = None, path: Path | None = None) -> list[dict]:
    """Published+verified local catalog search; no credentials, network, or implicit cap.

    Filters apply to display-time labels (including conservative legacy heuristics),
    before the optional limit. LIKE values are bound and wildcard characters literal.
    Unverified contests stay in the DB/admin APIs but never appear here.
    """
    if not isinstance(q, str):
        raise ValueError("查询必须是文本")
    statuses = catalog_filter_values(status, CATALOG_STATUSES)
    formats = set(catalog_filter_values(format, CATALOG_FORMATS))
    locations = catalog_filter_values(location,CATALOG_LOCATIONS)
    if locations and not formats & {'线下','混合'}:
        raise ValueError('地区筛选需先选择线下或混合')
    if formats & {"线上", "线下"}:
        formats.add("混合")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        raise ValueError("数量限制无效")
    conn = _connect(path)
    try:
        sql = "SELECT * FROM contests WHERE publication_state = 'published' AND deleted_at = '' AND " + VERIFIED_SQL
        params = []
        text = q.strip()
        if text:
            pattern = "%" + text.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            search_fields = ("contest_name", "name", "raw_title", "summary", "organizer",
                             "snippet", "host", "source_excerpt", "eligibility", "prizes")
            sql += " AND (" + " OR ".join('"' + n + "\" LIKE ? ESCAPE '!'" for n in search_fields) + ")"
            params = [pattern] * len(search_fields)
        sql += " ORDER BY search_time DESC, score DESC, id"
        cards = [_card_dict(row, current=True) for row in conn.execute(sql, params)]
        cards = [c for c in cards if (not statuses or c["status"] in statuses)
                 and (not formats or c["format"] in formats)
                 and (not locations or c['format'] in {'线下','混合'} and c['location_scope'] in locations)]
        return cards if limit is None else cards[:limit]
    finally:
        conn.close()


def count_contests(path: Path | None = None, *, include_drafts: bool = False) -> int:
    conn = _connect(path)
    try:
        if include_drafts:
            where = " WHERE publication_state != 'retired' AND merged_into = '' AND deleted_at = ''"
        else:
            where = " WHERE publication_state = 'published' AND deleted_at = '' AND " + VERIFIED_SQL
        return conn.execute("SELECT COUNT(*) FROM contests" + where).fetchone()[0]
    finally:
        conn.close()


def get_contest(contest_id: str, path: Path | None = None, *, include_drafts: bool = False) -> dict | None:
    conn = _connect(path)
    try:
        where = "" if include_drafts else " AND publication_state = 'published' AND deleted_at = '' AND " + VERIFIED_SQL
        row = conn.execute("SELECT * FROM contests WHERE deleted_at = '' AND id = ?" + where, (contest_id,)).fetchone()
        return _card_dict(row, current=not include_drafts) if row is not None else None
    finally:
        conn.close()


ADMIN_LIMITS = {"contest_name": 300, "name": 300, "snippet": 8000, "summary": 8000, "organizer": 1000,
                "host": 1000, "timeline": 8000, "eligibility": 8000, "prizes": 8000,
                "prize": 8000, "deadline": 1000, "source_url": 2048, "signup_url": 2048,
                "search_time": 100, "status": 16, "format": 16, "location_scope": 16, "publication_state": 16,
                "review_note": 2000}
ADMIN_ALIASES = {"title": "name", "mode": "format", "deadlines": "deadline"}


def _admin_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("赛事数据必须是对象")
    data = {}
    for key, value in payload.items():
        name = ADMIN_ALIASES.get(key, key)
        if name == 'expected_fingerprint':
            if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
                raise ValueError('fingerprint_required')
            data[name] = value
            continue
        if name == "human_reviewed":
            if not isinstance(value, bool):
                raise ValueError("人工校对状态必须是布尔值")
            data[name] = value
            continue
        if name not in ADMIN_LIMITS and name != "sources":
            raise ValueError("包含不支持的字段")
        if name == "sources":
            if not isinstance(value, list) or len(value) > 30:
                raise ValueError("来源列表格式无效")
            sources = []
            for source in value:
                if not isinstance(source, dict) or set(source) - {"url", "title", "excerpt"}:
                    raise ValueError("来源格式无效")
                cleaned = {}
                for part, maximum in (("url", 2048), ("title", 300), ("excerpt", 12000)):
                    text = source.get(part, "")
                    if not isinstance(text, str) or len(text) > maximum:
                        raise ValueError("来源字段格式或长度无效")
                    cleaned[part] = text.strip()
                if not safe_source_url(cleaned["url"]):
                    raise ValueError("来源必须使用有效的 HTTP(S) URL")
                sources.append(cleaned)
            data[name] = sources
            continue
        if name == "deadline" and isinstance(value, list) and all(isinstance(x, str) for x in value):
            value = "；".join(value)
        if not isinstance(value, str) or len(value) > ADMIN_LIMITS[name]:
            raise ValueError("字段格式或长度无效")
        if name in data and data[name] != value.strip():
            raise ValueError("字段别名冲突")
        data[name] = value.strip()
    for name in ("source_url", "signup_url"):
        if data.get(name) and not safe_source_url(data[name]):
            raise ValueError("链接必须使用有效的 HTTP(S) URL")
    for name, valid in (("status", {"报名中", "即将截止", "已结束", "未知"}),
                        ("format", {"线上", "线下", "混合", "未知"}),
                        ("location_scope", {"国内", "国外", "未知"}),
                        ("publication_state", {"draft", "published"})):
        if name in data and data[name] not in valid:
            raise ValueError("枚举字段无效")
    if "search_time" in data:
        try:
            stamp = data["search_time"].removesuffix(" Asia/Shanghai")
            if "T" not in stamp and " " not in stamp:
                raise ValueError
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("检索时间必须是有效的 ISO 日期时间") from None
    # Keep modern and historical presentation fields in sync.
    for modern, legacy in (("summary", "snippet"), ("organizer", "host"), ("prizes", "prize")):
        if modern in data:
            data[legacy] = data[modern]
        elif legacy in data:
            data[modern] = data[legacy]
    return data


def save_admin_contest(payload: dict, contest_id: str | None = None,
                       path: Path | None = None) -> dict:
    data = _admin_payload(payload)
    review = data.pop("human_reviewed", None)
    review_note = data.pop('review_note', '')
    if review_note and review is not True:
        raise ValueError('校对说明需要与本次人工确认一同提交')
    expected = data.pop('expected_fingerprint', None)
    conn = _connect(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone() if contest_id else None
            if contest_id and row is None:
                raise LookupError("赛事不存在")
            card = _decode(row) if row is not None else ContestCard(name="", id=uuid.uuid4().hex[:24],
                      publication_state="draft", record_origin="manual", search_time=_now_iso())
            if card.publication_state == "retired" or card.merged_into or card.deleted_at:
                raise ValueError("contest_retired")
            if row is not None and expected is not None:
                _check_fingerprint(card, expected)
            if row is not None and not card.raw_title:
                # Snapshot the original legacy title before a name-only admin edit.
                card.raw_title = card.name
            before_facts = facts_hash(card)
            for name, value in data.items():
                setattr(card, name, value)
            if ("sources" in data and "source_url" not in data
                    and (row is None or card.source_url not in {s['url'] for s in data['sources']})):
                card.source_url = data["sources"][0]["url"] if data["sources"] else ""
            if "source_url" in data and "sources" not in data:
                card.sources = [{"url": card.source_url, "title": card.name, "excerpt": card.source_excerpt}]
            # New payloads can use contest_name alone; legacy name/title stays supported.
            # Never replace raw_title: it is immutable retrieval evidence for this edit.
            if "contest_name" in data and not card.contest_name:
                raise ValueError("赛事名称不能为空")
            if "contest_name" in data and not card.name:
                card.name = card.contest_name
            if "name" in data and "contest_name" not in data:
                card.contest_name = card.name
            if not card.name or not safe_source_url(card.source_url) or not card.search_time:
                raise ValueError("名称、真实来源 URL 和检索时间必填")
            if not card.sources:
                card.sources = [{"url": card.source_url, "title": card.name, "excerpt": ""}]
            if (row is not None and before_facts != facts_hash(card)
                    and (card.luna_state == "verified" or card.ai_reviewed_at)):
                card.luna_state = "stale"
                card.verification_notes.append("管理员修改事实或主来源，原AI核验对应旧版本；请重新核验或完成人工校对。")
            if review is True:
                card.human_reviewed_at = datetime.now().astimezone().isoformat(timespec="seconds")
                card.human_reviewed_hash = facts_hash(card)
                if review_note:
                    card.verification_notes.append('授权校对说明：' + review_note)
            elif review is False:
                card.human_reviewed_at = card.human_reviewed_hash = ""
            card.curated = True
            if "管理员手动策展；来源与时间由管理员提供或记录。" not in card.verification_notes:
                card.verification_notes.append("管理员手动策展；来源与时间由管理员提供或记录。")
            _write(conn, card)
            result = describe(card)
            result['record_fingerprint'] = record_fingerprint(enrich_card(_as_card(card)))
            return result
    finally:
        conn.close()


# These helpers are deliberately local/SQLite-only. Authorization and model
# invocation belong to the administrator API, never to public catalog search.
def _as_card(value) -> ContestCard:
    return ContestCard.fromdict(asdict(value) if isinstance(value, ContestCard) else value)


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def record_fingerprint(card_or_dict) -> str:
    """Hash the complete supplied record without mutating or enriching it.

    Retrieval time, curation and publication metadata are intentionally included;
    unlike an extraction-input hash this also detects non-source fact edits.
    """
    card = _as_card(card_or_dict)
    return hashlib.sha256(_stable_json(asdict(card)).encode("utf-8")).hexdigest()


def list_admin_contests(*, verification: str = "all", page: int = 1,
                        page_size: int = 15, q: str = "", path: Path | None = None) -> dict:
    if verification not in {"all", "verified", "awaiting", "unverified"}:
        raise ValueError("invalid_verification_filter")
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in (page, page_size)):
        raise ValueError("invalid_pagination")
    if not isinstance(q, str) or len(q) > 1000:
        raise ValueError("invalid_query")
    page_size = min(page_size, 50)
    conn = _connect(path)
    try:
        where = "publication_state != 'retired' AND merged_into = '' AND deleted_at = ''"
        params = []
        if q.strip():
            pattern = "%" + q.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            where += " AND (" + " OR ".join('"' + n + "\" LIKE ? ESCAPE '!'" for n in
                        ("contest_name", "name", "raw_title", "summary", "organizer", "host")) + ")"
            params = [pattern] * 6
        counts = {"verified": 0, "awaiting": 0, "unverified": 0}
        # Count both tabs against the same query before applying the tab filter.
        for row in conn.execute("SELECT " + REVIEW_GROUP_SQL + " AS review_group, COUNT(*) AS n FROM contests WHERE " + where + " GROUP BY review_group", params):
            counts[row['review_group']] += row['n']
        if verification != "all":
            where += " AND (" + REVIEW_GROUP_SQL + ") = '" + verification + "'"
        total = sum(counts.values()) if verification == "all" else counts[verification]
        priority = ""
        rows = conn.execute("SELECT * FROM contests WHERE " + where +
                            " ORDER BY " + priority + "search_time DESC, score DESC, id LIMIT ? OFFSET ?",
                            params + [page_size, min((page - 1) * page_size, total)]).fetchall()
        return {"contests": [_card_dict(r) for r in rows], "total": total, "page": page,
                "page_size": page_size, "pages": (total + page_size - 1) // page_size,
                "counts": counts}
    finally:
        conn.close()


def list_deleted_contests(path=None):
    with closing(_connect(path)) as conn:
        return [_card_dict(r) for r in conn.execute("SELECT * FROM contests WHERE deleted_at != '' ORDER BY deleted_at DESC,id")]


def change_deleted_state(contest_id,expected_fingerprint,*,restore=False,path=None):
    with closing(_connect(path)) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM contests WHERE id=?',(contest_id,)).fetchone()
        if row is None:raise LookupError('contest_not_found')
        card=_decode(row)
        if card.publication_state=='retired' or card.merged_into:raise ValueError('contest_retired')
        if bool(card.deleted_at) != restore:raise ValueError('record_changed')
        _check_fingerprint(card,expected_fingerprint)
        before=record_fingerprint(card)
        card.deleted_at='' if restore else datetime.now().astimezone().isoformat(timespec='seconds')
        _write(conn,card)
        conn.execute('INSERT INTO contest_deletion_audit (contest_id,action,acted_at,before_fingerprint,after_fingerprint) VALUES (?,?,?,?,?)',
                     (card.id,'restore' if restore else 'delete',_now_iso(),before,record_fingerprint(card)))
        return describe(card)


def _source_identity(url: str) -> str:
    """Normalize only the known Tencent news/new article aliases.

    Keep arbitrary hosts and every article path/query distinct; a shared home
    page or organizer is not a universal contest identity.
    """
    identity = normalized_url(url)
    match = re.fullmatch(r"(?:new|news)\.qq\.com(/rain/a/[A-Za-z0-9]+(?:\.html)?)(\?.*)?", identity)
    return "qq.com" + match.group(1) + (match.group(2) or "") if match else identity


def _source_keys(card):
    return {_source_identity(url) for url in [card.source_url] + [s["url"] for s in card.sources]
            if _source_identity(url)}


def separate_ids(conn, contest_id):
    return {r[1] if r[0] == contest_id else r[0] for r in conn.execute(
        'SELECT first_id,second_id FROM contest_keep_separate WHERE first_id=? OR second_id=?',
        (contest_id, contest_id))}


def remember_separate(conn, contest_id, other_ids, job_id):
    for other in other_ids:
        first, second = sorted((contest_id, other))
        conn.execute('INSERT OR IGNORE INTO contest_keep_separate VALUES (?,?,?,?)',
                     (first, second, _now_iso(), job_id))


def _formal_name(card):
    from .classify import heuristic_contest_name
    name = card.contest_name or heuristic_contest_name(card.raw_title or card.name, card.source_excerpt or card.snippet)
    return "" if name in {"待核验赛事", "未知"} else re.sub(r"[\W_]+", "", name.casefold())


def _dates(card):
    text = card.deadline + " " + card.timeline
    return {"%04d-%02d-%02d" % tuple(map(int, m)) for m in
            re.findall(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)}


def find_duplicate_contests(card_or_dict, *, exclude_id: str | None = None,
                            path: Path | None = None) -> list[dict]:
    """Auditable heuristic suggestions only; never merge or call a provider."""
    from .luna import input_hash
    card = _as_card(card_or_dict)
    exclude_id = exclude_id or card.id
    keys, title, dates = _source_keys(card), _formal_name(card), _dates(card)
    host = _norm_title(card.organizer or card.host)
    original_hash = input_hash(card)
    # A model-output hash is not an independent proof if its source is stale.
    conn = _connect(path)
    try:
        matches = []
        blocked = separate_ids(conn, exclude_id)
        for row in conn.execute("SELECT * FROM contests WHERE publication_state != 'retired' AND merged_into = '' AND deleted_at = ''"):
            other = _decode(row)
            if other.id == exclude_id or other.id in blocked:
                continue
            reasons, score = [], 0.0
            # A shared list/news reference can describe different events. Only a
            # primary detail URL or matching formal name supplies identity here.
            shared = keys & _source_keys(other)
            same_name = bool(title and title == _formal_name(other))
            primary_match = bool(shared and (
                _source_identity(card.source_url) == _source_identity(other.source_url)
                or same_name and (_source_identity(card.source_url) in shared
                                 or _source_identity(other.source_url) in shared)))
            if primary_match:
                reasons.append("canonical_source_url_match")
                score = .99
            if original_hash == input_hash(other) and keys and _source_keys(other):
                reasons.append("source_input_hash_match")
                score = 1.0
            other_title = _formal_name(other)
            similarity = SequenceMatcher(None, title, other_title).ratio() if title and other_title else 0
            other_host = _norm_title(other.organizer or other.host)
            same_host = bool(host and host == other_host)
            other_dates = _dates(other)
            same_dates = bool(dates & other_dates)
            disjoint_dates = bool(dates and other_dates and not same_dates)
            # Identical generic names from different years/editions are not
            # enough; different explicit dates veto title-only suggestions.
            years = set(re.findall(r"20\d{2}", title))
            other_years = set(re.findall(r"20\d{2}", other_title))
            editions_differ = bool(years and other_years and years.isdisjoint(other_years))
            if similarity >= .9 and len(title) >= 5 and not disjoint_dates and not editions_differ:
                if title == other_title:
                    reasons.append("formal_name_exact")
                    score = max(score, .82)
                elif same_host and same_dates:
                    reasons.append("formal_name_similar")
                    score = max(score, .78)
                if reasons and same_host:
                    reasons.append("organizer_match")
                    score = min(1.0, score + .04)
                if reasons and same_dates:
                    reasons.append("timeline_date_match")
                    score = min(1.0, score + .06)
            if reasons:
                matches.append({"id": other.id, "contest_name": enrich_card(other).contest_name,
                                "score": round(score, 3), "reasons": reasons,
                                "source_url": other.source_url, "status": other.status,
                                "organizer": other.organizer or other.host, "deadline": other.deadline,
                                "timeline": other.timeline, "search_time": other.search_time,
                                "publication_state": other.publication_state,
                                "fingerprint": record_fingerprint(other)})
        return sorted(matches, key=lambda item: (-item["score"], item["id"]))[:50]
    finally:
        conn.close()



def _active_record(conn, contest_id):
    if not isinstance(contest_id, str) or not contest_id or len(contest_id) > 128:
        raise ValueError("invalid_contest_id")
    row = conn.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone()
    if row is None:
        raise LookupError("contest_not_found")
    card = _decode(row)
    if card.publication_state == "retired" or card.merged_into or card.deleted_at:
        raise ValueError("contest_retired")
    return card


def _check_fingerprint(card, expected):
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("fingerprint_required")
    # Administrator snapshots come from GET, which adds conservative legacy
    # display defaults. Normalize a copy of the locked row in exactly that way.
    if record_fingerprint(enrich_card(_as_card(card))) != expected:
        raise ValueError("record_changed")


def _verified_update(current, verified_card):
    """Apply an already Luna._apply-validated result to its exact source snapshot.

    Public edit payloads cannot set Luna metadata. This function is an internal
    commit boundary for the explicitly authenticated verification flow only.
    """
    from .luna import ENUMS, FACT_FIELDS, TEXT_LIMITS, input_hash
    verified = _as_card(verified_card)
    if verified.id and verified.id != current.id:
        raise ValueError("verification_record_mismatch")
    if (verified.luna_state != "verified" or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,150}", verified.luna_model)
            or not verified.luna_verified_at):
        raise ValueError("verification_incomplete")
    try:
        datetime.fromisoformat(verified.luna_verified_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("verification_incomplete") from None
    if not verified.luna_input_hash or verified.luna_input_hash != input_hash(current):
        raise ValueError("verification_source_changed")
    # Normalized sources from _apply have the same extraction hash. Neither a
    # different input record nor an edited model result source may slip through.
    if input_hash(verified) != verified.luna_input_hash:
        raise ValueError("verification_source_changed")
    for name in FACT_FIELDS:
        value = getattr(verified, name)
        if len(value) > TEXT_LIMITS.get(name, 20) or (name in ENUMS and value not in ENUMS[name]):
            raise ValueError("invalid_verification_fields")
    updated = _as_card(current)
    kept = []
    for name in FACT_FIELDS:
        previous, proposed = getattr(current, name), getattr(verified, name)
        if (current.curated or current.record_origin == "curated") and previous and previous != "未知":
            if previous != proposed:
                kept.append(name)
            continue
        setattr(updated, name, proposed)
    for name in ("luna_model", "luna_verified_at", "luna_input_hash", "luna_state"):
        setattr(updated, name, getattr(verified, name))
    updated.uncertain_fields = sorted(set(verified.uncertain_fields + kept))
    updated.verification_notes = list(dict.fromkeys(current.verification_notes + verified.verification_notes))
    if kept:
        updated.verification_notes.append("保留管理员非空策展字段，模型结果未覆盖：" + ", ".join(sorted(kept)) + "。")
    # raw_title, sources, source_url, source_excerpt, name and search_time all
    # remain the immutable pre-inference retrieval evidence from current.
    return updated


def commit_admin_verification(contest_id: str, verified_card, expected_fingerprint: str,
                              *, path: Path | None = None) -> dict:
    conn = _connect(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _active_record(conn, contest_id)
            _check_fingerprint(current, expected_fingerprint)
            updated = _verified_update(current, verified_card)
            _write(conn, updated)
            return asdict(updated)
    finally:
        conn.close()


def _merge_sources(cards):
    """One URL identity, all distinct source text retained in the excerpt."""
    result, seen = [], {}
    for card in cards:
        sources = list(card.sources)
        if safe_source_url(card.source_url):
            sources.append({"url": card.source_url, "title": card.raw_title or card.name,
                            "excerpt": card.source_excerpt or card.snippet})
        for source in sources:
            identity = _source_identity(source["url"])
            if not identity:
                continue
            entry = {k: source.get(k, "") for k in ("url", "title", "excerpt")}
            if identity not in seen:
                seen[identity] = len(result)
                result.append(entry)
                continue
            original = result[seen[identity]]
            if not original["title"]:
                original["title"] = entry["title"]
            # Preserve differently titled reports as source text, not as a new
            # display contest name. Never silently discard a richer excerpt.
            fragments = []
            if entry["title"] and entry["title"] != original["title"]:
                fragments.append(entry["title"])
            if entry["excerpt"]:
                fragments.append(entry["excerpt"])
            for fragment in fragments:
                if fragment not in original["excerpt"]:
                    original["excerpt"] = (original["excerpt"] + "\n\n" + fragment).strip()
    return result


MAX_MERGE_DUPLICATES = 20

# Prefer keeping an already-verified catalog row when OpenClaw proposes merges.
_MERGE_STATE_RANK = {"verified": 0, "stale": 1, "review": 2, "curated": 3,
                     "pending": 4, "failed": 5, "disabled": 6}


def _fact_richness(card) -> int:
    from .luna import FACT_FIELDS
    return sum(1 for name in FACT_FIELDS
               if (value := getattr(card, name, "")) and value != "未知")


def pick_merge_canonical(cards: list) -> str:
    """Choose the survivor id among active merge candidates.

    Prefer verified over weaker states, then richer facts, then earlier
    verification time / id so OpenClaw job items do not silently demote an
    already-verified public contest.
    """
    if not cards:
        raise ValueError("invalid_merge_ids")
    def key(card):
        return (0 if human_reviewed(card) else 1 if card.luna_state == 'verified'
                else _MERGE_STATE_RANK.get(getattr(card, "luna_state", "") or "", 9) + 2,
                -_fact_richness(card),
                getattr(card, "luna_verified_at", "") or "9999",
                getattr(card, "id", ""))
    return min(cards, key=key).id


def merge_contests(target_id: str, duplicate_ids: list[str], *, verified_card=None,
                   expected_fingerprints: dict | None = None, path: Path | None = None,
                   transaction_guard=None, sources_only=False) -> dict:
    """Atomically publish one canonical record and preserve source tombstones.

    Fingerprints for target AND every duplicate are mandatory. The optional
    default exists only for an explicit static validation error, not an unsafe
    concurrency bypass. No network/provider work may occur while holding lock.
    An internal transaction_guard may claim a curation proposal atomically;
    its changes roll back together with the records if validation fails.

    Surviving row keeps its luna_state when FACT fields are unchanged; source
    merges alone no longer demote verified→stale (that previously made merged
    contests vanish from the verified tab / public catalog).
    """
    from .luna import FACT_FIELDS, input_hash
    if (not isinstance(duplicate_ids, list) or not 1 <= len(duplicate_ids) <= MAX_MERGE_DUPLICATES
            or any(not isinstance(cid, str) or not cid or len(cid) > 128 for cid in duplicate_ids)
            or len(set(duplicate_ids)) != len(duplicate_ids) or target_id in duplicate_ids):
        raise ValueError("invalid_merge_ids")
    if not isinstance(target_id, str) or not target_id or len(target_id) > 128:
        raise ValueError("invalid_contest_id")
    ids = [target_id] + duplicate_ids
    if not isinstance(expected_fingerprints, dict) or any(cid not in expected_fingerprints for cid in ids):
        raise ValueError("fingerprint_required")
    conn = _connect(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if transaction_guard is not None:
                transaction_guard(conn)
            originals = [_active_record(conn, cid) for cid in ids]
            if any(separate_ids(conn, cid) & set(ids) for cid in ids):
                raise ValueError('records_kept_separate')
            for card in originals:
                _check_fingerprint(card, expected_fingerprints[card.id])
            before_records = [asdict(card) for card in originals]
            previous_states = {card.id: card.publication_state for card in originals}
            baseline = originals[0]
            canonical = (_verified_update(baseline, verified_card) if verified_card is not None
                         else _as_card(baseline))
            fact_before = {name: getattr(canonical, name) for name in FACT_FIELDS}
            conflicts, filled = [], []
            uncertain = set(canonical.uncertain_fields)
            for other in originals[1:]:
                has_source = bool(_source_keys(other))
                for name in FACT_FIELDS:
                    proposed = getattr(other, name)
                    current = getattr(canonical, name)
                    if not proposed or proposed == "未知":
                        continue
                    if not current or current == "未知":
                        if has_source and not sources_only:
                            setattr(canonical, name, proposed)
                            filled.append({"field": name, "source_id": other.id})
                            if other.luna_state != "verified" or name in other.uncertain_fields:
                                uncertain.add(name)
                    elif current != proposed:
                        conflicts.append({"field": name, "source_id": other.id,
                                          "kept": current, "alternative": proposed})
                        uncertain.add(name)
            canonical.sources = _merge_sources([canonical] + originals[1:])
            canonical.publication_state = baseline.publication_state if sources_only else "published"
            canonical.merged_into = ""
            canonical.uncertain_fields = sorted(uncertain)
            canonical.verification_notes = list(dict.fromkeys(canonical.verification_notes + [
                ("自动合并同一赛事的重复来源，保留主记录事实及发布状态。" if sources_only else "管理员显式合并重复记录；保留主记录非空事实；补充字段和冲突见合并审计。")
            ]))
            if conflicts:
                canonical.verification_notes.append("合并存在字段冲突，主记录值未覆盖：" +
                                                    ", ".join(sorted({c["field"] for c in conflicts})) + "。")
            facts_changed = any(getattr(canonical, name) != fact_before[name] for name in FACT_FIELDS)
            by_id = {card.id: card for card in originals}
            filled_from_unverified = any(
                by_id.get(item["source_id"], originals[0]).luna_state != "verified" for item in filled)
            # Demote only when FACT fields changed or were filled from an unverified donor.
            # Pure source-list merges (including absorbing a weaker duplicate) keep verified
            # so survivors stay on the verified tab / public catalog.
            if canonical.luna_state in {"verified", "stale"} and (facts_changed or filled_from_unverified):
                canonical.luna_state = "stale"
                canonical.verification_notes.append(
                    "合并后事实字段有补充或变化；核验模型、时间和哈希仍对应合并前快照，建议重新核验。")
            elif canonical.luna_state == "verified" and canonical.luna_input_hash != input_hash(canonical):
                canonical.verification_notes.append(
                    "合并后补充了来源片段；事实字段未改，仍保持已核验（公开目录可见）。")
                canonical.luna_input_hash = input_hash(canonical)
            enrich_card(canonical)
            _write(conn, canonical)
            for source in originals[1:]:
                # Only retirement metadata changes; the full prior record,
                # source/search time and its own verification history survive.
                source.publication_state = "retired"
                source.merged_into = target_id
                _write(conn, source)
            cursor = conn.execute("INSERT INTO contest_merge_audit "
                "(merged_at, target_id, source_ids, previous_states, conflicts, before_records, after_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now_iso(), target_id, _stable_json(duplicate_ids),
                 _stable_json(previous_states), _stable_json({"conflicts": conflicts, "filled": filled}),
                 _stable_json(before_records), record_fingerprint(canonical)))
            return {"contest": asdict(canonical), "retired_ids": list(duplicate_ids),
                    "survivor_id": target_id, "audit_id": cursor.lastrowid}
    finally:
        conn.close()
