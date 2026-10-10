"""``suggest --store`` 门店模式测试：假 client 回放菜单与 calculate-price，不触网。

calculate-price 响应字段实录（2026-10-10 只读探测，门店 1450330 pickup，
最便宜餐品 521799 日式鲜磨山葵酱 currentPrice ¥3.0）::

    {"success": true, "code": 200, "data": {
        "productOriginalPrice": 300, "productPrice": 300,
        "originalPrice": 300, "discount": 0, "price": 300, ...}}

即实付金额字段是 ``data.price``（计划猜的 payAmount 不存在），单位分
（除以 100 得元）。schema 另有 productPrice/productOriginalPrice 等，均分。
"""

import json
from pathlib import Path

import pytest

import cli
from mcdu.mcp_client import McduError
from mcdu.nutrition import NutritionItem, NutritionTable
from mcdu.orderflow import MEALS_TOOL, MenuItem
from scripts.suggest import (
    PRICE_POOL_MARGIN,
    PRICE_TOOL,
    MenuEntry,
    _price_yuan,
    build_candidates,
    suggest_store,
)

STORE = "1450244"


# ---------------------------------------------------------------------------
# 构造工具：营养条目 / 假表 / 假菜单返回 / 假 client
# ---------------------------------------------------------------------------


def _item(name: str, kcal: float, protein: float) -> NutritionItem:
    """快捷构造 NutritionItem（其余营养列给小值，不影响断言）。"""
    return NutritionItem(
        name=name,
        energy_kj=kcal * 4.184,
        energy_kcal=kcal,
        protein_g=protein,
        fat_g=10.0,
        carb_g=30.0,
        sodium_mg=500.0,
        calcium_mg=100.0,
    )


def _table(*items: NutritionItem) -> NutritionTable:
    """由条目直接构建 NutritionTable（离线，fetched_at 任意合法值）。"""
    return NutritionTable(list(items), "2026-10-10T00:00:00+00:00")


def _meals_raw(*entries: tuple[str, str, float]) -> dict:
    """(code, name, price) 列表 → query-meals 返回形态（单一分类）。

    currentPrice 按实录是字符串（如 "33.5"），这里保持一致。
    """
    meals = [{"code": code, "tags": []} for code, _, _ in entries]
    details = {code: {"name": name, "currentPrice": f"{price:g}"} for code, name, price in entries}
    return {
        "success": True,
        "data": {"categories": [{"name": "套餐", "meals": meals}], "meals": details},
    }


def _price_response(amount_fen: int) -> dict:
    """手工构造的最小合法 calculate-price 响应（字段形态对齐实录）。"""
    return {
        "success": True,
        "code": 200,
        "data": {"price": amount_fen, "originalPrice": amount_fen, "discount": 0},
    }


class FakeStoreClient:
    """McpClient 替身：query-meals 回放菜单，calculate-price 逐个弹出预置金额。

    calculate-price 调用超出预置金额数即断言失败（证明候选数控制没有被突破）。
    """

    def __init__(self, menu_raw: dict, price_amounts_fen: list[int]):
        self.menu_raw = menu_raw
        self._amounts = list(price_amounts_fen)
        self.calls: list[tuple[str, dict | None]] = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if name == MEALS_TOOL:
            return self.menu_raw
        if name == PRICE_TOOL:
            if not self._amounts:
                raise AssertionError("calculate-price 调用超出预置数量（候选数控制被突破）")
            return _price_response(self._amounts.pop(0))
        raise AssertionError(f"意外工具调用：{name}")

    def price_calls(self) -> list[tuple[str, dict | None]]:
        return [(n, a) for n, a in self.calls if n == PRICE_TOOL]


# ---------------------------------------------------------------------------
# calculate-price 响应字段（实录发现的硬断言）
# ---------------------------------------------------------------------------


