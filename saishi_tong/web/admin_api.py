"""Fail-closed administrator API. Server credentials never leave the server."""
from __future__ import annotations

import hmac
import json

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from saishi_tong import store
from saishi_tong.store import get_contest, save_admin_contest
from .admin_session import authenticate_admin, require_admin as session_require_admin, router as session_router
from .admin_session import SESSION_COOKIE as ADMIN_SESSION_COOKIE

router = APIRouter(prefix="/api/admin", tags=["admin"])
router.include_router(session_router)
MAX_BODY_BYTES = 512_000


async def _body(request: Request):
    cached = getattr(request.state, "admin_payload", None)
    if cached is not None:
        return cached
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="请求过大")
    # Populate Starlette's normal body cache for its form decoder.
    request._body = bytes(body)
    try:
        kind = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if kind == "application/json":
            data = json.loads(body or b"{}")
        elif kind in {"application/x-www-form-urlencoded", "multipart/form-data"}:
            data = dict(await request.form())
            for name in ("sources", "deadlines"):
                if name in data and isinstance(data[name], str) and data[name].lstrip().startswith("["):
                    data[name] = json.loads(data[name])
        elif not body:
            data = {}
        else:
            raise HTTPException(status_code=415, detail="请使用 JSON 或表单")
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=422, detail="请求数据格式无效") from None
    request.state.admin_payload = data
    return data


async def require_admin(request: Request) -> None:
    try:
        await session_require_admin(request)
        return
    except HTTPException as exc:
        # Legacy credential-bearing forms remain supported only WITHOUT a
        # cookie or Authorization header. Never bypass cookie-session CSRF or
        # the session helper's same-origin checks (which return 403).
        kind = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if (exc.status_code != 401 or request.cookies.get(ADMIN_SESSION_COOKIE)
                or request.headers.get("authorization") is not None
                or request.method not in {"POST", "PUT"}
                or kind not in {"application/x-www-form-urlencoded", "multipart/form-data"}):
            raise
    authenticate_admin((await _body(request)).get("token"))
    request.state.admin_auth_method = "form"


async def _save(payload: dict, contest_id: str | None = None):
    try:
        return await run_in_threadpool(save_admin_contest, payload, contest_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="赛事不存在") from None
    except ValueError as exc:
        # Store messages are fixed strings and never include submitted values.
        raise HTTPException(status_code=409 if str(exc) == 'record_changed' else 422, detail=str(exc)) from None


@router.get("/contests", dependencies=[Depends(require_admin)])
async def admin_list(verification: Literal["verified", "awaiting", "unverified", "all"] = "all",
                     page: int = Query(1, ge=1), page_size: int = Query(15, ge=1, le=50),
                     q: str = Query("", max_length=1000)):
    result = await run_in_threadpool(store.list_admin_contests, verification=verification,
                                     page=page, page_size=page_size, q=q)
    from saishi_tong.classify import display_status
    for card in result['contests']:
        card['current_status'] = display_status(card['status'], card['deadline'])
    return {"ok": True, **result}


@router.post("/contests", status_code=201, dependencies=[Depends(require_admin)])
async def admin_create(request: Request):
    payload = dict(await _body(request))
    payload.pop("token", None)
    return {"ok": True, "contest": await _save(payload)}


@router.get("/contests/{contest_id}", dependencies=[Depends(require_admin)])
async def admin_get(contest_id: str):
    card = await run_in_threadpool(get_contest, contest_id, include_drafts=True)
    if card is None:
        raise HTTPException(status_code=404, detail="赛事不存在")
    return {"ok": True, "contest": card}


@router.put("/contests/{contest_id}", dependencies=[Depends(require_admin)])
async def admin_edit(contest_id: str, request: Request):
    payload = dict(await _body(request))
    payload.pop("token", None)
    if payload.get('human_reviewed') is True and not payload.get('expected_fingerprint'):
        raise HTTPException(status_code=409, detail='请重新打开当前赛事，再确认人工校对。')
    return {"ok": True, "contest": await _save(payload, contest_id)}


@router.post("/contests/{contest_id}/publish", dependencies=[Depends(require_admin)])
async def admin_publish(contest_id: str):
    return {"ok": True, "contest": await _save({"publication_state": "published"}, contest_id)}


@router.get('/trash',dependencies=[Depends(require_admin)])
async def deleted_contests():
    return {'ok':True,'contests':await run_in_threadpool(store.list_deleted_contests)}


async def _deleted_state(contest_id,request,restore=False):
    payload=await _body(request)
    try:
        card=await run_in_threadpool(store.change_deleted_state,contest_id,payload.get('expected_fingerprint'),restore=restore)
    except LookupError:raise HTTPException(404,'赛事不存在') from None
    except ValueError as error:
        raise HTTPException(409 if str(error)=='record_changed' else 422,'记录已变化，请刷新后重试。' if str(error)=='record_changed' else '删除或恢复请求无效，请重新打开赛事。') from None
    return {'ok':True,'contest':card}


@router.delete('/contests/{contest_id}',dependencies=[Depends(require_admin)])
async def delete_contest(contest_id:str,request:Request):
    return await _deleted_state(contest_id,request)


@router.post('/contests/{contest_id}/restore',dependencies=[Depends(require_admin)])
async def restore_contest(contest_id:str,request:Request):
    return await _deleted_state(contest_id,request,restore=True)


