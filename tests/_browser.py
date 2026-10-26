"""Minimal in-process ASGI browser with a cookie jar (test helper)."""
import asyncio
import json
from http.cookies import SimpleCookie
from urllib.parse import urlsplit


class Browser:
    def __init__(self, app, scheme="http", host="127.0.0.1:8787"):
        self.app, self.scheme, self.host = app, scheme, host
        self.cookies = {}

    def request(self, path, method="GET", data=None, headers=None):
        async def run():
            target = urlsplit(path)
            request_headers = {"host": self.host, **(headers or {})}
            if self.cookies:
                request_headers["cookie"] = "; ".join(k + "=" + v for k, v in self.cookies.items())
            body = b"" if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
            if data is not None:
                request_headers.setdefault("content-type", "application/json")
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                     "scheme": self.scheme, "path": target.path, "raw_path": target.path.encode(),
                     "query_string": target.query.encode(), "root_path": "",
                     "headers": [(k.lower().encode(), v.encode()) for k, v in request_headers.items()],
                     "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8787)}
            messages = []
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            async def send(message):
                messages.append(message)
            await self.app(scope, receive, send)
            start = next(m for m in messages if m["type"] == "http.response.start")
            raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            headers_out = [(k.decode(), v.decode()) for k, v in start["headers"]]
            for k, v in headers_out:
                if k == "set-cookie":
                    cookies = SimpleCookie()
                    cookies.load(v)
                    for name, cookie in cookies.items():
                        if cookie["max-age"] == "0":
                            self.cookies.pop(name, None)
                        else:
                            self.cookies[name] = cookie.value
            return start["status"], dict(headers_out), raw
        return asyncio.run(run())
