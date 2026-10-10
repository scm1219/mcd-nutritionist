"""``extras`` 快查命令测试：假 client 回放券/积分/活动日历/一键领券，不触网。

安全门不变量与 order confirm/cancel 同源：
- claim 无 ``--yes-claim`` → 退出码 1 且**绝不**调用 auto-bind-coupons；
- 过门后只调 auto-bind-coupons 一次（参数为空对象）。

响应字段形态（对齐 2026-10-10 只读实录，详见 MCP_INTEGRATION.md）：
- query-my-account → ``data.availablePoint`` 等标量字段；
- query-my-coupons → ``data.coupons[]`` + 分页键（条目字段为空未实录，
  券名按 couponName 探测）；available-coupons → ``data[]``（couponName/
  couponStatus/label）；campaign-calendar → ``data.dailyList[].events[]``，
  标题在 ``articleDto.title``（activityTitle 实测常为空）；
- auto-bind-coupons 是写操作未实录，测试自造最小响应，payload 携带原始
  返回兜底（TODO 真实领券后回填精确字段名）。
"""

import json
from datetime import date as _real_date

import pytest

import cli
from mcdu.mcp_client import McduError
from scripts import extras
from scripts.extras import (
    ACCOUNT_TOOL,
    AUTO_BIND_TOOL,
    AVAILABLE_COUPONS_TOOL,
    CAMPAIGN_CALENDAR_TOOL,
    MY_COUPONS_TOOL,
    extract_campaign_days,
    extract_coupons,
    extract_points,
    render_coupon_list,
)


# ---------------------------------------------------------------------------
# 构造工具：假响应 / 假 client / 固定日期
# ---------------------------------------------------------------------------


#: 我的券：实录形态（data.coupons[] + 分页键；条目字段本意以券名键为主）
_MY_COUPONS_RAW = {
    "success": True,
    "code": 200,
    "data": {
        "coupons": [
            {
                "couponId": "C001",
                "couponCode": "WMB001",
                "couponName": "中薯条买一送一券",
                "tradeDateTime": "2026-10-01 00:00:00 ~ 2026-10-31 23:59:59",
            },
            {
                "couponId": "C002",
                "couponName": "板烧鸡腿堡立减5元券",
                "tradeDateTime": "2026-10-05 00:00:00 ~ 2026-11-05 23:59:59",
            },
        ],
        "totalCount": 2,
        "currentPage": 1,
        "pageSize": 20,
        "totalPages": 1,
    },
}

#: 可领券：data 直接是 list 的形态（实录：couponName/couponStatus/label）
_AVAILABLE_COUPONS_RAW = {
    "success": True,
    "data": [
        {
            "couponName": "早餐麦满分第二份半价券",
            "couponStatus": "CAN_GET",
            "label": "可领取",
        },
    ],
}

#: 账户：实录形态（availablePoint 为可用积分，字符串数字；其余字段原样展示）
_ACCOUNT_RAW = {
    "success": True,
    "data": {
        "availablePoint": "1250",
        "accumulativePoint": "7445.6",
        "memberLevel": "金卡会员",
    },
}

#: 活动日历：实录形态（data.dailyList[]，标题在 articleDto.title）
_CAMPAIGN_RAW = {
    "success": True,
    "data": {
        "dailyList": [
            {"date": "2026-10-09", "dateText": "2026年10月9日 往期回顾", "today": False, "events": []},
            {
                "date": "2026-10-10",
                "dateText": "2026年10月10日 今日",
                "today": True,
                "events": [
                    {
                        "activityCode": "185p",
                        "activityTitle": "",  # 实测常为空
                        "articleDto": {"title": "麦当劳 X PEACEMINUSONE"},
                        "price": "0",
                        "priceSuffix": "起",
                    },
                    {
                        "activityCode": "939d",
                        "activityTitle": "早餐半价周",  # 有直填标题的形态也兼容
                        "price": "26.9",
                        "priceSuffix": "起",
                    },
                ],
            },
        ]
    },
}

#: 自造最小 auto-bind-coupons 响应（真实字段名未实录）
_CLAIM_RAW = {"success": True, "code": 200, "message": "请求成功", "data": {"bindCount": 2}}


class FakeExtrasClient:
    """McpClient 替身：回放五个 extras 工具，记录调用。"""

    _RESPONSES = {
        MY_COUPONS_TOOL: _MY_COUPONS_RAW,
        AVAILABLE_COUPONS_TOOL: _AVAILABLE_COUPONS_RAW,
        ACCOUNT_TOOL: _ACCOUNT_RAW,
        CAMPAIGN_CALENDAR_TOOL: _CAMPAIGN_RAW,
        AUTO_BIND_TOOL: _CLAIM_RAW,
    }

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        try:
            return self._RESPONSES[name]
        except KeyError:
            raise AssertionError(f"意外工具调用：{name}") from None


