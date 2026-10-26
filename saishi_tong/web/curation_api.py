"""Admin + worker endpoints for OpenClaw 整理 (replaces the removed OpenAI OAuth verify).

Admin routes use the existing admin session (cookie + CSRF). Worker routes are
called only by the Jarvis worker with ``Authorization: Bearer $SAISHI_WORKER_TOKEN``;
that token lives server-side only (env file) and is never sent to browsers.
"""
from __future__ import annotations

import hmac
import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from saishi_tong import curation, source_intake
from .admin_api import _body, require_admin

router = APIRouter(prefix="/api/admin/openclaw", tags=["openclaw"])
worker_router = APIRouter(prefix="/api/worker/openclaw", tags=["openclaw-worker"])
MAX_WORKER_BODY = 400_000
ERRORS = {"invalid_scope": "整理范围无效", "invalid_limit": "数量需在 1–20 之间",
          "invalid_contest_ids": "赛事列表无效", "contest_not_found": "赛事不存在或已合并",
          "already_queued": "该赛事已在整理队列中", "nothing_to_do": "暂无可批量整理的新条目；近6小时已处理的赛事可在列表中单条重试",
          "proposal_not_found": "合并建议不存在或已处理", "only_proposed_ids": "只能合并 OpenClaw 建议的赛事",
          "record_changed": "赛事已变更，请刷新后重试", "item_closed": "该条目已结束",
          "invalid_status": "状态无效", "preview_required": "请先核对合并预览，再确认合并"}
ERRORS.update({'invalid_source': '信息源无效', 'invalid_source_url': '请填写不含凭据的公开网页链接',
               'source_domain_mismatch': '链接与所选平台不符，请选择指定链接',
               'invalid_source_limit': '每次解析数量需在1–5之间',
               'source_already_queued': '该来源已有进行中的解析任务',
               'source_claim_changed': '来源任务领取状态已变化', 'source_evidence_required': '缺少对应来源证据'})


def _fail(exc):
    code = str(exc)
    return HTTPException(409 if code in {"already_queued", "record_changed", "item_closed", "proposal_not_found", "source_already_queued", "source_claim_changed"} else 422,
                         detail=ERRORS.get(code, "请求无效"))


async def _run(call, *args, **kwargs):
    try:
        return await run_in_threadpool(call, *args, **kwargs)
    except curation.CurationError as exc:
        raise _fail(exc) from None
    except LookupError:
        raise HTTPException(404, detail="任务或赛事不存在") from None


@router.get("/jobs", dependencies=[Depends(require_admin)])
async def jobs(limit: int = Query(8, ge=1, le=50)):
    data = await _run(curation.list_jobs, limit)
    collected = await _run(source_intake.list_jobs, limit)
    data['jobs'] = sorted(data['jobs'] + collected, key=lambda j: (j['created_at'], j['id']), reverse=True)[:limit]
    return {"ok": True, **data}


@router.get('/sources', dependencies=[Depends(require_admin)])
async def sources():
    return {'ok': True, 'sources': source_intake.SOURCES}


@router.post('/source-jobs', status_code=201, dependencies=[Depends(require_admin)])
async def collect_source(request: Request):
    data = await _body(request)
    return {'ok': True, 'job': await _run(source_intake.create_job, data.get('source_id', 'modelscope'), data.get('url', ''), data.get('limit', 3))}


@router.post('/source-jobs/{job_id}/cancel', dependencies=[Depends(require_admin)])
async def cancel_source(job_id: str):
    return {'ok': True, 'job': await _run(source_intake.cancel_job, job_id)}


@router.post("/jobs", status_code=201, dependencies=[Depends(require_admin)])
async def create(request: Request):
    payload = await _body(request)
    scope = payload.get("scope", "unverified")
    limit = payload.get("limit", 5)
    ids = payload.get("contest_ids")
    if scope == "selected" and isinstance(ids, list):
        limit = max(1, len(ids))
    return {"ok": True, "job": await _run(curation.create_job, scope=scope, contest_ids=ids, limit=limit)}


@router.get("/jobs/{job_id}", dependencies=[Depends(require_admin)])
async def job(job_id: str):
    result = await run_in_threadpool(curation.get_job, job_id)
    if result is None:
        raise HTTPException(404, detail="任务不存在")
    return {"ok": True, "job": result}


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_admin)])
async def cancel(job_id: str):
    return {"ok": True, "job": await _run(curation.cancel_job, job_id)}


@router.post("/jobs/{job_id}/items/{contest_id}/merge", dependencies=[Depends(require_admin)])
async def merge(job_id: str, contest_id: str, request: Request):
    payload = await _body(request)
    result = await _run(curation.merge_proposal, job_id, contest_id, payload.get("duplicate_ids"),
                        expected_fingerprints=payload.get("expected_fingerprints"))
    return {"ok": True, "state": "merged", **result}


