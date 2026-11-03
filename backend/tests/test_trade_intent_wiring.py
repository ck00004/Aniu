"""交易意图链路端到端回归测试。

背景：线上 `trade_intents` 长期为 0 行，一度被误判为"注册逻辑没有接线"。
实测证据是：`check_and_register_trade_intent` 的**唯一**调用点在
`skills/mx_core/execution.py::_handle_moni_trade`，而该文件在
`.gitignore` 之外、位于 `backend/skills/`，之前只在 `app/` 下 grep 才会漏掉。
线上为 0 行的真实原因是：意图拦截上线后（2026-10-30 13:04 上海）再没有
发生过任何 BUY/SELL/CANCEL（工具日志最后一条 trade 为 10-30 06:41 UTC）。

本文件把这条件链锁进测试：
1. 真实 handler 下单 → 必须落一条 `submitted` 意图，带远端单号与 run_id；
2. 同一信号第二次下单 → 必须被拦截（RuntimeError），且不产生第二条意图；
3. 撤单意图按委托单号全局唯一；
4. 超时/传输中断 → 意图收敛为 UNKNOWN 并阻塞后续同向委托；
5. 执行计划链路把 intent_id 贯通到 executed_action，用于回填 action_id。
"""

from datetime import datetime
from pathlib import Path
import sys

import httpx
import pytest
from sqlalchemy import select

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.database import session_scope
from app.db.models import StrategyRun, TradeIntent
from app.services.aniu_service import aniu_service
from app.services.execution_runner_service import execution_runner_service
from app.services.trade_intent_service import extract_order_id, trade_intent_service
from skills.mx_core import execution as mx_execution


class _FakeTradeClient:
    """最小可用的妙想客户端替身。"""

    def __init__(self, *, trade_result=None, trade_error=None, orders_rows=None):
        self.trade_calls: list[dict] = []
        self._trade_result = trade_result or {"orderID": "ORD-1001"}
        self._trade_error = trade_error
        self._orders_rows = orders_rows or []
        self.cancel_calls: list[dict] = []

    def trade(self, **kwargs):
        self.trade_calls.append(kwargs)
        if self._trade_error is not None:
            raise self._trade_error
        return self._trade_result

    def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        return {"orderID": kwargs.get("order_id")}

    def get_orders(self, **_kwargs):
        return {"data": {"rows": self._orders_rows}}


@pytest.fixture()
def trade_env(monkeypatch, tmp_path):
    """接管数据库、交易门禁与预检，返回 (client, run_id)。"""
    from app.core import rate_limit as rate_limit_module
    from app.core.config import get_settings
    from app.db import database as database_module

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "intents.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    rate_limit_module._limiter.reset()
    from app.db.database import init_db

    init_db()

    monkeypatch.setattr(mx_execution, "is_market_open", lambda: True)
    monkeypatch.setattr(mx_execution, "preflight_trade", lambda **_kw: None)
    monkeypatch.setattr(mx_execution, "preflight_cancel", lambda **_kw: None)
    # 每个用例前清空当天信号（否则跨用例互相阻塞）
    with session_scope() as db:
        run = StrategyRun(trigger_source="schedule", run_type="trade", status="running")
        db.add(run)
        db.flush()
        run_id = int(run.id)

    yield run_id

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def _execute_trade(client, run_id: int, **arguments) -> dict:
    payload = {"action": "BUY", "symbol": "600000", "quantity": 100, "price_type": "MARKET"}
    payload.update(arguments)
    return mx_execution.mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_moni_trade",
        arguments=payload,
        run_id=run_id,
    )


def test_trade_handler_registers_intent_before_order(trade_env) -> None:
    run_id = trade_env
    client = _FakeTradeClient(trade_result={"orderID": "ORD-1001"})

    result = _execute_trade(client, run_id)

    assert result["ok"] is True
    assert len(client.trade_calls) == 1
    intent_id = result["executed_action"]["intent_id"]

    with session_scope() as db:
        intents = list(db.scalars(select(TradeIntent)).all())
    assert len(intents) == 1
    intent = intents[0]
    assert intent.id == intent_id
    assert intent.status == "submitted"
    assert intent.provider_order_id == "ORD-1001"
    assert intent.run_id == run_id
    assert intent.side == "BUY"
    assert intent.symbol == "600000"
    assert intent.tranche_id == 1
    assert intent.intent_key.endswith(":600000:BUY:1")


def test_duplicate_trade_signal_is_blocked_and_never_reaches_remote(trade_env) -> None:
    run_id = trade_env
    client = _FakeTradeClient(trade_result={"orderID": "ORD-1001"})
    _execute_trade(client, run_id)

    # 工具层把拦截原因收敛成 ok=False（模型在工具结果里看到原因），不抛异常
    blocked = _execute_trade(client, run_id)
    assert blocked["ok"] is False
    assert "未完成" in blocked["error"] or "重复" in blocked["error"]
    # 第二次被挡在本地，不能产生第二次远端下单
    assert len(client.trade_calls) == 1
    with session_scope() as db:
        assert len(list(db.scalars(select(TradeIntent)).all())) == 1


