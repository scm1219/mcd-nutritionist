#!/usr/bin/env python3
"""麦麦营养师 CLI：argparse 子命令骨架，已注册 ``analyze`` 与 ``history``。

各子命令处理函数模式统一：构造 payload → ``emit(payload, render_human(...), args.json)``
→ ``return EXIT_OK``；错误一律走 ``fail``（stderr + 退出码 1 业务 / 2 参数）。

用法::

    python cli.py analyze --items "双层吉士汉堡x1,中薯条x1" [--daily-kcal 2000]
        [--daily-sodium 2000] [--json]
    python cli.py history [--days N] [--detail] [--json]
    python cli.py suggest --max-kcal 600 [--min-protein 25] [--top 3] [--json]
        [--store CODE [--max-price 40] [--mode pickup|delivery] [--be-code BECODE]]
    python cli.py order stores --city 上海 --keyword 陆家嘴 [--json]
    python cli.py order menu --store CODE [--mode pickup|delivery]
        [--be-code BECODE] [--json]
    python cli.py order draft --store CODE [--mode pickup|delivery]
        --items "双层吉士汉堡x1,中薯条x1" [--coupon ID] [--address-id ID]
        [--no-tableware] [--be-code BECODE] [--json]
    python cli.py order confirm --draft-id ID --yes-place-order [--json]
    python cli.py order status --order-id ID [--json]
    python cli.py order cancel --order-id ID --reason CODE --yes-cancel [--json]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcdu.mcp_client import McduError, McpClient
from mcdu.nutrition import NutritionTable, load_aliases, parse_items_arg
from mcdu.orderflow import (
    MODE_BE_TYPES,
    MODE_ORDER_TYPES,
    STORES_TOOL,
    DraftStore,
    load_menu,
    parse_stores,
)
from mcdu.render import EXIT_BUSINESS, EXIT_OK, EXIT_USAGE, emit, fail
from scripts.analyze import build_analysis, render_human
from scripts.history import (
    TOOL_NAME as ORDER_LIST_TOOL,
    render_human as render_history,
    summarize as summarize_history,
)
from scripts.order import (
    ADDRESSES_TOOL,
    CANCEL_ORDER_TOOL,
    CREATE_ORDER_TOOL,
    QUERY_ORDER_TOOL,
    append_orders_log,
    build_cancel_output,
    build_confirm_output,
    build_draft,
    build_draft_payload,
    build_menu_payload,
    build_status_output,
    build_stores_payload,
    create_order_arguments,
    extract_order_id,
    parse_addresses,
    render_address_choices,
    render_cancel_preview,
    render_confirm_preview,
    render_draft,
    render_menu,
    render_order_cancelled,
    render_order_created,
    render_order_status,
    render_stores,
    require_yes,
)
from scripts.suggest import (
    render_human as render_suggest,
    render_store,
    suggest_combos,
    suggest_store,
    to_payload,
    to_store_payload,
)

#: 仓库根（cli.py 所在目录）：定位缓存与别名表
REPO_ROOT = Path(__file__).resolve().parent
#: 营养表缓存（gitignore 的 data/ 下）
CACHE_PATH = REPO_ROOT / "data" / "nutrition_cache.json"
#: 别名表（可选，不存在则不用别名）
ALIASES_PATH = REPO_ROOT / "aliases.json"
#: 门店菜单缓存目录（菜单缓存文件 menu_cache_{store}_{mode}.json 落在这里）
MENU_CACHE_DIR = REPO_ROOT / "data"
#: 下单草稿目录（draft_{id}.json 落在这里，30 分钟 TTL）
DRAFT_DIR = REPO_ROOT / "data"
#: 下单流水（confirm 成功后追加：[{draft_id, order_id, created_at, raw}]）
ORDERS_LOG_PATH = REPO_ROOT / "data" / "orders_log.json"


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 解析器：analyze / history / suggest 与 order 子命令组。"""
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
    suggest.add_argument(
        "--store",
        default=None,
        help="门店编码 storeCode（见 order stores）：启用门店模式，"
        "用真实菜单组合并以 calculate-price 取真实实付价",
    )
    suggest.add_argument(
        "--max-price",
        type=float,
        default=None,
        help="组合实付总价上限（元），仅 --store 门店模式有效",
    )
    suggest.add_argument(
        "--mode",
        choices=list(MODE_BE_TYPES),
        default="pickup",
        help="取餐方式：pickup=到店取餐（默认）/ delivery=麦乐送（需 --be-code）",
    )
    suggest.add_argument(
        "--be-code",
        default=None,
        help="业务编码 beCode：delivery（麦乐送）等场景必传，"
        "来自门店查询结果（order stores 输出的 beCode 字段）",
    )
    suggest.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    suggest.set_defaults(handler=cmd_suggest)

    order = subparsers.add_parser("order", help="门店/菜单/草稿/下单流程")
    order_sub = order.add_subparsers(dest="order_command", required=True, metavar="子命令")

    order_stores = order_sub.add_parser("stores", help="按城市+关键词搜索可点餐门店")
    order_stores.add_argument("--city", required=True, help="城市名，如 上海")
    order_stores.add_argument("--keyword", required=True, help="位置关键词，如 陆家嘴")
    order_stores.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    order_stores.set_defaults(handler=cmd_order_stores)

    order_menu = order_sub.add_parser("menu", help="查询门店菜单")
    order_menu.add_argument("--store", required=True, help="门店编码 storeCode（见 order stores）")
    order_menu.add_argument(
        "--mode",
        choices=list(MODE_BE_TYPES),
        default="pickup",
        help="取餐方式：pickup=到店取餐（默认）/ delivery=麦乐送",
    )
    order_menu.add_argument(
        "--be-code",
        default=None,
        help="业务编码 beCode：delivery（麦乐送）等场景必传，来自门店查询结果"
        "（order stores 输出的 beCode 字段；到店取餐不需要）",
    )
    order_menu.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    order_menu.set_defaults(handler=cmd_order_menu)

    order_draft = order_sub.add_parser(
        "draft", help="生成下单草稿（算价+营养预览，绝不真实下单）"
    )
    order_draft.add_argument("--store", required=True, help="门店编码 storeCode（见 order stores）")
    order_draft.add_argument(
        "--mode",
        choices=list(MODE_BE_TYPES),
        default="pickup",
        help="取餐方式：pickup=到店取餐（默认）/ delivery=麦乐送（需 --address-id 与 --be-code）",
    )
    order_draft.add_argument(
        "--items",
        required=True,
        help='点单内容，逗号分隔，如 "双层吉士汉堡x1,中薯条x1"',
    )
    order_draft.add_argument(
        "--coupon", default=None, help="优惠券 ID（透传 calculate-price 试算优惠）"
    )
    order_draft.add_argument(
        "--address-id",
        default=None,
        help="配送地址 ID（delivery 必填；缺省时列出可用地址供选择后退出）",
    )
    order_draft.add_argument(
        "--be-code",
        default=None,
        help="业务编码 beCode：delivery（麦乐送）等场景必传，来自门店查询结果"
        "（order stores 输出的 beCode 字段）",
    )
    order_draft.add_argument(
        "--no-tableware",
        dest="need_tableware",
        action="store_false",
        help="不需要餐具（默认需要）",
    )
    order_draft.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    order_draft.set_defaults(handler=cmd_order_draft)

    order_confirm = order_sub.add_parser(
        "confirm", help="确认草稿并真实下单（create-order，需 --yes-place-order）"
    )
    order_confirm.add_argument(
        "--draft-id", required=True, help="草稿 ID（order draft 输出的 draft_id）"
    )
    order_confirm.add_argument(
        "--yes-place-order",
        action="store_true",
        help="安全门：确认真实下单；缺省时只打印将执行的草稿摘要并退出",
    )
    order_confirm.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    order_confirm.set_defaults(handler=cmd_order_confirm)

    order_status = order_sub.add_parser(
        "status", help="查询订单详情（query-order 透传）"
    )
    order_status.add_argument("--order-id", required=True, help="订单号")
    order_status.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    order_status.set_defaults(handler=cmd_order_status)

    order_cancel = order_sub.add_parser(
        "cancel", help="取消订单（cancel-order，需 --yes-cancel）"
    )
    order_cancel.add_argument("--order-id", required=True, help="订单号")
    order_cancel.add_argument(
        "--reason",
        required=True,
        help="取消原因 code：1=改主意了 2=重复下单 3=点错了/点多了/点少了"
        " 4=地址/电话填错了 5=送达时间选错了 -1=其它原因",
    )
    order_cancel.add_argument(
        "--yes-cancel",
        action="store_true",
        help="安全门：确认取消（不可逆）；缺省时只打印将取消的订单并退出",
    )
    order_cancel.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    order_cancel.set_defaults(handler=cmd_order_cancel)
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
    """suggest 子命令：营养表内按约束搜索组合（基础模式）；``--store CODE``
    切换门店模式（真实菜单 + calculate-price 实付价，可加 ``--max-price``）。"""
    if args.top < 1:
        fail(f"参数错误：--top 必须为正整数（收到 {args.top}）", EXIT_USAGE)
    if args.max_price is not None and args.store is None:
        fail("参数错误：--max-price 仅在 --store 门店模式下可用", EXIT_USAGE)

    try:
        table = _load_table()
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    if args.store is not None:
        return _cmd_suggest_store(args, table)

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


