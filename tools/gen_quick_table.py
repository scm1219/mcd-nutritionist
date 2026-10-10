"""速查表生成器：营养表分类采样 top40 → 紧凑 Markdown 表 → 注入 SKILL.md。

用法::

    python tools/gen_quick_table.py                 # 表打到 stdout
    python tools/gen_quick_table.py --out table.md  # 写文件
    python tools/gen_quick_table.py --inject SKILL.md
        # 只替换 SKILL.md 中 QUICK-TABLE 标记区内的内容，区外文本不变

营养数据优先用本地缓存（data/nutrition_cache.json，无网络）；缓存缺失时
回退 MCP 拉取（需要 .env 里的 MCD_MCP_TOKEN）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

#: 仓库根（tools/ 的上一级）：`python tools/gen_quick_table.py` 直跑时补 sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcdu.mcp_client import McduError, McpClient  # noqa: E402
from mcdu.nutrition import NutritionTable, normalize_name  # noqa: E402
from scripts.suggest import classify  # noqa: E402

if TYPE_CHECKING:  # 仅类型标注用
    from mcdu.nutrition import NutritionItem

#: 营养表缓存（与 cli.py 同一处，gitignored）
CACHE_PATH = REPO_ROOT / "data" / "nutrition_cache.json"

#: SKILL.md 里速查表标记区（--inject 只替换两标记之间的内容）
QUICK_TABLE_START = "<!-- QUICK-TABLE-START -->"
QUICK_TABLE_END = "<!-- QUICK-TABLE-END -->"

#: 采样类别与名额分配顺序（与 scripts.suggest.classify 的类别名一致）
CATEGORY_ORDER = ("main", "side", "drink")

#: 速查表表头（名称|kcal|蛋白|脂肪|碳水|钠，测试锚定此字符串）
HEADER = "| 名称 | kcal | 蛋白(g) | 脂肪(g) | 碳水(g) | 钠(mg) |"


def pick_top(table: NutritionTable, n: int = 40) -> list["NutritionItem"]:
    """按分类采样取速查表条目：main/side/drink 各取蛋白靠前者，覆盖三类。

    名额在非空类别间均分（余数给靠前的类别，按 :data:`CATEGORY_ORDER`）；
    某类条目不足额时缺口用三类全局蛋白序补齐。同名（规范化后）重复行
    只留数据源中靠前的第一条。
    """
    buckets: dict[str, list[NutritionItem]] = {c: [] for c in CATEGORY_ORDER}
    seen: set[str] = set()
    for item in table.items:
        if classify(item.name) not in buckets:  # other 不进速查表候选
            continue
        key = normalize_name(item.name)
        if key in seen:
            continue
        seen.add(key)
        buckets[classify(item.name)].append(item)
    for items in buckets.values():
        items.sort(key=lambda i: i.protein_g, reverse=True)  # 稳定排序：并列保原序

    picked: list[NutritionItem] = []
    quotas = _quotas(n, {c: len(buckets[c]) for c in CATEGORY_ORDER})
    for category in CATEGORY_ORDER:
        picked.extend(buckets[category][: quotas[category]])

    if len(picked) < n:  # 某类不足额：三类全局蛋白序补齐（覆盖三类仍优先）
        chosen = {id(i) for i in picked}
        pool = sorted(
            (i for items in buckets.values() for i in items),
            key=lambda i: i.protein_g,
            reverse=True,
        )
        for item in pool:
            if len(picked) >= n:
                break
            if id(item) not in chosen:
                picked.append(item)
    return picked[:n]


def _quotas(n: int, sizes: dict[str, int]) -> dict[str, int]:
    """n 个名额在非空类别间均分：余数给靠前的类别，并按类别库存封顶。"""
    active = [c for c in CATEGORY_ORDER if sizes[c] > 0]
    if not active:
        return {c: 0 for c in CATEGORY_ORDER}
    base, extra = divmod(n, len(active))
    return {
        c: min(sizes[c], base + (1 if idx < extra else 0))
        for idx, c in enumerate(active)
    }


def _fmt(value: float) -> str:
    """数值紧凑格式：整数不带 ``.0``，非整数保留 1 位小数。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def render_markdown(items: list["NutritionItem"]) -> str:
    """渲染紧凑 Markdown 表：首行为表头，一行一个餐品。"""
    lines = [HEADER, "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for i in items:
        lines.append(
            f"| {i.name} | {_fmt(i.energy_kcal)} | {_fmt(i.protein_g)}"
            f" | {_fmt(i.fat_g)} | {_fmt(i.carb_g)} | {_fmt(i.sodium_mg)} |"
        )
    return "\n".join(lines) + "\n"


def inject(path: Path, markdown: str) -> None:
    """把 markdown 写入 ``path`` 的标记区内，标记与区外文本保持不变。

    :raises ValueError: 文件缺少任一标记（找不到可替换的标记区）。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    head, found_start, rest = text.partition(QUICK_TABLE_START)
    _, found_end, tail = rest.partition(QUICK_TABLE_END)
    if not found_start or not found_end:
        raise ValueError(
            f"{path} 缺少标记区：需要成对的 {QUICK_TABLE_START} 与 {QUICK_TABLE_END}"
        )
    body = markdown.rstrip("\n")
    path.write_text(
        f"{head}{QUICK_TABLE_START}\n{body}\n{QUICK_TABLE_END}{tail}",
        encoding="utf-8",
    )


def _load_table() -> NutritionTable:
    """加载营养表：优先本地缓存（可离线）；缓存缺失才走 MCP 拉取。"""
    try:
        return NutritionTable.load(CACHE_PATH)  # 纯缓存：新鲜直读，过期降级 stale
    except McduError:
        pass
    try:
        return NutritionTable.load(CACHE_PATH, client=McpClient())
    except McduError as exc:
        raise SystemExit(f"无法获取营养表：{exc.message}") from exc


def main(out_path: Path | None = None, inject_path: Path | None = None) -> int:
    """生成 top40 速查表：``--inject`` 注入标记区；否则写 ``out_path`` 或 stdout。"""
    markdown = render_markdown(pick_top(_load_table(), n=40))
    if inject_path is not None:
        try:
            inject(inject_path, markdown)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"已注入 {inject_path}（标记区内 40 项）")
    elif out_path is not None:
        Path(out_path).write_text(markdown, encoding="utf-8")
        print(f"已写入 {out_path}")
    else:
        sys.stdout.write(markdown)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen_quick_table.py", description="生成 top40 营养速查表（Markdown）"
    )
    parser.add_argument(
        "--inject",
        type=Path,
        default=None,
        metavar="SKILL_MD",
        help="在标记区 <!-- QUICK-TABLE-START/END --> 内替换写入（区外文本不变）",
    )
    parser.add_argument(
        "--out",
        "-o",
        type=Path,
        default=None,
        help="输出文件路径（缺省且无 --inject 时打到 stdout）",
    )
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    sys.exit(main(args.out, args.inject))
