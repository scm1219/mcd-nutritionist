"""麦当劳 MCP（open.mcd.cn/mcp 申请 Token）的无状态 JSON-RPC 客户端。

约定：
- 端点 ``https://mcp.mcd.cn``，Bearer Token 鉴权，超时 15s。
- 401 不重试，直接报清晰错误；429 指数退避（0.5/1/2s）重试，耗尽报限流错误。
- 响应可能是普通 JSON，也可能是 SSE（``event:`` 开头），后者取首个 ``data:`` 行。
- 工具输出的业务对象在 ``result.structuredContent``；若只有 markdown 文本，
  按 ``## Original Response`` 分割取后半解析。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

TOKEN_ENV_VAR = "MCD_MCP_TOKEN"

#: 仓库根的 .env（mcdu/ 包的上一级），load_token 的回退读取位置
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

AUTH_ERROR_MESSAGE = "Token 无效或过期，请到 open.mcd.cn/mcp 重新申请"

#: 429 退避基数：第 n 次重试前睡 BACKOFF_BASE_SECONDS * 2**n（0.5/1/2s）
BACKOFF_BASE_SECONDS = 0.5


class McduError(Exception):
    """mcdu 通用错误：code 为错误码（业务码或字符串标签），message 为人读信息。"""

    def __init__(self, code: Any = None, message: str = ""):
        self.code = code
        self.message = message or str(code)
        super().__init__(self.message)


class McduAuthError(McduError):
    """401：Token 无效或过期。"""

    def __init__(self):
        super().__init__(401, AUTH_ERROR_MESSAGE)


class McpRateLimitError(McduError):
    """429：指数退避重试耗尽后仍被限流。"""

    def __init__(self, retries: int = 3):
        super().__init__(429, f"请求被限流（429），退避重试 {retries} 次后仍失败")


def parse_env_file(path: str) -> dict[str, str]:
    """解析 .env 格式文件：``KEY=VALUE`` 行，支持 ``#`` 注释行与空行。"""
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def load_token() -> str:
    """读取 Bearer Token：环境变量 ``MCD_MCP_TOKEN`` 优先，其次仓库根 ``.env``（``ENV_FILE``）。

    :raises McduError: 两处都未配置时。
    """
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if token:
        return token
    if ENV_FILE.is_file():
        token = parse_env_file(str(ENV_FILE)).get(TOKEN_ENV_VAR, "").strip()
        if token:
            return token
    raise McduError(
        "CONFIG",
        "缺少 MCD_MCP_TOKEN：请设置环境变量，或复制 .env.example 为 .env 并填写"
        "（Token 请到 open.mcd.cn/mcp 申请）",
    )


def parse_original_response(text: str) -> Any:
    """从工具输出的 markdown 文本中取 ``## Original Response`` 之后的原始 JSON。"""
    _, _, remainder = text.partition("## Original Response")
    return json.loads(remainder.strip())


def _first_sse_data(text: str) -> str:
    """SSE 正文（以 ``event:`` 开头）取首个 ``data:`` 行；普通正文原样返回。"""
    if text.lstrip().startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                return line[len("data:"):].strip()
    return text


class McpClient:
    """麦当劳 MCP 的极薄 JSON-RPC 客户端，只做 ``tools/call``。"""

    def __init__(
        self,
        base_url: str = "https://mcp.mcd.cn",
        token: str | None = None,
        timeout: float = 15,
        retries: int = 3,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url
        self.token = token if token is not None else load_token()
        self.timeout = timeout
        self.retries = retries
        self.session = session if session is not None else requests.Session()
        self._next_id = 1

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """调用 MCP 工具并返回解析后的业务 JSON 对象。

        :raises McduAuthError: 401（Token 无效或过期）。
        :raises McpRateLimitError: 429 且重试耗尽。
        :raises McduError: 业务 ``success:false``、网络异常或响应无法解析。
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        self._next_id += 1
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        for attempt in range(self.retries + 1):  # 首发 + 至多 retries 次重试
            try:
                response = self.session.post(
                    self.base_url, headers=headers, json=payload, timeout=self.timeout
                )
            except requests.RequestException as exc:
                raise McduError("NETWORK", f"网络请求失败：{exc}") from exc
            if response.status_code == 401:
                raise McduAuthError()
            if response.status_code == 429:
                if attempt >= self.retries:
                    raise McpRateLimitError(self.retries)
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            if response.status_code != 200:
                raise McduError(
                    f"HTTP_{response.status_code}",
                    f"HTTP 状态码异常：{response.status_code}",
                )
            return self._unwrap(self._decode(response.text))
        raise McduError("INTERNAL", "重试循环意外退出")  # pragma: no cover

    def _decode(self, text: str) -> Any:
        try:
            return json.loads(_first_sse_data(text))
        except json.JSONDecodeError as exc:
            raise McduError("RESPONSE", f"响应不是合法 JSON：{exc}") from exc

    def _unwrap(self, envelope: Any) -> Any:
        if not isinstance(envelope, dict):
            raise McduError("RESPONSE", f"响应结构异常：{envelope!r}")
        if "error" in envelope:  # JSON-RPC 层错误
            err = envelope["error"] or {}
            raise McduError(err.get("code"), err.get("message") or "JSON-RPC 调用失败")
        result = envelope.get("result")
        if isinstance(result, dict):
            structured = result.get("structuredContent")
            payload = structured if isinstance(structured, dict) else self._payload_from_content(result)
        else:
            payload = envelope  # 非信封响应：按业务对象直接处理
        if isinstance(payload, dict) and payload.get("success") is False:
            raise McduError(payload.get("code"), payload.get("message") or "请求失败")
        return payload

    @staticmethod
    def _payload_from_content(result: dict) -> Any:
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if "## Original Response" in text:
                    try:
                        return parse_original_response(text)
                    except json.JSONDecodeError as exc:
                        raise McduError("RESPONSE", f"Original Response 解析失败：{exc}") from exc
        raise McduError("RESPONSE", "响应 result 中没有可解析的业务数据")