def _cmd_suggest_store(args: argparse.Namespace, table: NutritionTable) -> int:
    """suggest 的门店模式：菜单可走缓存，但算价必须联网（无 Token 即业务错误）。"""
    try:
        client = McpClient()
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    try:
        combos = suggest_store(
            client,
            table,
            args.store,
            args.mode,
            max_kcal=args.max_kcal,
            min_protein=args.min_protein,
            max_price=args.max_price,
            top=args.top,
            cache_dir=MENU_CACHE_DIR,
            be_code=args.be_code,
        )
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    if not combos:
        caps = [f"热量 ≤ {args.max_kcal:g} kcal"]
        if args.min_protein > 0:
            caps.append(f"蛋白质 ≥ {args.min_protein:g} g")
        if args.max_price is not None:
            caps.append(f"实付总价 ≤ {args.max_price:g} 元")
        fail(
            f"无满足条件的配餐组合（{' 且 '.join(caps)}），"
            "可放宽 --max-kcal / 降低 --min-protein / 提高 --max-price 后重试",
            EXIT_BUSINESS,
        )

    emit(
        to_store_payload(
            args.store, args.mode, args.max_kcal, args.min_protein, args.max_price, combos
        ),
        render_store(args.store, args.mode, combos),
        args.json,
    )
    return EXIT_OK


