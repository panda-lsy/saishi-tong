"""赛事通 CLI entry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .client import credentials_present
from .service import load_env_files, run_search


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="saishi",
        description="赛事通：用腾讯云元宝搜索检索技术黑客松/征文/开发者竞赛",
    )
    p.add_argument(
        "query",
        nargs="*",
        help="自然语言条件，如：AI 黑客松 本月截止",
    )
    p.add_argument("-q", "--query-flag", default="", help="同位置参数，便于脚本")
    p.add_argument("--extra", action="append", default=[], help="额外搜索词，可重复")
    p.add_argument("--queries", type=int, default=6, help="最多展开多少组 Query")
    p.add_argument("--cnt", type=int, default=10, choices=[10, 20, 30, 40, 50])
    p.add_argument("--freshness", default="m3", help="SearchPro Freshness，如 d7/m3/y1")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--closing", type=int, default=10)
    p.add_argument("--pause", type=float, default=0.35, help="Query 间暂停秒数")
    p.add_argument(
        "--out",
        type=Path,
        default=Path("out"),
        help="报告输出目录（默认 ./out）",
    )
    p.add_argument("--json", action="store_true", help="额外打印 JSON 到 stdout")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="不调 API，只展开 Query 并写空报告骨架",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="检查 SDK 导入与环境变量是否齐全（不调 API）",
    )
    p.add_argument("--env-file", type=Path, default=None, help="已弃用；仅加载 ~/.config/saishi-tong/tencent.env")
    p.add_argument("--version", action="version", version=f"saishi {__version__}")
    return p


def cmd_check() -> int:
    print(f"saishi {__version__}")
    try:
        import tencentcloud  # noqa: F401
        from tencentcloud.wsa.v20250508 import models, wsa_client  # noqa: F401

        print("OK  tencentcloud-sdk-python / wsa.v20250508 import")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL SDK import: {exc}")
        print("    pip install -r requirements.txt")
        return 1
    if credentials_present():
        print("OK  TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY present in env")
    else:
        print("WARN credentials not in env — set them before live search (see RUN.md)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_files()
    if args.env_file is not None:
        print("--env-file 已弃用：只读取 ~/.config/saishi-tong/tencent.env，现有环境变量优先。", file=sys.stderr)

    if args.check:
        return cmd_check()

    user_text = (args.query_flag or " ".join(args.query)).strip()
    if not user_text and not args.dry_run:
        # Allow empty → default topic via build_queries
        user_text = "AI 黑客松 征文 开发者竞赛 正在报名"

    result = run_search(
        user_text, query_count=args.queries, cnt=args.cnt, freshness=args.freshness,
        top=args.top, closing=args.closing, pause_sec=args.pause,
        dry_run=args.dry_run, out_dir=args.out, extra_queries=args.extra,
    )
    if not result["ok"]:
        print(result["error"], file=sys.stderr)
        return 2
    print(result["markdown"])
    print(f"\nWrote {result['report_path']}", file=sys.stderr)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
