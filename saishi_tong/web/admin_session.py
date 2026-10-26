"""Single-process signed opaque admin sessions; no credential persistence or echoes.

HTTPS always uses Secure cookies. Only an actual loopback HTTP request (URL host
AND peer address) may omit Secure for development. Never trust forwarded headers
here: TLS proxy scheme handling must be configured by the application server.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["admin-session"])  # Parent admin_api router supplies /api/admin.
SESSION_COOKIE = "saishi_admin_session"
SESSION_TTL = 8 * 60 * 60
MAX_SESSIONS = 1000
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def authenticate_admin(token: str | None) -> None:
    expected = os.environ.get("SAISHI_ADMIN_TOKEN", "")
    if not expected.strip():
        raise HTTPException(503, "管理员功能尚未配置")
    if not token:
        raise HTTPException(401, "需要管理员凭证", headers={"WWW-Authenticate": "Bearer"})
    if not isinstance(token, str) or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(403, "管理员凭证无效")


def token_fingerprint():
    expected = os.environ.get("SAISHI_ADMIN_TOKEN", "")
    return hashlib.sha256(expected.encode()).hexdigest() if expected.strip() else None


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def secure_cookie(request):
    return not (request.url.scheme == "http" and loopback(request.url.hostname)
                and request.client is not None and loopback(request.client.host))


def same_origin(request):
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True  # CLI; browsers send Origin and/or Fetch Metadata on writes.
    try:
        parsed = urlsplit(origin)
        target = request.url
        return (not parsed.username and not parsed.password and not parsed.query
                and not parsed.fragment and parsed.path == ""
                and parsed.scheme == target.scheme and parsed.hostname == target.hostname
                and (parsed.port or (443 if parsed.scheme == "https" else 80))
                == (target.port or (443 if target.scheme == "https" else 80)))
    except ValueError:
        return False


def check_origin(request):
    if not same_origin(request):
        raise HTTPException(403, "admin_cross_origin_forbidden")


def private(response):
    response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache",
                             "Referrer-Policy": "no-referrer"})
    return response


def cookie(response, request, value=None):
    options = dict(path="/", httponly=True, secure=secure_cookie(request), samesite="strict")
    if value is None:
        response.delete_cookie(SESSION_COOKIE, **options)
    else:
        response.set_cookie(SESSION_COOKIE, value, max_age=SESSION_TTL, **options)


class AdminSessionService:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.key = secrets.token_bytes(32)
        self.sessions = {}
        self.lock = threading.RLock()

    def prune(self):
        now, current = self.clock(), token_fingerprint()
        for key, session in list(self.sessions.items()):
            if session["expires_at"] <= now or session["credential"] != current:
                del self.sessions[key]

    def _key(self, opaque):
        if not isinstance(opaque, str) or len(opaque) > 200 or opaque.count(".") != 1:
            return None
        sid, signature = opaque.split(".")
        expected = hmac.new(self.key, sid.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            return None
        return hashlib.sha256(sid.encode()).hexdigest()

    def revoke(self, opaque):
        with self.lock:
            self.sessions.pop(self._key(opaque), None)

    def create(self, previous=None):
        with self.lock:
            self.prune()
            self.revoke(previous)
            if len(self.sessions) >= MAX_SESSIONS:
                raise HTTPException(503, "admin_sessions_busy")
            sid = secrets.token_urlsafe(32)
            signed = sid + "." + hmac.new(self.key, sid.encode(), hashlib.sha256).hexdigest()
            session = {"csrf_token": secrets.token_urlsafe(32), "expires_at": int(self.clock() + SESSION_TTL),
                       "credential": token_fingerprint()}
            self.sessions[self._key(signed)] = session
            return signed, session

    def resolve(self, opaque):
        with self.lock:
            self.prune()
            return self.sessions.get(self._key(opaque))


def service(request):
    if not hasattr(request.app.state, "admin_session_service"):
        request.app.state.admin_session_service = AdminSessionService()
    return request.app.state.admin_session_service


def bearer(request):
    authorization = request.headers.get("authorization")
    if authorization is None:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(403, "管理员凭证无效")
    return parts[1]


async def require_admin(request: Request) -> None:
    if token_fingerprint() is None:
        authenticate_admin(None)
    check_origin(request)  # Includes Bearer requests from cross-origin browsers.
    token = bearer(request)
    if token is not None:
        authenticate_admin(token)
        request.state.admin_auth_method = "bearer"
        return
    session = service(request).resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        authenticate_admin(None)
    if request.method not in SAFE_METHODS:
        csrf = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(csrf.encode(), session["csrf_token"].encode()):
            raise HTTPException(403, "admin_csrf_invalid")
    request.state.admin_auth_method = "session"


@router.post("/login")
async def login(request: Request):
    if token_fingerprint() is None:
        authenticate_admin(None)
    check_origin(request)
    token = bearer(request)
    if token is None:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise HTTPException(413, "请求过大")
        request._body = bytes(body)
        kind = request.headers.get("content-type", "").split(";", 1)[0].lower()
        try:
            if kind == "application/json":
                values = json.loads(body or b"{}")
                if not isinstance(values, dict):
                    raise ValueError
            elif kind in {"application/x-www-form-urlencoded", "multipart/form-data"}:
                values = dict(await request.form())
            elif not body:
                values = {}
            else:
                raise HTTPException(415, "请使用 JSON 或表单")
            token = values.get("token")
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, "请求数据格式无效") from None
    authenticate_admin(token)
    opaque, session = service(request).create(request.cookies.get(SESSION_COOKIE))
    response = private(JSONResponse({"ok": True, "authenticated": True,
        "csrf_token": session["csrf_token"], "expires_at": session["expires_at"]}))
    cookie(response, request, opaque)
    return response


@router.get("/session")
async def session_status(request: Request):
    if token_fingerprint() is None:
        authenticate_admin(None)
    check_origin(request)
    token = bearer(request)
    if token is not None:
        authenticate_admin(token)
        return private(JSONResponse({"ok": True, "authenticated": True}))
    session = service(request).resolve(request.cookies.get(SESSION_COOKIE))
    payload = {"ok": True, "authenticated": session is not None}
    if session:
        payload.update({key: session[key] for key in ("csrf_token", "expires_at")})
    response = private(JSONResponse(payload))
    if not session and request.cookies.get(SESSION_COOKIE):
        cookie(response, request)
    return response


@router.post("/logout")
async def logout(request: Request):
    await require_admin(request)
    service(request).revoke(request.cookies.get(SESSION_COOKIE))
    response = private(JSONResponse({"ok": True, "authenticated": False}))
    cookie(response, request)
    return response
