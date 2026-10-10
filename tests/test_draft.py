"""``order draft`` 下单草稿测试：假 client 回放菜单/详情/算价/地址，不触网。

安全断言（本任务的核心不变量）：draft 全程**不得**调用 create-order——
FakeDraftClient 收到 create-order 直接 AssertionError，各测试再显式断言
调用工具名集合不含 create-order（等价于精确断言只含预期只读工具）。

响应字段形态对齐实录/工具 schema：
- calculate-price 实付金额 ``data.price``（分），见 tests/test_suggest_store.py；
- query-meal-detail 的 ``data.rounds[].choices[]`` 以 ``isDefault=1`` 标默认组成；
- delivery-query-addresses 返回 ``data.addresses[]``
  （addressId/contactName/fullAddress/phone，渲染不回显 phone）。
"""

import json
import time
from pathlib import Path

import pytest

import cli
from mcdu.mcp_client import McduError
from mcdu.nutrition import NutritionItem, NutritionTable
from mcdu.orderflow import (
    MEALS_TOOL,
    MEAL_DETAIL_TOOL,
    Draft,
    DraftStore,
)
from scripts.order import (
    ADDRESSES_TOOL,
    build_draft,
    build_order_payload,
    build_price_details,
    default_combo_parts,
    parse_addresses,
    price_arguments,
    render_address_choices,
)
from scripts.suggest import PRICE_TOOL

STORE = "1450244"


# ---------------------------------------------------------------------------
# 构造工具：营养条目 / 假表 / 假菜单 / 假详情 / 假算价 / 假地址 / 假 client
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
    """由条目直接构建 NutritionTable（离线）。"""
    return NutritionTable(list(items), "2026-10-10T00:00:00+00:00")


def _meals_raw(*entries: tuple[str, str, float]) -> dict:
    """(code, name, price) 列表 → query-meals 返回形态（单一分类）。"""
    meals = [{"code": code, "tags": []} for code, _, _ in entries]
    details = {code: {"name": name, "currentPrice": f"{price:g}"} for code, name, price in entries}
    return {
        "success": True,
        "data": {"categories": [{"name": "主食", "meals": meals}], "meals": details},
    }


#: 套餐详情的最小合法形态：两轮选配，各含一个 isDefault=1 的默认选项
_COMBO_DETAIL = {
    "success": True,
    "data": {
        "code": "C1",
        "name": "麦辣鸡腿汉堡套餐",
        "rounds": [
            {
                "id": 1,
                "name": "主食",
                "choices": [
                    {"code": "C1A", "name": "麦辣鸡腿汉堡", "isDefault": 1},
                    {"code": "C1B", "name": "双层吉士汉堡", "isDefault": 0},
                ],
            },
            {"id": 2, "name": "饮料", "choices": [{"code": "C1C", "name": "中可乐", "isDefault": 1}]},
        ],
    },
}


def _price_response(amount_fen: int) -> dict:
    """手工构造的最小合法 calculate-price 响应（字段形态对齐实录）。"""
    return {
        "success": True,
        "code": 200,
        "data": {"price": amount_fen, "originalPrice": amount_fen, "discount": 0},
    }


_ADDRESSES_RAW = {
    "success": True,
    "data": {
        "addresses": [
            {
                "addressId": "ADDR1",
                "contactName": "张三",
                "fullAddress": "上海市浦东新区世纪大道100号",
                "phone": "138****0000",
            },
            {
                "addressId": "ADDR2",
                "contactName": "李四",
                "fullAddress": "上海市黄浦区南京东路200号",
                "phone": "139****0000",
            },
        ]
    },
}


class FakeDraftClient:
    """McpClient 替身：按工具名回放预置 payload，记录调用供精确断言。

    收到 create-order 立即 AssertionError——草稿流程绝不真实下单。
    """

    def __init__(self, menu_raw, details=None, price_fen=0, addresses_raw=None):
        self.menu_raw = menu_raw
        self.details = details or {}
        self.price_fen = price_fen
        self.addresses_raw = addresses_raw
        self.calls: list[tuple[str, dict | None]] = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if name == "create-order":
            raise AssertionError("draft 全程不得调用 create-order")
        if name == MEALS_TOOL:
            return self.menu_raw
        if name == MEAL_DETAIL_TOOL:
            code = (arguments or {}).get("code")
            if code not in self.details:
                raise AssertionError(f"未预置餐品详情：{code}")
            return self.details[code]
        if name == PRICE_TOOL:
            return _price_response(self.price_fen)
        if name == ADDRESSES_TOOL:
            return self.addresses_raw
        raise AssertionError(f"意外工具调用：{name}")

    def tool_names(self) -> set[str]:
        return {name for name, _ in self.calls}


