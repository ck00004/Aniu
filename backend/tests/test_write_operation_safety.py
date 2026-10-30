"""写操作安全专项测试（补丁 C 意图表，对应外部审计回归清单）。

场景编号对应审计第十一节清单：
#1/#2 同信号重复买入拦截（在途/已成交）
#3   同 SELL 信号重复触发拦截
#4   超时 UNKNOWN 不自动重发（见 test_tool_audit_fixes）
#6   成交后冷却期满的新分批允许
#7   退出后次日新机会允许
#9   同一委托重复撤单拦截
#10  崩溃后重启恢复（EXECUTING → UNKNOWN 阻塞）
#11  并发注册被唯一约束拦截
#12  合法分批交易不被误拦
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

import pytest
from sqlalchemy import select

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import TradeIntent
from app.services.trade_intent_service import (
    FILLED_REENTRY_COOLDOWN_MINUTES,
    MAX_TRANCHES_PER_SIGNAL,
    SHANGHAI_TZ,
    trade_intent_service,
)


def _use_temp_db(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None


def _reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


class _IntentClient:
    """意图服务桩：可编程的委托列表。"""

    def __init__(self, orders: list[dict[str, Any]] | None = None) -> None:
        self.orders = orders or []

    def get_orders(self) -> dict:
        return {"data": {"orders": self.orders}}


def _list_intents(**filters) -> list[TradeIntent]:
    with session_scope() as db:
        stmt = select(TradeIntent)
        for key, value in filters.items():
            stmt = stmt.where(getattr(TradeIntent, key) == value)
        rows = list(db.scalars(stmt.order_by(TradeIntent.id)).all())
        for row in rows:
            db.expunge(row)
        return rows


def _backdate_intent(intent_id: int, **fields) -> None:
    with session_scope() as db:
        intent = db.get(TradeIntent, intent_id)
        for key, value in fields.items():
            setattr(intent, key, value)
        db.add(intent)


# ---------------------------------------------------------------------------
# 清单 #1/#2：同信号重复买入拦截
# ---------------------------------------------------------------------------


def test_buy_intent_blocks_repeat_while_in_flight(monkeypatch, tmp_path) -> None:
    """在途（submitted）意图：同标的同方向再次注册被拦截。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        first = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(first.id, "ORD-1")

        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


def test_buy_intent_blocks_repeat_after_fill_within_cooldown(
    monkeypatch, tmp_path
) -> None:
    """清单 #1：买单全部成交后，冷却期内同信号再次买入被拦截。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        first = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(first.id, "ORD-1")
        _backdate_intent(first.id, status="filled")

        with pytest.raises(RuntimeError, match="冷却期内禁止"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


def test_buy_intent_blocks_repeat_after_partial_fill(monkeypatch, tmp_path) -> None:
    """清单 #2：部分成交（partially_filled 是阻塞状态）再次买入被拦截。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        first = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(first.id, status="partially_filled")

        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 清单 #6/#12：冷却期满后的合法新分批
# ---------------------------------------------------------------------------


def test_new_tranche_allowed_after_cooldown(monkeypatch, tmp_path) -> None:
    """清单 #6/#12：前一档已成交且冷却期已过 → 允许建立第 2 档。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        first = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(first.id, "ORD-1")
        # 成交时间拨回冷却期之前
        _backdate_intent(
            first.id,
            status="filled",
            updated_at=datetime.now(timezone.utc)
            - timedelta(minutes=FILLED_REENTRY_COOLDOWN_MINUTES + 10),
        )

        second = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=200
        )
        assert second.tranche_id == 2
        assert second.intent_key.endswith(":2")

        # 第 2 档在途时第 3 档被拦截
        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


def test_max_tranches_per_day_enforced(monkeypatch, tmp_path) -> None:
    """每日每标的每方向分批上限。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        past = datetime.now(timezone.utc) - timedelta(
            minutes=FILLED_REENTRY_COOLDOWN_MINUTES + 10
        )
        for tranche in range(1, MAX_TRANCHES_PER_SIGNAL + 1):
            intent = trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
            assert intent.tranche_id == tranche
            _backdate_intent(intent.id, status="filled", updated_at=past)

        with pytest.raises(RuntimeError, match="分批上限"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 清单 #3：SELL 信号重复
# ---------------------------------------------------------------------------


def test_sell_intent_blocks_repeat_signal(monkeypatch, tmp_path) -> None:
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        first = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="002475", side="SELL", quantity=5000
        )
        trade_intent_service.mark_intent_submitted(first.id, "SELL-1")

        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="002475", side="SELL", quantity=5000
            )
        # BUY 方向不受 SELL 意图影响
        buy = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="002475", side="BUY", quantity=100
        )
        assert buy.side == "BUY"
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 清单 #7：退出后次日新机会
# ---------------------------------------------------------------------------