def test_trade_timeout_marks_intent_unknown_and_blocks(trade_env) -> None:
    run_id = trade_env
    client = _FakeTradeClient(
        trade_error=httpx.TimeoutException("read timeout"),
        orders_rows=[],
    )

    failed = _execute_trade(client, run_id)
    assert failed["ok"] is False
    assert "状态未知" in failed["error"]

    with session_scope() as db:
        intent = db.scalars(select(TradeIntent)).one()
    assert intent.status == "unknown"

    # UNKNOWN 必须阻塞同标的同方向后续委托（at-most-once）
    blocked = _execute_trade(client, run_id)
    assert blocked["ok"] is False
    assert "未完成" in blocked["error"] or "重复" in blocked["error"]
    assert len(client.trade_calls) == 1


def test_cancel_intent_is_unique_per_order(trade_env) -> None:
    client = _FakeTradeClient()
    first = mx_execution.mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_moni_cancel",
        arguments={"cancel_type": "order", "order_id": "ORD-1001"},
        run_id=trade_env,
    )
    assert first["ok"] is True

    second = mx_execution.mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_moni_cancel",
        arguments={"cancel_type": "order", "order_id": "ORD-1001"},
        run_id=trade_env,
    )
    assert second["ok"] is False
    assert "撤单意图" in second["error"]
    assert len(client.cancel_calls) == 1


def test_intent_action_id_backfill_links_plan_action(trade_env) -> None:
    run_id = trade_env
    client = _FakeTradeClient(trade_result={"orderID": "ORD-2002"})
    result = _execute_trade(client, run_id)
    intent_id = result["executed_action"]["intent_id"]

    # 模拟"计划动作成功执行 + 回填 action_id"这一环
    from app.db.models import StrategyRunAction

    with session_scope() as db:
        action = StrategyRunAction(
            run_id=run_id,
            sequence_no=1,
            phase="execution",
            tool_name="mx_moni_trade",
            action_type="BUY",
            status="succeeded",
        )
        db.add(action)
        db.flush()
        action_id = int(action.id)

    execution_runner_service._backfill_intent_action_id(
        run_id=run_id,
        action_id=action_id,
        executed_action={"action": "BUY", "symbol": "600000", "intent_id": intent_id},
    )

    with session_scope() as db:
        intent = db.get(TradeIntent, intent_id)
    assert intent.action_id == action_id


def test_order_id_extraction_handles_nested_payloads() -> None:
    assert extract_order_id({"orderID": "A1"}) == "A1"
    assert extract_order_id({"data": {"orderId": "A2"}}) == "A2"
    assert extract_order_id({"data": {"result": {"entrust_no": "A3"}}}) == "A3"
    assert extract_order_id({"nothing": True}) is None


def test_finalize_warns_when_trade_has_no_intent_row(trade_env, caplog) -> None:
    """安全网：执行了买卖但没有意图行时必须留下告警（历史上就发生过）。"""
    import logging

    run_id = trade_env
    with session_scope() as db:
        with caplog.at_level(logging.WARNING, logger="app.services.aniu_service"):
            aniu_service._warn_on_unregistered_trade_intents(
                db,
                run_id=run_id,
                executed_actions=[
                    {"action": "BUY", "symbol": "600000", "intent_id": 999}
                ],
            )
    assert any(
        "trade executed without registered intent" in record.message
        for record in caplog.records
    )


def test_finalize_silent_when_intent_exists(trade_env, caplog) -> None:
    import logging

    run_id = trade_env
    client = _FakeTradeClient(trade_result={"orderID": "ORD-3003"})
    result = _execute_trade(client, run_id)
    with session_scope() as db:
        with caplog.at_level(logging.WARNING, logger="app.services.aniu_service"):
            aniu_service._warn_on_unregistered_trade_intents(
                db, run_id=run_id, executed_actions=[result["executed_action"]]
            )
    assert not any(
        "trade executed without registered intent" in record.message
        for record in caplog.records
    )


def test_generic_skill_path_propagates_run_id(trade_env) -> None:
    """通用 skill 路径（context 传 run_id）也必须把意图挂到这次运行上。

    历史缺陷：`skills/mx_core/handler.py::handle` 调用 execute_tool 时丢掉了
    run_id，导致意图的 run_id 为 NULL、审计链断开。
    """
    from skills.mx_core.handler import Skill

    run_id = trade_env
    client = _FakeTradeClient(trade_result={"orderID": "ORD-4004"})
    result = Skill().handle(
        tool_name="mx_moni_trade",
        arguments={
            "action": "BUY",
            "symbol": "600519",
            "quantity": 100,
            "price_type": "MARKET",
        },
        context={"client": client, "app_settings": None, "run_type": "trade", "run_id": run_id},
    )

    assert result["ok"] is True
    with session_scope() as db:
        intent = db.scalars(select(TradeIntent)).one()
    assert intent.run_id == run_id
    assert intent.symbol == "600519"
