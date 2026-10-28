"""Explicit online-search boundary; regular public search remains local."""
from dataclasses import asdict
from collections import deque
import threading
import time
from fastapi import APIRouter,HTTPException,Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from saishi_tong import online_search
from .admin_api import _body,require_admin
from .admin_session import check_origin,SESSION_COOKIE

router=APIRouter()


class OnlineLimits:
    """Bound a public fixed-endpoint relay without retaining signatures/keys."""
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.hits={};self.active=0;self.lock=threading.Lock()

    def enter(self,client):
        with self.lock:
            now=self.clock()
            for key in list(self.hits):
                while self.hits[key] and self.hits[key][0] <= now-60:self.hits[key].popleft()
                if not self.hits[key]:del self.hits[key]
            if self.active >= 8 or len(self.hits.get(client,())) >= 8 or len(self.hits) >= 2000:
                raise HTTPException(429,'在线查找过于频繁，请稍后重试。')
            self.hits.setdefault(client,deque()).append(now);self.active+=1

    def leave(self):
        with self.lock:self.active-=1
MESSAGES={'invalid_request':'在线查找请求无效，请刷新后重试。','invalid_signature':'请求签名或时间无效，请检查设备时间。',
          'invalid_query':'请填写1–300字的赛事关键词。','invalid_response':'搜索服务返回的数据无效，请稍后重试。',
          'search_auth_failed':'搜索密钥未通过验证或没有访问权限，请到设置页检查。',
          'search_not_open':'元宝搜索资源尚未开通，请在腾讯云控制台检查服务状态。',
          'search_resource_unavailable':'元宝搜索资源当前不可用，请在腾讯云控制台检查服务状态。',
          'search_rate_limited':'元宝搜索请求频率受限，请稍后再试。',
          'search_quota':'搜索额度或调用限制受限，请稍后再试。',
          'search_parameters':'元宝搜索未接受本次参数，请检查选择的搜索版本与设置。',
          'search_unavailable':'元宝搜索暂不可用，请稍后重试。'}


@router.post('/api/online-search')
async def online_search_request(request:Request):
    check_origin(request)
    admin=bool(request.cookies.get(SESSION_COOKIE) or request.headers.get('authorization'))
    if admin:await require_admin(request)
    envelope=await _body(request)
    limits=getattr(request.app.state,'online_limits',None)
    if limits is None:
        limits=request.app.state.online_limits=OnlineLimits()
    entered=False
    try:
        online_search.validate_envelope(envelope)
        limits.enter(request.client.host if request.client else 'unknown');entered=True
        result=await run_in_threadpool(online_search.search_results,envelope)
    except online_search.OnlineError as error:
        message=MESSAGES.get(str(error),'在线查找失败，请重试。')
        if error.request_id:message+=' 故障编号：'+error.request_id
        raise HTTPException(422 if str(error).startswith('invalid_') else 502,message) from None
    finally:
        if entered:limits.leave()
    cards=result.pop('cards')
    pending=result.pop('pending_cards',[])
    imported=await run_in_threadpool(online_search.import_admin_results,cards+pending) if admin else None
    return JSONResponse({'ok':True,'cards':[asdict(c) for c in cards],
                         **result,
                         'total':len(cards),'admin_import':imported,
                         'scope':'admin_drafts' if admin else 'private_preview'},headers={'Cache-Control':'no-store'})
