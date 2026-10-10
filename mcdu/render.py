"""双通道输出：``--json`` 走机器可读 JSON，否则打印中文人读文本。

所有 CLI 子命令的输出/退出码约定集中在这里：
- :func:`emit` 正常输出（stdout）；:func:`fail` 错误输出（stderr）后退出。
- 退出码：0 成功 / 1 业务错误 / 2 参数错误。
"""

from __future__ import annotations

import json
import sys
from typing import NoReturn

#: 成功
EXIT_OK = 0
#: 业务错误（数据不可得、无匹配等）
EXIT_BUSINESS = 1
#: 参数错误（缺参、坏数量等）
EXIT_USAGE = 2


def emit(payload: dict, human: str, as_json: bool) -> None:
    """正常输出到 stdout。

    ``as_json=True`` 打印 ``json.dumps(payload, ensure_ascii=False, indent=2)``
    （中文不转义）；否则原样打印人读文本 ``human``。
    """
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(human)


def fail(msg: str, code: int = EXIT_BUSINESS) -> NoReturn:
    """错误信息打到 stderr 后以 ``code`` 退出进程。"""
    print(msg, file=sys.stderr)
    sys.exit(code)
