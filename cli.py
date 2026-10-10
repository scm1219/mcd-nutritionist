#!/usr/bin/env python3
"""麦麦营养师 CLI：argparse 子命令骨架，已注册 ``analyze`` 与 ``history``。

各子命令处理函数模式统一：构造 payload → ``emit(payload, render_human(...), args.json)``
→ ``return EXIT_OK``；错误一律走 ``fail``（stderr + 退出码 1 业务 / 2 参数）。

用法::

    python cli.py analyze --items "双层吉士汉堡x1,中薯条x1" [--daily-kcal 2000]
        [--daily-sodium 2000] [--json]
    python cli.py history [--days N] [--detail] [--json]
    python cli.py suggest --max-kcal 600 [--min-protein 25] [--top 3] [--json]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcdu.mcp_client import McduError, McpClient
from mcdu.nutrition import NutritionTable, load_aliases, parse_items_arg
from mcdu.render import EXIT_BUSINESS, EXIT_OK, EXIT_USAGE, emit, fail
from scripts.analyze import build_analysis, render_human
from scripts.history import (
    TOOL_NAME as ORDER_LIST_TOOL,
    render_human as render_history,
    summarize as summarize_history,
)
from scripts.suggest import render_human as render_suggest, suggest_combos, to_payload

#: 仓库根（cli.py 所在目录）：定位缓存与别名表
REPO_ROOT = Path(__file__).resolve().parent
#: 营养表缓存（gitignore 的 data/ 下）
CACHE_PATH = REPO_ROOT / "data" / "nutrition_cache.json"
#: 别名表（可选，不存在则不用别名）
ALIASES_PATH = REPO_ROOT / "aliases.json"


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 解析器：子命令骨架，目前注册了 analyze。"""
    parser = argparse.ArgumentParser(
        prog="cli.py", description="麦麦营养师：麦当劳点单营养分析"
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="命令")

    analyze = subparsers.add_parser("analyze", help="分析一餐的营养合计")
    analyze.add_argument(
        "--items",
        required=True,
        help='点单内容，逗号分隔，如 "双层吉士汉堡x1,中薯条x1"',
    )
    analyze.add_argument(
        "--daily-kcal", type=float, default=2000.0, help="每日热量参考（kcal），默认 2000"
    )
    analyze.add_argument(
        "--daily-sodium", type=float, default=2000.0, help="每日钠参考（mg），默认 2000"
    )
    analyze.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    analyze.set_defaults(handler=cmd_analyze)

    history = subparsers.add_parser("history", help="历史订单营养回顾")
    history.add_argument(
        "--days",
        type=int,
        default=None,
        help="只看最近 N 天的订单（以数据中最新一单时间为基准）",
    )
    history.add_argument(
        "--detail", action="store_true", help="展开每单明细（默认只看汇总统计）"
    )
    history.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    history.set_defaults(handler=cmd_history)

    suggest = subparsers.add_parser("suggest", help="按热量/蛋白质约束推荐配餐组合")
    suggest.add_argument(
        "--max-kcal", type=float, required=True, help="单餐热量上限（kcal），必填"
    )
    suggest.add_argument(
        "--min-protein", type=float, default=0.0, help="蛋白质下限（g），默认 0"
    )
    suggest.add_argument("--top", type=int, default=3, help="输出组合数，默认 3")
    suggest.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    suggest.set_defaults(handler=cmd_suggest)
    return parser


def _load_table() -> NutritionTable:
    """加载营养表：能拿到 Token 就带上 client（缓存过期自动重拉），拿不到
    Token 时退回纯缓存模式，由 :meth:`NutritionTable.load` 决定回退链。
    """
    try:
        client: McpClient | None = McpClient()
    except McduError:
        client = None  # 未配置 Token：不拦 CLI，交给缓存回退逻辑
    return NutritionTable.load(CACHE_PATH, client=client)


def cmd_analyze(args: argparse.Namespace) -> int:
    """analyze 子命令：解析点单 → 匹配合计 → 双通道输出。"""
    try:
        entries = parse_items_arg(args.items)
    except ValueError as exc:
        fail(f"参数错误：{exc}", EXIT_USAGE)
    if not entries:
        fail(
            '参数错误：--items 不能为空，如 --items "双层吉士汉堡x1,中薯条x1"',
            EXIT_USAGE,
        )

    try:
        table = _load_table()
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    aliases = load_aliases(ALIASES_PATH) if ALIASES_PATH.is_file() else {}
    analysis = build_analysis(
        entries,
        table,
        daily_kcal=args.daily_kcal,
        daily_sodium_mg=args.daily_sodium,
        aliases=aliases,
    )

    # 全未知是业务错误：明确提示而不是输出一份全 0 的报告
    if analysis["unknown"] and not analysis["items"]:
        unknown = "、".join(analysis["unknown"])
        fail(
            f"未能匹配任何餐品：{unknown}。请检查名称拼写，"
            "或先运行一次命令以刷新 data/nutrition_cache.json，也可在 aliases.json 添加别名",
            EXIT_BUSINESS,
        )

    emit(analysis, render_human(analysis), args.json)
    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    """history 子命令：拉取 order-list → 展开子项 → 营养统计 → 双通道输出。"""
    if args.days is not None and args.days < 1:
        fail(f"参数错误：--days 必须为正整数（收到 {args.days}）", EXIT_USAGE)

    # 订单数据只能来自 MCP，没有 Token 就明确报业务错误
    try:
        orders_raw = McpClient().call_tool(ORDER_LIST_TOOL, {})
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    try:
        table = _load_table()
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    summary = summarize_history(orders_raw, table, args.days)
    summary["detail"] = args.detail  # 展示开关只给人读渲染用
    emit(summary, render_history(summary), args.json)
    return EXIT_OK


def cmd_suggest(args: argparse.Namespace) -> int:
    """suggest 子命令：营养表内按热量/蛋白质约束搜索配餐组合（基础模式，
    ``--store`` 门店菜单模式由 Task 7 扩展）。"""
    if args.top < 1:
        fail(f"参数错误：--top 必须为正整数（收到 {args.top}）", EXIT_USAGE)

    try:
        table = _load_table()
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    combos = suggest_combos(
        table, max_kcal=args.max_kcal, min_protein=args.min_protein, top=args.top
    )
    if not combos:
        fail(
            f"无满足条件的配餐组合（热量 ≤ {args.max_kcal:g} kcal"
            f" 且蛋白质 ≥ {args.min_protein:g} g），"
            "可放宽 --max-kcal 或降低 --min-protein 后重试",
            EXIT_BUSINESS,
        )

    emit(
        to_payload(args.max_kcal, args.min_protein, combos),
        render_suggest(combos),
        args.json,
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并分派到子命令处理函数。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
