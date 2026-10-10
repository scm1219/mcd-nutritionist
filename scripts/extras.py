"""extras 子命令组：快查类命令（券/积分/活动日历）与一键领券。

- ``coupons``：合并 ``query-my-coupons``（page="1"/pageSize="20"）与
  ``available-coupons``（无参），人读两段合并展示；
- ``points``：``query-my-account``（无参）透传；
- ``campaigns``：``campaign-calendar``（specifiedDate=当日 ``YYYY-MM-DD``，
  日期取值独立成 :func:`today_iso` 便于测试 monkeypatch）；
- ``claim``：``auto-bind-coupons``（无参，**写操作**）——必须先过 CLI 层的
  ``--yes-claim`` 安全门（复用 :func:`scripts.order.require_yes`），
  本模块的 :func:`claim_coupons` 只在过门后被调用。

响应字段形态（2026-10-10 只读实录，详见 MCP_INTEGRATION.md）：
- ``query-my-account`` → ``data`` 标量字段集（值多为字符串数字），可用积分
  为 ``availablePoint``（另有 accumulativePoint/usedPoint/expiredPoint 等）；
- ``available-coupons`` → ``data[]``，券名 ``couponName``，状态
  ``couponStatus``/``label``；``query-my-coupons`` → ``data.coupons[]``
  （分页键 totalCount/pageSize，条目字段本次为空列表未实录）；
- ``campaign-calendar`` → ``data.dailyList[]``（date/dateText/today）每天
  ``events[]``，``activityTitle`` 实测常为空，真实标题在
  ``articleDto.title``，``price``+``priceSuffix`` 为"0 起"类营销价。
解析仍保留通用探测回退（防形状漂移），识别不出时人读文本提示看 --json，
payload 始终携带原始返回兜底；auto-bind-coupons 是写操作未实录（TODO
真实领券后回填精确字段名）。

本模块不做网络 I/O 的构造（client 由调用方注入），便于测试。
"""

from __future__ import annotations

import math
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注用
    from mcdu.mcp_client import McpClient

#: 我的优惠券查询工具（分页参数为实录固定值：字符串页码）
MY_COUPONS_TOOL = "query-my-coupons"
#: query-my-coupons 的固定参数
MY_COUPONS_ARGUMENTS = {"page": "1", "pageSize": "20"}
#: 可领优惠券查询工具（无参）
AVAILABLE_COUPONS_TOOL = "available-coupons"
#: 账户（积分）查询工具（无参）
ACCOUNT_TOOL = "query-my-account"
#: 活动日历查询工具（specifiedDate=YYYY-MM-DD）
CAMPAIGN_CALENDAR_TOOL = "campaign-calendar"
#: 一键领券工具（写操作：只在过了 --yes-claim 安全门后调用）
AUTO_BIND_TOOL = "auto-bind-coupons"

#: 响应统一信封里的业务数据键
_DATA_KEY = "data"

#: 券/活动条目列表可能落的键（按探测顺序）
_LIST_KEYS = (
    "list",
    "coupons",
    "couponList",
    "campaigns",
    "activities",
    "records",
    "items",
)

#: 券条目的名称键（按探测顺序，取第一个命中；available-coupons 实录为 couponName）
_COUPON_NAME_KEYS = ("couponName", "title", "name")
#: 券条目的补充展示键（存在才打印，值原样；couponImage 之类不展示）
_COUPON_DETAIL_KEYS = (
    "couponId",
    "couponCode",
    "amount",
    "discount",
    "minCost",
    "tradeDateTime",
    "startDate",
    "endDate",
    "couponStatus",
    "label",
)
#: 积分数值键（按探测顺序；availablePoint 为实录字段且值为字符串数字，
#: 不含 balance——那是余额不是积分）
_POINT_KEYS = ("availablePoint", "point", "points", "integral", "memberPoint", "score")
#: 活动条目的名称键（按探测顺序；activityTitle 实录常为空，靠 articleDto 兜底）
_CAMPAIGN_NAME_KEYS = ("activityTitle", "campaignName", "activityName", "title", "name")


# ---------------------------------------------------------------------------
# 探测式解析（响应形状未实录，容错优先）
# ---------------------------------------------------------------------------


def _data_of(raw: object) -> object:
    """响应 → ``data`` 部分；不是 dict 或无 data 键时返回 None。"""
    if isinstance(raw, dict):
        return raw.get(_DATA_KEY)
    return None


def _dict_list(node: object) -> list[dict]:
    """探测节点里的对象列表：本体是 list 直接用；是 dict 则按 _LIST_KEYS 找。"""
    if isinstance(node, list):
        return [entry for entry in node if isinstance(entry, dict)]
    if isinstance(node, dict):
        for key in _LIST_KEYS:
            value = node.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def extract_coupons(raw: object) -> list[dict]:
    """query-my-coupons / available-coupons 返回 → 券条目列表（探测式）。"""
    return _dict_list(_data_of(raw))


