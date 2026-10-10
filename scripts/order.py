"""order 子命令组：门店查询（stores）/ 门店菜单（menu）/ 下单草稿（draft）。

stores/menu 是纯 payload/渲染；draft 与 ``scripts.suggest.suggest_store``
同惯例——MCP 调用经注入的 client 完成（菜单走 :func:`mcdu.orderflow.load_menu`
的 10 分钟 TTL 缓存），本地草稿文件的读写由调用方（cli.py）经
:class:`mcdu.orderflow.DraftStore` 完成。

安全边界：draft 全程只调用只读工具（query-meals / query-meal-detail /
delivery-query-addresses / calculate-price），**绝不调用 create-order**——
真实下单属 Task 9 的 confirm 命令且必须经 ``--yes-place-order`` 安全门。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from mcdu.mcp_client import McduError
from mcdu.orderflow import (
    MODE_BE_TYPES,
    MODE_ORDER_TYPES,
    Draft,
    MenuItem,
    load_menu,
    meal_detail,
    resolve_items,
)
from scripts.analyze import build_analysis
from scripts.suggest import PRICE_AMOUNT_FIELD, PRICE_TOOL

if TYPE_CHECKING:  # 仅类型标注用
    from mcdu.mcp_client import McpClient
    from mcdu.nutrition import NutritionTable
    from mcdu.orderflow import ResolvedItem

#: 配送地址查询工具（外送草稿需要 addressId）
ADDRESSES_TOOL = "delivery-query-addresses"

#: 草稿中按菜单名判定套餐类商品的关键词（调 query-meal-detail 取默认组成）
COMBO_KEYWORD = "套餐"

#: mode 的人读叫法（与 beType 1/2 对应）
_MODE_LABELS = {"pickup": "到店取餐", "delivery": "麦乐送"}


def build_stores_payload(stores: list[dict]) -> dict:
    """门店列表 → JSON payload。"""
    return {"count": len(stores), "stores": stores}


def render_stores(stores: list[dict]) -> str:
    """门店列表渲染为中文人读文本：序号 + 店名（编码）+ 地址 + 距离 + beCode。"""
    if not stores:
        return "未找到可点餐门店：换个城市或关键词试试"
    lines = [f"可点餐门店（{len(stores)} 家）"]
    for index, store in enumerate(stores, start=1):
        lines.append(f"[{index}] {store.get('storeName', '')}（{store.get('storeCode', '')}）")
        address = str(store.get("address") or "").strip()
        if address:
            lines.append(f"    地址：{address}")
        distance = store.get("distance")
        if isinstance(distance, (int, float)) and distance > 0:
            lines.append(f"    距离约 {int(distance)} 米")
        if store.get("beCode"):
            lines.append(f"    beCode：{store['beCode']}（外送等场景点单需要）")
    return "\n".join(lines)


def build_menu_payload(store_code: str, mode: str, items: list[MenuItem]) -> dict:
    """菜单 → JSON payload（MenuItem 转 dict，保留分类/价格/标签）。"""
    return {
        "store": store_code,
        "mode": mode,
        "count": len(items),
        "items": [asdict(item) for item in items],
    }


def render_menu(store_code: str, mode: str, items: list[MenuItem]) -> str:
    """菜单渲染为中文人读文本：按分类分组，逐项名称 + 价格 + 标签。

    分类名含换行（如"鸡肉汉堡/卷"实录为 ``"鸡肉汉堡\\n/卷"``），渲染时折叠
    为单行空白。
    """
    if not items:
        return f"门店 {store_code} 菜单为空或暂不可用"
    grouped: dict[str, list[MenuItem]] = {}
    for item in items:
        grouped.setdefault(" ".join(item.category.split()), []).append(item)
    label = _MODE_LABELS.get(mode, mode)
    lines = [f"门店 {store_code} 菜单（{label}，{len(grouped)} 个分类 / {len(items)} 项）"]
    for category, members in grouped.items():
        lines.append("")
        lines.append(f"[{category}] {len(members)} 项")
        for item in members:
            price = f"¥{item.price:g}" if item.price is not None else "价格待定"
            tags = f"（{'、'.join(item.tags)}）" if item.tags else ""
            lines.append(f"    {item.name}  {price}{tags}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 下单草稿（draft）：只读工具组成，绝不 create-order
# ---------------------------------------------------------------------------


def parse_addresses(data: object) -> list[dict]:
    """delivery-query-addresses 返回 → 精简地址列表。

    保留 ``addressId / contactName / fullAddress`` 原始字段；**不保留
    phone**（列表只为供用户选择，没必要回显手机号）。结构不符返回空列表。
    """
    body = data.get("data") if isinstance(data, dict) else None
    addresses = body.get("addresses") if isinstance(body, dict) else None
    picked: list[dict] = []
    for address in addresses if isinstance(addresses, list) else []:
        if not isinstance(address, dict) or not address.get("addressId"):
            continue
        entry = {
            key: address[key]
            for key in ("addressId", "contactName", "fullAddress")
            if key in address
        }
        picked.append(entry)
    return picked


def render_address_choices(addresses: list[dict]) -> str:
    """地址列表渲染为中文人读文本：引导用户带 --address-id 重新生成草稿。"""
    if not addresses:
        return "未找到配送地址：请先在麦当劳 App 添加收货地址，再重新生成外送草稿"
    lines = ["外送草稿需要配送地址，请从以下地址中选择，并用 --address-id 重新生成："]
    for index, address in enumerate(addresses, start=1):
        contact = address.get("contactName") or ""
        prefix = f"{contact} · " if str(contact).strip() else ""
        lines.append(
            f"[{index}] {prefix}{address.get('fullAddress', '')}"
            f"（addressId：{address['addressId']}）"
        )
    return "\n".join(lines)


def default_combo_parts(detail: object) -> list[str]:
    """query-meal-detail 返回 → 套餐默认组成名称列表（isDefault=1 的 choices）。

    v1 直接采用默认组合（不交互改选），仅用于草稿展示；数量非 1 的选项
    以 ``名称x数量`` 标注。
    """
    data = detail.get("data") if isinstance(detail, dict) else None
    rounds = data.get("rounds") if isinstance(data, dict) else None
    parts: list[str] = []
    for round_entry in rounds if isinstance(rounds, list) else []:
        if not isinstance(round_entry, dict):
            continue
        for choice in round_entry.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("isDefault") not in (1, True):
                continue
            name = str(choice.get("name") or "").strip()
            if not name:
                continue
            quantity = choice.get("quantity") or 1
            label = f"{name}x{quantity}" if isinstance(quantity, int) and quantity > 1 else name
            parts.append(label)
    return parts


def build_order_payload(
    resolved: "list[ResolvedItem]",
    mode: str,
    store_code: str,
    address_id: str | None = None,
    be_code: str | None = None,
    coupon_id: str | None = None,
    need_tableware: bool = True,
) -> dict:
    """构造 create-order 形状的完整 payload（草稿 items 的来源）。

    - items = ``[{productCode, quantity}]``；``coupon_id`` 给定时塞进
      ``items[0].couponId``（create-order/calculate-price 的 items 条目结构）；
    - 外送（delivery）附 ``addressId`` 与 ``beCode``（到店不传 beCode）；
    - ``needTableware`` 默认 true（``--no-tableware`` 置 false）。

    本函数只组装 dict，不发起任何调用——draft 阶段它仅供存草稿与算价。
    """
    items = [{"productCode": r.code, "quantity": r.qty} for r in resolved]
    if coupon_id is not None and items:
        items[0]["couponId"] = str(coupon_id)
    payload: dict = {
        "storeCode": str(store_code),
        "beType": MODE_BE_TYPES[mode],
        "orderType": MODE_ORDER_TYPES[mode],
        "items": items,
        "needTableware": bool(need_tableware),
    }
    if mode == "delivery":
        payload["addressId"] = str(address_id or "")
        if be_code is not None:
            payload["beCode"] = str(be_code)
    return payload


def price_arguments(payload: dict) -> dict:
    """create-order 形状 payload → calculate-price 参数。

    与 Task 7 已实录的算价参数形状保持一致（storeCode/beType/orderType/
    items[/beCode]，couponId 在 items[0] 内），不带 addressId（算价 schema
    无此键）与 needTableware（未经验证不进算价调用）。
    """
    return {
        key: value
        for key, value in payload.items()
        if key not in ("addressId", "needTableware")
    }


def build_price_details(raw: object) -> dict:
    """calculate-price 返回 → 价格明细（分 → 元）。

    ``amount`` 必取自 ``data.price``（实录字段，缺失即响应异常）；原价/
    优惠存在时一并换算。金额为 2 位小数十进制数，整数分 /100 无精度损失。
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        raise McduError("RESPONSE", f"calculate-price 返回结构异常：{raw!r}")
    amount = data.get(PRICE_AMOUNT_FIELD)
    if not isinstance(amount, (int, float)):
        raise McduError(
            "RESPONSE",
            f"calculate-price 返回缺少金额字段 data.{PRICE_AMOUNT_FIELD}：{raw!r}",
        )
    details = {"amount": round(amount / 100, 2)}
    original = data.get("originalPrice")
    if isinstance(original, (int, float)):
        details["original"] = round(original / 100, 2)
    discount = data.get("discount")
    if isinstance(discount, (int, float)):
        details["discount"] = round(discount / 100, 2)
    return details


