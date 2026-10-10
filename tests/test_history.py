"""history 命令的订单展开、统计与 CLI 行为测试：全部用 fixture 假数据，不触网。

fixture 真值（tests/fixtures/orders_raw.txt + nutrition_raw.txt）：
- 订单 1（较新，2026-04-05 11:15:51）：商品"两份泷情蜜意麦旋风"的子项
  泷情蜜意麦旋风（无小卡）x2 —— 不在 160 项营养表（季节限定），必须显式按
  未知处理并保留原名，绝不按 0 冒充已知；
- 订单 2（较旧，2026-03-15 12:04:07）：商品"麦旋风买一送一"的子项
  奥利奥麦旋风x1（266 kcal/蛋白5/脂肪9/碳水40/钠116/钙185）
  + 草莓麦旋风x1（295/6/9/47/122/186）→ 合计 561 kcal / 11 / 18 / 87 / 238 / 371。
"""

import json
from pathlib import Path

import pytest

import cli
from mcdu.mcp_client import parse_original_response
from scripts.history import flatten_orders, summarize

#: 订单 2（两单合计）的手算统计真值
EXPECTED_ORDER2_TOTALS = {
    "energy_kcal": 561.0,
    "protein_g": 11.0,
    "fat_g": 18.0,
    "carb_g": 87.0,
    "sodium_mg": 238.0,
    "calcium_mg": 371.0,
}


@pytest.fixture(scope="module")
def orders_raw():
    """order-list 工具的完整输出：信封 → content[0].text → Original Response。"""
    fixture = Path(__file__).parent / "fixtures" / "orders_raw.txt"
    envelope = json.loads(fixture.read_text(encoding="utf-8"))
    text = envelope["result"]["content"][0]["text"]
    return parse_original_response(text)


def _single_order_payload(orders_raw, index):
    """把 fixture 中第 index 单包成完整的 order-list 返回结构。"""
    return {"data": {"list": [orders_raw["data"]["list"][index]]}}


def test_flatten_prefers_combo_items(orders_raw):
    payload = _single_order_payload(orders_raw, 1)

    flattened = flatten_orders(payload)

    # 有 comboItemList 就用子项 (name, quantity)，营销套餐名不参与营养计算
    assert flattened == [("奥利奥麦旋风", 1), ("草莓麦旋风", 1)]
    assert "麦旋风买一送一" not in [name for name, _ in flattened]


def test_flatten_falls_back_to_product_name():
    payload = {
        "data": {
            "list": [
                {
                    "orderId": "t1",
                    "orderProductList": [
                        # comboItemList 为空列表：视同无子项
                        {"productName": "双层吉士汉堡", "quantity": 2, "comboItemList": []},
                        # 完全没有 comboItemList 键
                        {"productName": "中薯条", "quantity": 1},
                    ],
                }
            ]
        }
    }

    assert flatten_orders(payload) == [("双层吉士汉堡", 2), ("中薯条", 1)]


def test_summarize_stats(orders_raw, fake_table):
    summary = summarize(orders_raw, fake_table, None)

    # 每单结构：fixture 顺序（新单在前），字段齐全
    orders = summary["orders"]
    assert [o["create_time"] for o in orders] == [
        "2026-04-05 11:15:51",
        "2026-03-15 12:04:07",
    ]
    assert orders[0]["store"] == "麦当劳XX路测试餐厅"
    assert orders[0]["order_id"] == "101015298499627077455173"

    # 订单 2：两个子项都精确匹配，合计用 fixture 真值
    assert orders[1]["totals"] == EXPECTED_ORDER2_TOTALS
    assert orders[1]["unknown"] == []

    # 订单 1：季节限定子项不在表中 → 显式列为未知，绝不按 0 计入合计
    assert orders[0]["unknown"] == ["泷情蜜意麦旋风（无小卡）"]
    assert orders[0]["totals"]["energy_kcal"] == 0.0

    stats = summary["stats"]
    assert stats["meal_count"] == 2
    assert stats["avg_kcal"] == 280.5  # (0 + 561) / 2
    assert stats["max_kcal"] == 561.0
    assert stats["avg_sodium"] == 119.0  # (0 + 238) / 2

    # 供能比来自两单已知项克数合计：蛋白 11g / 脂肪 18g / 碳水 87g
    ratio = stats["macro_ratio"]
    assert ratio["protein"] == pytest.approx(7.9, abs=0.1)
    assert ratio["fat"] == pytest.approx(29.2, abs=0.1)
    assert ratio["carb"] == pytest.approx(62.8, abs=0.1)

    # 最常点：按份数降序（未知项按原名计入），并列保持首次出现顺序
    assert stats["top5"] == [
        ("泷情蜜意麦旋风（无小卡）", 2),
        ("奥利奥麦旋风", 1),
        ("草莓麦旋风", 1),
    ]
    assert stats["unknown_count"] == 2


