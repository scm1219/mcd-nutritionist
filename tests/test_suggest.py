"""suggest 命令的品类分类、组合搜索与 CLI 行为测试：全部用假表，不触网。

组合语义：主餐必选，小食/饮品可不选（空位=不选）。由全局约束
"max-kcal 50 无组合" 反推：饮品候选含 13 kcal 的鲜萃咖啡/浓缩咖啡（蛋白/
千卡比排进前 12），若主餐也允许空位则 50 kcal 仍可只点咖啡成单；而候选
主餐热量最低 246 kcal（原味板烧鸡腿麦满分），主餐必选时 50 kcal 必然空。

fixture 真值（tests/fixtures/nutrition_raw.txt）：
- 双层吉士汉堡 429 kcal / 蛋白 27（classify → main）
- 中薯条       289 kcal / 蛋白 4  （classify → side）
- 圆筒冰淇淋   93 kcal / 蛋白 2  （classify → other，不含任何规则关键词）
"""

import json

import pytest

import cli
from mcdu.nutrition import NutritionItem, NutritionTable
from scripts.suggest import classify, render_human, suggest_combos


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


def test_classify():
    assert classify("双层吉士汉堡") == "main"
    assert classify("猪柳蛋麦满分") == "main"
    assert classify("中薯条") == "side"
    assert classify("香芋派") == "side"
    assert classify("大杯鲜萃咖啡") == "drink"
    assert classify("锡兰红茶") == "drink"
    assert classify("圆筒冰淇淋") == "other"
    # 规则按序生效：主餐关键词优先于饮品关键词
    assert classify("麦满分配奶茶") == "main"


def test_classify_drink_juice_regression():
    """真实数据回归（评审授权的关键词表偏离）：裸"汁"子串会把蜜汁/脆汁鸡
    类餐食误判成饮品，曾占满饮品候选前 12；修正为词尾锚定"汁" + 整词
    "美汁源"后，7 项餐食落 other，2 个真果汁仍是 drink。"""
    for name in (
        "麦麦脆汁鸡-琵琶腿",
        "麦麦脆汁鸡-带骨里脊",
        "蜜汁BBQ风味薄皮脆汁鸡-带骨里脊",
        "蜜汁BBQ风味薄皮脆汁鸡-琵琶腿",
        "蒜了吧黄油风味薄皮脆汁鸡-琵琶腿",
        "蒜了吧黄油风味薄皮脆汁鸡-带骨里脊",
        "蜜汁快乐",
    ):
        assert classify(name) == "other", name
    # 真果汁仍是饮品："100%苹果汁"以汁结尾；美汁源不以汁结尾，靠整词命中
    assert classify("100%苹果汁") == "drink"
    assert classify("【美汁源】“黄金橙橙”") == "drink"


def test_drink_candidates_exclude_food(fake_table):
    """由 fixture 全表验证：饮品类候选不再含蜜汁/脆汁鸡类餐食。"""
    drinks = [i.name for i in fake_table.items if classify(i.name) == "drink"]
    assert drinks  # 真饮品仍在（可乐/咖啡/牛奶/茶…）
    for name in drinks:
        assert "脆汁鸡" not in name, name
        assert not name.startswith("蜜汁"), name
    # 曾被误判的 7 项现在全部不在饮品类
    assert "蜜汁快乐" not in drinks
    assert "麦麦脆汁鸡-琵琶腿" not in drinks
    # 两个真果汁仍在饮品类
    assert "100%苹果汁" in drinks
    assert "【美汁源】“黄金橙橙”" in drinks


def test_combo_respects_kcal_cap(fake_table):
    combos = suggest_combos(fake_table, max_kcal=600, top=10)

    assert combos  # 600 kcal 内必有组合（候选主餐最低 246 kcal）
    for combo in combos:
        assert combo["totals"]["energy_kcal"] <= 600
        assert combo["meets"] == {"kcal": True, "protein": True}
        assert combo["items"]  # 至少一个餐品
    # 源数据有重复行（如小杯玉米杯）：候选先去重，重复组合不得占 top-N 名额
    seen = [tuple(i.name for i in c["items"]) for c in combos]
    assert len(set(seen)) == len(seen)


def test_combo_respects_protein_floor(fake_table):
    combos = suggest_combos(fake_table, max_kcal=800, min_protein=25)

    assert len(combos) == 3  # 默认 top=3
    for combo in combos:
        assert combo["totals"]["protein_g"] >= 25
        assert combo["meets"]["protein"] is True


def test_combo_ranking():
    table = _table(
        _item("汉堡一", 500, 30),
        _item("汉堡二", 300, 25),
        _item("薯条一", 50, 5),
        _item("薯条二", 200, 8),
        _item("咖啡一", 20, 2),
        _item("咖啡二", 10, 0),
    )

    combos = suggest_combos(table, max_kcal=1000, top=4)

    # 首位蛋白最大：汉堡一+薯条二+咖啡一 = 40g / 720 kcal
    assert combos[0]["totals"]["protein_g"] == 40
    proteins = [c["totals"]["protein_g"] for c in combos]
    assert proteins == sorted(proteins, reverse=True)
    # 同蛋白热量低者前：38g 的两个组合，700 kcal 排在 710 kcal 前
    assert combos[1]["totals"]["protein_g"] == 38
    assert [i.name for i in combos[1]["items"]] == ["汉堡一", "薯条二"]
    assert combos[1]["totals"]["energy_kcal"] == 700
    assert [i.name for i in combos[2]["items"]] == ["汉堡一", "薯条二", "咖啡二"]
    assert combos[2]["totals"]["energy_kcal"] == 710


def test_no_result_error(capsys, monkeypatch, fake_table):
    # 候选主餐最低 246 kcal：50 kcal 连主餐都放不下 → 空结果
    assert suggest_combos(fake_table, max_kcal=50) == []

    monkeypatch.setattr(cli, "_load_table", lambda: fake_table)
    with pytest.raises(SystemExit) as exc:
        cli.main(["suggest", "--max-kcal", "50"])
    assert exc.value.code == 1  # 空结果是业务错误，不是参数错误
    assert capsys.readouterr().err.strip() != ""


def test_cli_suggest_human(capsys, monkeypatch, fake_table):
    monkeypatch.setattr(cli, "_load_table", lambda: fake_table)

    rc = cli.main(["suggest", "--max-kcal", "600", "--min-protein", "25"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "配餐建议" in out
    assert "[1]" in out and "[2]" in out and "[3]" in out  # 默认 top=3
    assert "+" in out  # 组合成员以 + 连接
    assert "堡" in out  # 主餐必选，高蛋白组合必含主餐


def test_cli_suggest_json(capsys, monkeypatch, fake_table):
    monkeypatch.setattr(cli, "_load_table", lambda: fake_table)

    rc = cli.main(["suggest", "--max-kcal", "600", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["max_kcal"] == 600.0
    assert len(payload["combos"]) == 3
    first = payload["combos"][0]
    assert first["items"][0]["name"]  # NutritionItem 已转 JSON 可序列化 dict
    assert first["totals"]["energy_kcal"] <= 600
    assert first["meets"] == {"kcal": True, "protein": True}
    assert "\\u" not in captured.out  # 中文不转义


def test_render_human(fake_table):
    combos = suggest_combos(fake_table, max_kcal=600, min_protein=25, top=3)
    text = render_human(combos)

    assert "配餐建议" in text
    assert "[1]" in text and "[3]" in text
    assert "热量" in text and "蛋白质" in text
    assert "钠" in text  # 营养明细行
