"""OpenClaw 整理 job queue and fail-closed result commit.

The website never calls a model or the OpenClaw gateway. An administrator
queues a job; the Jarvis-side worker (saishi_tong.openclaw_worker) pulls it
over HTTPS with a server-side bearer token, runs the OpenClaw ``saishi`` agent
locally, and posts one structured result per contest. Every result is validated
here before it touches the catalog. Publication state is never changed;
ambiguous duplicates need explicit review, while high-confidence duplicates
may merge into an existing verified record under the established rules.
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from . import store
from .classify import enrich_card, safe_source_url
from .parse import _now_iso
from .verification import VERIFIED_SQL, AI_REVIEWED_SQL, is_verified

JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
ITEM_STATES = ("pending", "running", "done", "failed", "skipped")
MAX_BATCH = 20
STALE_AFTER = timedelta(minutes=45)
BATCH_COOLDOWN = timedelta(hours=6)
AGENT_LABEL = "openclaw/saishi"

TEXT_LIMITS = {"contest_name": 300, "summary": 1800, "organizer": 400, "timeline": 2000,
               "eligibility": 1800, "prizes": 1800, "signup_url": 2048,
               "deadline": 100, "prize": 300, "host": 400}
ENUMS = {"status": {"报名中", "即将截止", "已结束", "未知"},
         "format": {"线上", "线下", "混合", "未知"},
         "location_scope": {"国内", "国外", "未知"}}
RESULT_FIELDS = tuple(TEXT_LIMITS) + tuple(ENUMS)
VERDICTS = {"verified", "needs_review", "not_contest"}
FIELD_LABELS = {"contest_name": "名称", "summary": "简介", "organizer": "主办", "timeline": "时间线",
                "eligibility": "参赛资格", "prizes": "奖项", "signup_url": "报名链接",
                "deadline": "截止", "status": "状态", "format": "线上/线下", "prize": "奖金", "host": "主办"}


class CurationError(ValueError):
    """Static, value-free error codes only."""


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS curation_jobs (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        scope TEXT NOT NULL, status TEXT NOT NULL, contest_ids TEXT NOT NULL,
        worker TEXT NOT NULL DEFAULT '', claimed_at TEXT NOT NULL DEFAULT '',
        heartbeat_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS curation_items (
        job_id TEXT NOT NULL, contest_id TEXT NOT NULL, position INTEGER NOT NULL,
        status TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
        changed_fields TEXT NOT NULL DEFAULT '[]', uncertain_fields TEXT NOT NULL DEFAULT '[]',
        merge_ids TEXT NOT NULL DEFAULT '[]', merged INTEGER NOT NULL DEFAULT 0,
        contest_name TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (job_id, contest_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS curation_worker (
        id INTEGER PRIMARY KEY CHECK (id = 1), seen_at TEXT NOT NULL, worker TEXT NOT NULL)""")


def _conn(path=None):
    conn = store._connect(path)
    _ensure(conn)
    conn.commit()
    return conn


def _loads(value, default):
    try:
        data = json.loads(value)
        return data if isinstance(data, type(default)) else default
    except (TypeError, ValueError):
        return default


def _job_dict(conn, row):
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM curation_items WHERE job_id = ? ORDER BY position", (row["id"],))]
    for item in items:
        for key in ("changed_fields", "uncertain_fields", "merge_ids"):
            item[key] = _loads(item[key], [])
        item["merged"] = bool(item["merged"])
    counts = {state: sum(1 for i in items if i["status"] == state) for state in ITEM_STATES}
    verdicts = {v: sum(1 for i in items if i["verdict"] == v) for v in VERDICTS}
    job = {k: row[k] for k in row.keys() if k != "contest_ids"}
    job.update(items=items, counts=counts, verdicts=verdicts, total=len(items))
    return job


def _active_ids(conn):
    rows = conn.execute("SELECT i.contest_id FROM curation_items i JOIN curation_jobs j ON j.id = i.job_id "
                        "WHERE j.status IN ('queued','running') AND i.status IN ('pending','running')")
    return {r[0] for r in rows}