class EmptyCouponsClient(FakeExtrasClient):
    """券两端都为空的假 client（空态是合法状态，应退出 0）。"""

    _RESPONSES = {
        **FakeExtrasClient._RESPONSES,
        MY_COUPONS_TOOL: {"success": True, "data": {"list": []}},
        AVAILABLE_COUPONS_TOOL: {"success": True, "data": []},
    }


class FailingExtrasClient(FakeExtrasClient):
    """所有工具必失败的假 client（业务错误路径）。"""

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        raise McduError("MCP", "券服务暂不可用")


class _FrozenDate:
    """替身 date 类：today() 固定返回 2026-10-10（extras.date 可 monkeypatch）。"""

    @staticmethod
    def today():
        return _real_date(2026, 10, 10)


def _patch_cli(monkeypatch, fake) -> None:
    monkeypatch.setattr(cli, "McpClient", lambda: fake)


# ---------------------------------------------------------------------------
# 探测式解析 / 渲染单元测试（响应形状未实录，容错是需求）
# ---------------------------------------------------------------------------


def test_extract_coupons_direct_list_and_dict_shapes():
    assert extract_coupons({"data": [{"couponName": "A"}]}) == [{"couponName": "A"}]
    assert extract_coupons({"data": {"coupons": [{"couponName": "B"}]}}) == [
        {"couponName": "B"}
    ]
    assert extract_coupons({"success": True}) == []  # 识别不出给空列表
    assert extract_coupons("junk") == []


def test_extract_points_prefers_available_point():
    """实录键 availablePoint 优先于其它积分键。"""
    raw = {"data": {"availablePoint": 29, "accumulativePoint": 7445.6}}
    assert extract_points(raw) == 29


def test_extract_points_probes_common_keys():
    assert extract_points({"data": {"point": 30}}) == 30
    assert extract_points({"data": {"points": 30}}) == 30
    assert extract_points({"point": 30}) == 30  # 无 data 信封也能兜住
    assert extract_points({"data": {"memberLevel": "金卡"}}) is None
    assert extract_points(None) is None


def test_extract_points_parses_numeric_string():
    """实录形态：积分为字符串数字（"29"），统一转数值；非数字串不算。"""
    assert extract_points({"data": {"availablePoint": "29"}}) == 29
    assert extract_points({"data": {"availablePoint": "7445.6"}}) == 7445.6
    assert extract_points({"data": {"availablePoint": "abc"}}) is None
    assert extract_points({"data": {"availablePoint": "nan"}}) is None  # 字面量不接受
    assert extract_points({"data": {"availablePoint": float("inf")}}) is None


def test_extract_campaign_days_daily_list_and_fallback():
    """实录形态（data.dailyList[]）优先，缺席时回退通用列表探测。"""
    raw = {"data": {"dailyList": [{"date": "2026-10-10", "events": [{"activityTitle": "A"}]}]}}
    assert extract_campaign_days(raw)[0]["events"][0]["activityTitle"] == "A"
    assert extract_campaign_days({"data": {"list": [{"title": "B"}]}}) == [
        {"title": "B"}
    ]
    assert extract_campaign_days("junk") == []


def test_render_coupon_list_unknown_name_fallback():
    """券名键全缺时仍可渲染（券名未知提示 + 其余已知字段照打）。"""
    text = render_coupon_list("我的优惠券", [{"couponId": "X9"}])
    assert "我的优惠券（1 张）" in text
    assert "券名未知" in text
    assert "X9" in text


# ---------------------------------------------------------------------------
# coupons：合并 query-my-coupons + available-coupons
# ---------------------------------------------------------------------------


def test_coupons_calls_both_tools_with_recorded_args(capsys, monkeypatch):
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)

    assert cli.main(["extras", "coupons"]) == 0

    assert fake.calls == [
        (MY_COUPONS_TOOL, {"page": "1", "pageSize": "20"}),
        (AVAILABLE_COUPONS_TOOL, {}),
    ]
    out = capsys.readouterr().out
    assert "我的优惠券（2 张）" in out
    assert "中薯条买一送一券" in out
    assert "板烧鸡腿堡立减5元券" in out
    assert "可领优惠券（1 张）" in out
    assert "早餐麦满分第二份半价券" in out
    assert "C001" in out  # couponId 展示（下单/算价引用）
    assert "可领取" in out  # label 状态展示
    assert "extras claim" in out  # 领券下一步提示


