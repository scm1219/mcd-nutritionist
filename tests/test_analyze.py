"""analyze 命令的分析与 CLI 行为测试：全部用 fixture 假表，不触网。

fixture 真值（tests/fixtures/nutrition_raw.txt）：
- 双层吉士汉堡 429 kcal / 蛋白 27 / 脂肪 22 / 碳水 31 / 钠 1017 / 钙 232
- 中薯条       289 kcal / 蛋白 4  / 脂肪 12 / 碳水 38 / 钠 165  / 钙 18
"""

import json

import pytest

import cli
from scripts.analyze import build_analysis, render_human

#: x1+x1 的合计真值
EXPECTED_TOTALS = {
    "energy_kcal": 718.0,
    "protein_g": 31.0,
    "fat_g": 34.0,
    "carb_g": 69.0,
    "sodium_mg": 1182.0,
    "calcium_mg": 250.0,
}


def test_totals_and_ratio(fake_table):
    analysis = build_analysis([("双层吉士汉堡", 1), ("中薯条", 1)], fake_table)

    assert analysis["totals"] == EXPECTED_TOTALS
    # 每项一行：名称、数量、匹配置信度、该项热量（按数量计）
    assert [i["name"] for i in analysis["items"]] == ["双层吉士汉堡", "中薯条"]
    assert [i["qty"] for i in analysis["items"]] == [1, 1]
    assert all(i["confidence"] == "exact" for i in analysis["items"])
    assert [i["energy_kcal"] for i in analysis["items"]] == [429.0, 289.0]

    # 供能比：蛋白 4 / 碳水 4 / 脂肪 9 kcal/g，三项之和 ≈ 100%
    ratio = analysis["ratio"]
    assert ratio["protein"] + ratio["fat"] + ratio["carb"] == pytest.approx(100, abs=0.5)
    assert ratio["fat"] > ratio["carb"] > ratio["protein"]  # 43.3 / 39.1 / 17.6
    assert ratio["protein"] == pytest.approx(17.6, abs=0.1)

    # 每日参考百分比（默认 2000 kcal / 2000 mg 钠）
    assert analysis["daily_pct"]["energy"] == pytest.approx(35.9, abs=0.1)
    assert analysis["daily_pct"]["sodium"] == pytest.approx(59.1, abs=0.1)

    # 未过阈值：无警告
    assert analysis["warnings"] == []
    assert analysis["unknown"] == []


def test_unknown_excluded_but_listed(fake_table):
    analysis = build_analysis(
        [("双层吉士汉堡", 1), ("不存在的餐品", 2)], fake_table
    )

    assert analysis["unknown"] == ["不存在的餐品"]
    assert [i["name"] for i in analysis["items"]] == ["双层吉士汉堡"]
    # 未知项绝不按 0 计入合计：totals 只算已知项
    assert analysis["totals"]["energy_kcal"] == 429.0
    assert analysis["totals"]["sodium_mg"] == 1017.0


def test_warnings_trigger(fake_table):
    # 双层吉士汉堡 x2：858 kcal（>800）且钠 2034 mg（>2000）→ 双警告
    analysis = build_analysis([("双层吉士汉堡", 2)], fake_table)
    assert analysis["totals"]["energy_kcal"] == 858.0
    assert "单餐热量超过 800 kcal" in analysis["warnings"]
    assert "钠摄入超过 2000 mg" in analysis["warnings"]

    # 自定义每日参考不影响单餐警告阈值，只影响 daily_pct
    custom = build_analysis([("双层吉士汉堡", 2)], fake_table, daily_kcal=3000.0)
    assert custom["warnings"] == analysis["warnings"]
    assert custom["daily_pct"]["energy"] == pytest.approx(858.0 / 3000 * 100, abs=0.1)


def test_exit_codes(tmp_path, capsys, monkeypatch, fake_table):
    monkeypatch.setattr(cli, "_load_table", lambda: fake_table)

    # 正常 → 0，stdout 出中文报告
    rc = cli.main(["analyze", "--items", "双层吉士汉堡x1,中薯条x1"])
    assert rc == cli.EXIT_OK == 0
    out = capsys.readouterr().out
    assert "双层吉士汉堡" in out and "中薯条" in out

    # --items 为空 → 2（参数错误）
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze", "--items", ""])
    assert exc.value.code == 2
    assert capsys.readouterr().err.strip() != ""

    # 坏数量 → 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze", "--items", "巨无霸x0"])
    assert exc.value.code == 2
    assert "巨无霸x0" in capsys.readouterr().err

    # 全未知 → 1，且 stderr 提示里列出未知餐品
    # （名字不能含 x/X/×/*，否则 parse_items_arg 先按坏数量报 2）
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze", "--items", "不存在的餐品"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "不存在的餐品" in err


def test_json_output_loadable(capsys, monkeypatch, fake_table):
    monkeypatch.setattr(cli, "_load_table", lambda: fake_table)

    rc = cli.main(
        ["analyze", "--items", "双层吉士汉堡x1,中薯条x1", "--json"]
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # --json 输出可被 json.loads
    assert payload["totals"] == EXPECTED_TOTALS
    assert "\\u" not in captured.out  # 中文不转义
    assert "双层吉士汉堡" in captured.out


def test_render_human_sections(fake_table):
    analysis = build_analysis(
        [("双层吉士汉堡", 2), ("不存在的餐品", 1)], fake_table
    )
    text = render_human(analysis)

    assert "双层吉士汉堡" in text
    assert "合计" in text
    assert "858" in text  # 合计热量
    assert "供能比" in text
    assert "蛋白" in text and "脂肪" in text and "碳水" in text
    assert "%" in text  # 每日参考百分比
    assert "⚠️" in text  # 警告前缀
    assert "单餐热量超过 800 kcal" in text
    assert "不存在的餐品" in text  # 未知项提示
    assert "未计入合计" in text
