"""Local-only public contest catalog; online ingestion belongs to the CLI."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from saishi_tong import __version__
from saishi_tong.store import get_contest, initialize, search_contests
from saishi_tong.store import CATALOG_STATUSES, CATALOG_FORMATS, CATALOG_LOCATIONS, catalog_filter_values
from saishi_tong.web.admin_api import router as admin_router
from saishi_tong.web.online_api import router as online_router
from saishi_tong.web.config import load_web_config
from saishi_tong.web.curation_api import router as curation_router, worker_router

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))


def source_domain(url):
    try:
        return urlsplit(url).hostname or "原始来源"
    except ValueError:
        return "原始来源"


TEMPLATES.env.filters["source_domain"] = source_domain
STATIC = ROOT / "static"
STATUS_OPTIONS = CATALOG_STATUSES
PUBLIC_STATUS_OPTIONS = tuple(status for status in CATALOG_STATUSES if status != '已结束')
FORMAT_OPTIONS = CATALOG_FORMATS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_web_config()
    initialize()
    yield


app = FastAPI(title="赛事通", version=__version__, docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.include_router(admin_router)
app.include_router(online_router)
app.include_router(curation_router)
app.include_router(worker_router)


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response


@app.exception_handler(RequestValidationError)
async def private_validation_error(request: Request, exc):
    return JSONResponse({"ok": False, "error": "请求字段格式无效，请检查后重试。"}, status_code=422)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "saishi-tong", "version": __version__, "search_mode": "local"}


def index_context(request: Request) -> dict:
    active = search_contests(status=["报名中", "即将截止"])
    return {"request": request, "version": __version__, "search_mode": "local",
            "contest_count": len(active), "recent_contests": active[:6]}


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "index.html", index_context(request))


def directory_context(request: Request, q: str = "", status="", format="", location="") -> dict:
    try:
        if not isinstance(q, str) or len(q) > 2000:
            raise ValueError
        q = q.strip()
        statuses = catalog_filter_values(status, CATALOG_STATUSES)
        formats = catalog_filter_values(format, FORMAT_OPTIONS)
        locations = catalog_filter_values(location, CATALOG_LOCATIONS)
        if locations and not set(formats) & {"线下", "混合"}: raise ValueError
    except ValueError:
        raise HTTPException(422, "筛选字段格式无效，请检查后重试。") from None
    cards = search_contests(q=q, status=statuses, format=formats, location=locations)
    def filter_url(text, states, modes, regions=None):
        regions = locations if regions is None else regions
        if not set(modes) & {"线下", "混合"}: regions = []
        values = ([('q', text)] if text else []) + [('status', v) for v in states] + [('format', v) for v in modes] + [('location', v) for v in regions]
        return '/contests' + ('?' + urlencode(values) if values else '') + '#directory-title'
    applied = []
    if q:
        applied.append({'label': '关键词：' + q, 'aria_label': '移除关键词筛选：' + q,
                        'url': filter_url('', statuses, formats)})
    for value in statuses:
        applied.append({'label': value, 'aria_label': '移除状态筛选：' + value,
                        'url': filter_url(q, [v for v in statuses if v != value], formats)})
    for value in formats:
        applied.append({'label': value, 'aria_label': '移除形式筛选：' + value,
                        'url': filter_url(q, statuses, [v for v in formats if v != value])})
    for value in locations:
        applied.append({'label': value, 'aria_label': '移除地区筛选：' + value,
                        'url': filter_url(q, statuses, formats, [v for v in locations if v != value])})
    def legacy_echo(values):
        return values[0] if len(values) == 1 else values if values else ''
    return {"request": request, "version": __version__, "search_mode": "local",
            "contests": cards, "total": len(cards), "q": q,
            "status": legacy_echo(statuses), "format": legacy_echo(formats),
            "statuses": statuses, "formats": formats, "locations": locations, "location": legacy_echo(locations), "applied_filters": applied,
            "location_options": CATALOG_LOCATIONS, "show_locations": bool(set(formats) & {"线下", "混合"}),
            "status_options": PUBLIC_STATUS_OPTIONS, "format_options": FORMAT_OPTIONS}


async def directory_filters(request: Request) -> dict:
    # Only these three public fields matter. Browser CAM credentials are ignored.
    values = request.query_params
    if request.method == "POST":
        kind = request.headers.get("content-type", "").split(";", 1)[0].lower()
        try:
            if kind == "application/json":
                values = await request.json()
                if not isinstance(values, dict):
                    raise ValueError
            elif kind in {"application/x-www-form-urlencoded", "multipart/form-data"}:
                values = await request.form()
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, "请求字段格式无效，请检查后重试。") from None
    q = values.get('q', '')
    if not isinstance(q, str) or len(q) > 2000:
        raise HTTPException(422, "请求字段格式无效，请检查后重试。")
    return {'q': q, **{key: values.getlist(key) if hasattr(values, 'getlist') else values.get(key, '')
                       for key in ('status', 'format', 'location')}}


@app.api_route("/contests", methods=["GET", "POST"], response_class=HTMLResponse)
@app.api_route("/search", methods=["GET", "POST"], response_class=HTMLResponse)
async def contests(request: Request) -> HTMLResponse:
    filters = await directory_filters(request)
    context = await run_in_threadpool(directory_context, request, **filters)
    return TEMPLATES.TemplateResponse(request, "contests.html", context)


@app.api_route("/api/contests", methods=["GET", "POST"])
@app.api_route("/api/search", methods=["GET", "POST"])
async def search_api(request: Request) -> JSONResponse:
    filters = await directory_filters(request)
    context = await run_in_threadpool(directory_context, request, **filters)
    return JSONResponse({key: context[key] for key in
                         ("contests", "total", "q", "status", "format", "statuses", "formats", "locations", "location", "search_mode")} | {"ok": True})


@app.get("/contests/{contest_id}", response_class=HTMLResponse)
def contest_detail(request: Request, contest_id: str) -> HTMLResponse:
    contest = get_contest(contest_id)
    if contest is None:
        raise HTTPException(status_code=404, detail="赛事不存在")
    return TEMPLATES.TemplateResponse(request, "detail.html", {
        "request": request, "version": __version__, "contest": contest,
    })


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "settings.html", {
        "request": request, "version": __version__, "role": "settings",
    })


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "admin.html", {'request': request, 'version': __version__, 'role': 'admin'})


@app.post("/admin")
def legacy_admin_post() -> RedirectResponse:
    return RedirectResponse('/admin', status_code=303)


def create_app() -> FastAPI:
    return app


def main(host: Optional[str] = None, port: Optional[int] = None) -> None:
    import uvicorn

    load_web_config()
    host = host or os.environ.get("SAISHI_HOST", "127.0.0.1")
    port = int(port or os.environ.get("SAISHI_PORT", "8787"))
    uvicorn.run("saishi_tong.web.app:app", host=host, port=port, log_level="info",
                access_log=False, reload=False)


if __name__ == "__main__":
    main()