def build_draft(
    client: "McpClient",
    table: "NutritionTable",
    store_code: str,
    mode: str,
    entries: list[tuple[str, int]],
    address_id: str | None = None,
    be_code: str | None = None,
    coupon_id: str | None = None,
    need_tableware: bool = True,
    cache_dir: Path = Path("data"),
    aliases: dict[str, str] | None = None,
) -> tuple[Draft, dict[str, list[str]], list[dict]]:
    """生成一份下单草稿（**绝不调用 create-order**）。

    流程：``load_menu``（10 分钟 TTL 缓存）→ ``resolve_items`` 定位编码 →
    套餐类商品（菜单名含"套餐"）调 ``meal_detail`` 取默认组成（v1 只用于
    展示，不改选）→ ``build_order_payload`` → ``calculate-price``（券 ID 在
    items[0].couponId）→ ``build_analysis`` 内联营养 → 构造
    :class:`~mcdu.orderflow.Draft`（draft_id = ``uuid4().hex[:8]``）。

    返回 ``(draft, combos, display)``：combos 是 ``{code: 默认组成名列表}``
    供渲染，display 是 ``[{code, name, qty, fuzzy}]`` 的人读品项表。

    :raises McduError: 菜单/详情/算价不可得或响应异常。
    :raises ValueError: mode 不合法、外送缺 address_id、点单项不在菜单中。
    """
    if mode not in MODE_BE_TYPES:
        raise ValueError(f"不支持的取餐方式：{mode}（可选 {'/'.join(MODE_BE_TYPES)}）")
    if mode == "delivery" and not address_id:
        raise ValueError("外送草稿需要 --address-id（缺省时可先不传以列出可用地址）")
    be_type = MODE_BE_TYPES[mode]
    order_type = MODE_ORDER_TYPES[mode]

    menu = load_menu(client, store_code, be_type, order_type, cache_dir, be_code=be_code)
    resolved = resolve_items(entries, menu)

    combos: dict[str, list[str]] = {}
    for item in resolved:
        if COMBO_KEYWORD in item.name:
            detail = meal_detail(
                client, store_code, be_type, order_type, item.code, be_code=be_code
            )
            parts = default_combo_parts(detail)
            if parts:
                combos[item.code] = parts

    payload = build_order_payload(
        resolved,
        mode,
        store_code,
        address_id=address_id,
        be_code=be_code,
        coupon_id=coupon_id,
        need_tableware=need_tableware,
    )
    price = build_price_details(client.call_tool(PRICE_TOOL, price_arguments(payload)))
    analysis = build_analysis([(r.name, r.qty) for r in resolved], table, aliases=aliases)

    display = [
        {"code": r.code, "name": r.name, "qty": r.qty, "fuzzy": r.fuzzy} for r in resolved
    ]
    draft = Draft(
        draft_id=uuid.uuid4().hex[:8],
        created_at=time.time(),
        mode=mode,
        store_code=store_code,
        items=payload["items"],
        address_id=address_id if mode == "delivery" else None,
        analysis=analysis,
        price=price,
    )
    return draft, combos, display