def cmd_order_stores(args: argparse.Namespace) -> int:
    """order stores：query-nearby-stores（beType=1 到店，searchType=2 位置搜索）。"""
    try:
        raw = McpClient().call_tool(
            STORES_TOOL,
            {
                "beType": 1,
                "searchType": 2,  # 实测 1=收藏餐厅（keyword 无效），2 才按城市+关键词过滤
                "city": args.city,
                "keyword": args.keyword,
            },
        )
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    stores = parse_stores(raw)
    emit(build_stores_payload(stores), render_stores(stores), args.json)
    return EXIT_OK


def cmd_order_menu(args: argparse.Namespace) -> int:
    """order menu：门店菜单（10 分钟 TTL 缓存，mode → beType/orderType）。"""
    be_type = MODE_BE_TYPES[args.mode]
    order_type = MODE_ORDER_TYPES[args.mode]
    # 缓存新鲜时不必联网：Token 缺失只在真正要重取时才报错
    try:
        client: McpClient | None = McpClient()
    except McduError:
        client = None
    try:
        items = load_menu(
            client, args.store, be_type, order_type, MENU_CACHE_DIR, be_code=args.be_code
        )
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    emit(
        build_menu_payload(args.store, args.mode, items),
        render_menu(args.store, args.mode, items),
        args.json,
    )
    return EXIT_OK


