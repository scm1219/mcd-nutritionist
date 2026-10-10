"""门店/菜单层：query-nearby-stores / query-meals / query-meal-detail 的封装。

职责边界（本模块不 import CLI，可独立测试）：
- :func:`parse_stores` / :func:`parse_meals`：把工具原始返回裁剪成最小稳定结构
  （门店 dict 挑原始字段，餐品转 :class:`MenuItem`），断言以实录 fixture 为准；
- :func:`load_menu`：门店菜单的 10 分钟 TTL 文件缓存
  （``menu_cache_{store}_{mode}.json``，mode 由 beType 反查 ``pickup|delivery``）；
- :func:`resolve_items`：点单名 → 菜单编码的纯名称匹配（精确 → 包含），不触网；
- :func:`meal_detail`：套餐详情的薄封装，供 Task 8 换餐/特调使用。

就餐参数约定：``beType`` 1=到店取餐 / 2=麦乐送；``orderType`` 1=到店 / 2=外送；
CLI 层的 mode 字符串 ``pickup|delivery`` 由 :data:`MODE_BE_TYPES` /
:data:`MODE_ORDER_TYPES` 映射到这对整数。beType=1 的门店没有 beCode，所有
调用都不传 beCode。
"""

from __future__ import annotations

import difflib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mcdu.mcp_client import McduError

if TYPE_CHECKING:  # 仅类型标注用
    from mcdu.mcp_client import McpClient

#: 门店查询工具
STORES_TOOL = "query-nearby-stores"
#: 门店菜单工具
MEALS_TOOL = "query-meals"
#: 餐品详情工具
MEAL_DETAIL_TOOL = "query-meal-detail"

#: 菜单缓存 TTL（秒）：缓存期内不得二次调用 MCP
MENU_TTL_SECONDS = 10 * 60

#: mode 字符串 → beType（1=到店取餐 / 2=麦乐送）
MODE_BE_TYPES: dict[str, int] = {"pickup": 1, "delivery": 2}
#: mode 字符串 → orderType（1=到店 / 2=外送）
MODE_ORDER_TYPES: dict[str, int] = {"pickup": 1, "delivery": 2}

#: parse_stores 保留的原始字段（beCode 仅在存在时保留）
_STORE_FIELDS = ("storeCode", "storeName", "address", "distance", "beCode")

#: 找不到餐品时列出的相近候选数上限
_CANDIDATE_LIMIT = 5


@dataclass
class MenuItem:
    """门店菜单中的一个餐品（code 在门店内唯一）。"""

    code: str
    name: str
    category: str
    price: float | None
    tags: list[str] = field(default_factory=list)


@dataclass
class ResolvedItem:
    """点单项定位到菜单后的结果（fuzzy=True 表示非精确名称命中）。"""

    name: str
    code: str
    qty: int
    fuzzy: bool


def mode_of_be_type(be_type: int) -> str:
    """beType → mode 字符串（pickup/delivery），其他取餐形态不支持。"""
    for mode, value in MODE_BE_TYPES.items():
        if value == be_type:
            return mode
    raise ValueError(f"不支持的 beType：{be_type}（仅支持 1=到店取餐 / 2=麦乐送）")


def menu_cache_path(cache_dir: Path, store_code: str, mode: str) -> Path:
    """菜单缓存文件路径：``menu_cache_{store}_{mode}.json``。"""
    return Path(cache_dir) / f"menu_cache_{store_code}_{mode}.json"


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


def parse_stores(data: dict) -> list[dict]:
    """query-nearby-stores 返回 → 精简门店列表。

    每店只保留 ``storeCode / storeName / address / distance / beCode``
    原始字段；beCode 仅在源数据里存在且非空时保留（beType=1 到店自取门店
    没有 beCode，不造空字段）。取 ``data["data"]`` 列表，结构不符返回空列表。
    """
    body = data.get("data") if isinstance(data, dict) else None
    if not isinstance(body, list):
        return []
    stores: list[dict] = []
    for store in body:
        if not isinstance(store, dict):
            continue
        picked = {
            key: store[key] for key in _STORE_FIELDS if key != "beCode" and key in store
        }
        be_code = store.get("beCode")
        if be_code:  # 得来速/外送门店才有，保留原值
            picked["beCode"] = be_code
        if picked.get("storeCode"):
            stores.append(picked)
    return stores


def _parse_price(raw: object) -> float | None:
    """currentPrice 字符串（如 ``"33.5"``）→ float；缺失/非数值 → None。"""
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def parse_meals(data: dict) -> list[MenuItem]:
    """query-meals 返回 → 菜单条目列表。

    结构（以实录 fixture 为准）：``data.categories[]`` 各含 ``name`` 与
    ``meals[]{code, tags[]}``，餐品名称/价格在 ``data.meals[code]`` 详情映射
    （``name`` / ``currentPrice``）。同一餐品可属多个分类，故按分类条目逐一
    产出（保留 category 归属）；详情缺失或无名的条目跳过。
    """
    body = data.get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        return []
    details = body.get("meals") if isinstance(body.get("meals"), dict) else {}
    items: list[MenuItem] = []
    for category in body.get("categories") or []:
        if not isinstance(category, dict):
            continue
        for entry in category.get("meals") or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code") or "").strip()
            detail = details.get(code) or {}
            name = str(detail.get("name") or "").strip()
            if not code or not name:
                continue
            items.append(
                MenuItem(
                    code=code,
                    name=name,
                    category=str(category.get("name") or "").strip(),
                    price=_parse_price(detail.get("currentPrice")),
                    tags=[str(tag) for tag in entry.get("tags") or []],
                )
            )
    return items