def test_price_from_response_uses_real_field():
    """实付金额字段是 data.price（分）：1234 分 → 12.34 元（非 payAmount）。"""
    assert _price_yuan(_price_response(1234)) == 12.34
    assert _price_yuan(_price_response(300)) == 3.0
    # 完整实录形态（含冗余字段）也能取值
    full = {
        "success": True,
        "code": 200,
        "data": {
            "productOriginalPrice": 300,
            "productPrice": 300,
            "originalPrice": 300,
            "discount": 0,
            "price": 300,
        },
    }
    assert _price_yuan(full) == 3.0


def test_price_from_response_missing_field_raises():
    for bad in ({}, {"data": {}}, {"data": {"originalPrice": 300}}, "not a dict"):
        with pytest.raises(McduError):
            _price_yuan(bad)


# ---------------------------------------------------------------------------
# suggest_store：三上限过滤 / 调用参数 / 未知营养标注 / 候选数控制
# ---------------------------------------------------------------------------


def test_store_mode_respects_kcal_cap(tmp_path):
    table = _table(_item("双层吉士汉堡", 429, 27), _item("巨无霸", 900, 40), _item("中薯条", 289, 4))
    fake = FakeStoreClient(
        _meals_raw(("M1", "双层吉士汉堡", 25.5), ("M2", "巨无霸", 30.0), ("F1", "中薯条", 13.5)),
        [2550],
    )

    combos = suggest_store(fake, table, STORE, "pickup", max_kcal=500, cache_dir=tmp_path)

    assert len(combos) == 1
    assert [i["name"] for i in combos[0]["items"]] == ["双层吉士汉堡"]
    assert combos[0]["totals"]["energy_kcal"] == 429  # 已知营养给真实合计
    assert all("巨无霸" not in i["name"] for c in combos for i in c["items"])


def test_store_mode_respects_protein_floor(tmp_path):
    table = _table(_item("高蛋白堡", 500, 30), _item("清淡堡", 300, 10))
    fake = FakeStoreClient(
        _meals_raw(("H1", "高蛋白堡", 20.0), ("H2", "清淡堡", 15.0)), [2000]
    )

    combos = suggest_store(
        fake, table, STORE, "pickup", max_kcal=1000, min_protein=25, cache_dir=tmp_path
    )

    assert [i["name"] for c in combos for i in c["items"]] == ["高蛋白堡"]
    assert combos[0]["totals"]["protein_g"] == 30


def test_store_mode_respects_price_cap(tmp_path):
    table = _table(_item("双层吉士汉堡", 429, 27), _item("麦香鱼堡", 330, 17))
    menu = _meals_raw(("M1", "双层吉士汉堡", 45.0), ("M2", "麦香鱼堡", 30.0))

    # 预算内候选（菜单价 ¥30 ≤ 40）先算价且保留；超预算的 ¥45 也被算价验证后滤掉
    fake = FakeStoreClient(menu, [3000, 4500])
    combos = suggest_store(
        fake, table, STORE, "pickup", max_kcal=1000, max_price=40, cache_dir=tmp_path
    )

    assert len(fake.price_calls()) == 2  # 被过滤的候选也算过价
    assert [i["name"] for i in combos[0]["items"]] == ["麦香鱼堡"]
    assert combos[0]["price"] == 30.0

    # 边界：价格恰好等于上限（≤）不算超；两个都预算内时按营养序（蛋白降序）
    fake_boundary = FakeStoreClient(menu, [4500, 3000])
    combos = suggest_store(
        fake_boundary,
        table,
        STORE,
        "pickup",
        max_kcal=1000,
        max_price=45,
        cache_dir=tmp_path,
    )
    assert [c["price"] for c in combos] == [45.0, 30.0]