def _point_number(value: object) -> int | float | None:
    """值 → 数值：bool 不算，数字原样，数字字符串解析（实录为 "29" 形态）；
    nan/inf 字面量不接受。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and value.strip():
        try:
            number = float(value)
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else number
    return None


def _find_point(raw: object) -> tuple[str, int | float] | None:
    """探测积分数值：返回 ``(命中的键, 数值)``（跳过展示用），未命中返回 None。"""
    nodes: list[dict] = []
    if isinstance(raw, dict):
        nodes.append(raw)
    data = _data_of(raw)
    if isinstance(data, dict):
        nodes.append(data)
    for node in nodes:
        for key in _POINT_KEYS:
            value = _point_number(node.get(key))
            if value is not None:
                return key, value
    return None


def extract_points(raw: object) -> int | float | None:
    """query-my-account 返回 → 积分数值（字符串数字统一转数值）；识别不出返回 None。"""
    found = _find_point(raw)
    return found[1] if found else None


def extract_campaign_days(raw: object) -> list[dict]:
    """campaign-calendar 返回 → 按天的活动条目列表。

    实录形状：``data.dailyList[]``，每天 ``{date, dateText, today, events[]}``
    （响应含昨日"往期回顾"与明日）；dailyList 缺席时回退通用列表探测。
    """
    data = _data_of(raw)
    if isinstance(data, dict) and isinstance(data.get("dailyList"), list):
        return [day for day in data["dailyList"] if isinstance(day, dict)]
    return _dict_list(data)


def _day_events(day: dict) -> list[dict]:
    """天条目 → ``events[]``（过滤非 dict 成员）。"""
    events = day.get("events")
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


def _campaign_title(event: dict) -> str:
    """活动标题：activityTitle 实测常为空，回退 ``articleDto.title``。"""
    title = _probe_str(event, _CAMPAIGN_NAME_KEYS)
    if title:
        return title
    article = event.get("articleDto")
    if isinstance(article, dict):
        return _probe_str(article, ("title",))
    return ""


def _event_price(event: dict) -> str:
    """活动价格文案：``price``（字符串数值）> 0 时拼 ``priceSuffix``。

    实测 price 恒为 "0"（"0 起"无信息量），只在真有价格时展示。
    """
    try:
        value = float(event.get("price"))
    except (TypeError, ValueError):
        return ""
    if not (math.isfinite(value) and value > 0):
        return ""
    suffix = str(event.get("priceSuffix") or "").strip()
    return f"{value:g} 元{suffix}"


def _probe_str(entry: dict, keys: tuple[str, ...]) -> str:
    """取条目里第一个非空字符串键的值（去首尾空白）；全缺返回空串。"""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# 人读渲染（中文）
# ---------------------------------------------------------------------------


def render_coupon_list(title: str, coupons: list[dict]) -> str:
    """一组券渲染为中文人读文本：券名 + 已知补充字段（存在才打印）。"""
    if not coupons:
        return f"{title}：暂无"
    lines = [f"{title}（{len(coupons)} 张）"]
    for index, coupon in enumerate(coupons, start=1):
        name = _probe_str(coupon, _COUPON_NAME_KEYS) or "（券名未知，详见 --json）"
        lines.append(f"[{index}] {name}")
        for key in _COUPON_DETAIL_KEYS:
            if key in coupon:
                lines.append(f"    {key}：{coupon[key]}")
    return "\n".join(lines)


def render_coupons(mine: list[dict], available: list[dict]) -> str:
    """coupons 命令的人读渲染：我的券 + 可领券两段合并 + 领券下一步。"""
    return "\n".join(
        [
            render_coupon_list("我的优惠券", mine),
            "",
            render_coupon_list("可领优惠券", available),
            "",
            "领取可领券：python cli.py extras claim --yes-claim",
        ]
    )


def render_points(raw: object) -> str:
    """points 的人读渲染：积分数（探测）+ data 里其余标量字段原样。"""
    data = _data_of(raw)
    found = _find_point(raw)
    lines = ["账户积分（query-my-account 透传）："]
    if found is not None:
        lines.append(f"  积分：{found[1]:g}")
    shown_extra = False
    if isinstance(data, dict):
        for key, value in data.items():
            if found is not None and key == found[0]:
                continue  # 已由"积分"行展示，不重复
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                lines.append(f"  {key}：{value}")
                shown_extra = True
    if found is None and not shown_extra:
        lines.append("  响应未携带可识别的账户字段（完整内容见 --json）")
    return "\n".join(lines)


def render_campaigns(specified_date: str, raw: object) -> str:
    """campaigns 的人读渲染：按天分组（dateText，今日标注），每活动一行
    （标题 + 有价活动拼价格），完整字段看 --json。"""
    days = extract_campaign_days(raw)
    total = sum(len(_day_events(day)) for day in days)
    if not days:
        return f"{specified_date} 暂无活动日历数据（完整内容见 --json）"
    lines = [f"活动日历（查询 {specified_date}，共 {total} 项）："]
    if not total:
        lines.append("  本期日历暂无活动条目")
        return "\n".join(lines)
    for day in days:
        events = _day_events(day)
        if not events:
            continue
        date_text = _probe_str(day, ("dateText",)) or str(day.get("date") or "")
        mark = " · 今日" if day.get("today") else ""
        lines += ["", f"[{date_text}]{mark} {len(events)} 项"]
        for index, event in enumerate(events, start=1):
            title = _campaign_title(event) or "（标题未知，详见 --json）"
            price = _event_price(event)
            suffix = f"  {price}" if price else ""
            lines.append(f"  {index}. {title}{suffix}")
    return "\n".join(lines)


def render_claim(raw: object) -> str:
    """auto-bind-coupons 结果的人读渲染：message + data 标量字段原样。"""
    lines = ["一键领券（auto-bind-coupons）已执行："]
    message = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(message, str) and message.strip():
        lines.append(f"  {message.strip()}")
    data = _data_of(raw)
    shown = False
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                lines.append(f"  {key}：{value}")
                shown = True
    elif isinstance(data, list):
        lines.append(f"  data 共 {len(data)} 条（详见 --json）")
        shown = True
    if not shown and not (isinstance(message, str) and message.strip()):
        lines.append("  响应未携带可识别的结果字段（完整内容见 --json）")
    lines += ["", "查看我的券：python cli.py extras coupons"]
    return "\n".join(lines)


def render_claim_preview() -> str:
    """claim 未加安全旗标时打印的"将执行什么"摘要（stdout，供人核对）。"""
    return (
        "将一键领取当前全部可领优惠券（auto-bind-coupons）。\n"
        "确认请加 --yes-claim 旗标。\n"
    )


# ---------------------------------------------------------------------------
# payload 构造（解析结果 + 原始返回兜底）
# ---------------------------------------------------------------------------


def build_coupons_payload(mine_raw: object, available_raw: object) -> dict:
    """两端券响应 → JSON payload（结构化列表 + 原始返回兜底）。"""
    mine = extract_coupons(mine_raw)
    available = extract_coupons(available_raw)
    return {
        "mine": {"count": len(mine), "coupons": mine},
        "available": {"count": len(available), "coupons": available},
        "raw": {MY_COUPONS_TOOL: mine_raw, AVAILABLE_COUPONS_TOOL: available_raw},
    }


def build_points_payload(raw: object) -> dict:
    """账户响应 → JSON payload（探测出的积分数 + 原始返回）。"""
    return {"points": extract_points(raw), "account": raw}


def build_campaigns_payload(specified_date: str, raw: object) -> dict:
    """活动日历响应 → JSON payload（日期 + 按天结构化 + 原始返回兜底）。"""
    days = extract_campaign_days(raw)
    return {
        "date": specified_date,
        "count": sum(len(_day_events(day)) for day in days),
        "days": [
            {
                "date": day.get("date"),
                "dateText": day.get("dateText"),
                "today": bool(day.get("today")),
                "events": _day_events(day),
            }
            for day in days
        ],
        "calendar": raw,
    }


def build_claim_payload(raw: object) -> dict:
    """领券响应 → JSON payload（原始返回透传）。"""
    return {"claim": raw}


# ---------------------------------------------------------------------------
# 命令主体（client 注入；claim 只在过了安全门后可被调用）
# ---------------------------------------------------------------------------


def today_iso() -> str:
    """当日日期（``YYYY-MM-DD``）；独立函数便于测试 monkeypatch。"""
    return date.today().isoformat()


def fetch_coupons(client: "McpClient") -> tuple[dict, str]:
    """coupons 命令主体：两个只读调用 → ``(payload, 人读文本)``。"""
    mine_raw = client.call_tool(MY_COUPONS_TOOL, dict(MY_COUPONS_ARGUMENTS))
    available_raw = client.call_tool(AVAILABLE_COUPONS_TOOL, {})
    return (
        build_coupons_payload(mine_raw, available_raw),
        render_coupons(extract_coupons(mine_raw), extract_coupons(available_raw)),
    )


def fetch_points(client: "McpClient") -> tuple[dict, str]:
    """points 命令主体：单只读调用 → ``(payload, 人读文本)``。"""
    raw = client.call_tool(ACCOUNT_TOOL, {})
    return build_points_payload(raw), render_points(raw)


def fetch_campaigns(client: "McpClient") -> tuple[dict, str]:
    """campaigns 命令主体：单只读调用（当日日期）→ ``(payload, 人读文本)``。"""
    specified_date = today_iso()
    raw = client.call_tool(CAMPAIGN_CALENDAR_TOOL, {"specifiedDate": specified_date})
    return build_campaigns_payload(specified_date, raw), render_campaigns(
        specified_date, raw
    )


def claim_coupons(client: "McpClient") -> tuple[dict, str]:
    """claim 命令主体（**写操作**）：auto-bind-coupons → ``(payload, 人读文本)``。

    调用方（cli.py）必须先过 ``--yes-claim`` 安全门。
    """
    raw = client.call_tool(AUTO_BIND_TOOL, {})
    return build_claim_payload(raw), render_claim(raw)
