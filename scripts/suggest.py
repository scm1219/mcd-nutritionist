"""suggest 子命令：按热量/蛋白质约束搜索配餐组合（基础模式 + ``--store`` 门店模式）。

组合语义：主餐必选，小食/饮品可不选（空位=不选）。主餐位不允许为空的
依据：全局约束要求 ``--max-kcal 50`` 必然无组合，而饮品候选含 13 kcal 的
鲜萃咖啡（蛋白/千卡比排进前 12），只有主餐必选（候选最低 246 kcal）才
能保证这一点，配餐语义上也更合理。

品类规则 :func:`classify` 供营养表与门店菜单（``--store`` 模式）两处复用，
组合生成 :func:`build_candidates` 同样两模式共用。营养表仍由调用方注入；
门店模式经注入的 client 取菜单与算价（菜单走 :func:`mcdu.orderflow.load_menu`
的 10 分钟 TTL 缓存，价格每次实时调 ``calculate-price``）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING

from mcdu.mcp_client import McduError
from mcdu.nutrition import NutritionItem, normalize_name
from mcdu.orderflow import (
    MODE_BE_TYPES,
    MODE_ORDER_TYPES,
    load_menu,
    resolve_items,
)

if TYPE_CHECKING:  # 仅类型标注用
    from mcdu.mcp_client import McpClient
    from mcdu.nutrition import NutritionTable
    from mcdu.orderflow import MenuItem

#: 每类（主餐/小食/饮品）参与组合的候选上限：按蛋白/千卡比降序取前 N
CANDIDATE_LIMIT = 12

#: 价格计算工具（计算含优惠的实付金额）
PRICE_TOOL = "calculate-price"

#: calculate-price 的实付金额字段（``data.price``，单位分）。计划原猜 payAmount，
#: 2026-10-10 只读实录（门店 1450330 pickup，餐品 521799 日式鲜磨山葵酱
#: currentPrice ¥3.0）确认响应为 ``{"data": {"price": 300, ...}}``，单位分，
#: 除以 100 得元；详见仓库根 MCP_INTEGRATION.md。
PRICE_AMOUNT_FIELD = "price"

#: 门店模式的算价候选池 = PRICE_POOL_MARGIN * top + 3：价格上限会淘汰部分
#: 营养达标的候选，按 top 的 2 倍再多 3 个留余量，同时控制 calculate-price
#: 调用量（菜单大时绝不全量算价；达标即提前停）。
PRICE_POOL_MARGIN = 2

#: 门店模式下营养达标候选的选取广度：从 build_candidates 取这么多候选供
#: 价格维度重排（菜单价合计 ≤ --max-price 的候选优先），够宽又不全量。
PRICE_SELECT_BREADTH = 60

#: totals 的字段与顺序（与 analyze/history 一致）
_TOTAL_FIELDS = ("energy_kcal", "protein_g", "fat_g", "carb_g", "sodium_mg", "calcium_mg")

#: classify 规则（按序，命中即返回）：品类 → 关键词组。饮品词里的"美汁源"
#: 是果汁品牌整词（补上不以"汁"结尾的【美汁源】"黄金橙橙"）。
_CLASSIFY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("main", ("汉堡", "麦满分", "卷", "堡", "三明治")),
    ("side", ("薯条", "麦乐鸡", "派", "玉米", "鸡翅", "鸡块")),
    (
        "drink",
        ("可乐", "雪碧", "芬达", "咖啡", "拿铁", "卡布", "奶", "茶", "美汁源", "水", "特饮"),
    ),
)

#: 饮品的词尾关键词：仅匹配名称结尾（评审授权对计划关键词表的偏离）。
#: 裸"汁"子串会把"蜜汁/脆汁鸡"类餐食误判成饮品（麦麦脆汁鸡、蜜汁BBQ/
#: 蒜了吧黄油风味薄皮脆汁鸡、蜜汁快乐共 7 项，18–22g 蛋白，蛋白密度高，
#: 曾占满饮品候选前 12）；词尾锚定后"100%苹果汁"仍是饮品，餐食落入
#: other（不含主餐/小食关键词）。
_DRINK_SUFFIXES = ("汁",)


def classify(name: str) -> str:
    """按名称判断餐品类别，返回 ``main|side|drink|other``（规则按序，命中即返回）。

    饮品规则 = 子串关键词（:data:`_CLASSIFY_RULES`）或词尾关键词
    (:data:`_DRINK_SUFFIXES`)；主餐/小食规则不受影响。
    """
    for category, keywords in _CLASSIFY_RULES:
        if any(keyword in name for keyword in keywords):
            return category
    if name.endswith(_DRINK_SUFFIXES):
        return "drink"
    return "other"


def _protein_density(item: NutritionItem) -> float:
    """蛋白/千卡比：千卡 ≤ 0（如无糖可乐）按 0 计，避免除零。"""
    return item.protein_g / item.energy_kcal if item.energy_kcal > 0 else 0.0


@dataclass
class MenuEntry:
    """门店菜单项 + 营养表匹配结果（``nutrition=None`` 表示未匹配，即营养未知）。

    营养字段以 property 透出：未知时为 None，:func:`build_candidates` 据此把
    整个组合的 totals 标为未知（绝不按 0 参与合计/过滤）。
    """

    item: "MenuItem"
    nutrition: NutritionItem | None

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def energy_kcal(self) -> float | None:
        return self.nutrition.energy_kcal if self.nutrition else None

    @property
    def protein_g(self) -> float | None:
        return self.nutrition.protein_g if self.nutrition else None

    @property
    def fat_g(self) -> float | None:
        return self.nutrition.fat_g if self.nutrition else None

    @property
    def carb_g(self) -> float | None:
        return self.nutrition.carb_g if self.nutrition else None

    @property
    def sodium_mg(self) -> float | None:
        return self.nutrition.sodium_mg if self.nutrition else None

    @property
    def calcium_mg(self) -> float | None:
        return self.nutrition.calcium_mg if self.nutrition else None


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


def _combo_totals(items: list) -> dict | None:
    """组合的精确营养合计；任一项营养未知（字段为 None）则整体返回 None。"""
    totals: dict[str, float] = {}
    for field in _TOTAL_FIELDS:
        values = [getattr(item, field, None) for item in items]
        if any(value is None for value in values):
            return None
        totals[field] = sum(values)
    return totals


def build_candidates(
    mains: list,
    sides: list,
    drinks: list,
    max_kcal: float,
    min_protein: float,
    top_limit: int,
) -> list[dict]:
    """组合生成（基础/门店两模式共用）：主餐必选 × 小食可不选 × 饮品可不选。

    - 入参是已按品类分桶、已截断的候选（:func:`_candidates` /
      :func:`_store_buckets` 负责分桶截断）；
    - 条目需暴露 :data:`_TOTAL_FIELDS` 营养属性（:class:`NutritionItem` 或
      :class:`MenuEntry`）；营养未知的组合（任一字段 None）不参与热量/蛋白
      过滤，但排在全部已知组合之后（生成序）；
    - 已知组合过滤 ``energy_kcal <= max_kcal`` 且 ``protein_g >= min_protein``，
      排序 ``(protein_g 降序, energy_kcal 升序)``；
    - 返回前 ``top_limit`` 个 ``{"items": [...], "exact": 合计|None}``。
    """
    candidates: list[dict] = []
    for main, side, drink in product(mains, [None, *sides], [None, *drinks]):
        items = [x for x in (main, side, drink) if x is not None]
        totals = _combo_totals(items)
        if totals is not None and (
            totals["energy_kcal"] > max_kcal or totals["protein_g"] < min_protein
        ):
            continue
        candidates.append({"items": items, "exact": totals})

    known = [c for c in candidates if c["exact"] is not None]
    unknown = [c for c in candidates if c["exact"] is None]
    known.sort(key=lambda c: (-c["exact"]["protein_g"], c["exact"]["energy_kcal"]))
    return (known + unknown)[:top_limit]


def suggest_combos(
    table: "NutritionTable",
    max_kcal: float,
    min_protein: float = 0,
    top: int = 3,
) -> list[dict]:
    """在营养表内搜索满足约束的配餐组合（基础模式）。

    组合生成与排序委托 :func:`build_candidates`（与门店模式共用）；营养表
    条目的营养字段恒有值，故此处 ``exact`` 恒非 None（未知标注仅门店模式出现）。

    返回 ``[{items: [NutritionItem...], totals, meets: {kcal, protein}}]``，
    ``totals`` 保留 1 位小数；过滤与排序用精确值，避免舍入翻转边界。
    """
    candidates = _candidates(table)
    combos = build_candidates(
        candidates["main"],
        candidates["side"],
        candidates["drink"],
        max_kcal,
        min_protein,
        top,
    )
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
        for combo in combos
    ]


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
# 门店模式（--store）：真实菜单 + 真实价格
# ---------------------------------------------------------------------------


def _store_buckets(
    menu: "list[MenuItem]", table: "NutritionTable"
) -> dict[str, list[MenuEntry]]:
    """门店菜单 → classify 分桶 + 营养表 join + 每类截断。

    - 同一餐品可属多个分类（parse_meals 按分类条目逐一产出），先按 code
      去重（保留首条），否则重复候选生成重复组合白占名额；
    - 营养未知（表无匹配）的条目排在该类已知条目之后（无法算蛋白/千卡比），
      仅在已知条目不足 :data:`CANDIDATE_LIMIT` 时补位；
    - ``other`` 类（不含品类关键词的餐品/周边，实录菜单中的蘸酱炸鸡、
      分享餐八件套、联名棒球帽等）不参与组合。
    """
    buckets: dict[str, list[MenuEntry]] = {"main": [], "side": [], "drink": []}
    seen: set[str] = set()
    for item in menu:
        if item.code in seen:
            continue
        seen.add(item.code)
        category = classify(item.name)
        if category not in buckets:
            continue
        buckets[category].append(
            MenuEntry(item=item, nutrition=table.match(item.name).item)
        )
    for category, entries in buckets.items():
        known = [e for e in entries if e.nutrition is not None]
        unknown = [e for e in entries if e.nutrition is None]
        known.sort(key=lambda e: _protein_density(e.nutrition), reverse=True)
        buckets[category] = (known + unknown)[:CANDIDATE_LIMIT]
    return buckets


def _menu_sum(candidate: dict) -> float | None:
    """组合的菜单价合计（元，currentPrice 之和）；任一项价格缺失返回 None。

    实付价（calculate-price）≈ 菜单价合计 - 优惠（到店无运费/打包费，实录
    1450330：双层吉士汉堡 ¥23 + 麦辣鸡翅 ¥15 = 算价 ¥38.00，无折扣时相等），
    故菜单价合计可作"预算内"的预筛排序依据；真实是否达标仍以算价结果为准。
    """
    total = 0.0
    for entry in candidate["items"]:
        if entry.item.price is None:
            return None
        total += entry.item.price
    return total


def _price_pool(qualified: list[dict], max_price: float | None) -> list[dict]:
    """算价顺序：``--max-price`` 给定时把"菜单价合计 ≤ 上限"的候选排到前面。

    营养序（蛋白降序）与高蛋白组合偏贵天然冲突——纯营养序会把最贵的多件
    组合排满算价池，预算内的便宜组合永远轮不到算价（实录：700 kcal/25 g/
    ¥40 下前 11 个候选全部超预算）。分层：

    1. 预算内且营养已知：按 ``(蛋白降序, 热量升序)``（预算内营养最优先行）；
    2. 预算内但营养未知：生成序殿后；
    3. 预算外/价格缺失：最后（优惠可能把实付价压进预算，仍有机会被验证）。

    ``max_price`` 为 None 时原序返回（与基础模式一致的纯营养序）。
    """
    if max_price is None:
        return list(qualified)
    sums = [_menu_sum(candidate) for candidate in qualified]
    in_budget_known = [
        c
        for c, s in zip(qualified, sums)
        if c["exact"] is not None and s is not None and s <= max_price
    ]
    in_budget_unknown = [
        c
        for c, s in zip(qualified, sums)
        if c["exact"] is None and s is not None and s <= max_price
    ]
    rest = [c for c, s in zip(qualified, sums) if s is None or s > max_price]
    in_budget_known.sort(
        key=lambda c: (-c["exact"]["protein_g"], c["exact"]["energy_kcal"])
    )
    return in_budget_known + in_budget_unknown + rest


def _price_yuan(raw: object) -> float:
    """calculate-price 返回 → 实付金额（元）。

    金额字段是 ``data.`` + :data:`PRICE_AMOUNT_FIELD`（分），为实录确认（计划
    猜的 payAmount 不存在，见模块 docstring / MCP_INTEGRATION.md）。金额为 2
    位小数的十进制数，整数分除以 100 的浮点结果与字面量一致，比较无精度损失。
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    amount = data.get(PRICE_AMOUNT_FIELD) if isinstance(data, dict) else None
    if not isinstance(amount, (int, float)):
        raise McduError(
            "RESPONSE",
            f"calculate-price 返回缺少金额字段 data.{PRICE_AMOUNT_FIELD}：{raw!r}",
        )
    return round(amount / 100, 2)