def test_next_day_new_signal_allowed(monkeypatch, tmp_path) -> None:
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        # 昨日的意图（signal_date 为昨天）
        yesterday = (
            datetime.now(SHANGHAI_TZ) - timedelta(days=1)
        ).date().isoformat()
        with session_scope() as db:
            db.add(
                TradeIntent(
                    signal_date=yesterday,
                    symbol="600519",
                    side="BUY",
                    tranche_id=1,
                    intent_key=f"{yesterday}:600519:BUY:1",
                    quantity=100,
                    status="filled",
                )
            )
        # 今日新信号：允许
        today = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        assert today.tranche_id == 1  # 新的一天新信号，从第 1 档开始
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 清单 #9：同一委托重复撤单
# ---------------------------------------------------------------------------


def test_cancel_intent_is_unique_per_order(monkeypatch, tmp_path) -> None:
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        first = trade_intent_service.check_and_register_cancel_intent(
            order_id="ORD-777"
        )
        trade_intent_service.mark_intent_submitted(first.id, "ORD-777")

        with pytest.raises(RuntimeError, match="不得重复撤单"):
            trade_intent_service.check_and_register_cancel_intent(order_id="ORD-777")
        # 失败的撤单意图允许重试（复用原行，重置为 executing）
        _backdate_intent(first.id, status="failed")
        retry = trade_intent_service.check_and_register_cancel_intent(
            order_id="ORD-777"
        )
        assert retry.id == first.id
        assert retry.status == "executing"
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 清单 #10：崩溃后重启恢复
# ---------------------------------------------------------------------------


def test_restart_recovers_stale_executing_intents_to_unknown(
    monkeypatch, tmp_path
) -> None:
    """进程崩溃时正在发送的意图（executing）重启后收敛为 UNKNOWN 并阻塞。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "1",
                    "orderCount": 100,
                    "dealCount": 0,
                    "orderID": "ORD-88",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        # 模拟崩溃：意图停留在 executing

        recovered = trade_intent_service.reconcile_stale_executing_intents()
        assert recovered == 1
        assert _list_intents(id=intent.id)[0].status == "unknown"

        # UNKNOWN 阻塞同标的同方向
        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 清单 #11：并发注册被唯一约束拦截
# ---------------------------------------------------------------------------


def test_concurrent_registration_blocked_by_unique_constraint(
    monkeypatch, tmp_path
) -> None:
    """两个执行者同时注册同一 (信号, 档位)：第二个被唯一约束拒绝。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        # 直接预插同一键，模拟第一个执行者已提交
        today = datetime.now(SHANGHAI_TZ).date().isoformat()
        with session_scope() as db:
            db.add(
                TradeIntent(
                    signal_date=today,
                    symbol="600519",
                    side="BUY",
                    tranche_id=1,
                    intent_key=f"{today}:600519:BUY:1",
                    quantity=100,
                    status="executing",
                )
            )
        # 第二个执行者查询时会被阻塞状态拦截（唯一约束在并发窗口兜底）
        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# UNKNOWN 核对收敛
# ---------------------------------------------------------------------------


def test_unknown_intent_reconciles_to_filled_via_orders(monkeypatch, tmp_path) -> None:
    """UNKNOWN 意图通过委托列表核对：已全部成交 → filled（冷却生效）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "1",
                    "orderCount": 100,
                    "dealCount": 100,
                    "orderID": "ORD-9",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown", provider_order_id="ORD-9")

        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert len(resolved) == 1
        assert resolved[0].status == "filled"

        # 收敛为 filled 后进入冷却期，同信号再次买入被冷却拦截
        with pytest.raises(RuntimeError, match="冷却期内禁止"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


def test_unknown_intent_reconciles_to_failed_without_trace(
    monkeypatch, tmp_path
) -> None:
    """UNKNOWN 意图核对：委托列表无痕迹 → 判定未提交（failed，允许重试）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(orders=[])
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown")

        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "failed"

        # failed 不阻塞：允许重试（新档）
        retry = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        assert retry.tranche_id == 2
    finally:
        _reset_db_state()