def test_coupons_json_payload(capsys, monkeypatch):
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)

    assert cli.main(["extras", "coupons", "--json"]) == 0

    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["mine"]["count"] == 2
    assert payload["mine"]["coupons"][0]["couponName"] == "中薯条买一送一券"
    assert payload["available"]["count"] == 1
    # 原始返回兜底（解析键为探测性猜测）
    assert payload["raw"][MY_COUPONS_TOOL] == _MY_COUPONS_RAW
    assert payload["raw"][AVAILABLE_COUPONS_TOOL] == _AVAILABLE_COUPONS_RAW
    assert "\\u" not in captured  # 中文不转义


def test_coupons_empty_exits_0_with_friendly_text(capsys, monkeypatch):
    fake = EmptyCouponsClient()
    _patch_cli(monkeypatch, fake)

    assert cli.main(["extras", "coupons"]) == 0

    out = capsys.readouterr().out
    assert "暂无" in out
    assert "我的优惠券" in out
    assert "可领优惠券" in out


def test_coupons_mcp_failure_exits_1(capsys, monkeypatch):
    fake = FailingExtrasClient()
    _patch_cli(monkeypatch, fake)

    with pytest.raises(SystemExit) as exc:
        cli.main(["extras", "coupons"])
    assert exc.value.code == 1
    assert "券服务暂不可用" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# points：query-my-account 透传
# ---------------------------------------------------------------------------


def test_points_calls_query_my_account(capsys, monkeypatch):
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)

    assert cli.main(["extras", "points"]) == 0

    assert fake.calls == [(ACCOUNT_TOOL, {})]
    out = capsys.readouterr().out
    assert "积分" in out
    assert "1250" in out  # 积分数关键字段（availablePoint）
    assert "金卡会员" in out  # 其余账户字段原样可见


def test_points_json_payload(capsys, monkeypatch):
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)

    assert cli.main(["extras", "points", "--json"]) == 0

    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["points"] == 1250
    assert payload["account"] == _ACCOUNT_RAW
    assert "\\u" not in captured


# ---------------------------------------------------------------------------
# campaigns：campaign-calendar + 当日日期
# ---------------------------------------------------------------------------


def test_campaigns_passes_today_as_specified_date(capsys, monkeypatch):
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)
    monkeypatch.setattr(extras, "date", _FrozenDate)

    assert cli.main(["extras", "campaigns"]) == 0

    assert fake.calls == [(CAMPAIGN_CALENDAR_TOOL, {"specifiedDate": "2026-10-10"})]
    out = capsys.readouterr().out
    assert "2026-10-10" in out
    assert "今日" in out  # 今日分组标注
    assert "麦当劳 X PEACEMINUSONE" in out  # articleDto.title 标题
    assert "早餐半价周" in out  # activityTitle 直填标题
    assert "26.9 元起" in out  # 有价活动拼价格


def test_campaigns_json_payload(capsys, monkeypatch):
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)
    monkeypatch.setattr(extras, "date", _FrozenDate)

    assert cli.main(["extras", "campaigns", "--json"]) == 0

    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["date"] == "2026-10-10"
    assert payload["count"] == 2
    today = next(day for day in payload["days"] if day["today"])
    assert len(today["events"]) == 2
    assert payload["calendar"] == _CAMPAIGN_RAW  # 原始返回兜底
    assert "\\u" not in captured


# ---------------------------------------------------------------------------
# claim：安全门（复用 require_yes）→ auto-bind-coupons
# ---------------------------------------------------------------------------


def test_claim_without_yes_blocked(capsys, monkeypatch):
    """无 --yes-claim：打印将执行的摘要，退出码 1，绝不 auto-bind-coupons。"""
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)

    with pytest.raises(SystemExit) as exc:
        cli.main(["extras", "claim"])
    assert exc.value.code == 1

    assert fake.calls == []  # 安全断言：未收到任何工具调用（含 auto-bind）
    captured = capsys.readouterr()
    assert "已阻止" in captured.err
    assert "确认请加 --yes* 旗标" in captured.err
    assert "--yes-claim" in captured.out  # 具体旗标名在 stdout 摘要里
    assert "auto-bind-coupons" in captured.out  # stdout 说明将执行什么


def test_claim_with_yes_calls_auto_bind(capsys, monkeypatch):
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)

    assert cli.main(["extras", "claim", "--yes-claim"]) == 0

    assert fake.calls == [(AUTO_BIND_TOOL, {})]
    out = capsys.readouterr().out
    assert "一键领券" in out
    assert "2" in out  # bindCount 原样可见
    assert "extras coupons" in out  # 下一步提示


def test_claim_json_payload(capsys, monkeypatch):
    fake = FakeExtrasClient()
    _patch_cli(monkeypatch, fake)

    assert cli.main(["extras", "claim", "--yes-claim", "--json"]) == 0

    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["claim"] == _CLAIM_RAW
    assert "\\u" not in captured
