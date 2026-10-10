"""``order confirm/status/cancel`` 测试：假 client 回放下单/查询/取消，不触网。

安全门是本任务的核心不变量：
- confirm 无 ``--yes-place-order`` → 退出码 1 且**绝不**调用 create-order，
  草稿文件保留（可加旗标重试）；
- cancel 无 ``--yes-cancel`` → 退出码 1 且绝不调用 cancel-order；
- 过期/不存在草稿 → 退出码 1 且提示重新 draft（同样不下单）。

响应字段形态：create-order 的订单号/支付链接字段名未经真实下单实录，
测试里自造最小响应（orderId / payUrl），实现侧原样打印支付相关字段并把
raw 全量写入 data/orders_log.json（TODO 首单实录后回填精确字段名）。
"""

import json
import time
from pathlib import Path

import pytest

import cli
from mcdu.mcp_client import McduError
from mcdu.orderflow import Draft, DraftStore
from scripts.order import (
    CANCEL_ORDER_TOOL,
    CREATE_ORDER_TOOL,
    QUERY_ORDER_TOOL,
    require_yes,
)

STORE = "1450244"
ORDERS_LOG = Path("orders_log.json")


# ---------------------------------------------------------------------------
# 构造工具：假草稿 / 假响应 / 假 client
# ---------------------------------------------------------------------------


def _save_draft(
    tmp_path: Path,
    draft_id: str = "abcd1234",
    mode: str = "pickup",
    be_code: str | None = None,
    need_tableware: bool = True,
    age_seconds: float = 0.0,
) -> None:
    """往 tmp_path 落一份最小合法草稿（DraftStore 真实落盘，含 TTL 语义）。"""
    address_id = "ADDR7" if mode == "delivery" else None
    draft = Draft(
        draft_id=draft_id,
        created_at=time.time() - age_seconds,
        mode=mode,
        store_code=STORE,
        items=[
            {"productCode": "M1", "quantity": 1},
            {"productCode": "F1", "quantity": 2, "couponId": "COUPON9"},
        ],
        address_id=address_id,
        analysis={"totals": {"energy_kcal": 718.0}},
        price={"amount": 36.5},
        be_code=be_code if mode == "delivery" else None,
        need_tableware=need_tableware,
    )
    DraftStore(tmp_path).save(draft)


#: 自造最小 create-order 响应：orderId + 疑似支付链接（字段名首单实录前未定）
_CREATE_RAW = {
    "success": True,
    "code": 200,
    "data": {
        "orderId": "ORD20261010001",
        "orderDetail": {"expirePayTime": "2026-10-10 12:30:00"},
        "payUrl": "https://pay.mcd.cn/ORD20261010001",
    },
}

_QUERY_RAW = {
    "success": True,
    "data": {
        "orderId": "ORD20261010001",
        "createTime": "2026-10-10 12:15:00",
        "orderStatus": "待支付",
        "expirePayTime": "2026-10-10 12:30:00",
    },
}

_CANCEL_RAW = {
    "success": True,
    "data": {"cancelResult": True, "orderId": "ORD20261010001"},
}


class FakeOrderClient:
    """McpClient 替身：回放 create-order/query-order/cancel-order，记录调用。"""

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if name == CREATE_ORDER_TOOL:
            return _CREATE_RAW
        if name == QUERY_ORDER_TOOL:
            return _QUERY_RAW
        if name == CANCEL_ORDER_TOOL:
            return _CANCEL_RAW
        raise AssertionError(f"意外工具调用：{name}")

    def tool_names(self) -> set[str]:
        return {name for name, _ in self.calls}


class FailingCreateClient(FakeOrderClient):
    """create-order 必失败的假 client（业务错误路径）。"""

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        raise McduError("ORDER", "门店暂不接单")


def _patch_cli(monkeypatch, tmp_path, fake) -> None:
    monkeypatch.setattr(cli, "McpClient", lambda: fake)
    monkeypatch.setattr(cli, "DRAFT_DIR", tmp_path)
    monkeypatch.setattr(cli, "ORDERS_LOG_PATH", tmp_path / ORDERS_LOG)


# ---------------------------------------------------------------------------
# require_yes：安全门单元测试
# ---------------------------------------------------------------------------


def test_require_yes_passes_when_flag_true():
    require_yes(True, "真实下单")  # 不抛即通过


def test_require_yes_blocks_when_flag_false(capsys):
    with pytest.raises(SystemExit) as exc:
        require_yes(False, "真实下单")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "已阻止 真实下单" in err
    assert "--yes" in err  # 提示加 --yes* 旗标


