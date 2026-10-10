"""order 子命令组：门店查询（stores）与门店菜单（menu）的 payload/人读渲染。

本模块不做任何 I/O（不联网、不读写缓存）：MCP 调用与缓存由 cli.py 组装
:mod:`mcdu.orderflow` 完成，这里只负责把结构化结果变成双通道输出。
"""

from __future__ import annotations

from dataclasses import asdict

from mcdu.orderflow import MenuItem

#: mode 的人读叫法（与 beType 1/2 对应）
_MODE_LABELS = {"pickup": "到店取餐", "delivery": "麦乐送"}


def build_stores_payload(stores: list[dict]) -> dict:
    """门店列表 → JSON payload。"""
    return {"count": len(stores), "stores": stores}


def render_stores(stores: list[dict]) -> str:
    """门店列表渲染为中文人读文本：序号 + 店名（编码）+ 地址。"""
    if not stores:
        return "未找到可点餐门店：换个城市或关键词试试"
    lines = [f"可点餐门店（{len(stores)} 家）"]
    for index, store in enumerate(stores, start=1):
        lines.append(f"[{index}] {store.get('storeName', '')}（{store.get('storeCode', '')}）")
        address = str(store.get("address") or "").strip()
        if address:
            lines.append(f"    地址：{address}")
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