def cmd_order_draft(args: argparse.Namespace) -> int:
    """order draft：菜单定位 → 套餐默认组成 → 算价 → 营养 → 落盘草稿。

    全程只调只读工具（query-meals/query-meal-detail/calculate-price/
    delivery-query-addresses），**绝不调用 create-order**。外送未给
    ``--address-id`` 时列出可用地址并以业务错误退出（退出码 1）。
    """
    try:
        entries = parse_items_arg(args.items)
    except ValueError as exc:
        fail(f"参数错误：{exc}", EXIT_USAGE)
    if not entries:
        fail(
            '参数错误：--items 不能为空，如 --items "双层吉士汉堡x1,中薯条x1"',
            EXIT_USAGE,
        )

    # 算价必须联网：草稿流程没有纯缓存回退
    try:
        client = McpClient()
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    if args.mode == "delivery" and not args.address_id:
        try:
            addresses = parse_addresses(client.call_tool(ADDRESSES_TOOL, {}))
        except McduError as exc:
            fail(exc.message, EXIT_BUSINESS)
        fail(render_address_choices(addresses), EXIT_BUSINESS)

    try:
        table = _load_table()
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)
    aliases = load_aliases(ALIASES_PATH) if ALIASES_PATH.is_file() else {}

    try:
        draft, combos, display = build_draft(
            client,
            table,
            args.store,
            args.mode,
            entries,
            address_id=args.address_id,
            be_code=args.be_code,
            coupon_id=args.coupon,
            need_tableware=args.need_tableware,
            cache_dir=MENU_CACHE_DIR,
            aliases=aliases,
        )
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)
    except ValueError as exc:  # 菜单找不到点单项 / 坏 mode
        fail(str(exc), EXIT_BUSINESS)

    DraftStore(DRAFT_DIR).save(draft)
    emit(
        build_draft_payload(draft, display, combos, coupon_id=args.coupon),
        render_draft(draft, display, combos, coupon_id=args.coupon),
        args.json,
    )
    return EXIT_OK


def cmd_order_confirm(args: argparse.Namespace) -> int:
    """order confirm：草稿 →（安全门）→ create-order → 流水 → 删草稿。

    顺序即安全边界：先确认草稿存在且未过期（否则退出 1 并提示重新
    draft），再过 ``--yes-place-order`` 安全门，最后才构造 client 下单。
    下单参数全部读自草稿本体（单一事实源），失败时草稿保留可重试。
    """
    draft_store = DraftStore(DRAFT_DIR)
    draft = draft_store.load(args.draft_id)
    if draft is None:
        fail(
            f"草稿 {args.draft_id} 不存在或已过期（30 分钟内有效），"
            "请重新 order draft 生成",
            EXIT_BUSINESS,
        )

    if not args.yes_place_order:
        print(render_confirm_preview(draft))
        require_yes(args.yes_place_order, f"真实下单（create-order，草稿 {args.draft_id}）")

    try:
        arguments = create_order_arguments(draft)
    except ValueError as exc:  # 草稿里 mode 不合法（旧版/手改文件）
        fail(str(exc), EXIT_BUSINESS)

    try:
        client = McpClient()
        raw = client.call_tool(CREATE_ORDER_TOOL, arguments)
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    order_id = extract_order_id(raw)
    append_orders_log(ORDERS_LOG_PATH, draft.draft_id, order_id, raw)
    draft_store.delete(draft.draft_id)
    emit(
        build_confirm_output(draft.draft_id, order_id, raw),
        render_order_created(draft.draft_id, order_id, raw),
        args.json,
    )
    return EXIT_OK


def cmd_order_status(args: argparse.Namespace) -> int:
    """order status：query-order 透传（orderId 单参数）。"""
    try:
        client = McpClient()
        raw = client.call_tool(QUERY_ORDER_TOOL, {"orderId": args.order_id})
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    emit(
        build_status_output(args.order_id, raw),
        render_order_status(args.order_id, raw),
        args.json,
    )
    return EXIT_OK


def cmd_order_cancel(args: argparse.Namespace) -> int:
    """order cancel：（安全门）→ cancel-order（orderId + cancelReasonCode）。"""
    if not args.yes_cancel:
        print(render_cancel_preview(args.order_id, args.reason))
        require_yes(args.yes_cancel, f"取消订单 {args.order_id}（cancel-order）")

    try:
        client = McpClient()
        raw = client.call_tool(
            CANCEL_ORDER_TOOL,
            {"orderId": args.order_id, "cancelReasonCode": str(args.reason)},
        )
    except McduError as exc:
        fail(exc.message, EXIT_BUSINESS)

    emit(
        build_cancel_output(args.order_id, raw),
        render_order_cancelled(args.order_id, raw),
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