# ---------------------------------------------------------------------------
# 菜单缓存（TTL 10 分钟）
# ---------------------------------------------------------------------------


def _read_cache(path: Path) -> dict | None:
    """读缓存文件；不存在、损坏或结构不对返回 None（调用方走重取）。"""
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not (isinstance(cached, dict) and isinstance(cached.get("raw"), dict)):
        return None
    if not isinstance(cached.get("fetched_at"), (int, float)):  # 坏时间戳视为无缓存
        return None
    return cached


def load_menu(
    client: "McpClient | None",
    store_code: str,
    be_type: int,
    order_type: int,
    cache_dir: Path,
) -> list[MenuItem]:
    """取门店菜单：缓存新鲜（< 10 分钟）直接用，否则调 MCP 并写缓存。

    缓存文件 ``menu_cache_{store}_{mode}.json`` 存原始工具返回（每次读取后
    重新解析，缓存内容即真实数据）。需要联网重取而 ``client`` 为 None
    （未配置 Token）时报业务错误。
    """
    path = menu_cache_path(cache_dir, store_code, mode_of_be_type(be_type))
    cached = _read_cache(path)
    fresh = cached is not None and time.time() - cached["fetched_at"] < MENU_TTL_SECONDS
    if fresh:
        return parse_meals(cached["raw"])

    if client is None:
        raise McduError(
            "CONFIG",
            "菜单缓存不可用且缺少 MCD_MCP_TOKEN，无法联网获取菜单"
            "（Token 请到 open.mcd.cn/mcp 申请，写入 .env）",
        )
    raw = client.call_tool(
        MEALS_TOOL, {"storeCode": store_code, "beType": be_type, "orderType": order_type}
    )
    if not isinstance(raw, dict):
        raise McduError("RESPONSE", f"query-meals 返回结构异常：{raw!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": time.time(), "raw": raw}, ensure_ascii=False),
        encoding="utf-8",
    )
    return parse_meals(raw)


# ---------------------------------------------------------------------------
# 点单项定位（纯名称匹配，不触网）
# ---------------------------------------------------------------------------


def _similar_names(name: str, menu: list[MenuItem]) -> list[str]:
    """相近候选：difflib 相近名（去重、保持菜单顺序），至多 :data:`_CANDIDATE_LIMIT` 个。"""
    close = difflib.get_close_matches(
        name, [item.name for item in menu], n=len(menu), cutoff=0.3
    )
    return list(dict.fromkeys(close))[:_CANDIDATE_LIMIT]


def resolve_items(
    entries: list[tuple[str, int]], menu: list[MenuItem]
) -> list[ResolvedItem]:
    """点单 ``(名称, 数量)`` 列表 → 菜单定位结果。

    匹配顺序：精确名相等（fuzzy=False）→ 包含匹配（输入名含菜单名或菜单名
    含输入名，取名字最短的候选，fuzzy=True）。命中失败抛
    ``ValueError("菜单中找不到: ...")`` 并列出相近候选（difflib）。
    """
    resolved: list[ResolvedItem] = []
    for raw_name, qty in entries:
        name = str(raw_name).strip()
        exact = [item for item in menu if item.name == name]
        if exact:
            resolved.append(
                ResolvedItem(name=exact[0].name, code=exact[0].code, qty=qty, fuzzy=False)
            )
            continue
        contained = [
            item for item in menu if name in item.name or item.name in name
        ]
        if contained:
            best = min(contained, key=lambda item: len(item.name))
            resolved.append(
                ResolvedItem(name=best.name, code=best.code, qty=qty, fuzzy=True)
            )
            continue
        similar = _similar_names(name, menu)
        hint = f"，相近候选：{'、'.join(similar)}" if similar else "，且无相近候选"
        raise ValueError(f"菜单中找不到: {name}{hint}")
    return resolved


# ---------------------------------------------------------------------------
# 餐品详情（Task 8 用）
# ---------------------------------------------------------------------------


def meal_detail(
    client: "McpClient", store_code: str, be_type: int, order_type: int, code: str
) -> dict:
    """查询餐品/套餐详情（选配轮次、特调选项），原样返回工具结果。

    beType=1（到店取餐）不得传 beCode，这里也只传四项必填参数。
    """
    result = client.call_tool(
        MEAL_DETAIL_TOOL,
        {
            "storeCode": str(store_code),
            "beType": be_type,
            "orderType": order_type,
            "code": str(code),
        },
    )
    return result if isinstance(result, dict) else {"data": result}