def test_store_mode_affordable_first_under_price_cap(tmp_path):
    """预算重排：菜单价合计超上限的高蛋白候选让位给预算内的低蛋白候选。

    纯营养序会把最贵的组合排满算价池（实录 700kcal/25g/¥40 下前 11 个候选
    全超预算），预算内的便宜组合永远轮不到算价——故 max_price 给定时菜单价
    合计 ≤ 上限的候选优先（预算内仍按蛋白降序）。
    """
    table = _table(_item("高蛋白贵堡", 500, 40), _item("划算堡", 400, 28))
    menu = _meals_raw(("M1", "高蛋白贵堡", 60.0), ("M2", "划算堡", 25.0))
    fake = FakeStoreClient(menu, [2500, 6000])

    combos = suggest_store(
        fake, table, STORE, "pickup", max_kcal=1000, max_price=40, cache_dir=tmp_path
    )

    # 算价顺序：划算堡（预算内）先于高蛋白贵堡（预算外殿后）
    priced = [call[1]["items"][0]["productCode"] for call in fake.price_calls()]
    assert priced == ["M2", "M1"]
    assert [i["name"] for i in combos[0]["items"]] == ["划算堡"]
    assert combos[0]["price"] == 25.0


def test_store_mode_pricing_call_args(tmp_path):
    """调用参数精确断言：query-meals → calculate-price（items 用菜单编码，qty=1）。"""
    table = _table(
        _item("双层吉士汉堡", 429, 27),
        _item("中薯条", 289, 4),
        _item("可乐", 100, 0),
    )
    fake = FakeStoreClient(
        _meals_raw(("M1", "双层吉士汉堡", 25.5), ("F1", "中薯条", 13.5), ("D1", "可乐", 9.5)),
        [2550, 2600],
    )

    combos = suggest_store(fake, table, STORE, "pickup", max_kcal=700, cache_dir=tmp_path)

    assert fake.calls == [
        (MEALS_TOOL, {"storeCode": STORE, "beType": 1, "orderType": 1}),
        (
            PRICE_TOOL,
            {
                "storeCode": STORE,
                "beType": 1,
                "orderType": 1,
                "items": [{"productCode": "M1", "quantity": 1}],
            },
        ),
        (
            PRICE_TOOL,
            {
                "storeCode": STORE,
                "beType": 1,
                "orderType": 1,
                "items": [
                    {"productCode": "M1", "quantity": 1},
                    {"productCode": "D1", "quantity": 1},
                ],
            },
        ),
    ]
    assert len(combos) == 2
    first = combos[0]
    assert first["items"] == [
        {"code": "M1", "name": "双层吉士汉堡", "qty": 1, "fuzzy": False}
    ]
    assert first["price"] == 25.5
    assert first["totals"]["energy_kcal"] == 429
    # 第二个组合：汉堡 + 可乐（529 kcal ≤ 700，蛋白同为 27，热量更高排后）
    assert [i["code"] for i in combos[1]["items"]] == ["M1", "D1"]


def test_store_mode_be_code_passthrough(tmp_path):
    """delivery 模式：query-meals 与 calculate-price 都必传 beCode（beType=2/orderType=2）。"""
    table = _table(_item("双层吉士汉堡", 429, 27))
    fake = FakeStoreClient(_meals_raw(("M1", "双层吉士汉堡", 25.5)), [2550])

    suggest_store(
        fake,
        table,
        STORE,
        "delivery",
        max_kcal=700,
        cache_dir=tmp_path,
        be_code="145024441",
    )

    meals_args = fake.calls[0][1]
    price_args = fake.calls[1][1]
    assert meals_args == {
        "storeCode": STORE,
        "beType": 2,
        "orderType": 2,
        "beCode": "145024441",
    }
    assert price_args["beCode"] == "145024441"
    assert price_args["beType"] == 2
    assert (tmp_path / f"menu_cache_{STORE}_delivery.json").is_file()