def suggest_store(
    client: "McpClient",
    table: "NutritionTable",
    store_code: str,
    mode: str,
    max_kcal: float,
    min_protein: float = 0,
    max_price: float | None = None,
    top: int = 3,
    cache_dir: Path = Path("data"),
    be_code: str | None = None,
) -> list[dict]:
    """门店菜单配餐（``--store``）：真实菜单 + calculate-price 实付价。

    流程：``load_menu``（10 分钟 TTL 缓存）→ classify 分桶 + 营养表 join →
    :func:`build_candidates` 生成营养达标候选（取 :data:`PRICE_SELECT_BREADTH`
    个）→ :func:`_price_pool` 按 ``--max-price`` 预算重排并截到
    :data:`PRICE_POOL_MARGIN` ``* top + 3`` 个 → 逐候选 ``resolve_items``
    定位编码 + ``calculate-price`` 取实付金额 → 过滤 ``price <= max_price``
    → 按候选序返回前 ``top`` 个 ``[{items, totals, price}]``。

    - 营养未知（菜单项在营养表无匹配）的组合 ``totals`` 为 None（绝不按 0），
      排在全部已知组合之后；
    - 算价达标即停：全部合格时只算 ``top`` 次；全部被价格上限滤掉时最多算
      池大小次；
    - ``be_code`` 仅 delivery 等场景必传（随 query-meals/calculate-price 透传）。

    :raises McduError: 菜单不可得 / 算价响应缺金额字段。
    :raises ValueError: mode 不是 pickup/delivery。
    """
    if mode not in MODE_BE_TYPES:
        raise ValueError(
            f"不支持的取餐方式：{mode}（可选 {'/'.join(MODE_BE_TYPES)}）"
        )
    be_type = MODE_BE_TYPES[mode]
    order_type = MODE_ORDER_TYPES[mode]
    menu = load_menu(client, store_code, be_type, order_type, cache_dir, be_code=be_code)

    buckets = _store_buckets(menu, table)
    qualified = build_candidates(
        buckets["main"],
        buckets["side"],
        buckets["drink"],
        max_kcal,
        min_protein,
        PRICE_SELECT_BREADTH,
    )
    pool = _price_pool(qualified, max_price)[: PRICE_POOL_MARGIN * top + 3]

    combos: list[dict] = []
    for candidate in pool:
        if len(combos) >= top:
            break  # 够数即停：控制 calculate-price 调用量
        entries = [(entry.item.name, 1) for entry in candidate["items"]]
        resolved = resolve_items(entries, menu)  # 候选来自菜单本身，恒精确命中
        arguments: dict = {
            "storeCode": store_code,
            "beType": be_type,
            "orderType": order_type,
            "items": [
                {"productCode": r.code, "quantity": r.qty} for r in resolved
            ],
        }
        if be_code is not None:
            arguments["beCode"] = be_code
        price = _price_yuan(client.call_tool(PRICE_TOOL, arguments))
        if max_price is not None and price > max_price:
            continue
        exact = candidate["exact"]
        combos.append(
            {
                "items": [
                    {"code": r.code, "name": r.name, "qty": r.qty, "fuzzy": r.fuzzy}
                    for r in resolved
                ],
                "totals": None
                if exact is None
                else {field: _round1(exact[field]) for field in _TOTAL_FIELDS},
                "price": price,
            }
        )
    return combos


