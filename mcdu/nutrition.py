"""营养数据层：解析 MCP 返回的 CSV、本地 JSON 缓存、名称模糊匹配与点单数量解析。

数据来源是 ``McpClient.call_tool("list-nutrition-foods")`` 返回 dict 的 ``data`` 字段，
形如::

    [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,
    carbohydrate,sodium,calcium}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
      ...

即一行头（``[条数]{字段列表}:``）+ 逗号分隔的数据行（名称, 描述, 7 个数值列）。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from mcdu.mcp_client import McduError

if TYPE_CHECKING:  # 仅类型标注用，避免模块级硬依赖
    from mcdu.mcp_client import McpClient

#: 拉取营养列表用的 MCP 工具名
TOOL_NAME = "list-nutrition-foods"

#: 缓存 TTL：7 天
DEFAULT_TTL_SECONDS = 7 * 86400

#: difflib 模糊匹配的最低相似度
FUZZY_THRESHOLD = 0.6

#: data 列（去掉 productName 与 nutritionDescription）→ NutritionItem 字段
_NUMERIC_FIELDS = (
    "energy_kj",
    "energy_kcal",
    "protein_g",
    "fat_g",
    "carb_g",
    "sodium_mg",
    "calcium_mg",
)


@dataclass
class NutritionItem:
    """一个餐品的营养数值（每份）。"""

    name: str
    energy_kj: float
    energy_kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    sodium_mg: float
    calcium_mg: float


@dataclass
class MatchResult:
    """名称匹配结果：confidence ∈ exact / fuzzy / unknown。

    unknown 时 item 为 None——未知餐品必须显式标记，绝不当 0 处理。
    """

    item: NutritionItem | None
    confidence: str
    matched_as: str


# ---------------------------------------------------------------------------
# 名称规范化
# ---------------------------------------------------------------------------

#: 各种成对括号（全角括号会先统一转成半角，故只列半角 + 中式括号）
_BRACKET_PAIRS = ("()", "[]", "{}", "【】", "「」", "『』", "《》", "〈〉")


def _to_halfwidth(s: str) -> str:
    """全角字符转半角：U+3000 转空格，U+FF01–U+FF5E 减 0xFEE0。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _strip_bracket_pair(s: str, opener: str, closer: str) -> str:
    """剥一种括号：成对的连内容剥掉；成单开括号砍到串尾；成单闭括号只删字符。"""
    while True:
        start = s.find(opener)
        if start < 0:
            break
        end = s.find(closer, start + 1)
        if end < 0:
            s = s[:start]  # 未闭合的开括号：其后视为备注，整段去掉
            break
        s = s[:start] + s[end + 1 :]  # 成对：剥掉括号及其中内容
    return s.replace(closer, "")  # 剩下的闭括号必是成单，仅删字符保留内容


def normalize_name(s: str) -> str:
    """餐品名规范化：去首尾空格 → 全角转半角 → lower → 剥括号及内容。"""
    s = _to_halfwidth(s.strip()).lower()
    for opener, closer in _BRACKET_PAIRS:
        s = _strip_bracket_pair(s, opener, closer)
    return s.strip()


# ---------------------------------------------------------------------------
# CSV 解析
# ---------------------------------------------------------------------------

_HEADER_LINE = re.compile(r"^\[\d+\]\{.*\}:$")


def parse_nutrition_data(data_str: str) -> list[NutritionItem]:
    """解析 ``list-nutrition-foods`` 的 CSV 文本为 :class:`NutritionItem` 列表。

    数值列出现 ``null``/空/非数值时整行跳过（而不是悄悄当 0）；名称为空同样跳过。
    """
    items: list[NutritionItem] = []
    for raw_line in data_str.splitlines():
        line = raw_line.strip()
        if not line or _HEADER_LINE.match(line):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 + len(_NUMERIC_FIELDS):
            continue
        name = parts[0]
        if not name or name.lower() == "null":
            continue
        try:
            values = [float(v) for v in parts[2 : 2 + len(_NUMERIC_FIELDS)]]
        except ValueError:
            continue
        items.append(NutritionItem(name=name, **dict(zip(_NUMERIC_FIELDS, values))))
    return items


# ---------------------------------------------------------------------------
# 名称匹配
# ---------------------------------------------------------------------------


