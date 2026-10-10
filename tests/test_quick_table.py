"""速查表生成器测试：分类采样、紧凑表渲染、--inject 标记区替换。

fixture 真值见 conftest.fake_table（160 项真实营养数据，不联网）；
classify 的品类计数：main 29 / side 13（去重后 12）/ drink 58 / other 60。
"""

import pytest

import tools.gen_quick_table as gen
from scripts.suggest import classify
from tools.gen_quick_table import (
    QUICK_TABLE_END,
    QUICK_TABLE_START,
    inject,
    main,
    pick_top,
    render_markdown,
)

#: render_markdown 钉死的表头（首行）
HEADER = "| 名称 | kcal | 蛋白(g) | 脂肪(g) | 碳水(g) | 钠(mg) |"


# ---------------------------------------------------------------------------
# pick_top
# ---------------------------------------------------------------------------


def test_pick_top_returns_exactly_40_and_covers_three_categories(fake_table):
    items = pick_top(fake_table)

    assert len(items) == 40
    categories = {classify(i.name) for i in items}
    assert {"main", "side", "drink"} <= categories


def test_pick_top_no_duplicate_names(fake_table):
    names = [i.name for i in pick_top(fake_table)]

    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def test_render_markdown_first_line_is_header(fake_table):
    markdown = render_markdown(pick_top(fake_table, n=5))
    lines = markdown.splitlines()

    assert lines[0] == HEADER
    assert lines[1].startswith("| ---")  # 第二行是分隔行
    assert len(lines) == 2 + 5  # 表头 + 分隔行 + 5 行数据
    # 数值紧凑：整数不带 ".0"
    assert ".0 |" not in lines[2]


def test_render_markdown_columns_match_brief(fake_table):
    row = render_markdown(pick_top(fake_table, n=1)).splitlines()[2]

    assert len(row.split("|")) == 8  # 两侧空 + 6 列


# ---------------------------------------------------------------------------
# inject（--inject 的核心：只替换标记区，区外文本不变）
# ---------------------------------------------------------------------------


def test_inject_replaces_only_marker_region(tmp_path):
    skill = tmp_path / "SKILL.md"
    before = "# 麦麦营养师\n\n模式说明文字。\n\n"
    after = "\n标记区之后的限制声明。\n"
    skill.write_text(
        before + QUICK_TABLE_START + "\n旧表内容\n" + QUICK_TABLE_END + after,
        encoding="utf-8",
    )

    inject(skill, "| 名称 | kcal |\n| --- | --- |\n| 巨无霸 | 553 |")

    text = skill.read_text(encoding="utf-8")
    assert text.startswith(before)
    assert text.endswith(after)
    assert "旧表内容" not in text
    assert "| 巨无霸 | 553 |" in text
    assert QUICK_TABLE_START in text and QUICK_TABLE_END in text


def test_inject_without_markers_raises(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("没有标记区的文件", encoding="utf-8")

    with pytest.raises(ValueError):
        inject(skill, "whatever")


# ---------------------------------------------------------------------------
# main：--inject 走标记区写入，默认写文件或 stdout
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_table(monkeypatch, fake_table):
    monkeypatch.setattr(gen, "_load_table", lambda: fake_table)
    return fake_table


def test_main_inject_writes_table_and_keeps_rest(tmp_path, patched_table):
    skill = tmp_path / "SKILL.md"
    head = "# 前置说明\n"
    tail = "# 限制声明\n"
    skill.write_text(
        head + QUICK_TABLE_START + "\n占位注释\n" + QUICK_TABLE_END + tail,
        encoding="utf-8",
    )

    assert main(None, inject_path=skill) == 0

    text = skill.read_text(encoding="utf-8")
    assert text.startswith(head)
    assert text.endswith(tail)
    assert "占位注释" not in text
    assert HEADER in text


def test_main_writes_out_path(tmp_path, patched_table):
    out = tmp_path / "table.md"

    assert main(out) == 0
    assert out.read_text(encoding="utf-8").splitlines()[0] == HEADER


def test_main_defaults_to_stdout(capsys, patched_table):
    assert main() == 0
    assert capsys.readouterr().out.splitlines()[0] == HEADER