def test_store_mode_unknown_nutrition_labeled_unknown(tmp_path):
    """菜单项在营养表无匹配：组合 totals 标注 None（unknown），绝不按 0 参与合计。"""
    table = _table(_item("双层吉士汉堡", 429, 27))  # 表里没有"神秘新品堡"
    fake = FakeStoreClient(
        _meals_raw(("M1", "双层吉士汉堡", 25.5), ("M2", "神秘新品堡", 22.0)), [2200]
    )

    combos = suggest_store(fake, table, STORE, "pickup", max_kcal=100, cache_dir=tmp_path)

    # 已知组合 429 kcal 全被热量上限滤掉，未知营养组合保留但 totals 为 None
    assert len(combos) == 1
    assert [i["name"] for i in combos[0]["items"]] == ["神秘新品堡"]
    assert combos[0]["totals"] is None
    assert combos[0]["price"] == 22.0


def test_store_mode_price_call_budget(tmp_path):
    """候选数控制：20 个主餐的菜单只对候选池算价（池 = PRICE_POOL_MARGIN*top+3），
    价格全超上限时恰好把池算完，绝不全量算价。"""
    items = [_item(f"高蛋白汉堡{i:02d}", 300, 40 - i) for i in range(20)]
    menu = _meals_raw(*[(f"M{i:02d}", f"高蛋白汉堡{i:02d}", 20.0) for i in range(20)])
    pool = PRICE_POOL_MARGIN * 3 + 3
    fake = FakeStoreClient(menu, [9999] * pool)  # ¥99.99 全部超过 40 元上限

    combos = suggest_store(
        fake, _table(*items), STORE, "pickup", max_kcal=1000, max_price=40, cache_dir=tmp_path
    )

    assert combos == []
    assert len(fake.price_calls()) == pool  # 9 << 20：未全量算价


def test_store_mode_early_stop_pricing(tmp_path):
    """够数即停：价格都合格时只算 top 次 calculate-price。"""
    items = [_item(f"高蛋白汉堡{i}", 300, 40 - i) for i in range(5)]
    menu = _meals_raw(*[(f"M{i}", f"高蛋白汉堡{i}", 20.0) for i in range(5)])
    fake = FakeStoreClient(menu, [1500, 1500, 1500])  # 恰好 3 份：多算一次就断言失败

    combos = suggest_store(
        fake, _table(*items), STORE, "pickup", max_kcal=1000, top=3, cache_dir=tmp_path
    )

    assert len(combos) == 3
    assert len(fake.price_calls()) == 3


def test_store_mode_bad_mode_rejected(tmp_path):
    fake = FakeStoreClient(_meals_raw(("M1", "双层吉士汉堡", 25.5)), [])
    with pytest.raises(ValueError):
        suggest_store(fake, _table(), STORE, "walk", max_kcal=700, cache_dir=tmp_path)


# ---------------------------------------------------------------------------
# build_candidates：两模式共用的组合生成
# ---------------------------------------------------------------------------


def test_build_candidates_ranks_unknown_after_known():
    """已知营养组合按 (蛋白降序, 热量升序)；未知营养组合不参与热量/蛋白过滤，
    排在全部已知组合之后。"""
    known = _item("高蛋白堡", 500, 30)
    unknown = MenuEntry(MenuItem(code="M2", name="神秘新品堡", category="主食", price=22.0), None)
    mains = [known, unknown]

    # 宽松约束：已知在前、未知在后
    ranked = build_candidates(mains, [], [], max_kcal=1000, min_protein=0, top_limit=5)
    assert [c["items"][0].name for c in ranked] == ["高蛋白堡", "神秘新品堡"]
    assert ranked[0]["exact"]["protein_g"] == 30
    assert ranked[1]["exact"] is None

    # 紧约束：已知组合被滤掉，未知组合不受热量上限影响仍保留（totals=None 标注）
    tight = build_candidates(mains, [], [], max_kcal=100, min_protein=0, top_limit=5)
    assert [c["items"][0].name for c in tight] == ["神秘新品堡"]
    assert tight[0]["exact"] is None