class NutritionTable:
    """餐品营养表：内存索引 + 本地缓存加载，支持别名/精确/子串/模糊四级匹配。"""

    def __init__(self, items: list[NutritionItem], fetched_at: str):
        self.items = list(items)
        self.fetched_at = fetched_at
        #: 缓存过期但无 client 可拉取时，用旧数据并置 True
        self.stale = False
        # 预计算规范化名：索引与子串/模糊匹配共用，避免每次 match 重复 normalize
        self._entries: list[tuple[NutritionItem, str]] = [
            (item, normalize_name(item.name)) for item in self.items
        ]
        # 规范化名 → 首个条目（数据源存在重复行）
        self._index: dict[str, NutritionItem] = {}
        for item, name in self._entries:
            self._index.setdefault(name, item)

    # -- 加载 / 缓存 -------------------------------------------------------

    @classmethod
    def load(
        cls,
        cache_path: Path,
        client: "McpClient | None" = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> "NutritionTable":
        """加载营养表：缓存新鲜直接读；过期且有 client 重拉并写缓存；过期且无
        client 用旧缓存并标记 ``.stale=True``；既无缓存又无 client 报错。
        """
        cached = cls._read_cache(cache_path)
        if cached is not None and not _is_expired(cached[1], ttl_seconds):
            return cls(cached[0], cached[1])
        if client is not None:
            table = cls._fetch(client)
            table.save(cache_path)
            return table
        if cached is not None:
            table = cls(cached[0], cached[1])
            table.stale = True
            return table
        raise McduError(
            "CACHE",
            f"无营养数据缓存（{cache_path}）且未提供 MCP client，无法获取餐品列表",
        )

    @classmethod
    def _fetch(cls, client: "McpClient") -> "NutritionTable":
        payload = client.call_tool(TOOL_NAME, {})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, str) or not data.strip():
            raise McduError("RESPONSE", f"{TOOL_NAME} 返回中缺少 data（CSV 文本）")
        fetched_at = datetime.now(timezone.utc).isoformat()
        return cls(parse_nutrition_data(data), fetched_at)

    def save(self, cache_path: Path) -> None:
        """写缓存 ``{"fetched_at": iso, "items": [...]}``，先写临时文件再原子替换。"""
        payload = {"fetched_at": self.fetched_at, "items": [asdict(i) for i in self.items]}
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_name(cache_path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp_path.replace(cache_path)

    @staticmethod
    def _read_cache(cache_path: Path) -> "tuple[list[NutritionItem], str] | None":
        cache_path = Path(cache_path)
        if not cache_path.is_file():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            fetched_at = str(payload["fetched_at"])
            rows = payload["items"]
            items = [
                NutritionItem(
                    name=str(row["name"]),
                    **{f: float(row[f]) for f in _NUMERIC_FIELDS},
                )
                for row in rows
            ]
        except (OSError, ValueError, TypeError, KeyError):
            return None  # 缓存损坏视同没有：有 client 就重拉
        return items, fetched_at

    # -- 匹配 ---------------------------------------------------------------

    def match(self, raw: str, aliases: dict[str, str] | None = None) -> MatchResult:
        """按 别名 → normalize 后精确 → 双向子串 → difflib≥0.6 的层级匹配。"""
        query = normalize_name(raw)
        target: str | None = None
        for alias, canonical in (aliases or {}).items():
            if query == normalize_name(alias):
                target = normalize_name(canonical)
                break

        # 1. 精确：别名目标优先，其次原始输入
        for candidate in (target, query):
            if candidate and candidate in self._index:
                item = self._index[candidate]
                return MatchResult(item, "exact", item.name)

        # 2. 双向子串（别名命中但表中无同名时，用更完整的目标名再找）
        needle = target or query
        if needle:
            for item, name in self._entries:
                if name and (name in needle or needle in name):
                    return MatchResult(item, "fuzzy", item.name)

            # 3. difflib 相似度，取最高分（并列取表中靠前者）
            best: NutritionItem | None = None
            best_score = 0.0
            for item, name in self._entries:
                score = SequenceMatcher(None, needle, name).ratio()
                if score > best_score:
                    best, best_score = item, score
            if best is not None and best_score >= FUZZY_THRESHOLD:
                return MatchResult(best, "fuzzy", best.name)

        return MatchResult(None, "unknown", "")


def _is_expired(fetched_at: str, ttl_seconds: float) -> bool:
    try:
        fetched = _parse_iso(fetched_at)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    return age > ttl_seconds


def _parse_iso(ts: str) -> datetime:
    ts = ts.strip()
    if ts.endswith(("Z", "z")):  # 兼容手写缓存的 Z 后缀
        ts = ts[:-1] + "+00:00"
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:  # 无时区按 UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# 别名表与点单参数
# ---------------------------------------------------------------------------


def load_aliases(path: Path) -> dict[str, str]:
    """读别名表 JSON（``{"别名": "标准名"}``）。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"别名表格式错误：应为 JSON 对象 {path}")
    return {str(k): str(v) for k, v in payload.items()}


#: 数量后缀分隔符：巨无霸x1 / 巨无霸X2 / 巨无霸*3 / 巨无霸×2
_QTY_SUFFIX = re.compile(r"^(?P<name>.+)[xX×*](?P<qty>[^xX×*]*)$")
#: 数量前缀：2个巨无霸 / 2份巨无霸
_QTY_PREFIX = re.compile(r"^(?P<qty>\d+)\s*(?:个|份)(?P<name>.+)$")
_QTY_INT = re.compile(r"\d+")


def parse_items_arg(s: str) -> list[tuple[str, int]]:
    """解析点单参数为 ``(餐品名, 数量)`` 列表；数量必须是正整数。

    支持写法：``双层吉士汉堡x1,中薯条x2``、``巨无霸X2``、``巨无霸*3``、
    ``巨无霸×2``、``2个巨无霸``、``2份巨无霸``；不带数量默认 1。

    :raises ValueError: 数量不是正整数（``无法解析数量: ...``）。
    """
    parsed: list[tuple[str, int]] = []
    for token in re.split(r"[,，]", s):
        token = token.strip()
        if token:
            parsed.append(_parse_one_item(token))
    return parsed


def _parse_one_item(token: str) -> tuple[str, int]:
    prefix = _QTY_PREFIX.match(token)
    if prefix:
        qty = int(prefix.group("qty"))
        _require_positive(qty, token)
        return prefix.group("name").strip(), qty

    suffix = _QTY_SUFFIX.match(token)
    if suffix:
        qty_text = suffix.group("qty").strip()
        if not _QTY_INT.fullmatch(qty_text):
            raise ValueError(f"无法解析数量: {token}")
        qty = int(qty_text)
        _require_positive(qty, token)
        return suffix.group("name").strip(), qty

    # 看起来带数量但两种写法都对不上：如 “2.5个巨无霸”“x2巨无霸”
    if (re.match(r"^\d", token) and ("个" in token or "份" in token)) or any(
        ch in token for ch in "xX×*"
    ):
        raise ValueError(f"无法解析数量: {token}")
    return token, 1


def _require_positive(qty: int, token: str) -> None:
    if qty < 1:
        raise ValueError(f"无法解析数量: {token}")
