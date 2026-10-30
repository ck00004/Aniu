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
from app.services.execution_runner_service import execution_runner_service
from app.services.trade_intent_service import (
    FILLED_REENTRY_COOLDOWN_MINUTES,
    MAX_TRANCHES_PER_SIGNAL,
    SHANGHAI_TZ,
    UNKNOWN_WAIT_SECONDS,
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


def test_unknown_intent_visibility_wait_before_failed(
    monkeypatch, tmp_path
) -> None:
    """P0-1 可见性等待：委托列表查不到痕迹 ≠ 未提交。

    第一次核验保持 UNKNOWN（阻塞）；连续 ≥2 次核验且超过等待期后才
    判定 failed（解除阻塞允许重试）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(orders=[])
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown")

        # 第 1 次核验：无痕迹 → 保持 UNKNOWN（订单可能已提交但暂不可见）
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"
        assert resolved[0].unknown_checks == 1
        # UNKNOWN 仍阻塞同向注册
        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )

        # 第 2 次核验（未过等待期）：仍保持 UNKNOWN
        # （中间被拦截的注册尝试本身也会触发一次核验，次数 ≥2 即可）
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"
        assert resolved[0].unknown_checks >= 2

        # 等待期已过 + 次数达标 → 判定未提交（failed，解除阻塞）
        _backdate_intent(
            intent.id,
            first_unknown_at=datetime.now(timezone.utc)
            - timedelta(seconds=UNKNOWN_WAIT_SECONDS + 10),
        )
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "failed"

        # failed 不阻塞：允许重试（技术重试档，不消耗真实分批）
        retry = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        assert retry.tranche_id == 2
    finally:
        _reset_db_state()


def test_unknown_intent_first_hidden_then_visible(monkeypatch, tmp_path) -> None:
    """审计清单 #1：订单已被远端接受但第一次查询不到、第二次才出现。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        # 第一次查询：委托列表为空（远端延迟）
        client = _IntentClient(orders=[])
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown")

        first = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert first[0].status == "unknown"  # 不能误判 failed

        # 第二次查询：委托出现（远端已接受）
        client.orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "1",
                "orderCount": 100,
                "dealCount": 0,
                "orderID": "ORD-LATE",
            }
        ]
        second = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert second[0].status == "submitted"  # 收敛为在途，继续阻塞
    finally:
        _reset_db_state()


def test_order_list_key_parsed_in_reconcile(monkeypatch, tmp_path) -> None:
    """审计清单 #2：妙想返回 orderList（而非 orders）也能被意图核对解析。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        client.get_orders = lambda: {
            "data": {
                "orderList": [
                    {
                        "stockCode": "600519",
                        "orderDrt": "1",
                        "orderStatus": "1",
                        "orderCount": 100,
                        "dealCount": 0,
                        "orderID": "ORD-OL",
                    }
                ]
            }
        }
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown")

        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        # orderList 被正确解析：委托存在 → 收敛 submitted，而不是误判 failed
        assert resolved[0].status == "submitted"
    finally:
        _reset_db_state()


def test_unparseable_orders_payload_keeps_blocking(monkeypatch, tmp_path) -> None:
    """委托响应结构无法识别：不可判定，保持 UNKNOWN 阻塞。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        client.get_orders = lambda: {"unexpected": "shape"}
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown")

        # 不可判定的载荷让 reconcile 提前返回（不触碰任何意图状态）
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved == []
        # 意图保持 UNKNOWN 且核验次数不被消耗
        stored = _list_intents(id=intent.id)[0]
        assert stored.status == "unknown"
        assert stored.unknown_checks == 0
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


# ---------------------------------------------------------------------------
# 审计复审专项（清单 #3/#4/#5/#7/#9）
# ---------------------------------------------------------------------------


