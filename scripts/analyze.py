"""analyze 子命令：营养合计 / 供能比 / 每日参考 / 警告的纯计算与人读渲染。

本模块不做任何 I/O（不联网、不读缓存），表与别名由调用方注入，便于测试。
"""

from __future__ import annotations

import unicodedata

from mcdu.nutrition import NutritionTable

#: 供能系数（kcal/g）：蛋白 4 / 碳水 4 / 脂肪 9
PROTEIN_KCAL_PER_G = 4.0
CARB_KCAL_PER_G = 4.0
FAT_KCAL_PER_G = 9.0

#: 单餐警告阈值（与每日参考无关的固定值）
SINGLE_MEAL_KCAL_WARN = 800.0
SINGLE_MEAL_SODIUM_WARN_MG = 2000.0

#: 警告文案（固定阈值写死在文案里，测试按文案断言）
WARNING_KCAL_TEXT = "单餐热量超过 800 kcal"
WARNING_SODIUM_TEXT = "钠摄入超过 2000 mg"

#: totals 的字段与顺序（energy_kj 不进合计，只作为数据源字段保留）
_TOTAL_FIELDS = ("energy_kcal", "protein_g", "fat_g", "carb_g", "sodium_mg", "calcium_mg")


def build_analysis(
    entries: list[tuple[str, int]],
    table: NutritionTable,
    daily_kcal: float = 2000.0,
    daily_sodium_mg: float = 2000.0,
    aliases: dict[str, str] | None = None,
) -> dict:
    """把 ``[(餐品名, 数量), ...]`` 汇总为分析 payload。

    - 未知餐品进 ``unknown`` 列表，绝不按 0 计入 totals；
    - ``ratio`` 为蛋白/脂肪/碳水的供能百分比（三项和 ≈ 100%）；
    - ``daily_pct`` 按传入的每日参考计算；
    - ``warnings`` 用固定单餐阈值（800 kcal / 钠 2000 mg）。
    """
    totals = dict.fromkeys(_TOTAL_FIELDS, 0.0)
    items: list[dict] = []
    unknown: list[str] = []

    for name, qty in entries:
        matched = table.match(name, aliases=aliases)
        if matched.item is None:  # 未知项：显式列出，跳过合计
            unknown.append(name)
            continue
        item = matched.item
        totals["energy_kcal"] += item.energy_kcal * qty
        totals["protein_g"] += item.protein_g * qty
        totals["fat_g"] += item.fat_g * qty
        totals["carb_g"] += item.carb_g * qty
        totals["sodium_mg"] += item.sodium_mg * qty
        totals["calcium_mg"] += item.calcium_mg * qty
        items.append(
            {
                "name": item.name,
                "qty": qty,
                "confidence": matched.confidence,
                "energy_kcal": _round1(item.energy_kcal * qty),
            }
        )

    totals = {field: _round1(totals[field]) for field in _TOTAL_FIELDS}

    # 供能比：由宏量营养素克数换算热量后归一化
    protein_kcal = totals["protein_g"] * PROTEIN_KCAL_PER_G
    fat_kcal = totals["fat_g"] * FAT_KCAL_PER_G
    carb_kcal = totals["carb_g"] * CARB_KCAL_PER_G
    macro_total = protein_kcal + fat_kcal + carb_kcal
    if macro_total > 0:
        ratio = {
            "protein": _round1(protein_kcal / macro_total * 100),
            "fat": _round1(fat_kcal / macro_total * 100),
            "carb": _round1(carb_kcal / macro_total * 100),
        }
    else:
        ratio = {"protein": 0.0, "fat": 0.0, "carb": 0.0}

    daily_pct = {
        "energy": _round1(totals["energy_kcal"] / daily_kcal * 100) if daily_kcal > 0 else 0.0,
        "sodium": (
            _round1(totals["sodium_mg"] / daily_sodium_mg * 100) if daily_sodium_mg > 0 else 0.0
        ),
    }

    warnings: list[str] = []
    if totals["energy_kcal"] > SINGLE_MEAL_KCAL_WARN:
        warnings.append(WARNING_KCAL_TEXT)
    if totals["sodium_mg"] > SINGLE_MEAL_SODIUM_WARN_MG:
        warnings.append(WARNING_SODIUM_TEXT)

    return {
        "totals": totals,
        "ratio": ratio,
        "daily_pct": daily_pct,
        "warnings": warnings,
        "items": items,
        "unknown": unknown,
    }


def render_human(analysis: dict) -> str:
    """分析结果渲染为中文人读报告：表头 + 每项一行 + 合计块 + 供能比 +
    每日参考百分比 + 警告（⚠️ 前缀）+ 未知项提示。
    """
    totals = analysis["totals"]
    ratio = analysis["ratio"]
    daily = analysis["daily_pct"]

    lines = ["麦当劳营养报告"]

    if analysis["items"]:
        name_width = max(
            [_display_width("餐品")] + [_display_width(i["name"]) for i in analysis["items"]]
        )
        lines.append("")
        lines.append(_pad("餐品", name_width) + " 数量  热量(kcal)")
        for item in analysis["items"]:
            mark = "" if item["confidence"] == "exact" else "（模糊匹配）"
            lines.append(
                _pad(item["name"] + mark, name_width)
                + f" x{_fmt(item['qty'])}  {_fmt(item['energy_kcal'])}"
            )

    lines += [
        "",
        "合计",
        f"  热量 {_fmt(totals['energy_kcal'])} kcal"
        f" | 蛋白质 {_fmt(totals['protein_g'])} g"
        f" | 脂肪 {_fmt(totals['fat_g'])} g"
        f" | 碳水 {_fmt(totals['carb_g'])} g",
        f"  钠 {_fmt(totals['sodium_mg'])} mg | 钙 {_fmt(totals['calcium_mg'])} mg",
        "",
        f"供能比：蛋白 {_fmt(ratio['protein'])}% | 脂肪 {_fmt(ratio['fat'])}%"
        f" | 碳水 {_fmt(ratio['carb'])}%",
        f"每日参考占比：热量 {_fmt(daily['energy'])}% | 钠 {_fmt(daily['sodium'])}%",
    ]

    for warning in analysis["warnings"]:
        lines.append("")
        lines.append(f"⚠️ {warning}")

    if analysis["unknown"]:
        lines.append("")
        lines.append(f"未知餐品（未计入合计）：{'、'.join(analysis['unknown'])}")

    return "\n".join(lines)


def _round1(value: float) -> float:
    """保留 1 位小数（整数值得 718.0 这类浮点，而不是长尾）。"""
    return round(value + 0.0, 1)


def _fmt(value: float) -> str:
    """数值人读化：整数不带小数点，否则保留 1 位。"""
    rounded = round(value)
    if abs(value - rounded) < 1e-9:
        return str(int(rounded))
    return f"{value:.1f}"


def _display_width(text: str) -> int:
    """终端显示宽度：中日韩宽字符按 2 列计。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格，至少留 1 空格分隔。"""
    return text + " " * max(1, width - _display_width(text))
