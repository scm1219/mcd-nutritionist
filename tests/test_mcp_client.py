"""mcdu.mcp_client 的单元测试：全部走注入的 fake session，不触网。"""

import json
from pathlib import Path

import pytest

from mcdu.mcp_client import (
    McduAuthError,
    McduError,
    McpClient,
    McpRateLimitError,
    load_token,
    parse_env_file,
    parse_original_response,
)

FIXTURES = Path(__file__).parent / "fixtures"

OK_PAYLOAD = {
    "success": True,
    "code": 200,
    "message": "请求成功",
    "data": {"list": []},
}


def make_client(fake_session, **kwargs):
    return McpClient(token="test-token", session=fake_session, **kwargs)


def test_parse_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 这是注释\n"
        "\n"
        "MCD_MCP_TOKEN=abc123\n"
        "OTHER_KEY = spaced value\n",
        encoding="utf-8",
    )
    assert parse_env_file(str(env)) == {
        "MCD_MCP_TOKEN": "abc123",
        "OTHER_KEY": "spaced value",
    }


def test_parse_original_response():
    envelope = json.loads((FIXTURES / "nutrition_raw.txt").read_text(encoding="utf-8"))
    text = envelope["result"]["content"][0]["text"]
    parsed = parse_original_response(text)
    assert isinstance(parsed, dict)
    assert parsed["success"] is True
    assert isinstance(parsed["data"], str)  # data 字段为 CSV 字符串
    assert "productName" in parsed["data"]
    assert "巨无霸" in parsed["data"]


def test_load_token_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    # 指到不存在的 .env：仓库根可能真实存在含 Token 的 .env，测试不得读到它
    monkeypatch.setattr("mcdu.mcp_client.ENV_FILE", tmp_path / ".env")
    with pytest.raises(McduError):
        load_token()


def test_call_tool_401(fake_session):
    fake_session.responses.append(fake_session.status(401))
    client = make_client(fake_session)
    with pytest.raises(McduAuthError, match="Token 无效或过期"):
        client.call_tool("get-order-list", {})
    assert len(fake_session.requests) == 1  # 401 不重试


def test_call_tool_429_then_ok(fake_session, monkeypatch):
    sleeps = []
    monkeypatch.setattr("mcdu.mcp_client.time.sleep", lambda s: sleeps.append(s))
    fake_session.responses.append(fake_session.status(429))
    fake_session.responses.append(fake_session.json(OK_PAYLOAD))
    client = make_client(fake_session)

    result = client.call_tool("get-order-list", {"pageIndex": 1})

    assert result == OK_PAYLOAD
    assert len(fake_session.requests) == 2  # 首发 1 次 + 重试计数 == 1
    assert sleeps == [0.5]

    # 请求形状：URL / 头 / JSON-RPC 体
    first = fake_session.requests[0]
    assert first["url"] == "https://mcp.mcd.cn"
    assert first["headers"]["Authorization"] == "Bearer test-token"
    assert first["headers"]["Content-Type"] == "application/json"
    assert "text/event-stream" in first["headers"]["Accept"]
    assert first["json"]["jsonrpc"] == "2.0"
    assert first["json"]["method"] == "tools/call"
    assert first["json"]["params"]["name"] == "get-order-list"
    assert first["json"]["params"]["arguments"] == {"pageIndex": 1}
    assert first["timeout"] == 15


def test_call_tool_429_exhausted(fake_session, monkeypatch):
    sleeps = []
    monkeypatch.setattr("mcdu.mcp_client.time.sleep", lambda s: sleeps.append(s))
    for _ in range(4):  # 首发 + 3 次重试，全部 429
        fake_session.responses.append(fake_session.status(429))
    client = make_client(fake_session)

    with pytest.raises(McpRateLimitError):
        client.call_tool("get-nutrition-list")

    assert len(fake_session.requests) == 4  # 恰好用尽重试预算，不多发
    assert sleeps == [0.5, 1.0, 2.0]  # 指数退避 0.5/1/2s


def test_call_tool_success_false(fake_session):
    bad = {"success": False, "code": 1001, "message": "参数非法"}
    fake_session.responses.append(fake_session.json(bad))
    client = make_client(fake_session)

    with pytest.raises(McduError) as excinfo:
        client.call_tool("get-order-list")

    assert not isinstance(excinfo.value, (McduAuthError, McpRateLimitError))
    assert excinfo.value.code == 1001
    assert "参数非法" in excinfo.value.message


def test_call_tool_sse_response(fake_session):
    """真实服务可能以 SSE 返回：取首个 data: 行再 json.loads。"""
    fake_session.responses.append(fake_session.sse(OK_PAYLOAD))
    client = make_client(fake_session)
    assert client.call_tool("get-nutrition-list") == OK_PAYLOAD