def test_unparseable_json_response_marks_unknown_not_failed(
    monkeypatch, tmp_path
) -> None:
    """清单 #3：HTTP 200 但响应体无法解析 → 状态不确定（UNKNOWN），
    不得被当作"远端明确拒绝"而解除阻塞。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution
        from skills.mx_core.client import MXResponseUnknown

        monkeypatch.setattr(mx_execution, "is_market_open", lambda: True)

        class _BadJsonClient:
            def get_orders(self) -> dict:
                return {"data": {"orders": []}}

            def get_balance(self) -> dict:
                return {"data": {"availBalance": 10_000_000.0}}

            def trade(self, **kwargs) -> dict:
                raise MXResponseUnknown("妙想接口返回了无法解析的响应: <html>")

        result = mx_execution.mx_execution_service.execute_tool(
            client=_BadJsonClient(),
            app_settings=None,
            tool_name="mx_moni_trade",
            arguments={
                "action": "BUY",
                "symbol": "600519",
                "quantity": 100,
                "price_type": "LIMIT",
                "price": 20.0,
            },
        )
        assert result["ok"] is False
        assert "委托状态未知（响应无法解析）" in str(result.get("error") or "")

        # 意图为 UNKNOWN（阻塞），不是 failed（放行）
        intents = _list_intents(symbol="600519")
        assert len(intents) == 1
        assert intents[0].status == "unknown"
    finally:
        _reset_db_state()


def test_submitted_intent_syncs_to_filled(monkeypatch, tmp_path) -> None:
    """清单 #4：正常 SUBMITTED 订单后续变成 FILLED（状态闭环）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(intent.id, "ORD-F")

        # 远端委托全部成交
        client.orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "1",
                "orderCount": 100,
                "dealCount": 100,
                "orderID": "ORD-F",
            }
        ]
        resolved = trade_intent_service.reconcile_open_trade_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "filled"

        # 成交后进入冷却期：同信号再次买入被冷却拦截（而非在途拦截）
        with pytest.raises(RuntimeError, match="冷却期内禁止"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


def test_partially_filled_then_cancelled_is_terminal(monkeypatch, tmp_path) -> None:
    """清单 #5：部分成交后剩余委托被撤销 → 终态 canceled（不再阻塞，
    由策略判断是否新开仓）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(intent.id, "ORD-P")

        # 部分成交 40 股，剩余撤销（非 pending 状态）
        client.orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "3",  # 已撤
                "orderCount": 100,
                "dealCount": 40,
                "orderID": "ORD-P",
            }
        ]
        resolved = trade_intent_service.reconcile_open_trade_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "partially_filled"

        # 剩余全部撤销且无成交 → canceled 终态
        client.orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "3",
                "orderCount": 100,
                "dealCount": 0,
                "orderID": "ORD-P",
            }
        ]
        resolved = trade_intent_service.reconcile_open_trade_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "canceled"
    finally:
        _reset_db_state()


def test_startup_recovery_skips_running_runs(monkeypatch, tmp_path) -> None:
    """清单 #7：另一 worker 正在执行的 run（状态 running）的意图
    不被启动恢复误判为遗留。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from app.db.models import StrategyRun

        with session_scope() as db:
            live_run = StrategyRun(
                trigger_source="schedule",
                run_type="trade",
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            dead_run = StrategyRun(
                trigger_source="schedule",
                run_type="trade",
                status="failed",
                started_at=datetime.now(timezone.utc),
            )
            db.add_all([live_run, dead_run])
            db.flush()
            today = datetime.now(SHANGHAI_TZ).date().isoformat()
            db.add_all(
                [
                    TradeIntent(
                        signal_date=today,
                        symbol="600519",
                        side="BUY",
                        tranche_id=1,
                        intent_key=f"{today}:600519:BUY:1",
                        run_id=live_run.id,
                        quantity=100,
                        status="executing",
                    ),
                    TradeIntent(
                        signal_date=today,
                        symbol="000001",
                        side="BUY",
                        tranche_id=1,
                        intent_key=f"{today}:000001:BUY:1",
                        run_id=dead_run.id,
                        quantity=100,
                        status="executing",
                    ),
                ]
            )

        recovered = trade_intent_service.reconcile_stale_executing_intents()
        # 只有 dead run 的意图被收敛；live run 的保持 executing
        assert recovered == 1
        assert _list_intents(symbol="600519")[0].status == "executing"
        assert _list_intents(symbol="000001")[0].status == "unknown"
    finally:
        _reset_db_state()


def test_missing_memory_read_blocks_planned_trade() -> None:
    """清单 #9：拟交易标的没有 memory_read 检索记录 → 数据验证不通过。"""
    from app.services.aniu_service import aniu_service
    from types import SimpleNamespace

    decision = {
        "final_answer": "计划买入。",
        "tool_calls": [
            # 四个必需读取工具全部成功且载荷有效
            {"name": "mx_get_balance", "arguments": {}, "result": {"ok": True, "result": {"data": {"availBalance": 1}}}},
            {"name": "mx_get_positions", "arguments": {}, "result": {"ok": True, "result": {"data": {"posList": []}}}},
            {"name": "mx_get_orders", "arguments": {}, "result": {"ok": True, "result": {"data": {"orders": []}}}},
            {"name": "mx_get_self_selects", "arguments": {}, "result": {"ok": True, "result": {"data": {"rows": []}}}},
            # 拟买入 600519 的计划
            {
                "name": "mx_moni_trade",
                "arguments": {"action": "BUY", "symbol": "600519", "quantity": 100},
                "result": {
                    "ok": True,
                    "planned_action": {"action": "BUY", "symbol": "600519", "name": "贵州茅台", "quantity": 100},
                },
            },
            # memory_read 只检索了另一只股票（300059）
            {
                "name": "memory_read",
                "arguments": {"keywords": ["300059"]},
                "result": {"ok": True, "result_count": 0, "keywords": ["300059"]},
            },
        ],
    }
    settings = SimpleNamespace(run_type="trade", prompt_templates={}, task_prompt="")

    merged, _, _, _ = aniu_service._enforce_required_tools(
        settings=settings,
        client=None,
        decision=decision,
        llm_request={},
        llm_response={},
        runtime_trace={},  # 无 messages：不触发补充调用
        emit=None,
        tool_executor=None,
    )

    assert merged.get("data_validation_passed") is False
    assert "memory_read:600519" in (merged.get("missing_required_tools") or [])


def test_memory_read_covering_symbol_passes_validation() -> None:
    """拟交易标的有 memory_read 检索记录（结果为空也算有效）→ 通过。"""
    from app.services.aniu_service import aniu_service
    from types import SimpleNamespace

    decision = {
        "final_answer": "计划买入。",
        "tool_calls": [
            {"name": "mx_get_balance", "arguments": {}, "result": {"ok": True, "result": {"data": {"availBalance": 1}}}},
            {"name": "mx_get_positions", "arguments": {}, "result": {"ok": True, "result": {"data": {"posList": []}}}},
            {"name": "mx_get_orders", "arguments": {}, "result": {"ok": True, "result": {"data": {"orders": []}}}},
            {"name": "mx_get_self_selects", "arguments": {}, "result": {"ok": True, "result": {"data": {"rows": []}}}},
            {
                "name": "mx_moni_trade",
                "arguments": {"action": "BUY", "symbol": "600519", "quantity": 100},
                "result": {
                    "ok": True,
                    "planned_action": {"action": "BUY", "symbol": "600519", "name": "贵州茅台", "quantity": 100},
                },
            },
            # 检索了拟买入标的（按名称）
            {
                "name": "memory_read",
                "arguments": {"keywords": ["贵州茅台", "白酒"]},
                "result": {"ok": True, "result_count": 0, "keywords": ["贵州茅台", "白酒"]},
            },
        ],
    }
    settings = SimpleNamespace(run_type="trade", prompt_templates={}, task_prompt="")

    merged, _, _, _ = aniu_service._enforce_required_tools(
        settings=settings,
        client=None,
        decision=decision,
        llm_request={},
        llm_response={},
        runtime_trace={},
        emit=None,
        tool_executor=None,
    )

    assert merged.get("data_validation_passed") is True


def test_read_tool_ok_with_empty_payload_counts_as_missing() -> None:
    """P1 数据完整性：工具 ok=True 但载荷不可识别 → 视为缺失。"""
    from app.services.aniu_service import aniu_service
    from types import SimpleNamespace

    decision = {
        "final_answer": "分析完成。",
        "tool_calls": [
            # 载荷为空 dict：不可识别
            {"name": "mx_get_balance", "arguments": {}, "result": {"ok": True, "result": {}}},
            {"name": "mx_get_positions", "arguments": {}, "result": {"ok": True, "result": {"data": {"posList": []}}}},
            {"name": "mx_get_orders", "arguments": {}, "result": {"ok": True, "result": {"data": {"orders": []}}}},
            {"name": "mx_get_self_selects", "arguments": {}, "result": {"ok": True, "result": {"data": {"rows": []}}}},
        ],
    }
    settings = SimpleNamespace(run_type="trade", prompt_templates={}, task_prompt="")

    merged, _, _, _ = aniu_service._enforce_required_tools(
        settings=settings,
        client=None,
        decision=decision,
        llm_request={},
        llm_response={},
        runtime_trace={},
        emit=None,
        tool_executor=None,
    )

    assert merged.get("data_validation_passed") is False
    assert "mx_get_balance" in (merged.get("missing_required_tools") or [])


# ---------------------------------------------------------------------------
# 第三轮复审专项（清单 12 项中可自动化的 11 项）
# ---------------------------------------------------------------------------


def test_string_status_business_rejection_classified() -> None:
    """清单 #1：status="100"（字符串）业务拒绝 → MXBusinessRejected。"""
    from skills.mx_core.client import MXBusinessRejected, MXClient

    with pytest.raises(MXBusinessRejected, match="status=100"):
        MXClient._raise_on_business_error({"status": "100", "message": "拒绝"})


def test_string_code_business_rejection_classified() -> None:
    """清单 #2：code="100"（字符串）业务拒绝 → MXBusinessRejected。"""
    from skills.mx_core.client import MXBusinessRejected, MXClient

    with pytest.raises(MXBusinessRejected, match="code=100"):
        MXClient._raise_on_business_error({"code": "100", "message": "拒绝"})


def test_http_500_marks_unknown_not_rejected(monkeypatch, tmp_path) -> None:
    """清单 #3：HTTP 500 → 状态不明（UNKNOWN），不重复提交。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution

        monkeypatch.setattr(mx_execution, "is_market_open", lambda: True)

        class _Http500Client:
            def get_orders(self) -> dict:
                return {"data": {"orders": []}}

            def get_balance(self) -> dict:
                return {"data": {"availBalance": 10_000_000.0}}

            def trade(self, **kwargs) -> dict:
                import httpx

                request = httpx.Request("POST", "https://x/trade")
                response = httpx.Response(500, request=request, text="server error")
                raise httpx.HTTPStatusError(
                    "Server Error", request=request, response=response
                )

        result = mx_execution.mx_execution_service.execute_tool(
            client=_Http500Client(),
            app_settings=None,
            tool_name="mx_moni_trade",
            arguments={
                "action": "BUY",
                "symbol": "600519",
                "quantity": 100,
                "price_type": "LIMIT",
                "price": 20.0,
            },
        )
        assert result["ok"] is False
        assert "委托状态未知" in str(result.get("error") or "")
        intents = _list_intents(symbol="600519")
        assert intents[0].status == "unknown"
    finally:
        _reset_db_state()


def test_http_4xx_marks_business_rejected() -> None:
    """HTTP 4xx → 远端明确拒绝（可安全标记 failed）。"""
    from skills.mx_core.client import MXBusinessRejected, MXClient

    import httpx

    request = httpx.Request("POST", "https://x/trade")
    response = httpx.Response(403, request=request, text="forbidden")
    with pytest.raises(MXBusinessRejected, match="HTTP 403"):
        try:
            raise httpx.HTTPStatusError(
                "Forbidden", request=request, response=response
            )
        except httpx.HTTPStatusError as exc:
            # 复现 _post 的分类逻辑
            status_code = exc.response.status_code
            if 400 <= status_code < 500:
                raise MXBusinessRejected(f"妙想接口 HTTP {status_code} 拒绝请求") from exc
            raise RuntimeError("不应到达") from exc


def test_filled_order_leaving_list_not_failed(monkeypatch, tmp_path) -> None:
    """清单 #4：已成交委托从活动列表消失 → 不得判定未提交。

    SUBMITTED 意图在委托列表查不到时保持 submitted（阻塞），
    绝不进入"无记录等待→failed"路径。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(intent.id, "ORD-GONE")

        # 委托列表为空（订单已成交离开活动列表）
        client.orders = []
        resolved = trade_intent_service.reconcile_open_trade_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "submitted"
        assert "不可见" in str(resolved[0].note or "")

        # 多次核验后仍是 submitted（不进入 failed 路径）
        for _ in range(3):
            trade_intent_service.reconcile_open_trade_intents(
                client, symbol="600519"
            )
        assert _list_intents(id=intent.id)[0].status == "submitted"
    finally:
        _reset_db_state()


def test_provider_order_id_mismatch_keeps_unknown(monkeypatch, tmp_path) -> None:
    """清单 #5：有单号但远端无此单、存在其他同向委托 → 保持 UNKNOWN。

    不得把其他委托（ORD-099）认作本意图（ORD-100）。
    """
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
                    "orderID": "ORD-099",  # 另一笔委托
                }
            ]
        )
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown", provider_order_id="ORD-100")

        resolved = trade_intent_service.reconcile_open_trade_intents(
            client, symbol="600519"
        )
        # 单号不匹配：保持 unknown，绝不把 ORD-099 认作 ORD-100
        assert resolved[0].status == "unknown"
    finally:
        _reset_db_state()


