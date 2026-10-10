"""history 子命令：历史订单的营养回顾（纯计算 + 人读渲染）。

数据来自 ``McpClient.call_tool("order-list", {})``：``data.list[]`` 每单带
``orderProductList[]``，商品可含 ``comboItemList[]`` 子项。展开规则：有子项取
子项 ``(name, quantity)``（营销套餐名不参与营养计算），无子项才用
``(productName, quantity)``。

本模块不做任何 I/O（不联网、不读缓存），营养表由调用方注入，便于测试。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mcdu.nutrition import NutritionTable
from scripts.analyze import CARB_KCAL_PER_G, FAT_KCAL_PER_G, PROTEIN_KCAL_PER_G

#: 拉取订单列表用的 MCP 工具名
TOOL_NAME = "order-list"

#: order-list 的 createTime 格式
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 最常点榜单长度
TOP_N = 5

#: totals 的字段与顺序（与 analyze 一致，正好对应 NutritionItem 同名字段）
_TOTAL_FIELDS = ("energy_kcal", "protein_g", "fat_g", "carb_g", "sodium_mg", "calcium_mg")


# ---------------------------------------------------------------------------
# 订单展开
# ---------------------------------------------------------------------------


def _order_list(data: dict) -> list[dict]:
    """从 order-list 返回中取 ``data.list``；结构不符给空列表而不是抛错。"""
    body = data.get("data") if isinstance(data, dict) else None
    if isinstance(body, dict):
        data = body
    orders = data.get("list") if isinstance(data, dict) else None
    if not isinstance(orders, list):
        return []
    return [order for order in orders if isinstance(order, dict)]


def _qty_of(entry: dict) -> int:
    """读条目数量：缺省按 1，解析不出或非正数返回 0（调用方跳过该条目）。"""
    try:
        qty = int(entry.get("quantity", 1))
    except (TypeError, ValueError):
        return 0
    return qty if qty > 0 else 0


def _flatten_order(order: dict) -> list[tuple[str, int]]:
    """单订单展开为 ``(餐品名, 数量)``：优先 comboItemList 子项，无子项才用 productName。"""
    items: list[tuple[str, int]] = []
    for product in order.get("orderProductList") or []:
        if not isinstance(product, dict):
            continue
        combos = [
            sub for sub in (product.get("comboItemList") or []) if isinstance(sub, dict)
        ]
        entries = (
            [(sub.get("name"), sub) for sub in combos]
            if combos
            else [(product.get("productName"), product)]
        )
        for raw_name, entry in entries:
            name = str(raw_name or "").strip()
            qty = _qty_of(entry)
            if name and qty > 0:
                items.append((name, qty))
    return items


def flatten_orders(data: dict) -> list[tuple[str, int]]:
    """全部订单展开为 ``(餐品名, 数量)`` 列表（营销套餐名不出现在结果里）。"""
    items: list[tuple[str, int]] = []
    for order in _order_list(data):
        items.extend(_flatten_order(order))
    return items


# ---------------------------------------------------------------------------
# 时间过滤
# ---------------------------------------------------------------------------


def _parse_time(order: dict) -> datetime | None:
    """解析 createTime；缺失或格式不对返回 None（由调用方决定去留）。"""
    raw = order.get("createTime")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), _TIME_FORMAT)
    except ValueError:
        return None


def _filter_days(orders: list[dict], days: int | None) -> list[dict]:
    """以数据中最新一单的 createTime 为基准回看 ``days`` 天（闭区间）。

    解析不出 createTime 的单不参与基准计算，也不会被过滤掉（无法证明超出
    范围时宁可多展示，且 --days 只影响展示范围，不影响营养计算本身）。
    """
    if days is None:
        return orders
    times = [_parse_time(order) for order in orders]
    known = [t for t in times if t is not None]
    if not known:
        return orders
    cutoff = max(known) - timedelta(days=days)
    return [order for order, t in zip(orders, times) if t is None or t >= cutoff]


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def summarize(orders_raw: dict, table: NutritionTable, days: int | None) -> dict:
    """订单列表 → 营养回顾 payload。

    - ``orders``：每单 ``{order_id, create_time, store, totals, unknown}``；
      未知餐品只进 ``unknown``（保留原始名），绝不按 0 计入 totals；
    - ``stats``：``meal_count / avg_kcal / max_kcal / avg_sodium / macro_ratio /
      top5 / unknown_count``。top5 按点单份数降序（已知项用标准名，未知项用
      原名，并列保持首次出现顺序）；macro_ratio 由全部已知项克数合计换算
      （蛋白 4 / 碳水 4 / 脂肪 9 kcal/g）；unknown_count 是未知项份数合计；
    - ``days`` 非 None 时按 createTime 过滤（以最新一单为基准），只影响展示
      范围，被滤掉的单不参与任何统计。
    """
    orders = _filter_days(_order_list(orders_raw), days)

    rows: list[dict] = []
    counts: dict[str, int] = {}
    grand = dict.fromkeys(_TOTAL_FIELDS, 0.0)
    unknown_count = 0

    for order in orders:
        totals = dict.fromkeys(_TOTAL_FIELDS, 0.0)
        unknown: list[str] = []
        for name, qty in _flatten_order(order):
            matched = table.match(name)
            if matched.item is None:  # 未知：显式标注，绝不按 0 计入合计
                unknown.append(name)
                unknown_count += qty
                display = name
            else:
                item = matched.item
                for field in _TOTAL_FIELDS:
                    value = getattr(item, field) * qty
                    totals[field] += value
                    grand[field] += value
                display = item.name
            counts[display] = counts.get(display, 0) + qty

        rows.append(
            {
                "order_id": str(order.get("orderId", "")),
                "create_time": str(order.get("createTime", "")),
                "store": str(order.get("storeName", "")),
                "totals": {field: _round1(totals[field]) for field in _TOTAL_FIELDS},
                "unknown": list(dict.fromkeys(unknown)),  # 去重且保持出现顺序
            }
        )

    meal_count = len(rows)
    kcal_values = [row["totals"]["energy_kcal"] for row in rows]
    sodium_values = [row["totals"]["sodium_mg"] for row in rows]

    # 供能比：由宏量营养素克数合计换算后归一化（与 analyze 同一套系数）
    protein_kcal = grand["protein_g"] * PROTEIN_KCAL_PER_G
    fat_kcal = grand["fat_g"] * FAT_KCAL_PER_G
    carb_kcal = grand["carb_g"] * CARB_KCAL_PER_G
    macro_total = protein_kcal + fat_kcal + carb_kcal
    if macro_total > 0:
        macro_ratio = {
            "protein": _round1(protein_kcal / macro_total * 100),
            "fat": _round1(fat_kcal / macro_total * 100),
            "carb": _round1(carb_kcal / macro_total * 100),
        }
    else:
        macro_ratio = {"protein": 0.0, "fat": 0.0, "carb": 0.0}

    # 稳定排序：份数降序，并列保持首次出现顺序
    top5 = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]

    return {
        "orders": rows,
        "stats": {
            "meal_count": meal_count,
            "avg_kcal": _round1(sum(kcal_values) / meal_count) if meal_count else 0.0,
            "max_kcal": _round1(max(kcal_values)) if kcal_values else 0.0,
            "avg_sodium": (
                _round1(sum(sodium_values) / meal_count) if meal_count else 0.0
            ),
            "macro_ratio": macro_ratio,
            "top5": top5,
            "unknown_count": unknown_count,
        },
    }


# ---------------------------------------------------------------------------
# 人读渲染
# ---------------------------------------------------------------------------


def render_human(summary: dict) -> str:
    """回顾结果渲染为中文人读报告：总览统计 +（``detail`` 为真时）每单明细。

    ``summary["detail"]`` 由 CLI 注入（summarize 本身不关心展示开关）。
    """
    stats = summary["stats"]
    orders = summary.get("orders") or []
    if not orders:
        return "暂无订单：账户近期没有可回顾的订单记录"

    ratio = stats["macro_ratio"]
    lines = [
        f"历史订单营养回顾（{stats['meal_count']} 单）",
        "",
        f"  平均热量 {_fmt(stats['avg_kcal'])} kcal"
        f" | 单笔最高 {_fmt(stats['max_kcal'])} kcal"
        f" | 平均钠 {_fmt(stats['avg_sodium'])} mg",
        f"  供能比：蛋白 {_fmt(ratio['protein'])}% | 脂肪 {_fmt(ratio['fat'])}%"
        f" | 碳水 {_fmt(ratio['carb'])}%",
    ]
    if stats["top5"]:
        board = "、".join(f"{name} x{count}" for name, count in stats["top5"])
        lines.append(f"  最常点 TOP{len(stats['top5'])}：{board}")
    if stats["unknown_count"]:
        names = list(dict.fromkeys(u for order in orders for u in order["unknown"]))
        lines.append(
            f"  ⚠️ {stats['unknown_count']} 份未知营养项未计入统计"
            f"（如：{'、'.join(names[:3])}，可在 aliases.json 添加别名）"
        )

    if summary.get("detail"):
        lines += ["", "每单明细"]
        for index, order in enumerate(orders, start=1):
            totals = order["totals"]
            lines.append(
                f"[{index}] {order['create_time']} · {order['store']}"
                f"（订单尾号 {order['order_id'][-4:]}）"
            )
            lines += [
                f"    热量 {_fmt(totals['energy_kcal'])} kcal"
                f" | 蛋白质 {_fmt(totals['protein_g'])} g"
                f" | 脂肪 {_fmt(totals['fat_g'])} g"
                f" | 碳水 {_fmt(totals['carb_g'])} g",
                f"    钠 {_fmt(totals['sodium_mg'])} mg"
                f" | 钙 {_fmt(totals['calcium_mg'])} mg",
            ]
            if order["unknown"]:
                lines.append(f"    未知（未计入合计）：{'、'.join(order['unknown'])}")

    return "\n".join(lines)


def _round1(value: float) -> float:
    """保留 1 位小数（与 scripts.analyze 的同名工具保持一致）。"""
    return round(value + 0.0, 1)


def _fmt(value: float) -> str:
    """数值人读化：整数不带小数点，否则保留 1 位（与 scripts.analyze 一致）。"""
    rounded = round(value)
    if abs(value - rounded) < 1e-9:
        return str(int(rounded))
    return f"{value:.1f}"
