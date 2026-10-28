"""Small CLIs for the OpenClaw saishi agent (run on Jarvis, never by the website).

  bin/saishi-searchpro "查询词" [--cnt 10] [--freshness y1] [--lite]
      腾讯云元宝搜索 SearchPro；密钥只从 ~/.config/saishi-tong/tencent.env 读取，从不输出。
  bin/saishi-fetch URL [--max 8000]
      抓取官网/来源页正文（纯文本），用于逐字核对字段。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
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
                    "snippet": get("passage", "content", "snippet", "Content")[:1200],
                    "passage": get("passage", "Passage")[:1200],
                    "content": get("content", "Content")[:1200]})
    return out


def _freshness(value, *, today=None):
    """Validate the documented SearchPro forms before credentials are loaded."""
    if value == "":
        return value
    relative = re.fullmatch(r"([dmy])([0-9]{1,2})?", value)
    if relative:
        amount = int(relative.group(2) or '1')
        if 1 <= amount <= {'d': 30, 'm': 12, 'y': 5}[relative.group(1)]:
            return value
    else:
        parts = value.split(',')
        if len(parts) in (1, 2) and all(re.fullmatch(r'\d{4}-\d{2}-\d{2}', part) for part in parts):
            try:
                dates = [date.fromisoformat(part) for part in parts]
            except ValueError:
                dates = []
            now = today or datetime.now(timezone(timedelta(hours=8))).date()
            if dates and all(date(1970, 1, 1) <= item <= now for item in dates):
                # The official text and example disagree on range ordering;
                # preserve the supplied order and let SearchPro decide it.
                return value
    raise argparse.ArgumentTypeError('请使用 d1–d30、m1–m12、y1–y5 或有效的 YYYY-MM-DD 日期／日期区间。')


def searchpro_main(argv=None):
    ap = argparse.ArgumentParser(prog="saishi-searchpro", description="腾讯云元宝搜索 SearchPro")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--cnt", type=int, default=None, choices=[10, 20, 30, 40, 50])
    ap.add_argument("--freshness", type=_freshness, default="", help="可选：d7/m3/y1、YYYY-MM-DD 或日期区间；默认不限")
    ap.add_argument("--lite", action="store_true", help="极速版不传 Mode 和 Cnt，保持每次一次调用")
    args = ap.parse_args(argv)
    if args.lite and args.cnt is not None:
        ap.error('极速版不支持 --cnt，请移除该参数。')
    query = " ".join(args.query).strip()
    _load_cam()
    from .client import search_pro
    try:
        raw = search_pro(query, cnt=None if args.lite else (args.cnt or 10),
                         mode=None if args.lite else 0, freshness=args.freshness or None)
    except Exception as exc:  # never print SDK text (may echo credential ids)
        print(json.dumps({"ok": False, "engine": "tencent_searchpro", "query": query,
                          "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    pages = _pages(raw)
    result = {"ok": True, "engine": "tencent_searchpro", "query": query, "count": len(pages),
              "results": pages}
    version = raw.get('Version')
    if isinstance(version, str) and version in {'lite', 'standard', 'flagship'}:
        result['version'] = version
    print(json.dumps(result, ensure_ascii=False, indent=1))
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
    keep = [f"[{t[:60]}]({h})" for h, t in keep if re.search(r"报名|注册|投稿|交稿|提交|参赛|sign|regist|apply|规则|赛程|rules|黑客松|大赛|挑战赛|征文|/events/\d+|activity-site/|/topics/|/writing(?:\?|/)", t + h, re.I)]
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