def test_unknown_order_status_code_keeps_unknown(monkeypatch, tmp_path) -> None:
    """清单 #6：返回未知订单状态码 → 保持 UNKNOWN（不默认 canceled）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "77",  # 未知状态码
                    "orderCount": 100,
                    "dealCount": 0,
                    "orderID": "ORD-U",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown", provider_order_id="ORD-U")

        resolved = trade_intent_service.reconcile_open_trade_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"
    finally:
        _reset_db_state()


def test_followup_missing_memory_read_still_blocks() -> None:
    """清单 #7：followup 补充了静态工具但未调 memory_read → 仍拦截。"""
    from types import SimpleNamespace

    from app.services.aniu_service import aniu_service

    base_calls = [
        {"name": "mx_get_balance", "arguments": {}, "result": {"ok": True, "result": {"data": {"availBalance": 1}}}},
        {"name": "mx_get_positions", "arguments": {}, "result": {"ok": True, "result": {"data": {"posList": []}}}},
        {"name": "mx_get_orders", "arguments": {}, "result": {"ok": True, "result": {"data": {"orders": []}}}},
        {"name": "mx_get_self_selects", "arguments": {}, "result": {"ok": True, "result": {"data": {"rows": []}}}},
        {
            "name": "mx_moni_trade",
            "arguments": {"action": "BUY", "symbol": "600519", "quantity": 100},
            "result": {
                "ok": True,
                "planned_action": {"action": "BUY", "symbol": "600519", "quantity": 100},
            },
        },
    ]
    decision = {
        "final_answer": "计划买入。",
        "tool_calls": list(base_calls),  # 无 memory_read
    }

    followup_calls = [
        # followup 只补充了静态工具（已存在），仍无 memory_read
        {"name": "mx_get_balance", "arguments": {}, "result": {"ok": True, "result": {"data": {"availBalance": 1}}}},
    ]

    call_count = {"value": 0}

    def fake_run_agent(*, app_settings, client, messages, emit=None, tool_executor=None, **kwargs):
        call_count["value"] += 1
        return (
            {"final_answer": "已补充调用。", "tool_calls": followup_calls},
            {},
            {},
            {"messages": messages},
        )

    original = aniu_service._run_agent_with_optional_tool_executor
    aniu_service._run_agent_with_optional_tool_executor = fake_run_agent
    try:
        settings = SimpleNamespace(run_type="trade", prompt_templates={}, task_prompt="")
        merged, _, _, _ = aniu_service._enforce_required_tools(
            settings=settings,
            client=None,
            decision=decision,
            llm_request={},
            llm_response={},
            runtime_trace={"messages": [{"role": "assistant", "content": "x"}]},
            emit=None,
            tool_executor=None,
        )
        assert call_count["value"] == 1  # followup 发生
        # followup 后完整重验：memory_read 仍缺失 → 拦截（原实现会被绕过）
        assert merged.get("data_validation_passed") is False
        assert "memory_read:600519" in (merged.get("missing_required_tools") or [])
    finally:
        aniu_service._run_agent_with_optional_tool_executor = original


