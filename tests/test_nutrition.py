"""mcdu.nutrition 的单元测试：解析 fixture 真实数据、缓存回写与名称匹配，不触网。"""

import json
from pathlib import Path

import pytest

from mcdu.mcp_client import parse_original_response
from mcdu.nutrition import (
    NutritionTable,
    load_aliases,
    normalize_name,
    parse_items_arg,
    parse_nutrition_data,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


def _fixture_data() -> str:
    """从 fixture 的完整工具输出中取 list-nutrition-foods 的 data（CSV 文本）。"""
    envelope = json.loads((FIXTURES / "nutrition_raw.txt").read_text(encoding="utf-8"))
    text = envelope["result"]["content"][0]["text"]
    return parse_original_response(text)["data"]


DATA = _fixture_data()
ITEMS = parse_nutrition_data(DATA)


class FakeNutritionClient:
    """假 McpClient：返回 fixture 的 data，记录调用次数。"""

    def __init__(self, data: str = DATA):
        self.data = data
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {"success": True, "code": 200, "message": "请求成功", "data": self.data}


def make_table() -> NutritionTable:
    return NutritionTable(ITEMS, "2026-10-10T00:00:00+00:00")


def test_parse_nutrition_data():
    items = parse_nutrition_data(DATA)
    assert len(items) >= 150  # 头行声称 [160]，fixture 实际 160 行

    by_name = {}
    for item in items:
        by_name.setdefault(item.name, item)  # 重复行（小杯玉米杯等）取首个
    item = by_name["双层吉士汉堡"]
    assert item.energy_kj == 1796
    assert item.energy_kcal == 429
    assert item.protein_g == 27
    assert item.fat_g == 22
    assert item.carb_g == 31
    assert item.sodium_mg == 1017
    assert item.calcium_mg == 232
    assert isinstance(item.energy_kcal, float)

    # 数值列出现 null/空/非数值 → 跳过该行，而不是当 0
    mini = (
        "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,"
        "carbohydrate,sodium,calcium}:\n"
        "测试汉堡,null,100,24,1,2,3,4,5\n"
        "坏行甲,null,,0,0,0,0,0\n"
        "坏行乙,null,1,2,3,4,5\n"
        "坏行丙,null,1,2,3,4,a,5,6\n"
    )
    parsed = parse_nutrition_data(mini)
    assert [p.name for p in parsed] == ["测试汉堡"]
    assert parsed[0].energy_kj == 100 and parsed[0].calcium_mg == 5


def test_normalize_name():
    assert normalize_name("泷情蜜意麦旋风（无小卡）") == "泷情蜜意麦旋风"  # 全角括号成对剥除
    assert normalize_name("纯牛奶（盒装）") == "纯牛奶"
    assert normalize_name("麦辣鸡翅(2块)") == "麦辣鸡翅"  # 半角括号剥除
    assert normalize_name("麦旋风杯(无小卡") == "麦旋风杯"  # 成单开括号：砍到串尾
    assert normalize_name("ＭｃＤｏｎａｌｄｓＣｈｉｃｋｅｎ") == "mcdonaldschicken"  # 全角→半角
    assert normalize_name("ＭｃＣｈｉｃｋｅｎ") == "mcchicken"
    assert normalize_name("  巨无霸 ") == "巨无霸"  # 去首尾空格 + lower 不误伤中文


def test_match_exact_alias():
    table = make_table()
    result = table.match("中薯", {"中薯": "中薯条"})
    assert result.confidence == "exact"
    assert result.item is not None
    assert result.item.name == "中薯条"
    assert result.matched_as == "中薯条"


def test_match_exact_normalized():
    table = make_table()
    result = table.match(" 双层吉士汉堡 ")
    assert result.confidence == "exact"
    assert result.item is not None and result.item.name == "双层吉士汉堡"


def test_match_fuzzy_contains():
    table = make_table()
    result = table.match("奥利奥麦旋风杯")
    assert result.confidence == "fuzzy"
    assert result.item is not None and result.item.name == "奥利奥麦旋风"
    assert result.matched_as == "奥利奥麦旋风"


def test_match_unknown():
    table = make_table()
    result = table.match("不存在的餐品xyz")
    assert result.confidence == "unknown"
    assert result.item is None
    assert result.matched_as == ""


def test_parse_items_arg_variants():
    assert parse_items_arg("双层吉士汉堡x1,中薯条x2") == [
        ("双层吉士汉堡", 1),
        ("中薯条", 2),
    ]
    assert parse_items_arg("巨无霸X2") == [("巨无霸", 2)]
    assert parse_items_arg("巨无霸*3") == [("巨无霸", 3)]
    assert parse_items_arg("巨无霸×2") == [("巨无霸", 2)]
    assert parse_items_arg("2个巨无霸") == [("巨无霸", 2)]
    assert parse_items_arg("2份巨无霸") == [("巨无霸", 2)]
    assert parse_items_arg("巨无霸") == [("巨无霸", 1)]  # 不写数量默认 1
    assert parse_items_arg("2个巨无霸，中薯条x2") == [("巨无霸", 2), ("中薯条", 2)]


def test_parse_items_arg_invalid():
    for bad in ("巨无霸x0", "巨无霸x-1", "巨无霸x2.5", "0个巨无霸", "巨无霸x"):
        with pytest.raises(ValueError, match="无法解析数量"):
            parse_items_arg(bad)


def test_cache_roundtrip(tmp_path):
    cache_path = tmp_path / "nutrition_cache.json"
    client = FakeNutritionClient()

    first = NutritionTable.load(cache_path, client)
    assert len(client.calls) == 1  # 无缓存 → 拉取一次
    assert client.calls[0][0] == "list-nutrition-foods"
    assert len(first.items) == len(ITEMS)
    assert first.stale is False
    assert cache_path.is_file()

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(payload) == {"fetched_at", "items"}
    assert len(payload["items"]) == len(ITEMS)

    second = NutritionTable.load(cache_path, client)
    assert len(client.calls) == 1  # 缓存新鲜 → 不再调用 client
    assert second.stale is False
    assert second.items == first.items


def test_cache_stale_without_client(tmp_path):
    cache_path = tmp_path / "nutrition_cache.json"
    fresh = NutritionTable.load(cache_path, FakeNutritionClient())
    assert fresh.stale is False

    # 缓存过期且无 client：用旧缓存并标记 stale，而不是报错或清空
    stale = NutritionTable.load(cache_path, None, ttl_seconds=0)
    assert stale.stale is True
    assert stale.items == fresh.items


def test_cache_missing_without_client(tmp_path):
    from mcdu.mcp_client import McduError

    with pytest.raises(McduError, match="缓存"):
        NutritionTable.load(tmp_path / "nope.json", None)


def test_load_aliases():
    aliases = load_aliases(REPO_ROOT / "aliases.json")
    assert aliases["中薯"] == "中薯条"
    assert aliases["麦辣鸡"] == "麦辣鸡腿堡"
    # 别名表配套真实数据可用：中薯 exact 命中
    table = make_table()
    result = table.match("中薯", aliases)
    assert result.confidence == "exact" and result.item.name == "中薯条"