@router.get("/jobs/{job_id}/items/{contest_id}/merge-preview", dependencies=[Depends(require_admin)])
async def merge_candidates(job_id: str, contest_id: str):
    return {"ok": True, **await _run(curation.preview_merge, job_id, contest_id)}


@router.post("/jobs/{job_id}/items/{contest_id}/merge-preview", dependencies=[Depends(require_admin)])
async def merge_preview(job_id: str, contest_id: str, request: Request):
    payload = await _body(request)
    return {"ok": True, **await _run(curation.preview_merge, job_id, contest_id, payload.get("duplicate_ids"))}


@router.post("/jobs/{job_id}/items/{contest_id}/keep-separate", dependencies=[Depends(require_admin)])
async def keep_separate(job_id: str, contest_id: str):
    return {"ok": True, **await _run(curation.keep_separate, job_id, contest_id)}


# ------------------------------------------------------------------ worker
def _worker_token():
    return os.environ.get("SAISHI_WORKER_TOKEN", "").strip()


async def require_worker(request: Request) -> str:
    expected = _worker_token()
    if len(expected) < 24:
        raise HTTPException(503, detail="worker_not_configured")
    header = request.headers.get("authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if request.cookies or not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, detail="unauthorized")
    name = request.headers.get("x-worker-name", "jarvis")
    return name if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) else "jarvis"


async def _worker_json(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_WORKER_BODY:
            raise HTTPException(413, detail="too_large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(422, detail="invalid_json") from None
    if not isinstance(data, dict):
        raise HTTPException(422, detail="invalid_json")
    return data


@worker_router.post("/claim")
async def claim(worker: str = Depends(require_worker)):
    job = await _run(curation.claim_job, worker)
    if job is None:
        return Response(status_code=204)
    return JSONResponse({"ok": True, **job})


@worker_router.post('/sources/claim')
async def source_claim(worker: str = Depends(require_worker)):
    job = await _run(source_intake.claim, worker)
    return JSONResponse({'ok': True, **job}) if job else Response(status_code=204)


@worker_router.post('/sources/{job_id}/heartbeat')
async def source_heartbeat(job_id: str, request: Request, worker: str = Depends(require_worker)):
    data = await _worker_json(request)
    return {'ok': True, **await _run(source_intake.heartbeat, job_id, data.get('claim_key'), worker)}


@worker_router.post('/sources/{job_id}/result')
async def source_result(job_id: str, request: Request, worker: str = Depends(require_worker)):
    data = await _worker_json(request)
    try:
        job = await run_in_threadpool(source_intake.commit, job_id, data.get('claim_key'), data.get('result'), data.get('model', ''), worker)
    except curation.CurationError as exc:
        if str(exc) not in {'source_claim_changed', 'item_closed'}:
            await run_in_threadpool(source_intake.fail_job, job_id, data.get('claim_key'), worker,
                                    model=data.get('model', ''), error_code=str(exc))
        raise _fail(exc) from None
    except LookupError:
        raise HTTPException(404, detail='任务不存在') from None
    return {'ok': True, 'job': job}


@worker_router.post("/jobs/{job_id}/heartbeat")
async def beat(job_id: str, request: Request, worker: str = Depends(require_worker)):
    data = await _worker_json(request)
    cid = data.get("contest_id") if isinstance(data.get("contest_id"), str) else None
    model = data.get("model") if isinstance(data.get("model"), str) else ""
    return {"ok": True, **await _run(curation.heartbeat, job_id, worker, cid, model)}


@worker_router.post("/jobs/{job_id}/items/{contest_id}")
async def result(job_id: str, contest_id: str, request: Request, worker: str = Depends(require_worker)):
    data = await _worker_json(request)
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, str):
        raise HTTPException(422, detail="fingerprint_required")
    model = data.get("model") if isinstance(data.get("model"), str) else ""
    try:
        outcome = await run_in_threadpool(curation.commit_result, job_id, contest_id, fingerprint,
                                          data.get("result"), model=model)
    except curation.CurationError as exc:
        # Record the failure on the item so the admin sees it, then report.
        if str(exc) != "item_closed":
            await run_in_threadpool(curation._set_item, job_id, contest_id, status="failed",
                                    summary="结果未写入：" + ERRORS.get(str(exc), str(exc))[:120])
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except LookupError:
        raise HTTPException(404, detail="item_not_found") from None
    outcome.pop("contest", None)
    return {"ok": True, **outcome}


@worker_router.post("/jobs/{job_id}/finish")
async def finish(job_id: str, request: Request, worker: str = Depends(require_worker)):
    data = await _worker_json(request)
    status = data.get("status", "done")
    error = data.get("error", "") if isinstance(data.get("error"), str) else ""
    return {"ok": True, "job": await _run(curation.finish_job, job_id, status=status, error=error, worker=worker)}