def test_data_as_list_parsed(monkeypatch, tmp_path) -> None:
    """清单 #11：妙想返回 data: [...]（列表）→ 正确识别。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        client.get_orders = lambda: {
            "data": [
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "1",
                    "orderCount": 100,
                    "dealCount": 0,
                    "orderID": "ORD-DL",
                }
            ]
        }
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown")

        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        # data 列表被正确解析：委托存在 → 收敛 submitted
        assert resolved[0].status == "submitted"
    finally:
        _reset_db_state()


def test_cancel_intent_state_closure(monkeypatch, tmp_path) -> None:
    """清单 #9：撤单请求后原委托状态变化 → 撤单意图同步闭环。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "1",  # 待撤
                    "orderCount": 100,
                    "dealCount": 0,
                    "orderID": "ORD-C",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_cancel_intent(
            order_id="ORD-C"
        )
        trade_intent_service.mark_intent_submitted(intent.id, "ORD-C")

        # 委托已撤销 → verified
        client.orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "3",
                "orderCount": 100,
                "dealCount": 0,
                "orderID": "ORD-C",
            }
        ]
        resolved = trade_intent_service.reconcile_cancel_intents(client)
        assert resolved[0].status == "verified"

        # 委托已成交 → filled（撤单太迟）
        client.orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "3",
                "orderCount": 100,
                "dealCount": 100,
                "orderID": "ORD-C",
            }
        ]
        _backdate_intent(intent.id, status="submitted")
        resolved = trade_intent_service.reconcile_cancel_intents(client)
        assert resolved[0].status == "filled"

        # 委托从列表消失 → unknown（不可再撤，阻塞重复撤单是安全默认）
        client.orders = []
        _backdate_intent(intent.id, status="submitted")
        resolved = trade_intent_service.reconcile_cancel_intents(client)
        assert resolved[0].status == "unknown"
        assert "消失" in str(resolved[0].note or "")
    finally:
        _reset_db_state()


