"""Small CLIs for the OpenClaw saishi agent (run on Jarvis, never by the website).

  bin/saishi-searchpro "查询词" [--cnt 10] [--freshness y1]
      腾讯云元宝搜索 SearchPro；密钥只从 ~/.config/saishi-tong/tencent.env 读取，从不输出。
  bin/saishi-fetch URL [--max 8000]
      抓取官网/来源页正文（纯文本），用于逐字核对字段。
"""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 saishi-tong/0.2"


def _load_cam():
    for path in (Path.home() / ".config/saishi-tong/tencent.env", Path("/etc/saishi-tong/tencent.env")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() in {"TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"}:
                os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def _pages(raw):
    out = []
    for page in raw.get("Pages") or []:
        if isinstance(page, str):
            try:
                page = json.loads(page)
            except ValueError:
                continue
        if not isinstance(page, dict):
            continue
        get = lambda *keys: next((str(page[k]) for k in keys if page.get(k)), "")
        out.append({"title": get("title", "Title")[:300], "url": get("url", "URL", "link")[:800],
                    "site": get("site", "Site")[:120], "date": get("date", "Date", "time")[:40],
                    "snippet": get("passage", "content", "snippet", "Content")[:1200]})
    return out


def searchpro_main(argv=None):
    ap = argparse.ArgumentParser(prog="saishi-searchpro", description="腾讯云元宝搜索 SearchPro")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--cnt", type=int, default=10, choices=[10, 20, 30, 40, 50])
    ap.add_argument("--freshness", default="", help="可选：d1/w1/m1/m3/y1；默认不限")
    args = ap.parse_args(argv)
    query = " ".join(args.query).strip()
    _load_cam()
    from .client import search_pro
    try:
        raw = search_pro(query, cnt=args.cnt, mode=0, freshness=args.freshness or None)
    except Exception as exc:  # never print SDK text (may echo credential ids)
        print(json.dumps({"ok": False, "engine": "tencent_searchpro", "query": query,
                          "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    pages = _pages(raw)
    print(json.dumps({"ok": True, "engine": "tencent_searchpro", "query": query, "count": len(pages),
                      "results": pages}, ensure_ascii=False, indent=1))
    return 0


def _text(raw: str) -> tuple[str, str]:
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", body)
    links = re.findall(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    keep = [(h, re.sub(r"<[^>]+>|\s+", " ", t).strip()) for h, t in links]
    keep = [f"[{t[:60]}]({h})" for h, t in keep if re.search(r"报名|注册|sign|regist|apply|规则|赛程|rules|黑客松|大赛|挑战赛|征文|/events/\d+|activity-site/|/topics/", t + h, re.I)]
    if keep:
        body += "\n\n赛事/报名/规则相关链接：\n" + "\n".join(dict.fromkeys(keep[:40]))
    return (html.unescape(title.group(1)).strip() if title else ""), body


def _public_fetch_url(url):
    from .classify import safe_source_url
    from urllib.parse import urlsplit
    if not safe_source_url(url):
        raise ValueError('invalid_source_url')
    parsed = urlsplit(url)
    if parsed.port not in (None, 80, 443):
        raise ValueError('non_public_source')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('non_public_source')


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_fetch_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_main(argv=None):
    ap = argparse.ArgumentParser(prog="saishi-fetch", description="抓取网页正文（纯文本）")
    ap.add_argument("url")
    ap.add_argument("--max", type=int, default=8000)
    args = ap.parse_args(argv)
    if not re.match(r"^https?://", args.url, re.I):
        print(json.dumps({"ok": False, "error": "only_http_https"}))
        return 2
    try:
        _public_fetch_url(args.url)
        req = urllib.request.Request(args.url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
        with urllib.request.build_opener(_PublicRedirect()).open(req, timeout=25) as resp:
            raw = resp.read(3_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            final = resp.geturl()
            status = resp.status
    except Exception as exc:
        print(json.dumps({"ok": False, "url": args.url, "error": type(exc).__name__,
                          "status": getattr(exc, "code", None)}, ensure_ascii=False))
        return 1
    try:
        text = raw.decode(charset, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    title, body = _text(text)
    print(json.dumps({"ok": True, "url": args.url, "final_url": final, "status": status, "title": title[:300],
                      "text": body[: max(500, min(args.max, 40000))], "truncated": len(body) > args.max},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(searchpro_main() if (sys.argv[1:2] or [""])[0] != "fetch" else fetch_main(sys.argv[2:]))
