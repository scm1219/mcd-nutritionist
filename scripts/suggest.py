"""suggest 子命令：按热量/蛋白质约束在营养表内搜索配餐组合（基础模式）。

组合语义：主餐必选，小食/饮品可不选（空位=不选）。主餐位不允许为空的
依据：全局约束要求 ``--max-kcal 50`` 必然无组合，而饮品候选含 13 kcal 的
鲜萃咖啡（蛋白/千卡比排进前 12），只有主餐必选（候选最低 246 kcal）才
能保证这一点，配餐语义上也更合理。

品类规则 :func:`classify` 供营养表与门店菜单（Task 7 的 ``--store`` 模式）
两处复用。本模块不做任何 I/O（不联网、不读缓存），营养表由调用方注入。
"""

from __future__ import annotations

from dataclasses import asdict
from itertools import product
from typing import TYPE_CHECKING

from mcdu.nutrition import NutritionItem, normalize_name

if TYPE_CHECKING:  # 仅类型标注用
    from mcdu.nutrition import NutritionTable

#: 每类（主餐/小食/饮品）参与组合的候选上限：按蛋白/千卡比降序取前 N
CANDIDATE_LIMIT = 12

#: totals 的字段与顺序（与 analyze/history 一致）
_TOTAL_FIELDS = ("energy_kcal", "protein_g", "fat_g", "carb_g", "sodium_mg", "calcium_mg")

#: classify 规则（按序，命中即返回）：品类 → 关键词组
_CLASSIFY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("main", ("汉堡", "麦满分", "卷", "堡", "三明治")),
    ("side", ("薯条", "麦乐鸡", "派", "玉米", "鸡翅", "鸡块")),
    (
        "drink",
        ("可乐", "雪碧", "芬达", "咖啡", "拿铁", "卡布", "奶", "茶", "汁", "水", "特饮"),
    ),
)


def classify(name: str) -> str:
    """按名称判断餐品类别，返回 ``main|side|drink|other``（规则按序，命中即返回）。"""
    for category, keywords in _CLASSIFY_RULES:
        if any(keyword in name for keyword in keywords):
            return category
    return "other"


def _protein_density(item: NutritionItem) -> float:
    """蛋白/千卡比：千卡 ≤ 0（如无糖可乐）按 0 计，避免除零。"""
    return item.protein_g / item.energy_kcal if item.energy_kcal > 0 else 0.0


def _candidates(table: "NutritionTable") -> dict[str, list[NutritionItem]]:
    """按 classify 分桶后，各类取蛋白/千卡比最高的前 :data:`CANDIDATE_LIMIT` 个。

    源数据存在重复行（如小杯玉米杯出现两次），先按规范化名去重（保留首行，
    与 :class:`NutritionTable` 匹配索引同语义），否则重复候选会生成重复组合
    白占 top-N 名额。``sorted`` 稳定：并列保持营养表原序，结果确定。
    """
    buckets: dict[str, list[NutritionItem]] = {"main": [], "side": [], "drink": []}
    seen: dict[str, set[str]] = {category: set() for category in buckets}
    for item in table.items:
        category = classify(item.name)
        if category not in buckets:
            continue
        key = normalize_name(item.name)
        if key in seen[category]:
            continue
        seen[category].add(key)
        buckets[category].append(item)
    for category, items in buckets.items():
        buckets[category] = sorted(items, key=_protein_density, reverse=True)[
            :CANDIDATE_LIMIT
        ]
    return buckets


def suggest_combos(
    table: "NutritionTable",
    max_kcal: float,
    min_protein: float = 0,
    top: int = 3,
) -> list[dict]:
    """在营养表内搜索满足约束的配餐组合。

    - 组合空间：主餐（必选）× 小食（可不选）× 饮品（可不选）；
    - 过滤：合计 ``energy_kcal <= max_kcal`` 且 ``protein_g >= min_protein``；
    - 排序：``(protein_g 降序, energy_kcal 升序)`` 取前 ``top`` 个。

    返回 ``[{items: [NutritionItem...], totals, meets: {kcal, protein}}]``，
    ``totals`` 保留 1 位小数；过滤与排序用精确值，避免舍入翻转边界。
    """
    candidates = _candidates(table)
    matched: list[dict] = []
    for main, side, drink in product(
        candidates["main"],
        [None, *candidates["side"]],
        [None, *candidates["drink"]],
    ):
        items = [x for x in (main, side, drink) if x is not None]
        totals = {
            field: sum(getattr(item, field) for item in items)
            for field in _TOTAL_FIELDS
        }
        if totals["energy_kcal"] > max_kcal or totals["protein_g"] < min_protein:
            continue
        matched.append({"items": items, "exact": totals})

    matched.sort(key=lambda c: (-c["exact"]["protein_g"], c["exact"]["energy_kcal"]))
    return [
        {
            "items": combo["items"],
            "totals": {
                field: _round1(combo["exact"][field]) for field in _TOTAL_FIELDS
            },
            "meets": {
                "kcal": combo["exact"]["energy_kcal"] <= max_kcal,
                "protein": combo["exact"]["protein_g"] >= min_protein,
            },
        }
        for combo in matched[:top]
    ]


def suggest_store(
    table: "NutritionTable",
    max_kcal: float,
    min_protein: float = 0,
    top: int = 3,
) -> list[dict]:
    """门店菜单配餐模式（``--store``）：占位，Task 7 填充实现。"""
    raise NotImplementedError("门店菜单配餐（--store）将在 Task 7 实现")


def to_payload(max_kcal: float, min_protein: float, combos: list[dict]) -> dict:
    """组合搜索结果 → JSON 可序列化 payload（NutritionItem 转 dict）。"""
    return {
        "max_kcal": max_kcal,
        "min_protein": min_protein,
        "combos": [
            {
                "items": [asdict(item) for item in combo["items"]],
                "totals": combo["totals"],
                "meets": combo["meets"],
            }
            for combo in combos
        ],
    }


# ---------------------------------------------------------------------------
# 人读渲染
# ---------------------------------------------------------------------------


def render_human(combos: list[dict]) -> str:
    """组合列表渲染为中文人读文本：标题 + 每组合三行（指标 / 成员 / 营养明细）。"""
    lines = [f"配餐建议 TOP{len(combos)}", ""]
    for index, combo in enumerate(combos, start=1):
        totals = combo["totals"]
        lines.append(
            f"[{index}] 蛋白质 {_fmt(totals['protein_g'])} g"
            f" | 热量 {_fmt(totals['energy_kcal'])} kcal"
        )
        lines.append("    " + " + ".join(item.name for item in combo["items"]))
        lines.append(
            f"    脂肪 {_fmt(totals['fat_g'])} g | 碳水 {_fmt(totals['carb_g'])} g"
            f" | 钠 {_fmt(totals['sodium_mg'])} mg | 钙 {_fmt(totals['calcium_mg'])} mg"
        )
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