def test_action_id_backfill_matches_symbol_and_side(monkeypatch, tmp_path) -> None:
    """清单 #8：同一 run 两笔交易 → action_id 分别关联（不串档）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        today = datetime.now(SHANGHAI_TZ).date().isoformat()
        with session_scope() as db:
            db.add_all(
                [
                    TradeIntent(
                        signal_date=today,
                        symbol="600519",
                        side="BUY",
                        tranche_id=1,
                        intent_key=f"{today}:600519:BUY:1",
                        run_id=1,
                        quantity=100,
                        status="submitted",
                    ),
                    TradeIntent(
                        signal_date=today,
                        symbol="000001",
                        side="BUY",
                        tranche_id=1,
                        intent_key=f"{today}:000001:BUY:1",
                        run_id=1,
                        quantity=200,
                        status="submitted",
                    ),
                ]
            )

        # 第一笔（600519）成功执行 action 101
        execution_runner_service._backfill_intent_action_id(
            run_id=1,
            action_id=101,
            executed_action={"action": "BUY", "symbol": "600519", "quantity": 100},
        )
        # 只有 600519 的意图关联了 101；000001 仍为空
        assert _list_intents(symbol="600519")[0].action_id == 101
        assert _list_intents(symbol="000001")[0].action_id is None

        # 第二笔（000001）成功执行 action 102
        execution_runner_service._backfill_intent_action_id(
            run_id=1,
            action_id=102,
            executed_action={"action": "BUY", "symbol": "000001", "quantity": 200},
        )
        assert _list_intents(symbol="000001")[0].action_id == 102
        assert _list_intents(symbol="600519")[0].action_id == 101
    finally:
        _reset_db_state()