def _make_draft(**overrides) -> Draft:
    """构造一个最小合法 Draft（字段可覆盖）。"""
    fields = dict(
        draft_id="abcd1234",
        created_at=time.time(),
        mode="pickup",
        store_code=STORE,
        items=[{"productCode": "M1", "quantity": 1}],
        address_id=None,
        analysis={"totals": {"energy_kcal": 429.0}},
        price={"amount": 25.5},
    )
    fields.update(overrides)
    return Draft(**fields)


# ---------------------------------------------------------------------------
# DraftStore：落盘 / 读取 / TTL 过期删除
# ---------------------------------------------------------------------------


def test_draft_store_roundtrip(tmp_path):
    store = DraftStore(tmp_path)
    draft = _make_draft()

    store.save(draft)

    saved_file = tmp_path / "draft_abcd1234.json"
    assert saved_file.is_file()
    loaded = store.load("abcd1234")
    assert loaded == draft  # dataclass 逐字段相等（analysis/price dict 也相等）


def test_draft_store_persists_be_code_and_tableware(tmp_path):
    """controller ruling：be_code / need_tableware 随草稿持久化并原样读回
    （confirm 只信草稿，这两个字段必须先在 draft 阶段落盘）。"""
    store = DraftStore(tmp_path)
    draft = _make_draft(
        mode="delivery",
        address_id="ADDR7",
        be_code="B1450244",
        need_tableware=False,  # --no-tableware
    )

    store.save(draft)
    loaded = store.load("abcd1234")

    assert loaded.be_code == "B1450244"
    assert loaded.need_tableware is False
    saved = json.loads((tmp_path / "draft_abcd1234.json").read_text(encoding="utf-8"))
    assert saved["be_code"] == "B1450244"  # JSON 持久化里真实存在
    assert saved["need_tableware"] is False


