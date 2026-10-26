"""Markdown report writers."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .classify import safe_source_url
from typing import List

from .parse import ContestCard, _now_iso


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def cards_table(cards: List[ContestCard]) -> str:
    lines = [
        "| 名称 | 截止 | 奖金 | 主办/站点 | 来源 | 检索时间 |",
        "|---|---|---|---|---|---|",
    ]
    for c in cards:
        name = _esc(c.name).replace("[", "［").replace("]", "］")[:60]
        url = quote(safe_source_url(c.source_url), safe=":/?&=%#@+;,~!$*-._")
        link = f"[{name}]({url})" if url else name
        lines.append(
            f"| {link} | {_esc(c.deadline) or '—'} | {_esc(c.prize) or '—'} "
            f"| {_esc(c.host or c.site) or '—'} | {_esc(url)[:50]} | {_esc(c.search_time)} |"
        )
    if len(lines) == 2:
        lines.append("| （无结果） | — | — | — | — | — |")
    return "\n".join(lines)


def render_markdown(
    *,
    user_query: str,
    queries: List[str],
    top: List[ContestCard],
    closing: List[ContestCard],
    note: str = "",
) -> str:
    parts = [
        "# 赛事通检索报告",
        "",
        f"- 用户条件：{user_query or '（默认）'}",
        f"- 生成时间（本机本地时区）：{_now_iso()}",
        f"- Query 组数：{len(queries)}",
        "",
        "## 使用的搜索词",
        "",
    ]
    for i, q in enumerate(queries, 1):
        parts.append(f"{i}. `{q}`")
    parts += ["", "## Top 清单", "", cards_table(top), "", "## 即将截止 / 有截止线索", "", cards_table(closing)]
    if note:
        parts += ["", "## 备注", "", note]
    parts += [
        "",
        "---",
        "由「赛事通」MVP 生成 · 数据来自腾讯云元宝搜索（WSA SearchPro）· 字段含启发式抽取，请点开来源核验。",
        "",
    ]
    return "\n".join(parts)


def write_report(md: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Filename-safe stamp (no slashes from zone labels)
    stamp = _now_iso().replace(" ", "_").replace(":", "").replace("/", "-")
    path = out_dir / f"saishi-report-{stamp}.md"
    path.write_text(md, encoding="utf-8")
    latest = out_dir / "latest.md"
    latest.write_text(md, encoding="utf-8")
    return path
