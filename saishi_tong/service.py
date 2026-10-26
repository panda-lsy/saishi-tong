"""Search orchestration for CLI/offline use; ordinary website search is local."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import credentials_present, search_many
from .classify import enrich_card
from .parse import ContestCard, _now_iso, closing_soon, dedupe, pages_to_cards, top_n
from .queries import build_queries
from .report import render_markdown, write_report
from .store import upsert_contests
from .luna import process_cards

PRESETS: List[Dict[str, str]] = [
    {"id": "week", "label": "本周截止", "query": "本周截止 黑客松 开发者竞赛 征文 报名"},
    {"id": "agent", "label": "AI Agent 黑客松", "query": "AI Agent 黑客松 正在报名 奖金"},
    {"id": "csdn", "label": "CSDN 征文", "query": "CSDN 征文大赛 开发者 截止 元宝搜索"},
    {"id": "tencent", "label": "腾讯云挑战", "query": "腾讯云 黑客松 开发者挑战赛 报名中"},
]


def load_env_files() -> None:
    """Only the approved user config file; existing process env wins.

    Project .env, legacy paths, and unrelated config variables are never loaded.
    """
    import os
    from .client import ENV_ID, ENV_KEY

    path = Path.home() / ".config" / "saishi-tong" / "tencent.env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in {ENV_ID, ENV_KEY} and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _failure(query: str, queries: list[str], message: str, *, dry_run: bool = False,
             failed_queries: int = 0, credential_source: str = "server") -> Dict[str, Any]:
    return {"ok": False, "error": message, "query": query, "queries": queries,
            "top": [], "closing": [], "cards": [], "markdown": "",
            "dry_run": dry_run, "note": message, "credentials": credentials_present(),
            "failed_queries": failed_queries, "successful_queries": 0, "result_count": 0,
            "search_time": "", "credential_source": credential_source, "warnings": []}


def run_search(
    user_query: str,
    *,
    query_count: int = 4,
    cnt: int = 10,
    freshness: str = "m3",
    top: int = 12,
    closing: int = 8,
    pause_sec: float = 0.35,
    dry_run: bool = False,
    out_dir: Optional[Path] = None,
    extra_queries: Optional[List[str]] = None,
    secret_id: str | None = None,
    secret_key: str | None = None,
    luna_adapter=None,
    luna_enabled: bool | None = None,
) -> Dict[str, Any]:
    """Execute multi-query Yuanbao search → structured cards + markdown."""
    # A supplied pair is request-scoped: never mutate process env or feed Luna.
    client_pair = secret_id is not None or secret_key is not None
    credential_source = "client" if client_pair else "server"
    if client_pair and (not isinstance(secret_id, str) or not secret_id.strip()
                        or not isinstance(secret_key, str) or not secret_key.strip()):
        return _failure(user_query or "", [], "请同时提供完整的腾讯云 SecretId 与 SecretKey。",
                        credential_source=credential_source)
    if not client_pair:
        load_env_files()
    user_query = (user_query or "").strip() or "AI 黑客松 征文 开发者竞赛 正在报名"
    queries = build_queries(user_query, extra=extra_queries, limit=query_count)
    has_credentials = client_pair or credentials_present()
    use_dry = dry_run or not has_credentials
    note = ""
    if use_dry and not dry_run:
        note = "未检测到腾讯云密钥，已 dry-run。请配置 ~/.config/saishi-tong/tencent.env"
    elif dry_run:
        note = "dry-run：未调用元宝 SearchPro。"

    search_kwargs = {}
    # Preserve old mock signatures when callers use server credentials.
    if client_pair:
        search_kwargs = {"secret_id": secret_id.strip(), "secret_key": secret_key.strip()}
    try:
        responses = search_many(
            queries,
            cnt=cnt,
            freshness=freshness,
            pause_sec=pause_sec,
            dry_run=use_dry,
            **search_kwargs,
        )
    except Exception:  # SDK errors must never escape into HTML/JSON/logged tracebacks.
        return _failure(user_query, queries, "元宝搜索暂不可用，请检查服务端配置后重试。",
                        dry_run=use_dry, failed_queries=len(queries), credential_source=credential_source)

    # Count malformed API envelopes as failures, not successful empty searches.
    responses = responses if isinstance(responses, list) else []
    responses = [r if isinstance(r, dict) and isinstance(r.get("Pages"), list)
                 and not r.get("Error") else {"error": "invalid_response"}
                 for r in responses]
    failed_queries = sum(1 for response in responses if response.get("error"))
    successful_queries = len(responses) - failed_queries
    if not use_dry and not successful_queries:
        return _failure(user_query, queries, "元宝搜索全部失败，请稍后重试或检查服务端配置。",
                        failed_queries=failed_queries, credential_source=credential_source)
    if failed_queries:
        note = f"部分搜索失败（{failed_queries}/{len(responses)}），仅展示成功查询结果，请核验来源。"

    search_time = _now_iso()
    cards = dedupe(pages_to_cards([r for r in responses if not r.get("error")], search_time=search_time))
    # Model errors never block search; states/warnings remain explicit.
    cards, warnings, luna_stats = process_cards(cards, adapter=luna_adapter, enabled=luna_enabled)
    if warnings:
        note = " ".join([note, *warnings]).strip()
    # Persist full collection, never dry-run or hand-curated snapshots.
    if not use_dry:
        upsert_contests([c for c in cards if not c.curated and c.record_origin != "curated"])
    # Drafts must not leak through the public search response/report.
    cards = [c for c in cards if c.publication_state == "published" and not c.deleted_at]
    if not use_dry and not cards:
        note = (note + " " if note else "") + "元宝搜索调用成功，但未找到可展示的结果；请调整关键词。"
    top_cards = top_n(cards, top)
    closing_cards = closing_soon(cards, closing)
    md = render_markdown(
        user_query=user_query,
        queries=queries,
        top=top_cards,
        closing=closing_cards,
        note=note,
    )
    report_path = ""
    if out_dir is not None:
        report_path = str(write_report(md, Path(out_dir)))

    def pack(cs: List[ContestCard]) -> List[Dict[str, Any]]:
        return [asdict(enrich_card(c)) for c in cs]

    return {
        "ok": True,
        "query": user_query,
        "queries": queries,
        "cards": pack(cards),
        "result_count": len(cards),
        "successful_queries": successful_queries,
        "failed_queries": failed_queries,
        "top": pack(top_cards),
        "closing": pack(closing_cards),
        "markdown": md,
        "dry_run": use_dry,
        "note": note,
        "credentials": has_credentials,
        "credential_source": credential_source,
        "warnings": warnings,
        "luna": luna_stats,
        "report_path": report_path,
        "search_time": search_time,
    }