# ---------------------------------------------------------------------------
# CLI：--store / --max-price 用法与双通道输出
# ---------------------------------------------------------------------------


def test_cli_max_price_without_store_is_usage_error(capsys):
    """--max-price 仅 --store 模式有效：无 --store 传入即用法错误（退出码 2）。"""
    with pytest.raises(SystemExit) as exc:
        cli.main(["suggest", "--max-kcal", "600", "--max-price", "40"])
    assert exc.value.code == 2
    assert capsys.readouterr().err.strip() != ""


def _patch_cli(monkeypatch, tmp_path, fake, table):
    monkeypatch.setattr(cli, "McpClient", lambda: fake)
    monkeypatch.setattr(cli, "_load_table", lambda: table)
    monkeypatch.setattr(cli, "MENU_CACHE_DIR", tmp_path)


def test_cli_suggest_store_human(capsys, monkeypatch, tmp_path):
    table = _table(_item("双层吉士汉堡", 429, 27))
    fake = FakeStoreClient(_meals_raw(("M1", "双层吉士汉堡", 25.5)), [2550])
    _patch_cli(monkeypatch, tmp_path, fake, table)

    rc = cli.main(
        ["suggest", "--max-kcal", "700", "--min-protein", "25", "--store", STORE, "--max-price", "40"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "门店配餐建议" in out
    assert "双层吉士汉堡" in out
    assert "25.50" in out  # 真实价格（元，两位小数）


def test_cli_suggest_store_json(capsys, monkeypatch, tmp_path):
    table = _table(_item("双层吉士汉堡", 429, 27))
    fake = FakeStoreClient(_meals_raw(("M1", "双层吉士汉堡", 25.5)), [2550, 2550])
    _patch_cli(monkeypatch, tmp_path, fake, table)

    # 第一次（人读）已写菜单缓存：第二次只补 calculate-price
    assert cli.main(["suggest", "--max-kcal", "700", "--store", STORE]) == 0
    capsys.readouterr()
    assert cli.main(
        ["suggest", "--max-kcal", "700", "--store", STORE, "--max-price", "40", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["store"] == STORE
    assert payload["mode"] == "pickup"
    assert payload["max_price"] == 40.0
    assert len(payload["combos"]) == 1
    combo = payload["combos"][0]
    assert combo["items"][0] == {"code": "M1", "name": "双层吉士汉堡", "qty": 1, "fuzzy": False}
    assert combo["totals"]["energy_kcal"] == 429
    assert combo["price"] == 25.5
    assert "\\u" not in json.dumps(payload, ensure_ascii=False)


def test_cli_suggest_store_delivery_be_code(capsys, monkeypatch, tmp_path):
    table = _table(_item("双层吉士汉堡", 429, 27))
    fake = FakeStoreClient(_meals_raw(("M1", "双层吉士汉堡", 25.5)), [2550])
    _patch_cli(monkeypatch, tmp_path, fake, table)

    rc = cli.main(
        ["suggest", "--max-kcal", "700", "--store", STORE, "--mode", "delivery", "--be-code", "B1"]
    )
    assert rc == 0
    assert fake.calls[0][1]["beCode"] == "B1"  # query-meals
    assert fake.calls[1][1]["beCode"] == "B1"  # calculate-price


def test_cli_suggest_store_no_result_business_error(capsys, monkeypatch, tmp_path):
    """价格上限滤掉全部候选：业务错误（退出码 1），提示放宽条件。"""
    table = _table(_item("双层吉士汉堡", 429, 27))
    fake = FakeStoreClient(_meals_raw(("M1", "双层吉士汉堡", 99.99)), [9999])
    _patch_cli(monkeypatch, tmp_path, fake, table)

    with pytest.raises(SystemExit) as exc:
        cli.main(["suggest", "--max-kcal", "700", "--store", STORE, "--max-price", "40"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "配餐组合" in err
    assert "40" in err
