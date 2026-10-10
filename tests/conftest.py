"""共享测试夹具：可编程的假 requests.Session、由 fixture 构建的营养假表。"""

import json
from pathlib import Path

import pytest


class FakeResponse:
    """requests.Response 的最小替身：只有 status_code 与 text。"""

    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    """可编程假 session：预置 responses 队列，逐个弹出；记录每次请求便于断言。

    - responses: list[FakeResponse]，按请求顺序消费，耗尽后再来请求即断言失败
      （用于证明重试次数没有被突破）。
    - requests: list[dict]，记录 post() 的 url/headers/json/timeout。
    - json(payload) / sse(payload) / status(code)：构造常见响应的便捷方法。
    """

    def __init__(self):
        self.responses = []
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append(
            {"url": url, "headers": dict(headers or {}), "json": json, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("FakeSession 收到超出预置数量的请求（重试次数突破上限）")
        return self.responses.pop(0)

    @staticmethod
    def status(status_code):
        """只有状态码、无正文的响应（401/429 等）。"""
        return FakeResponse(status_code, "")

    @staticmethod
    def _wrap(structured):
        result = {
            "content": [{"type": "text", "text": "(mock) tool output"}],
            "isError": False,
            "structuredContent": structured,
        }
        return json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": result}, ensure_ascii=False
        )

    @classmethod
    def json(cls, structured):
        """200 + 普通 JSON-RPC 信封（structuredContent 为业务对象）。"""
        return FakeResponse(200, cls._wrap(structured))

    @classmethod
    def sse(cls, structured):
        """200 + SSE 流式正文（event: 行 + data: 行），结构与真实服务一致。"""
        return FakeResponse(200, "event: message\r\ndata: " + cls._wrap(structured) + "\r\n\r\n")


@pytest.fixture
def fake_session():
    return FakeSession()


@pytest.fixture(scope="session")
def fake_table():
    """由 fixture 真实数据构建的 NutritionTable（160 项，不联网）。

    tests/fixtures/nutrition_raw.txt 是 list-nutrition-foods 的完整工具输出，
    这里复用 Task 1 的信封解包 + Task 2 的 CSV 解析拿到条目。
    """
    from mcdu.mcp_client import parse_original_response
    from mcdu.nutrition import NutritionTable, parse_nutrition_data

    fixture = Path(__file__).parent / "fixtures" / "nutrition_raw.txt"
    envelope = json.loads(fixture.read_text(encoding="utf-8"))
    text = envelope["result"]["content"][0]["text"]
    data = parse_original_response(text)["data"]
    return NutritionTable(parse_nutrition_data(data), "2026-10-10T00:00:00+00:00")
