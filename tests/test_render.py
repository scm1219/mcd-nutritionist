"""mcdu.render 的单元测试：emit 双通道输出与 fail 的退出码语义。"""

import json

import pytest

from mcdu.render import EXIT_BUSINESS, EXIT_OK, EXIT_USAGE, emit, fail


def test_exit_code_constants():
    assert (EXIT_OK, EXIT_BUSINESS, EXIT_USAGE) == (0, 1, 2)


def test_emit_json_is_loadable_and_chinese_literal(capsys):
    payload = {"餐品": "双层吉士汉堡", "能量": 429}
    emit(payload, "人类可读", as_json=True)
    captured = capsys.readouterr()
    assert captured.err == ""  # 正常输出走 stdout
    assert json.loads(captured.out) == payload
    # ensure_ascii=False：中文原样输出，不出现 \uXXXX 转义
    assert "\\u" not in captured.out
    assert "双层吉士汉堡" in captured.out


def test_emit_json_indent_two(capsys):
    emit({"a": 1}, "x", as_json=True)
    out = capsys.readouterr().out
    assert out == '{\n  "a": 1\n}\n'  # indent=2 + 换行


def test_emit_human_prints_as_is(capsys):
    emit({"a": 1}, "中文营养报告", as_json=False)
    captured = capsys.readouterr()
    assert captured.out == "中文营养报告\n"
    assert captured.err == ""


def test_fail_prints_stderr_and_exits_with_code(capsys):
    with pytest.raises(SystemExit) as exc:
        fail("参数错误：无法解析数量", EXIT_USAGE)
    assert exc.value.code == EXIT_USAGE
    captured = capsys.readouterr()
    assert "无法解析数量" in captured.err
    assert captured.out == ""  # 错误不污染 stdout


def test_fail_defaults_to_business_code(capsys):
    with pytest.raises(SystemExit) as exc:
        fail("业务失败")
    assert exc.value.code == EXIT_BUSINESS
    assert "业务失败" in capsys.readouterr().err