def test_days_filter(orders_raw, fake_table):
    # 两单相差 21 天，--days 以数据中最新一单（2026-04-05）为基准回看
    assert summarize(orders_raw, fake_table, 21)["stats"]["meal_count"] == 2

    narrowed = summarize(orders_raw, fake_table, 20)
    assert narrowed["stats"]["meal_count"] == 1
    assert narrowed["orders"][0]["create_time"] == "2026-04-05 11:15:51"
    # 只剩全未知的订单 1：统计随范围收敛，未知份数仍显式保留
    assert narrowed["stats"]["max_kcal"] == 0.0
    assert narrowed["stats"]["unknown_count"] == 2
    assert narrowed["stats"]["top5"] == [("泷情蜜意麦旋风（无小卡）", 2)]


# ---------------------------------------------------------------------------
# CLI 行为：假 client + fixture 假表，不触网
# ---------------------------------------------------------------------------


class _FakeClient:
    """McpClient 替身：记录调用并回放预置 payload。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self.payload


def test_cli_history(orders_raw, fake_table, capsys, monkeypatch):
    fake = _FakeClient(orders_raw)
    monkeypatch.setattr(cli, "McpClient", lambda: fake)
    monkeypatch.setattr(cli, "_load_table", lambda: fake_table)

    # 正常 → 0，stdout 出中文人读报告，且调用的是 order-list
    assert cli.main(["history"]) == cli.EXIT_OK == 0
    out = capsys.readouterr().out
    assert fake.calls == [("order-list", {})]
    assert "历史订单营养回顾" in out
    assert "561" in out  # 单笔最高
    assert "未知" in out  # 未知项显式提示
    assert "麦旋风买一送一" not in out  # 营销名不进报告
    assert "每单明细" not in out  # 默认不展开每单

    # --detail 才展开每单明细
    assert cli.main(["history", "--detail"]) == 0
    detail_out = capsys.readouterr().out
    assert "每单明细" in detail_out
    assert "2026-04-05 11:15:51" in detail_out
    assert "泷情蜜意麦旋风（无小卡）" in detail_out

    # --json 可 json.loads、中文不转义
    assert cli.main(["history", "--json"]) == 0
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["stats"]["meal_count"] == 2
    assert payload["stats"]["max_kcal"] == 561.0
    assert payload["orders"][1]["totals"] == EXPECTED_ORDER2_TOTALS
    assert "\\u" not in captured

    # --days 非正整数 → 2（参数错误），stderr 有提示
    with pytest.raises(SystemExit) as exc:
        cli.main(["history", "--days", "0"])
    assert exc.value.code == 2
    assert capsys.readouterr().err.strip() != ""


def test_cli_history_no_orders(fake_table, capsys, monkeypatch):
    monkeypatch.setattr(
        cli, "McpClient", lambda: _FakeClient({"data": {"list": []}})
    )
    monkeypatch.setattr(cli, "_load_table", lambda: fake_table)

    # 空订单是正常状态：退出码 0，人读输出给出明确提示
    assert cli.main(["history"]) == 0
    assert "暂无订单" in capsys.readouterr().out
