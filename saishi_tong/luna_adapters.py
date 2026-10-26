"""Opt-in adapters. No OpenClaw/auth/config files are read."""
from __future__ import annotations
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .luna import LunaError


def server_cam() -> tuple[str, str]:
    values = {}
    try:
        lines = (Path.home() / ".config/saishi-tong/tencent.env").read_text(encoding="utf-8").splitlines()
    except OSError:
        raise LunaError("server_cam_unavailable") from None
    for line in lines:
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip() in {"TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"}:
            values[key.strip()] = value.strip().strip(chr(34)).strip(chr(39))
    pair = values.get("TENCENTCLOUD_SECRET_ID", ""), values.get("TENCENTCLOUD_SECRET_KEY", "")
    if not all(pair):
        raise LunaError("server_cam_unavailable")
    return pair


class HunyuanAdapter:
    def __init__(self, model="hunyuan-turbos-latest"):
        self.model = model

    def complete(self, messages):
        # CAM comes ONLY from approved server file, never search request keys.
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.hunyuan.v20230901 import hunyuan_client, models
            ident, secret = server_cam()
            profile = ClientProfile()
            profile.httpProfile = HttpProfile(endpoint="hunyuan.tencentcloudapi.com", reqTimeout=45)
            client = hunyuan_client.HunyuanClient(credential.Credential(ident, secret), "ap-guangzhou", profile)
            request = models.ChatCompletionsRequest()
            request.from_json_string(json.dumps({"Model": self.model,
                "Messages": [{"Role": m["role"], "Content": m["content"]} for m in messages],
                "Stream": False, "Temperature": 0}))
            response = json.loads(client.ChatCompletions(request).to_json_string())
            return response["Choices"][0]["Message"]["Content"]
        except LunaError:
            raise
        except Exception as error:
            # Never propagate SDK exception text, request ids or request bodies.
            code = getattr(error, "get_code", lambda: "hunyuan_failed")()
            raise LunaError(str(code)) from None


class GatewayAdapter:
    """Explicit env token only; restrict destination to local Gateway."""
    def __init__(self, token, model="openai/gpt-6.1-sol", endpoint="http://127.0.0.1:18789/v1/chat/completions"):
        if not token:
            raise LunaError("gateway_token_missing")
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
            raise LunaError("gateway_endpoint_invalid")
        self._token, self.model, self.endpoint = token, model, endpoint

    def complete(self, messages):
        try:
            request = Request(self.endpoint, data=json.dumps({"model": self.model,
                "messages": messages, "stream": False}).encode(), headers={
                    "Content-Type": "application/json", "Authorization": "Bearer " + self._token})
            with urlopen(request, timeout=90) as response:
                body = response.read(200001)
            if len(body) > 200000:
                raise LunaError("gateway_response_too_large")
            return json.loads(body)["choices"][0]["message"]["content"]
        except LunaError:
            raise
        except Exception:
            raise LunaError("gateway_failed") from None


def configured_adapter():
    provider = os.environ.get("SAISHI_LUNA_PROVIDER", "hunyuan")
    if provider == "hunyuan":
        return HunyuanAdapter(os.environ.get("SAISHI_LUNA_MODEL", "hunyuan-turbos-latest"))
    if provider == "gateway":
        return GatewayAdapter(os.environ.get("SAISHI_LUNA_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN"),
            model=os.environ.get("SAISHI_LUNA_MODEL", "openai/gpt-6.1-sol"))
    raise LunaError("provider_invalid")
