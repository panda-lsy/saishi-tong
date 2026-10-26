"""SearchPro client with server defaults or request-scoped credentials."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

ENV_ID = "TENCENTCLOUD_SECRET_ID"
ENV_KEY = "TENCENTCLOUD_SECRET_KEY"


class MissingCredentials(RuntimeError):
    pass


def credentials_present() -> bool:
    return bool(os.environ.get(ENV_ID) and os.environ.get(ENV_KEY))


def get_client(*, secret_id: Optional[str] = None, secret_key: Optional[str] = None):
    """A complete explicit pair wins; never change shared process environment."""
    supplied = secret_id is not None or secret_key is not None
    if not supplied:
        secret_id, secret_key = os.environ.get(ENV_ID, ""), os.environ.get(ENV_KEY, "")
    if not isinstance(secret_id, str) or not isinstance(secret_key, str) or not secret_id.strip() or not secret_key.strip():
        raise MissingCredentials("A complete Tencent Cloud credential pair is required.")
    secret_id, secret_key = secret_id.strip(), secret_key.strip()

    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.wsa.v20250508 import wsa_client

    cred = credential.Credential(secret_id, secret_key)
    http_profile = HttpProfile()
    http_profile.endpoint = "wsa.tencentcloudapi.com"
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    # Region empty string per official sample
    return wsa_client.WsaClient(cred, "", client_profile)


def search_pro(
    query: str,
    *,
    cnt: int = 10,
    mode: Optional[int] = 0,
    freshness: Optional[str] = "m3",
    client=None,
) -> Dict[str, Any]:
    """Call SearchPro once; return parsed dict with Query/Pages/RequestId."""
    from tencentcloud.wsa.v20250508 import models

    if client is None:
        client = get_client()

    params: Dict[str, Any] = {"Query": query}
    if cnt in (10, 20, 30, 40, 50):
        params["Cnt"] = cnt
    if mode is not None:
        params["Mode"] = mode
    if freshness:
        params["Freshness"] = freshness

    req = models.SearchProRequest()
    req.from_json_string(json.dumps(params))
    resp = client.SearchPro(req)
    return json.loads(resp.to_json_string())


def search_many(
    queries: List[str],
    *,
    cnt: int = 10,
    freshness: str = "m3",
    pause_sec: float = 0.35,
    dry_run: bool = False,
    secret_id: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run SearchPro for each query with polite pacing."""
    results: List[Dict[str, Any]] = []
    if dry_run:
        for q in queries:
            results.append(
                {
                    "Query": q,
                    "Pages": [],
                    "dry_run": True,
                    "note": "no API call (dry-run / missing keys)",
                }
            )
        return results

    client = get_client(secret_id=secret_id, secret_key=secret_key) if secret_id is not None or secret_key is not None else get_client()
    for i, q in enumerate(queries):
        if i:
            time.sleep(pause_sec)
        try:
            results.append(search_pro(q, cnt=cnt, freshness=freshness, client=client))
        except Exception:  # Never propagate SDK exception text or credentials.
            results.append({"Query": q, "Pages": [], "error": "search_failed"})
    return results