def _next_command(draft_id: str) -> str:
    """草稿的下一步命令行提示（confirm 属 Task 9，必须带安全旗标）。"""
    return f"python cli.py order confirm --draft-id {draft_id} --yes-place-order"


def build_draft_payload(
    draft: Draft,
    display: list[dict],
    combos: dict[str, list[str]],
    need_tableware: bool = True,
    coupon_id: str | None = None,
) -> dict:
    """草稿 → JSON payload（含下一步命令；analysis/price 原样内联）。"""
    return {
        "draft_id": draft.draft_id,
        "store": draft.store_code,
        "mode": draft.mode,
        "items": draft.items,
        "names": display,
        "address_id": draft.address_id,
        "coupon_id": coupon_id,
        "need_tableware": bool(need_tableware),
        "combos": combos,
        "analysis": draft.analysis,
        "price": draft.price,
        "next": _next_command(draft.draft_id),
    }


def render_draft(
    draft: Draft,
    display: list[dict],
    combos: dict[str, list[str]],
    need_tableware: bool = True,
    coupon_id: str | None = None,
) -> str:
    """草稿渲染为中文人读文本：品项/数量/套餐组成 + 营养 + 价格明细 + 下一步。"""
    label = _MODE_LABELS.get(draft.mode, draft.mode)
    totals = draft.analysis["totals"]
    lines = [f"下单草稿 {draft.draft_id}（门店 {draft.store_code} · {label}）", "", "品项："]
    for entry in display:
        mark = "" if not entry["fuzzy"] else "（模糊匹配）"
        lines.append(f"  {entry['name']}{mark} x{entry['qty']}")
        parts = combos.get(entry["code"])
        if parts:
            lines.append(f"    套餐默认组成：{' + '.join(parts)}")

    lines += [
        "",
        "营养合计：",
        f"  热量 {_fmt(totals['energy_kcal'])} kcal"
        f" | 蛋白质 {_fmt(totals['protein_g'])} g"
        f" | 脂肪 {_fmt(totals['fat_g'])} g"
        f" | 碳水 {_fmt(totals['carb_g'])} g",
        f"  钠 {_fmt(totals['sodium_mg'])} mg | 钙 {_fmt(totals['calcium_mg'])} mg",
    ]
    for warning in draft.analysis.get("warnings") or []:
        lines.append(f"  ⚠️ {warning}")
    if draft.analysis.get("unknown"):
        lines.append(f"  未知餐品（未计入合计）：{'、'.join(draft.analysis['unknown'])}")

    price = draft.price
    parts = [f"实付 ¥{price['amount']:.2f}"]
    if "original" in price:
        parts.append(f"原价 ¥{price['original']:.2f}")
    if "discount" in price:
        parts.append(f"优惠 ¥{price['discount']:.2f}")
    lines += ["", "价格明细：", "  " + " | ".join(parts)]
    lines.append(f"  餐具：{'需要' if need_tableware else '不需要'}")
    if coupon_id:
        lines.append(f"  优惠券：{coupon_id}")
    if draft.mode == "delivery" and draft.address_id:
        lines.append(f"  配送地址：{draft.address_id}")

    lines += [
        "",
        "草稿已保存（30 分钟内有效）：data/draft_%s.json" % draft.draft_id,
        "本草稿尚未下单。下一步（确认后才会真实创建订单）：",
        f"  {_next_command(draft.draft_id)}",
    ]
    return "\n".join(lines)


def _fmt(value: float) -> str:
    """数值人读化：整数不带小数点，否则保留 1 位（与 scripts.analyze 一致）。"""
    rounded = round(value)
    if abs(value - rounded) < 1e-9:
        return str(int(rounded))
    return f"{value:.1f}"
