"""Manual source collection queue. Network/model execution belongs to Jarvis only.

Imports are always private review drafts; existing records are never overwritten.
The transaction commits draft inserts and the task receipt together.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import re
import uuid
from urllib.parse import urlsplit

from . import curation, store
from .classify import normalized_url, safe_source_url, current_date
from .parse import ContestCard

SOURCES = (
    {'id': 'modelscope', 'name': '魔搭 ModelScope', 'url': 'https://modelscope.cn/events',
     'query': 'site:modelscope.cn/events 黑客松 挑战赛 征文 报名'},
    {'id': 'csdn', 'name': 'CSDN 官方活动', 'url': 'https://bbs.csdn.net/forums/Activity',
     'query': 'site:csdn.net 征文大赛 开发者大赛 报名'},
)


def source_url(value):
    """Public HTTP source only; reject credentials, local addresses and token URLs."""
    value = safe_source_url(value)
    if not value or len(value) > 2048:
        raise curation.CurationError('invalid_source_url')
    parsed = urlsplit(value)
    host = parsed.hostname.lower().rstrip('.')
    if parsed.port not in (None, 80, 443) or host == 'localhost' or '.' not in host or host.endswith(('.localhost', '.local', '.internal')):
        raise curation.CurationError('invalid_source_url')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None or re.fullmatch(r'[0-9.]+', host) or re.search(r'(?:token|secret|password|authorization|api[_-]?key)=', parsed.query, re.I):
        raise curation.CurationError('invalid_source_url')
    return value


def _conn(path=None):
    conn = store._connect(path)
    conn.execute('''CREATE TABLE IF NOT EXISTS source_intake_jobs (
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_name TEXT NOT NULL,
        source_url TEXT NOT NULL, query TEXT NOT NULL, item_limit INTEGER NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL DEFAULT '', worker TEXT NOT NULL DEFAULT '',
        claim_key TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', results TEXT NOT NULL DEFAULT '[]',
        skipped INTEGER NOT NULL DEFAULT 0)''')
    conn.commit()
    return conn


def _job(row):
    value = dict(row)
    value.pop('claim_key', None)
    value['kind'] = 'source'
    value['results'] = json.loads(value['results'])
    value['total'] = 1
    value['items'] = []
    value['counts'] = {'done': int(value['status'] == 'done'), 'failed': int(value['status'] == 'failed')}
    return value


def create_job(source_id='modelscope', url='', limit=3, path=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise curation.CurationError('invalid_source_limit')
    sources = {s['id']: s for s in SOURCES}
    if not isinstance(source_id, str) or source_id not in (*sources, 'url'):
        raise curation.CurationError('invalid_source')
    if not isinstance(url, str):
        raise curation.CurationError('invalid_source_url')
    spec = sources.get(source_id)
    chosen = source_url(url.strip() or (spec['url'] if spec else ''))
    if spec:
        domain = {'modelscope': 'modelscope.cn', 'csdn': 'csdn.net'}[source_id]
        host = urlsplit(chosen).hostname.lower().rstrip('.')
        if host != domain and not host.endswith('.' + domain):
            raise curation.CurationError('source_domain_mismatch')
    name = spec['name'] if spec else '指定赛事链接'
    query = spec['query'] if spec else '核对指定页面中的具体赛事'
    with closing(_conn(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        busy = conn.execute("SELECT source_url FROM source_intake_jobs WHERE status IN ('queued','running')").fetchall()
        if any(normalized_url(r[0]) == normalized_url(chosen) for r in busy):
            raise curation.CurationError('source_already_queued')
        now, key = curation._utc(), 'source-' + uuid.uuid4().hex[:16]
        conn.execute('INSERT INTO source_intake_jobs (id,source_id,source_name,source_url,query,item_limit,status,created_at,updated_at) VALUES (?,?,?,?,?,?,\'queued\',?,?)',
                     (key, source_id, name, chosen, query, limit, now, now))
        return _job(conn.execute('SELECT * FROM source_intake_jobs WHERE id=?', (key,)).fetchone())


def list_jobs(limit=10, path=None):
    with closing(_conn(path)) as conn:
        jobs = [_job(row) for row in conn.execute('SELECT * FROM source_intake_jobs ORDER BY created_at DESC,id LIMIT ?', (max(1, min(limit, 50)),))]
        ids = list({r['id'] for job in jobs for r in job['results']})
        times = dict(conn.execute('SELECT id,search_time FROM contests WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids)) if ids else {}
        for job in jobs:
            for result in job['results']:
                result.setdefault('search_time', times.get(result['id'], ''))
        return jobs


def cancel_job(job_id, path=None):
    with closing(_conn(path)) as conn, conn:
        row = conn.execute('SELECT * FROM source_intake_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise LookupError('job_not_found')
        conn.execute("UPDATE source_intake_jobs SET status='cancelled',updated_at=? WHERE id=? AND status IN ('queued','running')", (curation._utc(), job_id))
        return _job(conn.execute('SELECT * FROM source_intake_jobs WHERE id=?', (job_id,)).fetchone())


def claim(worker='jarvis', path=None):
    with closing(_conn(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec='seconds')
        conn.execute("UPDATE source_intake_jobs SET status='queued',worker='',claim_key='' WHERE status='running' AND heartbeat_at < ?", (cutoff,))
        # No same-worker resume: an unexpired claim must remain exclusive.
        row = conn.execute("SELECT * FROM source_intake_jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if row is None:
            return None
        now, claim_key = curation._utc(), uuid.uuid4().hex
        conn.execute("UPDATE source_intake_jobs SET status='running',worker=?,claim_key=?,heartbeat_at=?,updated_at=? WHERE id=?", (worker[:64], claim_key, now, now, row['id']))
        return {'job_id': row['id'], 'kind': 'source', 'source': {k: row[k] for k in ('source_id','source_name','source_url','query','item_limit')},
                'claim_key': claim_key, 'today': current_date().isoformat()}


def heartbeat(job_id, claim_key, worker='jarvis', path=None):
    with closing(_conn(path)) as conn, conn:
        row = conn.execute('SELECT status,claim_key,worker FROM source_intake_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise LookupError('job_not_found')
        if row['claim_key'] != claim_key or row['worker'] != worker[:64]:
            raise curation.CurationError('source_claim_changed')
        if row['status'] == 'running':
            conn.execute('UPDATE source_intake_jobs SET heartbeat_at=?,updated_at=? WHERE id=?', (curation._utc(), curation._utc(), job_id))
        return {'status': row['status']}


def fail_job(job_id, claim_key, worker='jarvis', path=None, *, model='', error_code=''):
    model = model if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9_./:-]{1,100}', model) else ''
    code = error_code if re.fullmatch(r'[a-z_]{1,60}', error_code or '') else 'invalid_result'
    message = ('缺少赛事主链接的来源记录；请补齐真实链接并重试' if code == 'source_evidence_required'
               else '返回结果未通过校验；未导入赛事，请重试或调整来源')
    with closing(_conn(path)) as conn, conn:
        conn.execute("UPDATE source_intake_jobs SET status='failed',error=?,model=?,updated_at=? WHERE id=? AND claim_key=? AND worker=? AND status='running'",
                     (message + '（' + code + '）', model, curation._utc(), job_id, claim_key if isinstance(claim_key, str) else '', worker[:64]))


def commit(job_id, claim_key, result, model='', worker='jarvis', path=None):
    from .luna import input_hash
    with closing(_conn(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM source_intake_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise LookupError('job_not_found')
        if row['claim_key'] != claim_key or row['worker'] != worker[:64]:
            raise curation.CurationError('source_claim_changed')
        if row['status'] in {'done', 'failed'}:
            return _job(row)  # response lost after commit: idempotent receipt
        if row['status'] != 'running':
            raise curation.CurationError('item_closed')
        valid_model = isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9_./:-]{1,100}', model)
        if (not valid_model or not isinstance(result, dict) or result.get('error')
                or not isinstance(result.get('contests'), list) or len(result['contests']) > row['item_limit']):
            conn.execute("UPDATE source_intake_jobs SET status='failed',error='来源解析未完成或模型信息缺失',model=?,updated_at=? WHERE id=?", (model if valid_model else '', curation._utc(), job_id))
            return _job(conn.execute('SELECT * FROM source_intake_jobs WHERE id=?', (job_id,)).fetchone())
        output, skipped = [], 0
        identities = {normalized_url(r[0]) for r in conn.execute('SELECT source_url FROM contests') if r[0]}
        for item in result['contests']:
            if not isinstance(item, dict):
                raise curation.CurationError('invalid_result')
            if (not isinstance(item.get('sources'), list) or not isinstance(item.get('evidence', {}), dict)
                    or not isinstance(item.get('uncertain_fields', []), list) or not isinstance(item.get('notes', []), list)):
                raise curation.CurationError('invalid_result')
            url = source_url(item.get('source_url', ''))
            if normalized_url(url) in identities:
                skipped += 1
                continue  # includes retired rows: do not revive or overwrite any prior record
            checked = curation.validate_result(item, {'source_url': url, 'sources': []})
            fields = checked['fields']
            if not fields['contest_name'] or checked['verdict'] == 'not_contest' or not checked['sources']:
                skipped += 1
                continue
            if url not in {s['url'] for s in checked['sources']}:
                raise curation.CurationError('source_evidence_required')
            card = ContestCard(name=fields['contest_name'], raw_title=str(item.get('raw_title') or fields['contest_name'])[:300],
                               source_url=url, search_time=curation._utc(), publication_state='draft',
                               record_origin='source_intake', luna_state='review', **fields)
            card.id = store.stable_id(card)
            card.snippet = card.summary
            card.sources = [{k: s[k] for k in ('url','title','excerpt')} for s in checked['sources']]
            card.source_excerpt = card.sources[0]['excerpt']
            from .contest_scope import classify_scope
            if classify_scope(card)=='out_of_scope':
                skipped += 1
                continue
            card.luna_model = 'openclaw/saishi:' + model
            card.luna_verified_at = curation._utc()
            card.luna_input_hash = input_hash(card)
            card.uncertain_fields = checked['uncertain']
            card.verification_notes = ['来源解析导入；尚待逐条核验和人工发布。', '来源入口：' + row['source_url']] + checked['notes']
            store._write(conn, card)
            identities.add(normalized_url(url))
            output.append({'id': card.id, 'contest_name': card.contest_name, 'source_url': url, 'search_time': card.search_time})
        conn.execute("UPDATE source_intake_jobs SET status='done',model=?,results=?,skipped=?,updated_at=? WHERE id=?", (model, json.dumps(output, ensure_ascii=False), skipped, curation._utc(), job_id))
        return _job(conn.execute('SELECT * FROM source_intake_jobs WHERE id=?', (job_id,)).fetchone())