# ---------------------------------------------------------------------------
# confirm：安全门 / 草稿门 / 成功路径
# ---------------------------------------------------------------------------


def test_confirm_without_yes_blocks_and_keeps_draft(capsys, monkeypatch, tmp_path):
    """无 --yes-place-order：打印将执行的摘要，退出码 1，绝不 create-order。"""
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)
    _save_draft(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cli.main(["order", "confirm", "--draft-id", "abcd1234"])
    assert exc.value.code == 1

    assert fake.calls == []  # 安全断言：未收到任何工具调用（含 create-order）
    captured = capsys.readouterr()
    # 拒绝信息为全局约束固定格式（--yes* 为字面量）；具体旗标名在 stdout 摘要里
    assert "已阻止" in captured.err
    assert "确认请加 --yes* 旗标" in captured.err
    assert "--yes-place-order" in captured.out
    # stdout 摘要让人能看清将执行什么（门店/品项/价格）
    assert STORE in captured.out
    assert "36.50" in captured.out
    assert (tmp_path / "draft_abcd1234.json").is_file()  # 草稿保留可重试


def test_confirm_missing_draft_exits_1(capsys, monkeypatch, tmp_path):
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)

    with pytest.raises(SystemExit) as exc:
        cli.main(["order", "confirm", "--draft-id", "nosuchid", "--yes-place-order"])
    assert exc.value.code == 1
    assert fake.calls == []  # 草稿不存在：连 client 都不必动
    err = capsys.readouterr().err
    assert "nosuchid" in err
    assert "重新" in err  # 提示重新 draft
    assert "draft" in err


def test_confirm_expired_draft_exits_1(capsys, monkeypatch, tmp_path):
    """过期草稿（> 30 分钟）视同不存在：退出 1、提示重新 draft、不下单。"""
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)
    _save_draft(tmp_path, age_seconds=1801)

    with pytest.raises(SystemExit) as exc:
        cli.main(["order", "confirm", "--draft-id", "abcd1234", "--yes-place-order"])
    assert exc.value.code == 1
    assert fake.calls == []
    assert "重新" in capsys.readouterr().err
    assert not (tmp_path / "draft_abcd1234.json").exists()  # DraftStore.load 已删过期文件


def test_confirm_success_pickup_calls_create_and_logs(capsys, monkeypatch, tmp_path):
    """pickup 快乐路径：create-order 参数全部取自草稿；写 orders_log；删草稿。"""
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)
    _save_draft(tmp_path)

    assert (
        cli.main(["order", "confirm", "--draft-id", "abcd1234", "--yes-place-order"]) == 0
    )

    # 只有一次 create-order（不重新算价、不查菜单），items 原样来自草稿
    assert fake.calls == [
        (
            CREATE_ORDER_TOOL,
            {
                "storeCode": STORE,
                "beType": 1,
                "orderType": 1,
                "items": [
                    {"productCode": "M1", "quantity": 1},
                    {"productCode": "F1", "quantity": 2, "couponId": "COUPON9"},
                ],
                "needTableware": True,
            },
        )
    ]

    # orders_log：[{draft_id, order_id, created_at, raw}]，中文/URL 不转义
    log_path = tmp_path / ORDERS_LOG
    assert log_path.is_file()
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(log) == 1
    assert log[0]["draft_id"] == "abcd1234"
    assert log[0]["order_id"] == "ORD20261010001"
    assert isinstance(log[0]["created_at"], float)
    assert log[0]["raw"] == _CREATE_RAW
    assert "\\u" not in log_path.read_text(encoding="utf-8")

    # 草稿已删（防复用二次下单）
    assert not (tmp_path / "draft_abcd1234.json").exists()

    # 人读输出：订单号 + 支付相关字段原样打印
    out = capsys.readouterr().out
    assert "ORD20261010001" in out
    assert "https://pay.mcd.cn/ORD20261010001" in out
    assert "status" in out  # 下一步提示


def test_confirm_success_delivery_payload_from_draft(capsys, monkeypatch, tmp_path):
    """delivery：addressId/beCode/needTableware 全部读自草稿（单一事实源）。"""
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)
    _save_draft(tmp_path, mode="delivery", be_code="B1450244", need_tableware=False)

    assert (
        cli.main(["order", "confirm", "--draft-id", "abcd1234", "--yes-place-order"]) == 0
    )

    assert len(fake.calls) == 1  # 只有 create-order
    args = fake.calls[0][1]
    assert args["storeCode"] == STORE
    assert args["beType"] == 2
    assert args["orderType"] == 2
    assert args["addressId"] == "ADDR7"
    assert args["beCode"] == "B1450244"
    assert args["needTableware"] is False  # --no-tableware 在 draft 阶段捕获