def _reclaim_stale(conn):
    cutoff = (datetime.now(timezone.utc) - STALE_AFTER).isoformat(timespec="seconds")
    for row in conn.execute("SELECT id FROM curation_jobs WHERE status = 'running' AND heartbeat_at < ?",
                            (cutoff,)).fetchall():
        conn.execute("UPDATE curation_items SET status = 'pending' WHERE job_id = ? AND status = 'running'", (row[0],))
        conn.execute("UPDATE curation_jobs SET status = 'queued', worker = '', updated_at = ? WHERE id = ?",
                     (_utc(), row[0]))


def create_job(*, scope="unverified", contest_ids=None, limit=5, path=None) -> dict:
    if scope not in {"unverified", "selected"}:
        raise CurationError("invalid_scope")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_BATCH:
        raise CurationError("invalid_limit")
    conn = _conn(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            _reclaim_stale(conn)
            busy = _active_ids(conn)
            if scope == "selected":
                if (not isinstance(contest_ids, list) or not 1 <= len(contest_ids) <= MAX_BATCH
                        or any(not isinstance(c, str) or not 1 <= len(c) <= 128 for c in contest_ids)):
                    raise CurationError("invalid_contest_ids")
                ids = list(dict.fromkeys(contest_ids))
                for cid in ids:
                    row = conn.execute("SELECT publication_state, merged_into, deleted_at FROM contests WHERE id = ?", (cid,)).fetchone()
                    if row is None or row["publication_state"] == "retired" or row["merged_into"] or row["deleted_at"]:
                        raise CurationError("contest_not_found")
                if any(cid in busy for cid in ids):
                    raise CurationError("already_queued")
            else:
                cutoff = (datetime.now(timezone.utc) - BATCH_COOLDOWN).isoformat(timespec="seconds")
                rows = conn.execute(
                    "SELECT id FROM contests WHERE publication_state != 'retired' AND merged_into = '' AND deleted_at = '' "
                    "AND NOT " + VERIFIED_SQL + " AND NOT " + AI_REVIEWED_SQL + " AND NOT EXISTS ("
                    "SELECT 1 FROM curation_items i WHERE i.contest_id = contests.id "
                    "AND i.status IN ('done','failed') AND i.updated_at > ?) "
                    "ORDER BY (luna_state = 'review'), search_time DESC, id", (cutoff,)).fetchall()
                ids = [r[0] for r in rows if r[0] not in busy][:limit]
                if not ids:
                    raise CurationError("nothing_to_do")
            job_id = uuid.uuid4().hex[:16]
            now = _utc()
            conn.execute("INSERT INTO curation_jobs (id, created_at, updated_at, scope, status, contest_ids) "
                         "VALUES (?, ?, ?, ?, 'queued', ?)", (job_id, now, now, scope, json.dumps(ids)))
            for pos, cid in enumerate(ids):
                name = conn.execute("SELECT contest_name, name FROM contests WHERE id = ?", (cid,)).fetchone()
                conn.execute("INSERT INTO curation_items (job_id, contest_id, position, status, contest_name, updated_at) "
                             "VALUES (?, ?, ?, 'pending', ?, ?)",
                             (job_id, cid, pos, (name["contest_name"] or name["name"] or "")[:300], now))
            return _job_dict(conn, conn.execute("SELECT * FROM curation_jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()


def list_jobs(limit=10, path=None) -> dict:
    conn = _conn(path)
    try:
        with conn:
            _reclaim_stale(conn)
        rows = conn.execute("SELECT * FROM curation_jobs ORDER BY created_at DESC, id LIMIT ?",
                            (max(1, min(int(limit), 50)),)).fetchall()
        seen = conn.execute("SELECT seen_at, worker FROM curation_worker WHERE id = 1").fetchone()
        return {"jobs": [_job_dict(conn, r) for r in rows],
                "worker": {"seen_at": seen["seen_at"], "name": seen["worker"]} if seen else None}
    finally:
        conn.close()


def get_job(job_id, path=None):
    conn = _conn(path)
    try:
        row = conn.execute("SELECT * FROM curation_jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_dict(conn, row) if row else None
    finally:
        conn.close()


def cancel_job(job_id, path=None):
    conn = _conn(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM curation_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise LookupError("job_not_found")
            if row["status"] in {"queued", "running"}:
                now = _utc()
                conn.execute("UPDATE curation_jobs SET status = 'cancelled', updated_at = ?, finished_at = ? WHERE id = ?",
                             (now, now, job_id))
                conn.execute("UPDATE curation_items SET status = 'skipped', updated_at = ? "
                             "WHERE job_id = ? AND status IN ('pending','running')", (now, job_id))
            return _job_dict(conn, conn.execute("SELECT * FROM curation_jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------- worker side
def _touch_worker(conn, worker):
    conn.execute("INSERT INTO curation_worker (id, seen_at, worker) VALUES (1, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET seen_at = excluded.seen_at, worker = excluded.worker",
                 (_utc(), worker[:64]))


def _snapshot(contest_id, path=None):
    card = store.get_contest(contest_id, path, include_drafts=True)
    if card is None or card.get("publication_state") == "retired" or card.get("merged_into"):
        return None
    fingerprint = store.record_fingerprint(card)
    public = {k: card.get(k) for k in ("id", "contest_name", "name", "raw_title", "summary", "organizer",
              "timeline", "eligibility", "prizes", "signup_url", "deadline", "status", "format", "location_scope",
              "source_url", "site", "published", "search_time", "source_excerpt", "sources",
              "uncertain_fields", "luna_state", "publication_state", "curated")}
    public["sources"] = [{"url": s.get("url", ""), "title": s.get("title", "")[:300],
                          "excerpt": s.get("excerpt", "")[:4000]} for s in (card.get("sources") or [])[:12]]
    public["source_excerpt"] = (public.get("source_excerpt") or "")[:4000]
    candidates = store.find_duplicate_contests(card, exclude_id=contest_id, path=path)[:8]
    return {"contest": public, "fingerprint": fingerprint,
            "duplicate_candidates": [{k: c.get(k) for k in ("id", "contest_name", "score", "reasons", "source_url",
                                      "organizer", "deadline", "timeline", "status")} for c in candidates]}


def _similar_names(contest_id, path=None, limit=12):
    """Cheap extra merge hints: other active records sharing a long name token."""
    conn = _conn(path)
    try:
        row = conn.execute("SELECT contest_name, name FROM contests WHERE id = ?", (contest_id,)).fetchone()
        if row is None:
            return []
        base = row["contest_name"] or row["name"] or ""
        tokens = [t for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{4,}", base) if not t.isdigit()][:4]
        out = []
        for token in tokens:
            core = token[:8]
            for other in conn.execute(
                    "SELECT id, contest_name, name, source_url, deadline, luna_state FROM contests "
                    "WHERE id != ? AND publication_state != 'retired' AND merged_into = '' AND deleted_at = '' "
                    "AND (contest_name LIKE ? OR name LIKE ?) LIMIT 6", (contest_id, "%" + core + "%", "%" + core + "%")):
                if other["id"] not in {o["id"] for o in out}:
                    out.append({"id": other["id"], "contest_name": other["contest_name"] or other["name"],
                                "source_url": other["source_url"], "deadline": other["deadline"],
                                "luna_state": other["luna_state"]})
        return out[:limit]
    finally:
        conn.close()


def claim_job(worker="jarvis", path=None):
    store.archive_ended(path=path)
    conn = _conn(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            _touch_worker(conn, worker)
            _reclaim_stale(conn)
            # Resume a job this worker was running when it died (the worker holds a
            # host-wide lock, so a claim from the same name means the old run is gone).
            row = conn.execute("SELECT * FROM curation_jobs WHERE status = 'running' AND worker = ? "
                               "ORDER BY created_at, id LIMIT 1", (worker[:64],)).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM curation_jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1").fetchone()
            if row is None:
                return None
            now = _utc()
            conn.execute("UPDATE curation_jobs SET status = 'running', worker = ?, claimed_at = ?, heartbeat_at = ?, "
                         "updated_at = ? WHERE id = ?", (worker[:64], now, now, now, row["id"]))
            pending = [r[0] for r in conn.execute(
                "SELECT contest_id FROM curation_items WHERE job_id = ? AND status IN ('pending','running') ORDER BY position",
                (row["id"],))]
    finally:
        conn.close()
    items = []
    for cid in pending:
        snap = _snapshot(cid, path)
        if snap is None:
            _set_item(row["id"], cid, status="skipped", summary="赛事已合并或不存在，跳过。", path=path)
            continue
        snap["similar_names"] = _similar_names(cid, path)
        items.append(snap)
    return {"job_id": row["id"], "items": items, "today": _now_iso()[:10]}


def heartbeat(job_id, worker="jarvis", contest_id=None, model="", path=None):
    conn = _conn(path)
    try:
        with conn:
            _touch_worker(conn, worker)
            row = conn.execute("SELECT status FROM curation_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise LookupError("job_not_found")
            if row["status"] != "running":
                return {"status": row["status"]}
            now = _utc()
            conn.execute("UPDATE curation_jobs SET heartbeat_at = ?, updated_at = ?, model = CASE WHEN ? != '' THEN ? ELSE model END WHERE id = ?",
                         (now, now, model[:120], model[:120], job_id))
            if contest_id:
                conn.execute("UPDATE curation_items SET status = 'running', updated_at = ? WHERE job_id = ? AND contest_id = ? AND status = 'pending'",
                             (now, job_id, contest_id))
            return {"status": "running"}
    finally:
        conn.close()


def _set_item(job_id, contest_id, path=None, **values):
    conn = _conn(path)
    try:
        with conn:
            values["updated_at"] = _utc()
            for key in ("changed_fields", "uncertain_fields", "merge_ids"):
                if key in values:
                    values[key] = json.dumps(values[key], ensure_ascii=False)
            if "merged" in values:
                values["merged"] = int(bool(values["merged"]))
            sets = ", ".join(k + " = ?" for k in values)
            conn.execute("UPDATE curation_items SET " + sets + " WHERE job_id = ? AND contest_id = ?",
                         list(values.values()) + [job_id, contest_id])
    finally:
        conn.close()


def finish_job(job_id, *, status="done", error="", worker="jarvis", path=None):
    if status not in {"done", "failed"}:
        raise CurationError("invalid_status")
    conn = _conn(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            _touch_worker(conn, worker)
            row = conn.execute("SELECT status FROM curation_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise LookupError("job_not_found")
            if row["status"] == "running":
                now = _utc()
                left = conn.execute("SELECT COUNT(*) FROM curation_items WHERE job_id = ? AND status IN ('pending','running')",
                                    (job_id,)).fetchone()[0]
                if left:
                    conn.execute("UPDATE curation_items SET status = 'failed', summary = '整理未完成：worker 提前结束。', updated_at = ? "
                                 "WHERE job_id = ? AND status IN ('pending','running')", (now, job_id))
                conn.execute("UPDATE curation_jobs SET status = ?, error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                             (status, re.sub(r"[^\w\u4e00-\u9fff .:/()（）：，,-]", "", error)[:300], now, now, job_id))
            return _job_dict(conn, conn.execute("SELECT * FROM curation_jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()


def _clean_text(value, limit):
    if not isinstance(value, str):
        raise CurationError("invalid_field_type")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
    if len(value) > limit:
        value = value[:limit].rstrip()
    return value


def validate_result(result, snapshot) -> dict:
    """Shape/enum/URL validation; unsupported facts are blanked and flagged."""
    if not isinstance(result, dict):
        raise CurationError("invalid_result")
    verdict = result.get("verdict")
    if verdict not in VERDICTS:
        raise CurationError("invalid_verdict")
    raw_fields = result.get("fields")
    if not isinstance(raw_fields, dict):
        raise CurationError("invalid_fields")
    sources = []
    for source in (result.get("sources") or [])[:12]:
        if not isinstance(source, dict):
            continue
        url = safe_source_url(source.get("url", "")) if isinstance(source.get("url"), str) else ""
        if not url:
            continue
        sources.append({"url": url, "title": _clean_text(str(source.get("title", "")), 300),
                        "excerpt": _clean_text(str(source.get("excerpt", "")), 3000),
                        "official": bool(source.get("official"))})
    known_urls = {s["url"] for s in sources} | {s.get("url") for s in snapshot.get("sources", [])} | {snapshot.get("source_url")}
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    uncertain = {f for f in (result.get("uncertain_fields") or []) if isinstance(f, str) and f in RESULT_FIELDS}
    fields, extra_notes = {}, []
    for name in RESULT_FIELDS:
        if name in {"host", "prize"}:
            continue  # legacy mirrors, derived below
        value = raw_fields.get(name, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise CurationError("invalid_field_type")
        value = _clean_text(value, TEXT_LIMITS.get(name, 20))
        if name in ENUMS and value not in ENUMS[name]:
            value = "未知"
        if name == "signup_url" and value and not safe_source_url(value):
            value = ""
        refs = evidence.get(name, [])
        refs = [refs] if isinstance(refs, str) else refs
        backed = isinstance(refs, list) and any(isinstance(r, str) and r in known_urls for r in refs)
        if value and value != "未知" and not backed:
            # No cited URL for this fact: keep it out of the catalog, mark 待核验.
            value = "未知" if name in ENUMS else ""
        if not value or value == "未知":
            uncertain.add(name)
        fields[name] = value
    fields["host"] = fields["organizer"]
    fields["prize"] = fields["prizes"].splitlines()[0][:300] if fields["prizes"] else ""
    uncertain -= {"host", "prize"}
    if fields["status"] in {"报名中", "即将截止"} and fields["deadline"]:
        from .classify import current_date
        found = re.findall(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", fields["deadline"])
        try:
            day = datetime(*map(int, found[-1])).date() if found else None
        except ValueError:
            day = None
        if day and day < current_date():
            fields["status"] = "已结束"
            extra_notes.append("报名截止日已过，状态按日期改为已结束。")
    notes = [_clean_text(n, 400) for n in (result.get("notes") or [])[:8] if isinstance(n, str) and n.strip()] + extra_notes
    merge_ids = [m for m in (result.get("merge_with") or [])[:10]
                 if isinstance(m, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", m) and m != snapshot.get("id")]
    if verdict == "verified" and (not fields["contest_name"] or not any(s["official"] for s in sources)
                                  or not (fields["organizer"] or fields["timeline"] or fields["deadline"])):
        verdict = "needs_review"
        notes.append("缺少官方来源，或名称/主办/时间未能核实，降级为待核验。")
    return {"verdict": verdict, "fields": fields, "sources": sources, "uncertain": sorted(uncertain),
            "notes": notes, "merge_with": merge_ids}


def _merge_source_lists(current, new):
    out, seen = [], set()
    for source in list(new) + list(current):
        identity = store._source_identity(source.get("url", ""))
        if not identity or identity in seen:
            continue
        seen.add(identity)
        out.append({"url": source["url"], "title": source.get("title", ""), "excerpt": source.get("excerpt", "")})
    return out[:30]


def commit_result(job_id, contest_id, fingerprint, result, *, model="", path=None) -> dict:
    """Write one validated OpenClaw result back to the catalog atomically."""
    from .luna import FACT_FIELDS, input_hash
    conn = _conn(path)
    try:
        item = conn.execute("SELECT i.status, j.status AS job_status FROM curation_items i JOIN curation_jobs j ON j.id = i.job_id "
                            "WHERE i.job_id = ? AND i.contest_id = ?", (job_id, contest_id)).fetchone()
    finally:
        conn.close()
    if item is None:
        raise LookupError("item_not_found")
    if item["job_status"] != "running" or item["status"] not in {"pending", "running"}:
        raise CurationError("item_closed")
    if isinstance(result, dict) and result.get("error"):
        _set_item(job_id, contest_id, path=path, status="failed",
                  summary=("OpenClaw 未完成：" + _clean_text(str(result.get("error")), 200)))
        return {"state": "failed"}
    snapshot = store.get_contest(contest_id, path, include_drafts=True)
    if snapshot is None:
        _set_item(job_id, contest_id, path=path, status="skipped", summary="赛事已不存在或已合并。")
        return {"state": "skipped"}
    checked = validate_result(result, snapshot)
    model_label = AGENT_LABEL + (":" + model if model and re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", model) else "")
    conn = store._connect(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = store._active_record(conn, contest_id)
                store._check_fingerprint(current, fingerprint)
            except (LookupError, ValueError):
                raise CurationError("record_changed") from None
            updated = store._as_card(current)
            changed, kept = [], []
            for name in RESULT_FIELDS:
                proposed = checked["fields"][name]
                previous = getattr(current, name)
                if (current.curated or current.record_origin == "curated") and previous and previous != "未知":
                    if proposed and proposed != previous:
                        kept.append(name)
                    continue
                if not proposed or proposed == "未知":
                    # Never erase an existing text value; flag it instead. Status/format
                    # were heuristic guesses before, so an unconfirmed enum becomes 未知.
                    if name in ENUMS and previous != "未知":
                        setattr(updated, name, "未知")
                        changed.append(name)
                    continue
                if proposed != previous:
                    setattr(updated, name, proposed)
                    changed.append(name)
            # summary/snippet stay in sync for legacy displays
            if "summary" in changed:
                updated.snippet = updated.summary
            updated.sources = _merge_source_lists(current.sources, checked["sources"])
            official = next((s["url"] for s in checked["sources"] if s["official"]), "")
            notes = ["OpenClaw saishi 整理（SearchPro 检索 + 官网/来源页核对）：" +
                     {"verified": "已核验", "needs_review": "部分字段待核验", "not_contest": "疑似非赛事，建议人工下线"}[checked["verdict"]] + "。"]
            if official:
                notes.append("官方来源：" + official)
            notes += checked["notes"]
            if kept:
                notes.append("保留管理员手动字段，未覆盖：" + "、".join(FIELD_LABELS.get(k, k) for k in sorted(kept)) + "。")
            # Drop earlier OpenClaw notes so repeated runs do not pile up.
            previous_notes = [n for n in current.verification_notes if not n.startswith(
                ("OpenClaw saishi", "官方来源：", "Luna 未完成核验", "Luna 未启用", "仅核对给定搜索来源片段", "部分输出无逐字来源证据"))]
            updated.verification_notes = list(dict.fromkeys(previous_notes + notes))[:24]
            updated.uncertain_fields = sorted({f for f in checked["uncertain"] if f in FACT_FIELDS} | set(kept))
            updated.luna_state = "verified" if checked["verdict"] == "verified" else "review"
            from .contest_scope import classify_scope
            if classify_scope(updated)=='out_of_scope':
                updated.verification_notes.append('收录范围外：普通作文或文化征文不是本站开发、计算机类赛事，不得确认发布。')
            updated.luna_model = model_label
            updated.luna_verified_at = _utc()
            updated.ai_reviewed_at = updated.luna_verified_at
            updated.luna_input_hash = input_hash(updated)
            enrich_card(updated)
            store._write(conn, updated)
    finally:
        conn.close()
    # Dedupe-before-verify: union agent suggestions with high-confidence DB matches.
    merge_ids = []
    with closing(store._connect(path)) as decisions:
        blocked = store.separate_ids(decisions, contest_id)
    for mid in checked["merge_with"]:
        if mid in blocked:
            continue
        other = store.get_contest(mid, path, include_drafts=True)
        if other and other.get("publication_state") != "retired" and not other.get("merged_into"):
            merge_ids.append(mid)
    db_dups = store.find_duplicate_contests(updated, exclude_id=contest_id, path=path)
    strong = []
    for dup in db_dups:
        reasons = set(dup.get("reasons") or [])
        score = float(dup.get("score") or 0)
        if "canonical_source_url_match" in reasons or "source_input_hash_match" in reasons:
            strong.append(dup)
            if dup["id"] not in merge_ids:
                merge_ids.append(dup["id"])
        elif score >= 0.78 and dup["id"] not in merge_ids:
            merge_ids.append(dup["id"])
    auto_merged = None
    # Safe auto-merge: URL/hash-level duplicate into an existing verified row so we
    # never leave two verified copies of the same contest.
    anchor = next((d for d in strong
                   if (lambda c: c.get("luna_state") == "verified" or is_verified(c))(store.get_contest(d["id"], path, include_drafts=True) or {})),
                  None)
    if anchor and contest_id != anchor["id"]:
        try:
            cards = []
            fingerprints = {}
            for cid in (contest_id, anchor["id"]):
                card = store.get_contest(cid, path, include_drafts=True)
                if card is None or card.get("publication_state") == "retired" or card.get("merged_into"):
                    raise CurationError("record_changed")
                fingerprints[cid] = store.record_fingerprint(card)
                cards.append(store._as_card(card))
            survivor_id = store.pick_merge_canonical(cards)
            retire_ids = [cid for cid in (contest_id, anchor["id"]) if cid != survivor_id]
            auto_merged = store.merge_contests(survivor_id, retire_ids,
                                               expected_fingerprints=fingerprints, path=path, sources_only=True)
            updated = store._as_card(auto_merged["contest"])
            merge_ids = [mid for mid in merge_ids if mid not in retire_ids and mid != survivor_id]
        except (LookupError, ValueError, CurationError):
            auto_merged = None
    verdict_text = {"verified": "已核验", "needs_review": "待核验", "not_contest": "疑似非赛事"}[checked["verdict"]]
    summary = verdict_text + "；更新：" + ("、".join(FIELD_LABELS.get(f, f) for f in changed if f not in {"host", "prize"}) or "无")
    if updated.uncertain_fields:
        summary += "；待核验：" + "、".join(FIELD_LABELS.get(f, f) for f in updated.uncertain_fields if f not in {"host", "prize"})
    if auto_merged:
        survivor = auto_merged.get("survivor_id") or updated.id
        summary += "；已自动合并进已有记录 " + survivor[:12] + "…"
    elif merge_ids:
        summary += "；建议合并 " + str(len(merge_ids)) + " 条重复"
    _set_item(job_id, contest_id, path=path, status="done", verdict=checked["verdict"], summary=summary[:600],
              changed_fields=changed, uncertain_fields=updated.uncertain_fields, merge_ids=merge_ids,
              merged=bool(auto_merged),
              contest_name=(updated.contest_name or updated.name)[:300])
    out = {"state": "done", "verdict": checked["verdict"], "changed": changed, "merge_ids": merge_ids,
           "contest": asdict(updated)}
    if auto_merged:
        out["auto_merged"] = True
        out["survivor_id"] = auto_merged.get("survivor_id") or updated.id
        out["retired_ids"] = auto_merged.get("retired_ids") or []
    return out


def preview_merge(job_id, contest_id, duplicate_ids=None, path=None) -> dict:
    """Read current proposed records without committing a merge."""
    job = get_job(job_id, path)
    if job is None:
        raise LookupError("job_not_found")
    item = next((i for i in job["items"] if i["contest_id"] == contest_id), None)
    if item is None or item["merged"] or item["status"] != "done" or not item["merge_ids"]:
        raise CurationError("proposal_not_found")
    if duplicate_ids is None:
        duplicate_ids = item["merge_ids"]
    if (not isinstance(duplicate_ids, list) or not duplicate_ids
            or any(not isinstance(d, str) or d not in item["merge_ids"] for d in duplicate_ids)):
        raise CurationError("only_proposed_ids")
    ids = list(dict.fromkeys([contest_id] + duplicate_ids))
    cards = []
    for cid in ids:
        card = store.get_contest(cid, path, include_drafts=True)
        if card is None or card["publication_state"] == "retired" or card.get("merged_into"):
            raise CurationError("record_changed")
        cards.append(card)
    survivor_id = store.pick_merge_canonical([store._as_card(c) for c in cards])
    survivor = next(c for c in cards if c["id"] == survivor_id)
    fields = ("contest_name", "organizer", "deadline", "timeline", "eligibility", "prizes", "signup_url", "summary", "status", "format")
    # These are comparisons, not a guessed final merged record. The transaction
    # remains responsible for protection of curated facts, conflicts and audit.
    comparisons = []
    for field in fields:
        values = [{"id": c["id"], "value": c.get(field, "")} for c in cards]
        distinct = {v["value"] for v in values if v["value"] and v["value"] != "未知"}
        if len(distinct) > 1 or (distinct and not survivor.get(field)):
            comparisons.append({"field": field, "label": FIELD_LABELS.get(field, field),
                                "conflict": len(distinct) > 1, "values": values})
    return {"contest_id": contest_id, "survivor_id": survivor_id,
            "survivor_name": survivor.get("contest_name") or survivor["name"],
            "duplicate_ids": [cid for cid in ids if cid != contest_id],
            "retired_ids": [cid for cid in ids if cid != survivor_id],
            "fingerprints": {c["id"]: store.record_fingerprint(c) for c in cards},
            "contests": [{key: c.get(key, "") for key in
                          ("id", "contest_name", "organizer", "deadline", "timeline", "luna_state", "publication_state", "source_url", "search_time")} for c in cards],
            "comparisons": comparisons}


def keep_separate(job_id, contest_id, path=None):
    conn = _conn(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM curation_items WHERE job_id = ? AND contest_id = ?",
                                (job_id, contest_id)).fetchone()
            if item is None or item["merged"] or item["status"] != "done" or not _loads(item["merge_ids"], []):
                raise CurationError("proposal_not_found")
            summary = (item["summary"][:550] + "；管理员选择保留独立，不执行合并。")
            store.remember_separate(conn, contest_id, _loads(item['merge_ids'], []), job_id)
            conn.execute("UPDATE curation_items SET merge_ids = '[]', summary = ?, updated_at = ? "
                         "WHERE job_id = ? AND contest_id = ?", (summary, _utc(), job_id, contest_id))
    finally:
        conn.close()
    return {"state": "kept_separate"}


def merge_proposal(job_id, contest_id, duplicate_ids, path=None, *, expected_fingerprints=None) -> dict:
    """Admin-confirmed merge of an OpenClaw proposal, bound to current snapshots.

    Survivor is the best record among the proposal item and its duplicates
    (prefer verified), not blindly the OpenClaw job item — that previously
    retired verified contests into weaker copies and hid them from the UI.
    """
    if not isinstance(expected_fingerprints, dict) or not expected_fingerprints:
        raise CurationError("preview_required")
    job = get_job(job_id, path)
    if job is None:
        raise LookupError("job_not_found")
    item = next((i for i in job["items"] if i["contest_id"] == contest_id), None)
    if item is None or item["merged"] or item["status"] != "done" or not item["merge_ids"]:
        raise CurationError("proposal_not_found")
    if (not isinstance(duplicate_ids, list) or not duplicate_ids
            or any(d not in item["merge_ids"] for d in duplicate_ids)):
        raise CurationError("only_proposed_ids")
    ids = [contest_id] + list(dict.fromkeys(duplicate_ids))
    cards, fingerprints = [], {}
    for cid in ids:
        card = store.get_contest(cid, path, include_drafts=True)
        if card is None or card.get("publication_state") == "retired" or card.get("merged_into"):
            raise CurationError("record_changed")
        fingerprints[cid] = store.record_fingerprint(card)
        if expected_fingerprints.get(cid) != fingerprints[cid]:
            raise CurationError("record_changed")
        cards.append(store._as_card(card))
    survivor_id = store.pick_merge_canonical(cards)
    retire_ids = [cid for cid in ids if cid != survivor_id]
    name_by_id = {c.id: (c.contest_name or c.name or c.id) for c in cards}
    def confirm_proposal(conn):
        # A concurrent "keep separate" decision must win over a stale browser
        # confirmation. Claim the proposal in the same transaction as the merge.
        current = conn.execute("SELECT * FROM curation_items WHERE job_id = ? AND contest_id = ?",
                               (job_id, contest_id)).fetchone()
        if (current is None or current["merged"] or current["status"] != "done"
                or any(cid not in _loads(current["merge_ids"], []) for cid in duplicate_ids)):
            raise CurationError("record_changed")
        summary = (current["summary"][:400] + "；已合并保留「" + name_by_id[survivor_id][:80]
                   + "」，下线 " + str(len(retire_ids)) + " 条。")
        conn.execute("UPDATE curation_items SET merged = 1, summary = ?, updated_at = ? "
                     "WHERE job_id = ? AND contest_id = ?", (summary, _utc(), job_id, contest_id))
    try:
        result = store.merge_contests(survivor_id, retire_ids, expected_fingerprints=fingerprints, path=path,
                                      transaction_guard=confirm_proposal)
    except (LookupError, ValueError):
        raise CurationError("record_changed") from None
    survivor_name = (result.get("contest") or {}).get("contest_name") or name_by_id.get(survivor_id, survivor_id)
    result["requested_id"] = contest_id
    result["survivor_id"] = survivor_id
    result["survivor_name"] = survivor_name
    result["retired_names"] = [name_by_id.get(rid, rid) for rid in retire_ids]
    return result


def main(argv=None):
    """``python -m saishi_tong.curation enqueue --limit 5`` (optional daily timer on the site host)."""
    import argparse
    ap = argparse.ArgumentParser(prog="saishi_tong.curation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    enq = sub.add_parser("enqueue")
    enq.add_argument("--limit", type=int, default=5)
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "enqueue":
        try:
            job = create_job(scope="unverified", limit=args.limit)
        except CurationError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 0 if str(exc) in {"nothing_to_do"} else 1
        print(json.dumps({"ok": True, "job": job["id"], "total": job["total"]}))
        return 0
    data = list_jobs(5)
    for job in data["jobs"]:
        print(job["id"], job["status"], job["created_at"], job["counts"], job["verdicts"])
    print("worker:", data["worker"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