def test_draft_store_defaults_backward_compatible(tmp_path):
    """旧版草稿（无 be_code/need_tableware 键）仍可读：默认 None/True。"""
    legacy = _make_draft()
    legacy.be_code = None
    payload = json.loads(json.dumps(legacy.__dict__, ensure_ascii=False))
    del payload["be_code"]
    del payload["need_tableware"]
    (tmp_path / "draft_abcd1234.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    loaded = DraftStore(tmp_path).load("abcd1234")
    assert loaded is not None
    assert loaded.be_code is None
    assert loaded.need_tableware is True


def test_draft_store_expired_load_returns_none_and_deletes(tmp_path):
    store = DraftStore(tmp_path, ttl_seconds=1800)
    stale = _make_draft(created_at=time.time() - 1801)  # 超过 30 分钟
    store.save(stale)

    assert store.load("abcd1234") is None
    assert not (tmp_path / "draft_abcd1234.json").exists()  # 过期文件被删除


def test_draft_store_fresh_within_ttl(tmp_path):
    store = DraftStore(tmp_path, ttl_seconds=1800)
    edge = _make_draft(created_at=time.time() - 1799)  # TTL 内

    store.save(edge)
    assert store.load("abcd1234") == edge


def test_draft_store_missing_returns_none(tmp_path):
    assert DraftStore(tmp_path).load("nosuchid") is None


def test_draft_store_corrupt_returns_none(tmp_path):
    (tmp_path / "draft_bad1.json").write_text("not json", encoding="utf-8")
    assert DraftStore(tmp_path).load("bad1") is None


# ---------------------------------------------------------------------------
# 纯函数：build_order_payload / price_arguments / default_combo_parts /
# build_price_details / parse_addresses
# ---------------------------------------------------------------------------


def test_build_order_payload_pickup_shape():
    from mcdu.orderflow import ResolvedItem

    resolved = [
        ResolvedItem(name="双层吉士汉堡", code="M1", qty=1, fuzzy=False),
        ResolvedItem(name="中薯条", code="F1", qty=2, fuzzy=True),
    ]

    payload = build_order_payload(resolved, "pickup", STORE)

    assert payload == {
        "storeCode": STORE,
        "beType": 1,
        "orderType": 1,
        "items": [
            {"productCode": "M1", "quantity": 1},
            {"productCode": "F1", "quantity": 2},
        ],
        "needTableware": True,  # 默认 true
    }


def test_build_order_payload_delivery_coupon_tableware():
    from mcdu.orderflow import ResolvedItem

    resolved = [ResolvedItem(name="双层吉士汉堡", code="M1", qty=1, fuzzy=False)]

    payload = build_order_payload(
        resolved,
        "delivery",
        STORE,
        address_id="ADDR1",
        be_code="145024441",
        coupon_id="C1",
        need_tableware=False,  # --no-tableware
    )

    assert payload["addressId"] == "ADDR1"  # 外送附 addressId
    assert payload["beCode"] == "145024441"
    assert payload["needTableware"] is False
    # 优惠券进 items[0].couponId（create-order/calculate-price 的 items 结构）
    assert payload["items"] == [{"productCode": "M1", "quantity": 1, "couponId": "C1"}]


def test_price_arguments_drops_address_and_tableware():
    """calculate-price 参数（实录形状）不含 addressId/needTableware。"""
    payload = {
        "storeCode": STORE,
        "beType": 2,
        "orderType": 2,
        "items": [{"productCode": "M1", "quantity": 1, "couponId": "C1"}],
        "needTableware": True,
        "addressId": "ADDR1",
        "beCode": "B1",
    }

    assert price_arguments(payload) == {
        "storeCode": STORE,
        "beType": 2,
        "orderType": 2,
        "items": [{"productCode": "M1", "quantity": 1, "couponId": "C1"}],
        "beCode": "B1",
    }


def test_default_combo_parts_picks_is_default():
    parts = default_combo_parts(_COMBO_DETAIL)

    assert parts == ["麦辣鸡腿汉堡", "中可乐"]  # 只取 isDefault=1


def test_default_combo_parts_no_rounds():
    assert default_combo_parts({"success": True, "data": {"code": "M1"}}) == []
    assert default_combo_parts({"success": True, "data": {"rounds": []}}) == []


def test_build_price_details_fen_to_yuan():
    details = build_price_details(_price_response(3650))
    assert details == {"amount": 36.5, "original": 36.5, "discount": 0.0}

    discounted = {
        "success": True,
        "code": 200,
        "data": {"price": 2800, "originalPrice": 3800, "discount": 1000},
    }
    assert build_price_details(discounted) == {"amount": 28.0, "original": 38.0, "discount": 10.0}


def test_build_price_details_missing_amount_raises():
    for bad in ({}, {"data": {}}, {"data": {"originalPrice": 300}}, "not a dict"):
        with pytest.raises(McduError):
            build_price_details(bad)


def test_parse_addresses_and_render_choices():
    addresses = parse_addresses(_ADDRESSES_RAW)

    assert [a["addressId"] for a in addresses] == ["ADDR1", "ADDR2"]
    assert all("phone" not in a for a in addresses)  # 不回显手机号
    text = render_address_choices(addresses)
    assert "--address-id" in text
    assert "世纪大道100号" in text
    assert "ADDR1" in text
    # 空地址列表也要给出可行动的提示
    assert "配送地址" in render_address_choices([])


# ---------------------------------------------------------------------------
# build_draft / CLI：内容完整、落盘、安全断言、外送地址门、coupon 透传
# ---------------------------------------------------------------------------


def _patch_cli(monkeypatch, tmp_path, fake, table):
    monkeypatch.setattr(cli, "McpClient", lambda: fake)
    monkeypatch.setattr(cli, "_load_table", lambda: table)
    monkeypatch.setattr(cli, "MENU_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli, "DRAFT_DIR", tmp_path)


MENU = _meals_raw(("M1", "双层吉士汉堡", 23.0), ("F1", "中薯条", 13.5), ("C1", "麦辣鸡腿汉堡套餐", 33.5))
TABLE = _table(_item("双层吉士汉堡", 429, 27), _item("中薯条", 289, 4), _item("麦辣鸡腿汉堡套餐", 650, 28))


def test_cli_draft_pickup_complete(capsys, monkeypatch, tmp_path):
    """pickup 快乐路径：草稿内容完整（品项/营养/价格/下一步）+ 文件落盘 +
    工具名集合精确等于只读集合（绝无 create-order）。"""
    fake = FakeDraftClient(MENU, price_fen=3650)
    _patch_cli(monkeypatch, tmp_path, fake, TABLE)

    assert (
        cli.main(
            ["order", "draft", "--store", STORE, "--mode", "pickup",
             "--items", "双层吉士汉堡x1,中薯条x1"]
        )
        == 0
    )

    # 只调了 query-meals 与 calculate-price；算价参数与 Task 7 实录形状一致
    assert fake.calls == [
        (MEALS_TOOL, {"storeCode": STORE, "beType": 1, "orderType": 1}),
        (
            PRICE_TOOL,
            {
                "storeCode": STORE,
                "beType": 1,
                "orderType": 1,
                "items": [
                    {"productCode": "M1", "quantity": 1},
                    {"productCode": "F1", "quantity": 1},
                ],
            },
        ),
    ]
    assert fake.tool_names() == {MEALS_TOOL, PRICE_TOOL}  # 安全断言：无 create-order

    out = capsys.readouterr().out
    assert "下单草稿" in out
    assert "双层吉士汉堡" in out
    assert "中薯条" in out
    assert "718" in out  # 429 + 289 kcal 营养合计
    assert "36.50" in out  # 实付价（元，两位小数）
    assert "下一步" in out
    assert "--yes-place-order" in out

    # 草稿文件落盘且内容完整
    files = list(Path(tmp_path).glob("draft_*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["store_code"] == STORE
    assert saved["mode"] == "pickup"
    assert saved["items"] == [
        {"productCode": "M1", "quantity": 1},
        {"productCode": "F1", "quantity": 1},
    ]
    assert saved["address_id"] is None
    assert saved["analysis"]["totals"]["energy_kcal"] == 718.0
    assert saved["price"]["amount"] == 36.5
    assert len(saved["draft_id"]) == 8  # uuid4().hex[:8]
    assert saved["created_at"] <= time.time()

    # draft_id 同时出现在人读输出（供 confirm 引用）
    assert saved["draft_id"] in out


def test_cli_draft_json_payload(capsys, monkeypatch, tmp_path):
    fake = FakeDraftClient(MENU, price_fen=3650)
    _patch_cli(monkeypatch, tmp_path, fake, TABLE)

    assert (
        cli.main(
            ["order", "draft", "--store", STORE, "--items", "双层吉士汉堡x1",
             "--no-tableware", "--json"]
        )
        == 0
    )
    captured = capsys.readouterr().out
    payload = json.loads(captured)

    assert payload["store"] == STORE
    assert payload["mode"] == "pickup"
    assert payload["items"] == [{"productCode": "M1", "quantity": 1}]
    assert payload["need_tableware"] is False  # --no-tableware 生效
    # --no-tableware 在 draft 阶段落进草稿本体（confirm 只信草稿）
    saved = json.loads(
        next(Path(tmp_path).glob("draft_*.json")).read_text(encoding="utf-8")
    )
    assert saved["need_tableware"] is False
    assert payload["analysis"]["totals"]["energy_kcal"] == 429.0
    assert payload["price"]["amount"] == 36.5
    assert payload["next"].startswith("python cli.py order confirm --draft-id")
    assert "\\u" not in captured  # 中文不转义
    # 存储的草稿可原样读回（draft_id ↔ 文件 ↔ payload 一致）
    assert DraftStore(tmp_path).load(payload["draft_id"]).items == payload["items"]


def test_cli_draft_combo_uses_meal_detail(capsys, monkeypatch, tmp_path):
    """套餐类商品（名含"套餐"）调 query-meal-detail 取默认组成用于草稿展示。"""
    fake = FakeDraftClient(MENU, details={"C1": _COMBO_DETAIL}, price_fen=3350)
    _patch_cli(monkeypatch, tmp_path, fake, TABLE)

    assert (
        cli.main(["order", "draft", "--store", STORE, "--items", "麦辣鸡腿汉堡套餐x1"]) == 0
    )

    assert fake.calls == [
        (MEALS_TOOL, {"storeCode": STORE, "beType": 1, "orderType": 1}),
        (
            MEAL_DETAIL_TOOL,
            {"storeCode": STORE, "beType": 1, "orderType": 1, "code": "C1"},
        ),
        (
            PRICE_TOOL,
            {
                "storeCode": STORE,
                "beType": 1,
                "orderType": 1,
                "items": [{"productCode": "C1", "quantity": 1}],
            },
        ),
    ]
    out = capsys.readouterr().out
    assert "套餐默认组成" in out
    assert "麦辣鸡腿汉堡" in out
    assert "中可乐" in out
    # v1 默认组合只用于展示：create-order items 仍是 productCode/quantity
    saved = json.loads(
        next(Path(tmp_path).glob("draft_*.json")).read_text(encoding="utf-8")
    )
    assert saved["items"] == [{"productCode": "C1", "quantity": 1}]


def test_cli_draft_coupon_into_price_and_items(capsys, monkeypatch, tmp_path):
    """--coupon 的券 ID 透传进 calculate-price 的 items[0].couponId 并入草稿。"""
    fake = FakeDraftClient(MENU, price_fen=2000)
    _patch_cli(monkeypatch, tmp_path, fake, TABLE)

    assert (
        cli.main(
            ["order", "draft", "--store", STORE, "--items", "双层吉士汉堡x1,中薯条x1",
             "--coupon", "COUPON9"]
        )
        == 0
    )

    price_args = fake.calls[-1][1]
    assert fake.calls[-1][0] == PRICE_TOOL
    assert price_args["items"][0] == {"productCode": "M1", "quantity": 1, "couponId": "COUPON9"}
    assert price_args["items"][1] == {"productCode": "F1", "quantity": 1}  # 只进首项

    saved = json.loads(
        next(Path(tmp_path).glob("draft_*.json")).read_text(encoding="utf-8")
    )
    assert saved["items"][0]["couponId"] == "COUPON9"


def test_cli_draft_delivery_without_address_lists_addresses_exit_1(
    capsys, monkeypatch, tmp_path
):
    """外送未给 --address-id：列 delivery-query-addresses 地址供选择并退出码 1。"""
    fake = FakeDraftClient(MENU, price_fen=3650, addresses_raw=_ADDRESSES_RAW)
    _patch_cli(monkeypatch, tmp_path, fake, TABLE)

    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["order", "draft", "--store", STORE, "--mode", "delivery",
             "--items", "双层吉士汉堡x1"]
        )
    assert exc.value.code == 1

    # 失败即停：只调了地址查询，绝无 create-order
    assert fake.calls == [(ADDRESSES_TOOL, {})]
    assert "create-order" not in fake.tool_names()
    err = capsys.readouterr().err
    assert "世纪大道100号" in err
    assert "ADDR1" in err
    assert "--address-id" in err


def test_cli_draft_delivery_with_address(capsys, monkeypatch, tmp_path):
    """外送给 --address-id：query-meals/calculate-price 透传 beCode，addressId 入草稿。"""
    fake = FakeDraftClient(MENU, price_fen=3650)
    _patch_cli(monkeypatch, tmp_path, fake, TABLE)

    assert (
        cli.main(
            ["order", "draft", "--store", STORE, "--mode", "delivery",
             "--items", "双层吉士汉堡x1", "--address-id", "ADDR7",
             "--be-code", "B1450244"]
        )
        == 0
    )

    meals_args = fake.calls[0][1]
    price_args = fake.calls[1][1]
    assert meals_args["beCode"] == "B1450244"
    assert meals_args["beType"] == 2
    assert price_args["beCode"] == "B1450244"
    assert "addressId" not in price_args  # 算价参数不含 addressId（schema 无此键）
    assert fake.tool_names() == {MEALS_TOOL, PRICE_TOOL}

    saved = json.loads(
        next(Path(tmp_path).glob("draft_*.json")).read_text(encoding="utf-8")
    )
    assert saved["address_id"] == "ADDR7"
    assert saved["be_code"] == "B1450244"  # be_code 在 draft 阶段落盘（controller ruling）
    assert saved["mode"] == "delivery"

    out = capsys.readouterr().out
    assert "麦乐送" in out
    assert "ADDR7" in out


def test_cli_draft_unknown_item_business_error(capsys, monkeypatch, tmp_path):
    fake = FakeDraftClient(MENU, price_fen=3650)
    _patch_cli(monkeypatch, tmp_path, fake, TABLE)

    with pytest.raises(SystemExit) as exc:
        cli.main(["order", "draft", "--store", STORE, "--items", "不存在的汉堡x1"])
    assert exc.value.code == 1
    assert "菜单中找不到" in capsys.readouterr().err
    assert not list(Path(tmp_path).glob("draft_*.json"))  # 未落盘


def test_cli_draft_bad_items_usage_error(capsys, monkeypatch, tmp_path):
    fake = FakeDraftClient(MENU, price_fen=3650)
    _patch_cli(monkeypatch, tmp_path, fake, TABLE)

    with pytest.raises(SystemExit) as exc:
        cli.main(["order", "draft", "--store", STORE, "--items", "巨无霸x0"])
    assert exc.value.code == 2
    assert capsys.readouterr().err.strip() != ""


def test_build_draft_never_calls_create_order(tmp_path):
    """直达 build_draft：安全属性独立于 CLI 层成立（假 client 见 create-order 即炸）。"""
    fake = FakeDraftClient(MENU, price_fen=3650)

    draft, combos, display = build_draft(
        fake, TABLE, STORE, "pickup", [("双层吉士汉堡", 1), ("中薯条", 1)],
        cache_dir=tmp_path,
    )

    assert "create-order" not in fake.tool_names()
    assert draft.store_code == STORE
    assert combos == {}
    assert [d["name"] for d in display] == ["双层吉士汉堡", "中薯条"]
    assert draft.analysis["totals"]["energy_kcal"] == 718.0
    assert draft.price["amount"] == 36.5