def test_failed_intent_allows_retry_new_tranche(monkeypatch, tmp_path) -> None:
    """远端明确拒绝（failed）：不阻塞，允许重试。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        first = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_failed(first.id, "远端拒绝")

        retry = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        assert retry.tranche_id == 2
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 补丁 D：后端权威执行报告
# ---------------------------------------------------------------------------


def test_backend_execution_report_reflects_all_states() -> None:
    from app.services.aniu_service import aniu_service

    decision = {
        "consistency_analysis": {
            "trade": {
                "gap_detected": True,
                "claimed_changes": [
                    {"action": "SELL", "target": "立讯精密"},
                ],
            }
        },
        "missing_required_tools": ["mx_get_balance"],
    }
    executed_actions = [
        {
            "action": "BUY",
            "symbol": "600519",
            "quantity": 100,
            "status": "submitted",
        },
        {
            "action": "CANCEL",
            "order_id": "ORD-1",
            "status": "cancel_requested",
        },
        {
            "action": "MANAGE_SELF_SELECT",
            "query": "将600519加入自选股",
            "verified": True,
        },
        {
            "action": "MANAGE_SELF_SELECT",
            "query": "将000001从自选股删除",
            "verified": False,
            "noop": False,
        },
        {
            "action": "MANAGE_SELF_SELECT",
            "query": "将600030加入自选股",
            "noop": True,
        },
    ]
    blocked_write_drafts = [{"tool_name": "mx_moni_trade", "action_type": "SELL"}]
    execution_summary = {"unresolved_count": 1}

    report = aniu_service._build_backend_execution_report(
        decision=decision,
        executed_actions=executed_actions,
        blocked_write_drafts=blocked_write_drafts,
        execution_summary=execution_summary,
    )

    assert "系统执行结果（后端权威记录）" in report
    assert "交易执行（SUBMITTED）：BUY 600519 100股" in report
    assert "撤单请求（SUBMITTED）：撤单 ORD-1" in report
    assert "已回查确认（VERIFIED）：将600519加入自选股" in report
    assert "状态未确认（UNKNOWN）：将000001从自选股删除" in report
    assert "NOOP" in report
    assert "NOT_CALLED" in report
    assert "SELL 立讯精密" in report
    assert "BLOCKED_MISSING_DATA" in report
    assert "未落地计划动作（FAILED/UNKNOWN）：1 条" in report
    assert "必需工具缺失（mx_get_balance）" in report


def test_backend_execution_report_empty_run() -> None:
    from app.services.aniu_service import aniu_service

    report = aniu_service._build_backend_execution_report(
        decision={},
        executed_actions=[],
        blocked_write_drafts=[],
        execution_summary={"unresolved_count": 0},
    )

    assert "交易执行（SUBMITTED）：无" in report
    assert "自选股变更：无" in report
    assert "NOT_CALLED" not in report
    assert "BLOCKED_MISSING_DATA" not in report


def test_execution_report_payload_structure() -> None:
    """补丁 D：结构化执行报告（前端卡片数据源）。"""
    from app.services.aniu_service import aniu_service

    decision = {
        "consistency_analysis": {
            "trade": {
                "gap_detected": True,
                "claimed_changes": [{"action": "SELL", "target": "立讯精密"}],
            }
        },
        "missing_required_tools": ["mx_get_balance"],
        "data_validation_passed": False,
    }
    executed_actions = [
        {"action": "BUY", "symbol": "600519", "quantity": 100},
        {"action": "CANCEL", "order_id": "ORD-1", "stock_code": "600519"},
        {"action": "MANAGE_SELF_SELECT", "query": "将600519加入自选股", "verified": True},
        {"action": "MANAGE_SELF_SELECT", "query": "将000001从自选股删除", "verified": False, "noop": False},
        {"action": "MANAGE_SELF_SELECT", "query": "将600030加入自选股", "noop": True},
    ]
    payload = aniu_service._build_execution_report_payload(
        decision=decision,
        executed_actions=executed_actions,
        blocked_write_drafts=[{"tool_name": "mx_moni_trade", "action_type": "SELL"}],
        execution_summary={"unresolved_count": 1},
    )

    assert payload["schema"] == "backend_execution_report_v1"
    assert payload["overall_status"] == "NOT_CALLED"
    assert payload["trades"] == [
        {"symbol": "600519", "action": "BUY", "quantity": 100, "status": "SUBMITTED"}
    ]
    assert payload["cancels"][0]["order_id"] == "ORD-1"
    assert [item["status"] for item in payload["self_selects"]] == [
        "VERIFIED",
        "UNKNOWN",
        "NOOP",
    ]
    assert payload["not_called_claims"] == [{"action": "SELL", "target": "立讯精密"}]
    assert payload["blocked_actions"][0]["tool_name"] == "mx_moni_trade"
    assert payload["unresolved_count"] == 1
    assert payload["missing_required_tools"] == ["mx_get_balance"]
    assert payload["data_validation_passed"] is False


def test_execution_report_payload_clean_run() -> None:
    from app.services.aniu_service import aniu_service

    payload = aniu_service._build_execution_report_payload(
        decision={},
        executed_actions=[],
        blocked_write_drafts=[],
        execution_summary={"unresolved_count": 0},
    )
    assert payload["overall_status"] == "NO_ACTION"
    assert payload["verified"] is False
    assert payload["trades"] == []
    assert payload["data_validation_passed"] is True