def test_confirm_appends_to_existing_log(capsys, monkeypatch, tmp_path):
    """orders_log 是追加语义：已有旧流水时保留并追加新条目。"""
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)
    old_entry = {
        "draft_id": "old00001",
        "order_id": "ORD_OLD",
        "created_at": 1.0,
        "raw": {"success": True},
    }
    (tmp_path / ORDERS_LOG).write_text(
        json.dumps([old_entry], ensure_ascii=False), encoding="utf-8"
    )
    _save_draft(tmp_path)

    assert (
        cli.main(["order", "confirm", "--draft-id", "abcd1234", "--yes-place-order"]) == 0
    )

    log = json.loads((tmp_path / ORDERS_LOG).read_text(encoding="utf-8"))
    assert len(log) == 2
    assert log[0] == old_entry  # 旧流水原样保留
    assert log[1]["draft_id"] == "abcd1234"


def test_confirm_create_failure_keeps_draft_and_no_log(capsys, monkeypatch, tmp_path):
    """create-order 业务失败：退出 1、草稿保留（可重试）、不写 orders_log。"""
    fake = FailingCreateClient()
    _patch_cli(monkeypatch, tmp_path, fake)
    _save_draft(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cli.main(["order", "confirm", "--draft-id", "abcd1234", "--yes-place-order"])
    assert exc.value.code == 1
    assert "门店暂不接单" in capsys.readouterr().err
    assert (tmp_path / "draft_abcd1234.json").is_file()  # 草稿保留
    assert not (tmp_path / ORDERS_LOG).exists()  # 未写流水


def test_confirm_json_payload(capsys, monkeypatch, tmp_path):
    """--json：订单号与原始返回在 payload 里，中文不转义。"""
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)
    _save_draft(tmp_path)

    assert (
        cli.main(
            ["order", "confirm", "--draft-id", "abcd1234", "--yes-place-order", "--json"]
        )
        == 0
    )
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["order_id"] == "ORD20261010001"
    assert payload["draft_id"] == "abcd1234"
    assert payload["order"] == _CREATE_RAW  # 原始返回透传
    assert "\\u" not in captured


# ---------------------------------------------------------------------------
# status / cancel
# ---------------------------------------------------------------------------


def test_status_passthrough_query_order(capsys, monkeypatch, tmp_path):
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)

    assert cli.main(["order", "status", "--order-id", "ORD20261010001", "--json"]) == 0

    assert fake.calls == [(QUERY_ORDER_TOOL, {"orderId": "ORD20261010001"})]
    payload = json.loads(capsys.readouterr().out)
    assert payload["order_id"] == "ORD20261010001"
    assert payload["order"] == _QUERY_RAW  # query-order 结果透传


def test_status_human_render(capsys, monkeypatch, tmp_path):
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)

    assert cli.main(["order", "status", "--order-id", "ORD20261010001"]) == 0

    out = capsys.readouterr().out
    assert "ORD20261010001" in out
    assert "待支付" in out  # 关键字段人读可见


def test_cancel_without_yes_blocked(capsys, monkeypatch, tmp_path):
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)

    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["order", "cancel", "--order-id", "ORD20261010001", "--reason", "1"]
        )
    assert exc.value.code == 1
    assert fake.calls == []  # 绝不 cancel-order
    captured = capsys.readouterr()
    assert "已阻止" in captured.err
    assert "确认请加 --yes* 旗标" in captured.err
    assert "--yes-cancel" in captured.out  # 具体旗标名在 stdout 预览里
    assert "ORD20261010001" in captured.out  # stdout 提示将取消哪单


def test_cancel_with_yes_calls_cancel_order(capsys, monkeypatch, tmp_path):
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)

    assert (
        cli.main(
            [
                "order", "cancel", "--order-id", "ORD20261010001",
                "--reason", "2", "--yes-cancel",
            ]
        )
        == 0
    )

    assert fake.calls == [
        (CANCEL_ORDER_TOOL, {"orderId": "ORD20261010001", "cancelReasonCode": "2"})
    ]
    out = capsys.readouterr().out
    assert "ORD20261010001" in out
    assert "取消" in out


def test_cancel_json_payload(capsys, monkeypatch, tmp_path):
    fake = FakeOrderClient()
    _patch_cli(monkeypatch, tmp_path, fake)

    assert (
        cli.main(
            ["order", "cancel", "--order-id", "ORD20261010001",
             "--reason", "2", "--yes-cancel", "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["order_id"] == "ORD20261010001"
    assert payload["cancel"] == _CANCEL_RAW
