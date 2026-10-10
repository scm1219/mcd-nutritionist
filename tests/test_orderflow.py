"""orderflow 层与 ``order stores/menu`` 命令测试：fixture 真值 + 假 client，不触网。

fixture 真值（2026-10-10 实录；1450330 已停业，菜单取自 stores 结果中的
1450244 麦当劳上海延安西路地铁站餐厅）：
- stores.json：query-nearby-stores（beType=1/searchType=1/city=上海/keyword=陆家嘴）
  返回 2 家店；data[0] = 1450244 / 麦当劳上海延安西路地铁站餐厅 /
  上海轨道交通三号线延安西路站3-108；beType=1 到店自取门店无 beCode；
- meals.json：query-meals（beType=1/orderType=1/storeCode=1450244）返回
  15 个分类、136 个分类条目、124 个唯一编码；data.meals[code] 详情映射中
  9900005456 = 麦辣鸡腿汉堡三件套（currentPrice "33.5"，分类"鸡肉汉堡/卷"），
  4810 = 薯条（"13.5"，分类"小食甜品/其他"，tags ["小食"]）。
"""

import json
import time
from pathlib import Path

import pytest

import cli
from mcdu.orderflow import (
    MEALS_TOOL,
    MEAL_DETAIL_TOOL,
    STORES_TOOL,
    ResolvedItem,
    load_menu,
    meal_detail,
    parse_meals,
    parse_stores,
    resolve_items,
)

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeClient:
    """McpClient 替身：记录调用并回放预置 payload。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self.payload


@pytest.fixture(scope="module")
def stores_raw():
    """query-nearby-stores 的完整真实返回。"""
    return json.loads((FIXTURES / "stores.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def meals_raw():
    """query-meals 的完整真实返回。"""
    return json.loads((FIXTURES / "meals.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def menu(meals_raw):
    """由 fixture 解析出的门店菜单。"""
    return parse_meals(meals_raw)


# ---------------------------------------------------------------------------
# 解析器：断言必须贴合录制的真实字段
# ---------------------------------------------------------------------------


def test_parse_stores_keeps_real_fields(stores_raw):
    stores = parse_stores(stores_raw)

    assert len(stores) == 2
    first = stores[0]
    assert first["storeCode"] == "1450244"
    assert first["storeName"] == "麦当劳上海延安西路地铁站餐厅"
    assert first["address"] == "上海轨道交通三号线延安西路站3-108"
    # beType=1（到店自取）门店没有 beCode：不造空字段
    assert "beCode" not in first


def test_parse_meals_real_items(meals_raw):
    items = parse_meals(meals_raw)

    # 136 个分类条目 / 124 个唯一编码（同一餐品可属多个分类）
    assert len(items) == 136
    assert len({item.code for item in items}) == 124

    by_code = {item.code: item for item in items}
    combo = by_code["9900005456"]
    assert combo.name == "麦辣鸡腿汉堡三件套"
    assert combo.price == 33.5  # "33.5" 字符串 → float
    assert combo.category == "鸡肉汉堡\n/卷"
    fries = by_code["4810"]
    assert (fries.name, fries.price, fries.tags) == ("薯条", 13.5, ["小食"])
    # 本店菜单全部有价且可解析为 float
    assert all(isinstance(item.price, float) for item in items)
    assert all(item.name and item.code and item.category for item in items)


# ---------------------------------------------------------------------------
# load_menu：TTL 缓存
# ---------------------------------------------------------------------------


def test_load_menu_cache_hit_no_call(meals_raw, tmp_path):
    # 预写一份新鲜缓存：TTL 内不得再调 MCP
    fresh = tmp_path / "menu_cache_1450244_pickup.json"
    fresh.write_text(
        json.dumps({"fetched_at": time.time(), "raw": meals_raw}, ensure_ascii=False),
        encoding="utf-8",
    )
    fake = _FakeClient(meals_raw)

    items = load_menu(fake, "1450244", 1, 1, tmp_path)

    assert fake.calls == []
    assert len(items) == 136


def test_load_menu_fetch_writes_cache_then_hits(meals_raw, tmp_path):
    fake = _FakeClient(meals_raw)

    items = load_menu(fake, "1450244", 1, 1, tmp_path)

    assert fake.calls == [
        (MEALS_TOOL, {"storeCode": "1450244", "beType": 1, "orderType": 1})
    ]
    cache = tmp_path / "menu_cache_1450244_pickup.json"
    assert cache.is_file()
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert time.time() - saved["fetched_at"] < 60  # 刚写入
    assert saved["raw"]["success"] is True

    # TTL 内二次调用走缓存：不二次调用 MCP
    again = load_menu(fake, "1450244", 1, 1, tmp_path)
    assert len(fake.calls) == 1
    assert again == items


def test_load_menu_expired_refetches(meals_raw, tmp_path):
    stale = tmp_path / "menu_cache_1450244_pickup.json"
    stale.write_text(
        json.dumps(
            {"fetched_at": time.time() - 601, "raw": meals_raw}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    fake = _FakeClient(meals_raw)

    items = load_menu(fake, "1450244", 1, 1, tmp_path)

    assert len(fake.calls) == 1  # 超过 10 分钟即重取
    assert len(items) == 136
    saved = json.loads(stale.read_text(encoding="utf-8"))
    assert time.time() - saved["fetched_at"] < 60  # 且已覆写为新鲜缓存


def test_load_menu_delivery_mode_uses_own_cache_file(meals_raw, tmp_path):
    fake = _FakeClient(meals_raw)

    load_menu(fake, "1450244", 2, 2, tmp_path)

    assert fake.calls == [
        (MEALS_TOOL, {"storeCode": "1450244", "beType": 2, "orderType": 2})
    ]
    assert (tmp_path / "menu_cache_1450244_delivery.json").is_file()
    assert not (tmp_path / "menu_cache_1450244_pickup.json").exists()


def test_load_menu_corrupt_cache_refetches(meals_raw, tmp_path):
    (tmp_path / "menu_cache_1450244_pickup.json").write_text("not json", encoding="utf-8")
    fake = _FakeClient(meals_raw)

    items = load_menu(fake, "1450244", 1, 1, tmp_path)

    assert len(fake.calls) == 1  # 坏缓存不致命：静默重取
    assert len(items) == 136


# ---------------------------------------------------------------------------
# resolve_items：纯名称匹配定位 code，不触网
# ---------------------------------------------------------------------------


def test_resolve_items_exact_match(menu):
    resolved = resolve_items([("麦辣鸡腿汉堡三件套", 2)], menu)

    assert resolved == [
        ResolvedItem(name="麦辣鸡腿汉堡三件套", code="9900005456", qty=2, fuzzy=False)
    ]


def test_resolve_items_containment_fuzzy(menu):
    # "中薯条"不在菜单，但菜单名"薯条"是其子串 → fuzzy 命中
    resolved = resolve_items([("中薯条", 1)], menu)

    assert resolved[0].name == "薯条"
    assert resolved[0].code == "4810"
    assert resolved[0].fuzzy is True


def test_resolve_items_multiple_entries_keep_order(menu):
    resolved = resolve_items(
        [("麦辣鸡腿汉堡三件套", 1), ("中薯条", 2), ("薯条", 1)], menu
    )

    assert [r.qty for r in resolved] == [1, 2, 1]
    assert [r.fuzzy for r in resolved] == [False, True, False]


def test_resolve_items_not_found_lists_candidates(menu):
    with pytest.raises(ValueError) as exc:
        resolve_items([("不存在的汉堡", 1)], menu)

    message = str(exc.value)
    assert "菜单中找不到: 不存在的汉堡" in message
    # 相近候选要列出真实餐品名（含"汉堡"的选项）
    assert "汉堡" in message.replace("菜单中找不到: 不存在的汉堡", "")


def test_resolve_items_no_candidates_still_clear(menu):
    with pytest.raises(ValueError) as exc:
        resolve_items([("zzz", 1)], menu)

    assert "菜单中找不到: zzz" in str(exc.value)


# ---------------------------------------------------------------------------
# meal_detail：Task 8 用的薄封装
# ---------------------------------------------------------------------------


def test_meal_detail_passthrough():
    payload = {"success": True, "data": {"code": "9900005456", "rounds": []}}
    fake = _FakeClient(payload)

    detail = meal_detail(fake, "1450244", 1, 1, "9900005456")

    assert detail == payload
    assert fake.calls == [
        (
            MEAL_DETAIL_TOOL,
            {
                "storeCode": "1450244",
                "beType": 1,
                "orderType": 1,
                "code": "9900005456",
            },
        )
    ]


# ---------------------------------------------------------------------------
# CLI：order stores / order menu
# ---------------------------------------------------------------------------


def test_cli_order_stores(stores_raw, capsys, monkeypatch):
    fake = _FakeClient(stores_raw)
    monkeypatch.setattr(cli, "McpClient", lambda: fake)

    assert cli.main(["order", "stores", "--city", "上海", "--keyword", "陆家嘴"]) == 0
    assert fake.calls == [
        (
            STORES_TOOL,
            {"beType": 1, "searchType": 1, "city": "上海", "keyword": "陆家嘴"},
        )
    ]
    out = capsys.readouterr().out
    assert "麦当劳上海延安西路地铁站餐厅" in out
    assert "1450244" in out

    # --json：可解析、count 正确、中文不转义
    assert cli.main(
        ["order", "stores", "--city", "上海", "--keyword", "陆家嘴", "--json"]
    ) == 0
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["count"] == 2
    assert payload["stores"][0]["storeCode"] == "1450244"
    assert "\\u" not in captured


def test_cli_order_menu_pickup_with_cache(meals_raw, capsys, monkeypatch, tmp_path):
    fake = _FakeClient(meals_raw)
    monkeypatch.setattr(cli, "McpClient", lambda: fake)
    monkeypatch.setattr(cli, "MENU_CACHE_DIR", tmp_path)

    assert cli.main(["order", "menu", "--store", "1450244", "--mode", "pickup"]) == 0
    assert fake.calls == [
        (MEALS_TOOL, {"storeCode": "1450244", "beType": 1, "orderType": 1})
    ]
    out = capsys.readouterr().out
    assert "1450244" in out
    assert "麦辣鸡腿汉堡三件套" in out

    # 缓存生效：重复查询不再触网
    assert cli.main(["order", "menu", "--store", "1450244", "--mode", "pickup"]) == 0
    assert len(fake.calls) == 1
    capsys.readouterr()

    # --json：count/mode/items 齐全
    assert cli.main(
        ["order", "menu", "--store", "1450244", "--mode", "pickup", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["store"] == "1450244"
    assert payload["mode"] == "pickup"
    assert payload["count"] == 136
    assert payload["items"][0]["code"]


def test_cli_order_menu_bad_mode_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["order", "menu", "--store", "1450244", "--mode", "walk"])

    assert exc.value.code == 2
    assert capsys.readouterr().err.strip() != ""


def test_cli_order_stores_business_error(monkeypatch, capsys):
    from mcdu.mcp_client import McduError

    def boom():
        raise McduError(500, "门店可能已关闭或不在营业时间")

    monkeypatch.setattr(cli, "McpClient", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main(["order", "stores", "--city", "上海", "--keyword", "x"])
    assert exc.value.code == 1
    assert "门店可能已关闭" in capsys.readouterr().err