def to_store_payload(
    store_code: str,
    mode: str,
    max_kcal: float,
    min_protein: float,
    max_price: float | None,
    combos: list[dict],
) -> dict:
    """门店模式结果 → JSON payload（combos 已是 JSON 可序列化 dict）。"""
    return {
        "store": store_code,
        "mode": mode,
        "max_kcal": max_kcal,
        "min_protein": min_protein,
        "max_price": max_price,
        "combos": combos,
    }


# ---------------------------------------------------------------------------
# 人读渲染
# ---------------------------------------------------------------------------

#: mode 的人读叫法（与 scripts.order 的同名表一致，仅 3 项不值得共享）
_MODE_LABELS = {"pickup": "到店取餐", "delivery": "麦乐送"}


def render_store(store_code: str, mode: str, combos: list[dict]) -> str:
    """门店模式组合渲染为中文人读文本：标题（门店/取餐方式）+ 每组合三行。

    营养未知（totals=None，菜单项未匹配营养表）的组合明示"营养数据未知"，
    不展示 0 值营养。
    """
    label = _MODE_LABELS.get(mode, mode)
    lines = [f"门店配餐建议 TOP{len(combos)}（{store_code} · {label}）", ""]
    for index, combo in enumerate(combos, start=1):
        totals = combo["totals"]
        price = f"¥{combo['price']:.2f}"
        if totals is None:
            lines.append(f"[{index}] 价格 {price} | 营养数据未知")
        else:
            lines.append(
                f"[{index}] 价格 {price}"
                f" | 蛋白质 {_fmt(totals['protein_g'])} g"
                f" | 热量 {_fmt(totals['energy_kcal'])} kcal"
            )
        lines.append("    " + " + ".join(item["name"] for item in combo["items"]))
        if totals is not None:
            lines.append(
                f"    脂肪 {_fmt(totals['fat_g'])} g | 碳水 {_fmt(totals['carb_g'])} g"
                f" | 钠 {_fmt(totals['sodium_mg'])} mg | 钙 {_fmt(totals['calcium_mg'])} mg"
            )
    return "\n".join(lines)


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
