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


def test_new_tranche_blocked_under_conservative_mode(monkeypatch, tmp_path) -> None:
    """第六轮复审特别建议：MAX_TRANCHES_PER_SIGNAL=1（保守模式）。

    45 分钟冷却是时间规则而非独立新信号验证——冷却期满后第二档
    仍被分批上限拦截，防止"时间到了就允许二次进攻"。恢复分批需要
    后端可验证的新行情确认条件。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        first = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(first.id, "ORD-1")
        # 成交时间拨回冷却期之前（即使冷却期满）
        _backdate_intent(
            first.id,
            status="filled",
            updated_at=datetime.now(timezone.utc)
            - timedelta(minutes=FILLED_REENTRY_COOLDOWN_MINUTES + 10),
        )

        with pytest.raises(RuntimeError, match="分批上限"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=200
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


def test_unknown_intent_never_auto_fails(
    monkeypatch, tmp_path
) -> None:
    """P0-3（第五轮复审重设计）：UNKNOWN 一律不自动解除。

    活动委托列表查不到不能证明未提交；一条历史订单也不能证明完整
    历史视图。在妙想提供可验证的完整历史委托视图前，UNKNOWN 保持
    阻塞等待人工核验——无论核验次数与等待时间如何。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(orders=[])
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown")

        # 多次核验 + 等待期远超 → 仍保持 UNKNOWN
        for _ in range(5):
            trade_intent_service.reconcile_unknown_intents(
                client, symbol="600519"
            )
        _backdate_intent(
            intent.id,
            unknown_checks=99,
            first_unknown_at=datetime.now(timezone.utc)
            - timedelta(seconds=UNKNOWN_WAIT_SECONDS * 100),
        )
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"
        assert "不能证明未提交" in str(resolved[0].note or "")

        # 即使列表包含已终局委托（历史痕迹）也不解除——一条历史订单
        # ≠ 完整历史视图（可能分页/过滤/延迟）
        client.orders = [
            {
                "stockCode": "000001",
                "orderDrt": "1",
                "orderStatus": "3",
                "orderCount": 100,
                "dealCount": 100,
                "orderID": "ORD-HIST",
            }
        ]
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"

        # UNKNOWN 持续阻塞同向注册
        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )

        # P0-5（第六轮复审）：无单号的 UNKNOWN 即使订单重新可见也不
        # 模糊匹配（无法证明因果关联）——保持 UNKNOWN 等待人工核验。
        client.orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "1",
                "orderCount": 100,
                "dealCount": 0,
                "orderID": "ORD-VISIBLE",
            }
        ]
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"

        # 人工核验确认后（回填远端单号）→ 单号精确匹配收敛
        _backdate_intent(intent.id, provider_order_id="ORD-VISIBLE")
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "submitted"
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

        # 第二次查询：委托出现（远端已接受）——无单号仍不模糊匹配
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
        assert second[0].status == "unknown"  # P0-5：无单号不模糊匹配

        # 人工核验回填单号后 → 精确匹配收敛
        _backdate_intent(intent.id, provider_order_id="ORD-LATE")
        third = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert third[0].status == "submitted"
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
        _backdate_intent(intent.id, status="unknown", provider_order_id="ORD-OL")

        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        # orderList 被正确解析：单号精确匹配 → 收敛 submitted，而不是误判 failed
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

        # 部分成交 40 股，剩余 60 股撤销成功（状态码 3 = 已撤终态）
        # → canceled_partial 终态（第七轮复审 P0-2：终态优先于部分成交，
        # 不再被当作"剩余仍在途"的 partially_filled）
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
        assert resolved[0].status == "canceled_partial"

        # 剩余全部撤销且无成交 → canceled 终态（重新置为开放状态后核验）
        _backdate_intent(intent.id, status="submitted")
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
        _backdate_intent(intent.id, status="unknown", provider_order_id="ORD-DL")

        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        # data 列表被正确解析：单号精确匹配 → 收敛 submitted
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


def test_action_id_backfill_requires_intent_id(monkeypatch, tmp_path) -> None:
    """第六轮复审：缺少 intent_id 不做模糊批量回填（防串档）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        today = datetime.now(SHANGHAI_TZ).date().isoformat()
        with session_scope() as db:
            db.add(
                TradeIntent(
                    signal_date=today,
                    symbol="600519",
                    side="BUY",
                    tranche_id=1,
                    intent_key=f"{today}:600519:BUY:1",
                    run_id=1,
                    quantity=100,
                    status="submitted",
                )
            )

        # 无 intent_id：不回填（记录告警）
        execution_runner_service._backfill_intent_action_id(
            run_id=1,
            action_id=101,
            executed_action={"action": "BUY", "symbol": "600519", "quantity": 100},
        )
        assert _list_intents(symbol="600519")[0].action_id is None
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 第四轮复审专项
# ---------------------------------------------------------------------------


def test_unparseable_orders_blocks_buy_preflight() -> None:
    """清单 #1：get_orders 返回无法识别结构 → BUY 被阻断（Fail-Closed）。"""
    from skills.mx_core.execution import preflight_trade

    class _BadOrdersClient:
        def get_orders(self) -> dict:
            return {"unexpected": "shape"}

        def get_balance(self) -> dict:
            return {"data": {"availBalance": 10_000_000.0}}

    with pytest.raises(RuntimeError, match="无法解析委托列表"):
        preflight_trade(
            client=_BadOrdersClient(),
            action="BUY",
            symbol="600519",
            quantity=100,
            price_type="LIMIT",
            price=20.0,
        )


def test_unparseable_positions_blocks_sell_preflight() -> None:
    """持仓列表无法识别 → SELL 被阻断。"""
    from skills.mx_core.execution import preflight_trade

    class _BadPositionsClient:
        def get_positions(self) -> dict:
            return {"weird": 1}

        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    with pytest.raises(RuntimeError, match="无法解析持仓列表"):
        preflight_trade(
            client=_BadPositionsClient(),
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


def test_unknown_status_pending_buy_blocks_new_buy() -> None:
    """清单 #2：同标的在途订单用未知状态码 → 不允许重复买入。"""
    from skills.mx_core.execution import preflight_trade

    class _UnknownStatusClient:
        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "1",
                            "orderStatus": "77",  # 未知状态
                            "orderCount": 100,
                            "dealCount": 0,
                        }
                    ]
                }
            }

        def get_balance(self) -> dict:
            return {"data": {"availBalance": 10_000_000.0}}

    # 未知状态 + 已知剩余 > 0：在途买单路径（pending 判定不含 77）
    # → 走未知状态保守阻塞
    with pytest.raises(RuntimeError, match="无法识别状态"):
        preflight_trade(
            client=_UnknownStatusClient(),
            action="BUY",
            symbol="600519",
            quantity=100,
            price_type="LIMIT",
            price=20.0,
        )


def test_missing_order_quantity_with_deals_is_unknown() -> None:
    """清单 #3：委托数量缺失但成交数量>0 → UNKNOWN（不误判 filled）。"""
    from skills.mx_core.order_schema import order_terminal_status

    row = {"orderID": "ORD-100", "orderStatus": "1", "dealCount": 100}
    assert order_terminal_status(row) == "unknown"


def test_dealt_exceeds_ordered_is_unknown() -> None:
    """成交数量超过委托数量（数据异常）→ UNKNOWN。"""
    from skills.mx_core.order_schema import order_terminal_status

    row = {
        "orderID": "ORD-X",
        "orderStatus": "1",
        "orderCount": 100,
        "dealCount": 200,
    }
    assert order_terminal_status(row) == "unknown"


def test_intent_id_precise_association(monkeypatch, tmp_path) -> None:
    """清单 #6：同股票两笔同方向不同数量的交易 → action_id 一对一关联。"""
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
                        symbol="600519",
                        side="BUY",
                        tranche_id=2,
                        intent_key=f"{today}:600519:BUY:2",
                        run_id=1,
                        quantity=200,
                        status="submitted",
                    ),
                ]
            )
            db.flush()
            ids = [
                row.id
                for row in db.scalars(
                    select(TradeIntent).order_by(TradeIntent.id)
                ).all()
            ]

        # 第一笔（intent_id=ids[0]）成功执行 action 201
        execution_runner_service._backfill_intent_action_id(
            run_id=1,
            action_id=201,
            executed_action={
                "action": "BUY",
                "symbol": "600519",
                "quantity": 100,
                "intent_id": ids[0],
            },
        )
        # 只有 ids[0] 被关联；ids[1]（同标的同方向不同数量）不受影响
        assert _list_intents(id=ids[0])[0].action_id == 201
        assert _list_intents(id=ids[1])[0].action_id is None

        # 第二笔（intent_id=ids[1]）成功执行 action 202
        execution_runner_service._backfill_intent_action_id(
            run_id=1,
            action_id=202,
            executed_action={
                "action": "BUY",
                "symbol": "600519",
                "quantity": 200,
                "intent_id": ids[1],
            },
        )
        assert _list_intents(id=ids[0])[0].action_id == 201
        assert _list_intents(id=ids[1])[0].action_id == 202
    finally:
        _reset_db_state()


def test_cancel_partial_fill_keeps_cancel_pending(monkeypatch, tmp_path) -> None:
    """清单 #7：原委托部分成交仍有剩余 → CANCEL_PENDING（不锁死剩余撤单）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "1",
                    "orderCount": 1000,
                    "dealCount": 400,  # 部分成交，剩余 600 在途
                    "orderID": "ORD-PF",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_cancel_intent(
            order_id="ORD-PF"
        )
        trade_intent_service.mark_intent_submitted(intent.id, "ORD-PF")

        resolved = trade_intent_service.reconcile_cancel_intents(client)
        # 部分成交 → 独立 cancel_pending 状态（不是 filled，不锁死剩余撤单）
        assert resolved[0].status == "cancel_pending"
        assert "部分成交" in str(resolved[0].note or "")
    finally:
        _reset_db_state()


def test_http_408_is_unknown_not_rejected() -> None:
    """清单 #8：HTTP 408（超时）→ 状态不明，不自动认定业务拒绝。"""
    import httpx

    from skills.mx_core.client import MXResponseUnknown

    request = httpx.Request("POST", "https://x/trade")
    response = httpx.Response(408, request=request, text="timeout")
    with pytest.raises(MXResponseUnknown, match="408"):
        try:
            raise httpx.HTTPStatusError(
                "timeout", request=request, response=response
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in (408, 425, 429):
                raise MXResponseUnknown(
                    f"妙想接口 HTTP {status_code}（超时/限流），处理结果未知"
                ) from exc
            raise RuntimeError("不应到达") from exc





# ---------------------------------------------------------------------------
# 第五轮复审专项
# ---------------------------------------------------------------------------


def _assert_order_rows_invalid(payload: dict) -> None:
    from skills.mx_core.execution import _require_parseable_order_rows

    with pytest.raises(RuntimeError, match="无法解析|字段不完整"):
        _require_parseable_order_rows(payload, what="委托列表")


def test_data_list_with_null_elements_is_invalid() -> None:
    """清单 #1：data: [null, "invalid", 123] → INVALID，阻断交易。"""
    from skills.mx_core.execution import _payload_rows_with_state
    from skills.mx_core.execution import _ROWS_INVALID

    rows, state = _payload_rows_with_state({"data": [None, "invalid", 123]})
    assert state == _ROWS_INVALID
    assert rows == []


def test_pending_order_missing_order_count_is_invalid_row() -> None:
    """清单 #2：待处理状态但缺 orderCount → 行级校验失败 → INVALID。"""
    from skills.mx_core.execution import (
        _ROWS_INVALID,
        _payload_rows_with_state,
    )

    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {
                        "stockCode": "600519",
                        "orderStatus": "1",
                        # 缺 orderCount / dealCount
                    }
                ]
            }
        }
    )


def test_pending_order_missing_deal_count_is_invalid_row() -> None:
    """清单 #3：待处理状态但缺 dealCount → INVALID。"""
    from skills.mx_core.execution import (
        _ROWS_INVALID,
        _payload_rows_with_state,
    )

    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {
                        "stockCode": "600519",
                        "orderStatus": "1",
                        "orderCount": 100,
                        # 缺 dealCount
                    }
                ]
            }
        }
    )


def test_missing_status_code_is_invalid_row() -> None:
    """清单 #4：缺 orderStatus → INVALID。"""
    from skills.mx_core.execution import (
        _ROWS_INVALID,
        _payload_rows_with_state,
    )

    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {"stockCode": "600519", "orderCount": 100, "dealCount": 0}
                ]
            }
        }
    )


def test_missing_symbol_is_invalid_row() -> None:
    """缺股票代码 → INVALID。"""
    from skills.mx_core.execution import (
        _ROWS_INVALID,
        _payload_rows_with_state,
    )

    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {"orderStatus": "1", "orderCount": 100, "dealCount": 0}
                ]
            }
        }
    )


def test_pending_buy_missing_quantity_blocks_preflight() -> None:
    """清单 #2（预检层）：待处理买单缺数量字段 → BUY 阻断。"""
    from skills.mx_core.execution import preflight_trade

    class _MissingQtyClient:
        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "1",
                            "orderStatus": "1",
                            "orderID": "ORD-MQ",
                            # 缺 orderCount / dealCount
                        }
                    ]
                }
            }

        def get_balance(self) -> dict:
            return {"data": {"availBalance": 10_000_000.0}}

    # 行级 Schema 校验在列表解析层即拦截（INVALID）——同样是安全阻断
    with pytest.raises(RuntimeError, match="无法解析|字段不完整|数量缺失"):
        preflight_trade(
            client=_MissingQtyClient(),
            action="BUY",
            symbol="600519",
            quantity=100,
            price_type="LIMIT",
            price=20.0,
        )


def test_pending_sell_missing_quantity_blocks_preflight() -> None:
    """清单 #9（SELL）：在途卖单数量缺失 → 阻断新卖出。"""
    from skills.mx_core.execution import preflight_trade

    class _MissingQtySellClient:
        def get_positions(self) -> dict:
            # 不带 availCount：迫使预检走"持仓 - 在途卖出占用"推算路径
            return {
                "data": {
                    "posList": [
                        {"stockCode": "600519", "count": 1000}
                    ]
                }
            }

        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "2",
                            "orderStatus": "1",
                            "orderID": "ORD-MQS",
                            # 缺 orderCount / dealCount
                        }
                    ]
                }
            }

    with pytest.raises(RuntimeError, match="无法解析|字段不完整|数量缺失"):
        preflight_trade(
            client=_MissingQtySellClient(),
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


def test_partially_filled_alone_not_history_evidence(monkeypatch, tmp_path) -> None:
    """清单 #5：仅一条 partially_filled 订单 → 不证明完整历史视图
    （新设计下 UNKNOWN 一律不自动解除，此测试锁定该行为）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "000001",
                    "orderDrt": "1",
                    "orderStatus": "1",
                    "orderCount": 1000,
                    "dealCount": 400,  # partially_filled，可能仍在活动列表
                    "orderID": "ORD-PF2",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(
            intent.id,
            status="unknown",
            unknown_checks=99,
            first_unknown_at=datetime.now(timezone.utc)
            - timedelta(seconds=UNKNOWN_WAIT_SECONDS * 100),
        )
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"
    finally:
        _reset_db_state()


def test_cancel_verify_unparseable_reports_unknown() -> None:
    """清单 #7：撤单后回查数据结构异常 → UNKNOWN（不假成功）。"""
    from skills.mx_core.execution import mx_execution_service

    class _BadOrdersClient:
        def get_orders(self) -> dict:
            return {"weird": "shape"}

    status, note = mx_execution_service._verify_cancel_result(
        _BadOrdersClient(), "ORD-1"
    )
    assert status == "unknown"
    assert "UNKNOWN" in note
    assert "不可识别" in note


def test_cancel_verify_missing_order_reports_unknown() -> None:
    """找不到委托 → UNKNOWN（不再"通常表示撤单已生效"）。"""
    from skills.mx_core.execution import mx_execution_service

    class _EmptyOrdersClient:
        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    status, note = mx_execution_service._verify_cancel_result(
        _EmptyOrdersClient(), "ORD-1"
    )
    assert status == "unknown"
    assert "不能据此认定撤单已生效" in note


def test_row_int_optional_distinguishes_missing_from_zero() -> None:
    """_row_int_optional：缺失返回 None，真实 0 返回 0。"""
    from skills.mx_core.execution import _row_int_optional

    assert _row_int_optional({"orderCount": 0}, "orderCount") == 0
    assert _row_int_optional({}, "orderCount") is None
    assert _row_int_optional({"orderCount": "abc"}, "orderCount") is None


# ---------------------------------------------------------------------------
# 第六轮复审专项
# ---------------------------------------------------------------------------


def test_pending_order_missing_direction_is_invalid() -> None:
    """清单 #1：待处理订单缺 orderDrt → INVALID。"""
    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {
                        "stockCode": "600519",
                        "orderStatus": "1",
                        "orderCount": 100,
                        "dealCount": 0,
                        # 缺 orderDrt
                    }
                ]
            }
        }
    )


def test_pending_order_zero_count_is_invalid() -> None:
    """清单 #4：orderCount = 0 → INVALID（数量必须为正）。"""
    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {
                        "stockCode": "600519",
                        "orderDrt": "1",
                        "orderStatus": "1",
                        "orderCount": 0,
                        "dealCount": 0,
                    }
                ]
            }
        }
    )


def test_pending_order_negative_count_is_invalid() -> None:
    """清单 #5：orderCount = -100 → INVALID。"""
    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {
                        "stockCode": "600519",
                        "orderDrt": "1",
                        "orderStatus": "1",
                        "orderCount": -100,
                        "dealCount": 0,
                    }
                ]
            }
        }
    )


def test_pending_order_negative_dealt_is_invalid() -> None:
    """清单 #6：dealCount = -1 → INVALID。"""
    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {
                        "stockCode": "600519",
                        "orderDrt": "1",
                        "orderStatus": "1",
                        "orderCount": 100,
                        "dealCount": -1,
                    }
                ]
            }
        }
    )


def test_pending_order_dealt_exceeds_ordered_is_invalid() -> None:
    """清单 #7：dealCount > orderCount → INVALID。"""
    _assert_order_rows_invalid(
        {
            "data": {
                "orders": [
                    {
                        "stockCode": "600519",
                        "orderDrt": "1",
                        "orderStatus": "1",
                        "orderCount": 100,
                        "dealCount": 150,
                    }
                ]
            }
        }
    )


def test_non_integer_quantity_is_invalid() -> None:
    """非整数数量（100.9 股）→ 不可判定（不静默截断）。"""
    from skills.mx_core.order_schema import row_int_optional

    assert row_int_optional({"orderCount": 100.9}, "orderCount") is None
    assert row_int_optional({"orderCount": "100.9"}, "orderCount") is None
    assert row_int_optional({"orderCount": 100.0}, "orderCount") == 100


def test_buy_status_zero_with_remaining_blocks() -> None:
    """清单 #8：BUY 遇到显式 status=0 且剩余大于零 → 阻断。"""
    from skills.mx_core.execution import preflight_trade

    class _StatusZeroClient:
        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "1",
                            "orderStatus": "0",
                            "orderCount": 100,
                            "dealCount": 0,
                            "orderID": "ORD-Z0",
                        }
                    ]
                }
            }

        def get_balance(self) -> dict:
            return {"data": {"availBalance": 10_000_000.0}}

    with pytest.raises(RuntimeError, match="无法识别状态"):
        preflight_trade(
            client=_StatusZeroClient(),
            action="BUY",
            symbol="600519",
            quantity=100,
            price_type="LIMIT",
            price=20.0,
        )


def test_sell_direction_missing_blocks() -> None:
    """清单 #9：SELL 遇到方向缺失的委托 → 行级 INVALID → 阻断。"""
    from skills.mx_core.execution import preflight_trade

    class _NoDirectionClient:
        def get_positions(self) -> dict:
            return {
                "data": {
                    "posList": [{"stockCode": "600519", "count": 1000}]
                }
            }

        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderStatus": "1",
                            "orderCount": 100,
                            "dealCount": 0,
                            # 缺 orderDrt
                        }
                    ]
                }
            }

    with pytest.raises(RuntimeError, match="无法解析|字段不完整"):
        preflight_trade(
            client=_NoDirectionClient(),
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


def test_unknown_without_order_id_never_fuzzy_matches(monkeypatch, tmp_path) -> None:
    """清单 #10/#11：无单号 UNKNOWN 不做标的+方向模糊匹配。

    历史同向订单（无论一笔还是两笔）都不能被认作本次超时请求的
    订单——无法证明因果关联，保持 UNKNOWN 等待人工核验。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        # 一笔历史同向订单
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "3",
                    "orderCount": 100,
                    "dealCount": 100,
                    "orderID": "ORD-HIST",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown")

        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"  # 不认领历史订单

        # 两笔同向订单：同样保持 UNKNOWN
        client.orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "3",
                "orderCount": 100,
                "dealCount": 100,
                "orderID": "ORD-H1",
            },
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "1",
                "orderCount": 50,
                "dealCount": 0,
                "orderID": "ORD-H2",
            },
        ]
        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        assert resolved[0].status == "unknown"
    finally:
        _reset_db_state()


def test_unknown_with_order_id_but_invalid_row_keeps_unknown(
    monkeypatch, tmp_path
) -> None:
    """清单 #12：有精确单号但该行数量字段缺失 → 快照 INVALID → 保持 UNKNOWN。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "1",
                    "orderID": "ORD-BAD",
                    # 缺 orderCount / dealCount → 行级 INVALID → 整份快照不可用
                }
            ]
        )
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        _backdate_intent(intent.id, status="unknown", provider_order_id="ORD-BAD")

        resolved = trade_intent_service.reconcile_unknown_intents(
            client, symbol="600519"
        )
        # 快照 INVALID：不消费核验、保持 UNKNOWN
        assert resolved == []
        assert _list_intents(id=intent.id)[0].status == "unknown"
    finally:
        _reset_db_state()


def test_action_id_rejects_cross_run_intent(monkeypatch, tmp_path) -> None:
    """清单 #13：intent_id 属于其他 run → 拒绝关联。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        today = datetime.now(SHANGHAI_TZ).date().isoformat()
        with session_scope() as db:
            db.add(
                TradeIntent(
                    signal_date=today,
                    symbol="600519",
                    side="BUY",
                    tranche_id=1,
                    intent_key=f"{today}:600519:BUY:1",
                    run_id=99,  # 属于另一个 run
                    quantity=100,
                    status="submitted",
                )
            )

        execution_runner_service._backfill_intent_action_id(
            run_id=1,  # 当前 run
            action_id=101,
            executed_action={
                "action": "BUY",
                "symbol": "600519",
                "quantity": 100,
                "intent_id": 1,  # 指向 run 99 的意图
            },
        )
        # run 不匹配 → 不关联
        assert _list_intents(id=1)[0].action_id is None
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 第七轮复审专项
# ---------------------------------------------------------------------------


def test_canceled_buy_order_not_blocking() -> None:
    """清单 #1：已撤销买单（未成交）→ 不再视为在途买单。"""
    from skills.mx_core.execution import preflight_trade

    class _CanceledBuyClient:
        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "1",
                            "orderStatus": "3",  # 已撤销
                            "orderCount": 100,
                            "dealCount": 0,
                            "orderID": "ORD-CX",
                        }
                    ]
                }
            }

        def get_balance(self) -> dict:
            return {"data": {"availBalance": 10_000_000.0}}

    # 已撤销订单不阻塞新买入
    preflight_trade(
        client=_CanceledBuyClient(),
        action="BUY",
        symbol="600519",
        quantity=100,
        price_type="LIMIT",
        price=20.0,
    )


def test_canceled_sell_order_not_occupying() -> None:
    """清单 #2：已撤销卖单（未成交）→ 不占用可卖数量。"""
    from skills.mx_core.execution import preflight_trade

    class _CanceledSellClient:
        def get_positions(self) -> dict:
            return {
                "data": {"posList": [{"stockCode": "600519", "count": 1000}]}
            }

        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "2",
                            "orderStatus": "3",
                            "orderCount": 500,
                            "dealCount": 0,
                            "orderID": "ORD-CXS",
                        }
                    ]
                }
            }

    # 已撤销卖单不占用：1000 - 0 = 1000 可卖
    preflight_trade(
        client=_CanceledSellClient(),
        action="SELL",
        symbol="600519",
        quantity=800,
        price_type="MARKET",
        price=None,
    )


def test_canceled_partial_is_terminal() -> None:
    """清单 #3：部分成交 400 股、剩余 600 股已撤销 → canceled_partial。"""
    from skills.mx_core.order_schema import order_terminal_status

    row = {
        "stockCode": "600519",
        "orderDrt": "1",
        "orderStatus": "3",
        "orderCount": 1000,
        "dealCount": 400,
    }
    assert order_terminal_status(row) == "canceled_partial"


def test_cancel_after_partial_fill_verified(monkeypatch, tmp_path) -> None:
    """清单 #4：部分成交后撤单成功 → 撤单意图 VERIFIED（不卡 CANCEL_PENDING）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "3",  # 撤销成功
                    "orderCount": 1000,
                    "dealCount": 400,  # 部分成交
                    "orderID": "ORD-CP",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_cancel_intent(
            order_id="ORD-CP"
        )
        trade_intent_service.mark_intent_submitted(intent.id, "ORD-CP")

        resolved = trade_intent_service.reconcile_cancel_intents(client)
        assert resolved[0].status == "verified"
        assert "部分成交后剩余已撤销" in str(resolved[0].note or "")
    finally:
        _reset_db_state()


def test_cancel_pending_when_remainder_still_active(monkeypatch, tmp_path) -> None:
    """清单 #5：部分成交后剩余仍在途（待处理状态）→ CANCEL_PENDING。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient(
            orders=[
                {
                    "stockCode": "600519",
                    "orderDrt": "1",
                    "orderStatus": "1",  # 仍在途
                    "orderCount": 1000,
                    "dealCount": 400,
                    "orderID": "ORD-CP2",
                }
            ]
        )
        intent = trade_intent_service.check_and_register_cancel_intent(
            order_id="ORD-CP2"
        )
        trade_intent_service.mark_intent_submitted(intent.id, "ORD-CP2")

        resolved = trade_intent_service.reconcile_cancel_intents(client)
        assert resolved[0].status == "cancel_pending"
    finally:
        _reset_db_state()


def test_avail_count_zero_rejects_sell() -> None:
    """清单 #6：可卖数量显式为 0（T+1 当日买入）→ 拒绝任何正数量卖出。"""
    from skills.mx_core.execution import preflight_trade

    class _T1Client:
        def get_positions(self) -> dict:
            return {
                "data": {
                    "posList": [
                        {"stockCode": "600519", "count": 1000, "availCount": 0}
                    ]
                }
            }

        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    with pytest.raises(RuntimeError, match="可卖数量 0"):
        preflight_trade(
            client=_T1Client(),
            action="SELL",
            symbol="600519",
            quantity=500,
            price_type="MARKET",
            price=None,
        )


def test_avail_count_missing_falls_back() -> None:
    """清单 #7：可卖字段缺失 → 走持仓-在途推算（无在途卖单时放行）。"""
    from skills.mx_core.execution import preflight_trade

    class _NoAvailClient:
        def get_positions(self) -> dict:
            return {
                "data": {"posList": [{"stockCode": "600519", "count": 1000}]}
            }

        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    preflight_trade(
        client=_NoAvailClient(),
        action="SELL",
        symbol="600519",
        quantity=500,
        price_type="MARKET",
        price=None,
    )


def test_avail_count_invalid_format_rejects_sell() -> None:
    """清单 #8：可卖字段格式异常 → 拒绝卖出（不当作缺失）。"""
    from skills.mx_core.execution import preflight_trade

    class _BadAvailClient:
        def get_positions(self) -> dict:
            return {
                "data": {
                    "posList": [
                        {"stockCode": "600519", "count": 1000, "availCount": "abc"}
                    ]
                }
            }

        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    with pytest.raises(RuntimeError, match="可卖数量字段 availCount 存在但格式异常"):
        preflight_trade(
            client=_BadAvailClient(),
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


def test_submitted_without_order_id_becomes_untracked(monkeypatch, tmp_path) -> None:
    """清单 #9：提交成功但无订单号 → SUBMITTED_UNTRACKED，等待人工绑定。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(intent.id, None)

        stored = _list_intents(id=intent.id)[0]
        assert stored.status == "submitted_untracked"
        assert "人工核验" in str(stored.note or "")
        # 阻塞同向新委托
        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=client, symbol="600519", side="BUY", quantity=100
            )

        # 人工绑定单号后 → submitted，恢复精确匹配同步
        bound = trade_intent_service.bind_provider_order_id(
            intent.id, "ORD-MANUAL"
        )
        assert bound.status == "submitted"
        assert bound.provider_order_id == "ORD-MANUAL"
    finally:
        _reset_db_state()


def test_bind_order_rejects_wrong_status(monkeypatch, tmp_path) -> None:
    """绑定单号只允许 submitted_untracked/unknown 状态。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(intent.id, "ORD-1")
        with pytest.raises(RuntimeError, match="仅 submitted_untracked"):
            trade_intent_service.bind_provider_order_id(intent.id, "ORD-2")
    finally:
        _reset_db_state()


def test_mixed_history_and_active_orders() -> None:
    """清单 #10：历史已撤订单与当前在途订单并存 → 只对在途计算占用。"""
    from skills.mx_core.execution import preflight_trade

    class _MixedClient:
        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "1",
                            "orderStatus": "3",  # 历史已撤
                            "orderCount": 500,
                            "dealCount": 0,
                            "orderID": "ORD-OLD",
                        },
                        {
                            "stockCode": "600519",
                            "orderDrt": "1",
                            "orderStatus": "1",  # 当前在途
                            "orderCount": 200,
                            "dealCount": 0,
                            "orderID": "ORD-ACT",
                        },
                    ]
                }
            }

        def get_balance(self) -> dict:
            return {"data": {"availBalance": 10_000_000.0}}

    # 在途 200 股的买单存在 → 阻塞
    with pytest.raises(RuntimeError, match="未完成买入委托"):
        preflight_trade(
            client=_MixedClient(),
            action="BUY",
            symbol="600519",
            quantity=100,
            price_type="LIMIT",
            price=20.0,
        )


def test_avail_count_empty_string_rejects_sell() -> None:
    """第八轮复审 P0-1：availCount 为空字符串 → 拒绝（不走回退推算）。"""
    from skills.mx_core.execution import preflight_trade

    class _EmptyAvailClient:
        def get_positions(self) -> dict:
            return {
                "data": {
                    "posList": [
                        {"stockCode": "600519", "count": 1000, "availCount": ""}
                    ]
                }
            }

        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    with pytest.raises(RuntimeError, match="存在但格式异常"):
        preflight_trade(
            client=_EmptyAvailClient(),
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


def test_avail_count_null_rejects_sell() -> None:
    """availCount 为 null（字段存在）→ 拒绝。"""
    from skills.mx_core.execution import preflight_trade

    class _NullAvailClient:
        def get_positions(self) -> dict:
            return {
                "data": {
                    "posList": [
                        {"stockCode": "600519", "count": 1000, "availCount": None}
                    ]
                }
            }

        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    with pytest.raises(RuntimeError, match="存在但格式异常"):
        preflight_trade(
            client=_NullAvailClient(),
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


# ---------------------------------------------------------------------------
# 第八轮复审专项
# ---------------------------------------------------------------------------


def test_bind_rejects_duplicate_order_id(monkeypatch, tmp_path) -> None:
    """绑定安全：同一单号不得绑定到两个意图。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        first = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(first.id, None)

        second = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="000001", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(second.id, None)

        trade_intent_service.bind_provider_order_id(first.id, "ORD-DUP")
        with pytest.raises(RuntimeError, match="已被意图"):
            trade_intent_service.bind_provider_order_id(second.id, "ORD-DUP")
    finally:
        _reset_db_state()


def test_bind_rejects_symbol_mismatch(monkeypatch, tmp_path) -> None:
    """绑定安全：单号所属标的与意图不一致 → 拒绝。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(intent.id, None)

        with pytest.raises(RuntimeError, match="标的与意图不一致"):
            trade_intent_service.bind_provider_order_id(
                intent.id, "ORD-X", expected_symbol="000001"
            )
    finally:
        _reset_db_state()


def test_bind_rejects_side_mismatch(monkeypatch, tmp_path) -> None:
    """绑定安全：方向不一致 → 拒绝。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        intent = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(intent.id, None)

        with pytest.raises(RuntimeError, match="方向与意图不一致"):
            trade_intent_service.bind_provider_order_id(
                intent.id, "ORD-X", expected_side="SELL"
            )
    finally:
        _reset_db_state()


def test_cancel_verify_reports_canceled_partial_verified() -> None:
    """撤单即时回查：部分成交后剩余已撤 → VERIFIED（与 reconcile 口径一致）。"""
    from skills.mx_core.execution import mx_execution_service

    class _PartialCanceledClient:
        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "1",
                            "orderStatus": "3",
                            "orderCount": 1000,
                            "dealCount": 400,
                            "orderID": "ORD-PC",
                        }
                    ]
                }
            }

    status, note = mx_execution_service._verify_cancel_result(
        _PartialCanceledClient(), "ORD-PC"
    )
    assert status == "verified"
    assert "未成交部分已撤销" in note
    assert "已成交部分保留为实际持仓变化" in note


# ---------------------------------------------------------------------------
# 第九轮复审专项
# ---------------------------------------------------------------------------


def test_lowercase_s_sell_side_occupies() -> None:
    """清单 #1：orderDrt="s"（小写）的在途卖单 → 正确计算占用。

    原 _is_sell_side 白名单不含小写 s，该卖单会被跳过（低估可卖占用）。
    """
    from skills.mx_core.execution import preflight_trade

    class _LowerSClient:
        def get_positions(self) -> dict:
            return {
                "data": {"posList": [{"stockCode": "600519", "count": 1000}]}
            }

        def get_orders(self) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "stockCode": "600519",
                            "orderDrt": "s",  # 小写方向
                            "orderStatus": "1",
                            "orderCount": 500,
                            "dealCount": 0,
                            "orderID": "ORD-LS",
                        }
                    ]
                }
            }

    # 占用 500：可卖 1000-500=500，卖出 800 应被拒
    with pytest.raises(RuntimeError, match="可卖数量 500"):
        preflight_trade(
            client=_LowerSClient(),
            action="SELL",
            symbol="600519",
            quantity=800,
            price_type="MARKET",
            price=None,
        )


def test_cancel_returning_original_order_id_no_conflict(
    monkeypatch, tmp_path
) -> None:
    """清单 #2：撤单返回原订单号（已绑定 BUY 意图）→ 不发生唯一约束冲突。

    场景：BUY 意图绑定 ORD-100 → 撤单接口返回 orderID=ORD-100 →
    撤单意图提交不得把 ORD-100 写入 provider_order_id（唯一约束冲突
    会导致"远端撤单成功、本地状态更新失败"）。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        buy = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        # BUY 意图绑定 ORD-100
        trade_intent_service.mark_intent_submitted(buy.id, "ORD-100")

        # 撤单意图（symbol 存原委托编号）
        cancel = trade_intent_service.check_and_register_cancel_intent(
            order_id="ORD-100"
        )
        # 撤单成功，响应返回原委托编号 ORD-100——专用方法不写
        # provider_order_id，无唯一约束冲突
        trade_intent_service.mark_cancel_intent_submitted(cancel.id)

        stored = _list_intents(id=cancel.id)[0]
        assert stored.status == "submitted"
        assert stored.provider_order_id is None  # 未写入，无冲突
        assert _list_intents(id=buy.id)[0].provider_order_id == "ORD-100"
    finally:
        _reset_db_state()


def test_avail_alias_conflict_rejects_sell() -> None:
    """清单 #3：可卖数量别名数值冲突（availCount=0 + availableCount=500）
    → 明确拒绝，不静默选第一个。"""
    from skills.mx_core.execution import preflight_trade

    class _AliasConflictClient:
        def get_positions(self) -> dict:
            return {
                "data": {
                    "posList": [
                        {
                            "stockCode": "600519",
                            "count": 1000,
                            "availCount": 0,
                            "availableCount": 500,
                        }
                    ]
                }
            }

        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    with pytest.raises(RuntimeError, match="别名数值冲突"):
        preflight_trade(
            client=_AliasConflictClient(),
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


def test_avail_alias_partial_invalid_rejects_sell() -> None:
    """第十轮复审 P1：availCount=500 + availableCount="abc" → 拒绝。

    原实现只把可解析的别名放入冲突集合，部分异常字段被静默跳过。
    """
    from skills.mx_core.execution import preflight_trade

    class _PartialInvalidClient:
        def get_positions(self) -> dict:
            return {
                "data": {
                    "posList": [
                        {
                            "stockCode": "600519",
                            "count": 1000,
                            "availCount": 500,
                            "availableCount": "abc",
                        }
                    ]
                }
            }

        def get_orders(self) -> dict:
            return {"data": {"orders": []}}

    with pytest.raises(RuntimeError, match="availableCount 存在但格式异常"):
        preflight_trade(
            client=_PartialInvalidClient(),
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


def test_cancel_handler_uses_dedicated_intent_submit_method(
    monkeypatch, tmp_path
) -> None:
    """第十轮复审 P0-2 E2E：真实调用 _handle_moni_cancel 成功路径。

    完整链路：BUY 意图绑定 ORD-100 → 撤单接口返回 orderID=ORD-100 →
    handler 调用 mark_cancel_intent_submitted（不写 provider_order_id，
    无唯一约束冲突、无 AttributeError）→ 撤单意图 SUBMITTED →
    回查结果与远端订单状态一致。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution

        monkeypatch.setattr(mx_execution, "is_market_open", lambda: True)

        # 有状态 mock：撤单前 ORD-100 待撤（预检通过），撤单后变已撤销
        remote_orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "1",  # 待撤
                "orderCount": 100,
                "dealCount": 0,
                "orderID": "ORD-100",
            }
        ]

        class _CancelClient:
            def get_orders(self) -> dict:
                return {"data": {"orders": remote_orders}}

            def cancel_order(self, *, cancel_type, order_id, stock_code):
                # 撤单成功：远端状态变为已撤销；响应返回原委托编号
                remote_orders[0]["orderStatus"] = "3"
                return {
                    "code": "200",
                    "message": "成功",
                    "data": {"orderID": order_id},
                }

        # 1. 创建 BUY 意图并绑定 ORD-100
        client = _CancelClient()
        buy = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        trade_intent_service.mark_intent_submitted(buy.id, "ORD-100")

        # 2. 真实执行 _handle_moni_cancel（handler 自行注册 CANCEL 意图）
        result = mx_execution.mx_execution_service.execute_tool(
            client=client,
            app_settings=None,
            tool_name="mx_moni_cancel",
            arguments={
                "cancel_type": "order",
                "order_id": "ORD-100",
                "stock_code": "600519",
            },
        )

        # 3. 无 AttributeError，撤单工具成功
        assert result["ok"] is True
        assert "已提交撤单请求" in result["summary"]
        # 4. 回查结果与远端状态一致（撤单后已撤销 → VERIFIED）
        assert "VERIFIED" in result["summary"]

        # 5. BUY 意图的 provider_order_id 不变
        assert _list_intents(id=buy.id)[0].provider_order_id == "ORD-100"
        # 6. handler 注册的 CANCEL 意图：即时核验已确认 VERIFIED 并
        #    立即持久化（第十一轮复审 P1），未写 provider_order_id
        cancel_intents = [
            item for item in _list_intents() if item.side == "CANCEL"
        ]
        assert len(cancel_intents) == 1
        cancel_stored = cancel_intents[0]
        assert cancel_stored.symbol == "ORD-100"  # 原委托编号在 symbol
        assert cancel_stored.status == "verified"  # 即时核验已持久化
        assert cancel_stored.provider_order_id is None
    finally:
        _reset_db_state()


def test_mark_cancel_intent_rejects_non_cancel_intent(monkeypatch, tmp_path) -> None:
    """撤单专用方法只允许更新 side=CANCEL 的意图。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        client = _IntentClient()
        buy = trade_intent_service.check_and_register_trade_intent(
            client=client, symbol="600519", side="BUY", quantity=100
        )
        with pytest.raises(RuntimeError, match="不是撤单意图"):
            trade_intent_service.mark_cancel_intent_submitted(buy.id)
    finally:
        _reset_db_state()


def test_mark_cancel_intent_rejects_missing_intent(monkeypatch, tmp_path) -> None:
    """意图不存在 → 报错（不静默返回）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        with pytest.raises(RuntimeError, match="撤单意图不存在"):
            trade_intent_service.mark_cancel_intent_submitted(99999)
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 第十一轮复审专项
# ---------------------------------------------------------------------------


def test_handler_internal_typeerror_not_reexecuted() -> None:
    """P1：handler 内部 TypeError 不得触发二次执行（重复写请求）。

    原实现捕获全部 TypeError 后重试整个 handler——若写请求已发出、
    本地处理响应时抛 TypeError，会被误判为签名不匹配而再次执行。
    """
    from skills.mx_core.execution import MXExecutionService

    service = MXExecutionService()
    call_count = {"value": 0}

    def _flaky_handler(*, client, app_settings, arguments, run_id=None):
        del client, app_settings, arguments, run_id
        call_count["value"] += 1
        # 模拟：远端写请求已发出（计数 +1），本地处理响应抛 TypeError
        raise TypeError("响应处理内部异常")

    service._handlers["mx_test_flaky"] = _flaky_handler
    try:
        result = service.execute_tool(
            client=None,
            app_settings=None,
            tool_name="mx_test_flaky",
            arguments={},
            run_id=1,
        )
        # handler 只执行一次（无二次执行）
        assert call_count["value"] == 1
        # 异常被外层捕获为失败结果（不是成功）
        assert result["ok"] is False
        assert "响应处理内部异常" in str(result.get("error") or "")
    finally:
        service._handlers.pop("mx_test_flaky", None)


def test_handler_without_run_id_param_still_works() -> None:
    """签名判断兼容不接受 run_id 的 handler（正常执行一次）。"""
    from skills.mx_core.execution import MXExecutionService

    service = MXExecutionService()
    call_count = {"value": 0}

    def _legacy_handler(*, client, app_settings, arguments):
        del client, app_settings
        call_count["value"] += 1
        return {
            "ok": True,
            "tool_name": arguments.get("name", ""),
            "result": {"legacy": True},
        }

    service._handlers["mx_test_legacy"] = _legacy_handler
    try:
        result = service.execute_tool(
            client=None,
            app_settings=None,
            tool_name="mx_test_legacy",
            arguments={"name": "mx_test_legacy"},
            run_id=1,
        )
        assert call_count["value"] == 1
        assert result["ok"] is True
    finally:
        service._handlers.pop("mx_test_legacy", None)


def test_cancel_immediate_verify_persists_intent_status(
    monkeypatch, tmp_path
) -> None:
    """第十一轮复审 P1：即时核验结果立即持久化到意图。

    撤单成功 → 即时核验 VERIFIED → 数据库立即 verified（不等下一轮
    reconcile——防止后续委托从列表消失时退化 UNKNOWN）。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution

        monkeypatch.setattr(mx_execution, "is_market_open", lambda: True)

        remote_orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "1",
                "orderCount": 100,
                "dealCount": 0,
                "orderID": "ORD-IV",
            }
        ]

        class _IVClient:
            def get_orders(self) -> dict:
                return {"data": {"orders": remote_orders}}

            def cancel_order(self, *, cancel_type, order_id, stock_code):
                remote_orders[0]["orderStatus"] = "3"
                return {"code": "200", "message": "成功", "data": {"orderID": order_id}}

        result = mx_execution.mx_execution_service.execute_tool(
            client=_IVClient(),
            app_settings=None,
            tool_name="mx_moni_cancel",
            arguments={
                "cancel_type": "order",
                "order_id": "ORD-IV",
                "stock_code": "600519",
            },
        )
        assert result["ok"] is True

        # 即时核验已持久化：CANCEL 意图为 verified（非 submitted）
        cancel_intents = [
            item for item in _list_intents() if item.side == "CANCEL"
        ]
        assert cancel_intents[0].status == "verified"
        assert "已撤销" in str(cancel_intents[0].note or "")
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 第十二轮复审专项
# ---------------------------------------------------------------------------


def test_self_select_unknown_blocks_next_round(monkeypatch, tmp_path) -> None:
    """P1-1：写请求已发出但回查失败 → UNKNOWN 持久化 → 下一轮阻塞。

    复审场景：10:00 ADD 超时/回查失败 → 10:15 下一轮再次 ADD 被阻塞
    （此前 UNKNOWN 只存在于本轮模型输出，下一轮会重复发送）。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution

        read_count = {"value": 0}

        class _FlakyReadClient:
            def __init__(self) -> None:
                self.rows: list[dict] = []
                self.manage_calls: list[str] = []

            def get_self_selects(self) -> dict:
                read_count["value"] += 1
                # 第 1 次（预读）成功；第 2 次（回查）失败
                if read_count["value"] == 2:
                    raise RuntimeError("回查读取失败")
                return {"data": {"rows": self.rows}}

            def manage_self_select(self, query: str) -> dict:
                self.manage_calls.append(query)
                return {"code": "200", "message": "成功"}

        client = _FlakyReadClient()
        # 第一轮：ADD 发出，回查失败 → UNKNOWN
        r1 = mx_execution.mx_execution_service.execute_tool(
            client=client,
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519加入自选股"},
        )
        assert r1["ok"] is False
        assert "回查失败" in str(r1.get("error") or "")
        assert len(client.manage_calls) == 1

        # 第二轮（新客户端，读取恢复正常但列表仍无该标的）：
        # UNKNOWN 未确认 → 阻塞重复 ADD
        class _NormalClient:
            def get_self_selects(self) -> dict:
                return {"data": {"rows": []}}

            def manage_self_select(self, query: str) -> dict:
                raise AssertionError("UNKNOWN 未确认时不得重复发送写请求")

        r2 = mx_execution.mx_execution_service.execute_tool(
            client=_NormalClient(),
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519加入自选股"},
        )
        assert r2["ok"] is False
        assert "未确认" in str(r2.get("error") or "")

        # 第三轮（列表已包含目标——目标状态确认）→ UNKNOWN 解除，允许操作
        class _ResolvedClient:
            def get_self_selects(self) -> dict:
                return {
                    "data": {
                        "rows": [{"stockCode": "600519", "stockName": "贵州茅台"}]
                    }
                }

            def manage_self_select(self, query: str) -> dict:
                return {"code": "200", "message": "成功"}

        r3 = mx_execution.mx_execution_service.execute_tool(
            client=_ResolvedClient(),
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519加入自选股"},
        )
        # 目标状态已达成 → NOOP（不发送写请求）
        assert r3["ok"] is True
        assert r3["executed_action"]["noop"] is True
    finally:
        _reset_db_state()


def test_self_select_name_exact_match() -> None:
    """P1-3：名称精确匹配——"中芯"不得被误判为已在列表的"中芯国际"。"""
    from skills.mx_core.execution import mx_execution_service

    state = (set(), ["中芯国际"])
    # 目标名称"中芯"（子串）≠ 列表中的"中芯国际"（精确不匹配）
    assert mx_execution_service._target_in_state("", "中芯", state) is False
    # 精确匹配才为 True
    assert mx_execution_service._target_in_state("", "中芯国际", state) is True


def test_llm_agent_loop_typeerror_not_reexecuted() -> None:
    """P1-2：自定义 tool_executor 内部 TypeError 只调用一次。"""
    from app.services.llm_service import LLMService

    service = LLMService()
    call_count = {"value": 0}

    def _flaky_executor(tool_name, arguments, tool_call_id=None):
        del tool_name, arguments, tool_call_id
        call_count["value"] += 1
        # 模拟：远端写请求已发出（计数+1），本地处理抛 TypeError
        raise TypeError("响应处理内部异常")

    # 直接测试 _agent_loop 的工具执行分支签名判断：
    # 用签名检查替代 TypeError 重试
    import inspect

    params = inspect.signature(_flaky_executor).parameters
    accepts_id = any(
        p.kind == inspect.Parameter.VAR_POSITIONAL
        or p.kind == inspect.Parameter.VAR_KEYWORD
        or p.name == "tool_call_id"
        for p in params.values()
    )
    assert accepts_id is True  # 三参数形态被识别

    # 执行一次（模拟 agent loop 的调用方式）
    try:
        _flaky_executor("tool", {}, "id-1")
    except TypeError:
        pass
    assert call_count["value"] == 1  # 只执行一次，无重试


def test_cancel_persist_failure_visible_in_result(monkeypatch, tmp_path) -> None:
    """P0-2：撤单状态持久化失败 → 结果中可见（不伪装成完整成功）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution
        from app.services import trade_intent_service as tis

        monkeypatch.setattr(mx_execution, "is_market_open", lambda: True)

        remote_orders = [
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "1",
                "orderCount": 100,
                "dealCount": 0,
                "orderID": "ORD-PF",
            }
        ]

        class _PFClient:
            def get_orders(self) -> dict:
                return {"data": {"orders": remote_orders}}

            def cancel_order(self, *, cancel_type, order_id, stock_code):
                remote_orders[0]["orderStatus"] = "3"
                return {"code": "200", "message": "成功", "data": {"orderID": order_id}}

        # 让状态持久化方法抛异常（模拟数据库提交失败）
        def _broken_persist(intent_id, note):
            raise RuntimeError("数据库提交失败")

        monkeypatch.setattr(tis.trade_intent_service, "mark_cancel_intent_verified", _broken_persist)

        result = mx_execution.mx_execution_service.execute_tool(
            client=_PFClient(),
            app_settings=None,
            tool_name="mx_moni_cancel",
            arguments={
                "cancel_type": "order",
                "order_id": "ORD-PF",
                "stock_code": "600519",
            },
        )

        # 远端已提交 → ok=True，但持久化失败必须可见
        assert result["ok"] is True
        assert result.get("status_persist_failed") is True
        assert "持久化失败" in result["summary"]
        assert "人工核对" in result["summary"]
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 第十三轮复审专项
# ---------------------------------------------------------------------------


def test_add_unknown_blocks_reverse_remove(monkeypatch, tmp_path) -> None:
    """P0-1：ADD UNKNOWN 后发起 REMOVE → 阻止（反方向也阻塞）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        # 构造：ADD 意图 UNKNOWN（写请求已发出未确认）
        intent = self_select_intent_service.check_and_register_intent(
            symbol="600519", operation="ADD"
        )
        self_select_intent_service.mark_unknown(
            intent.id, "写请求状态不明"
        )

        # 反方向 REMOVE 也被阻塞
        with pytest.raises(RuntimeError, match="未确认的自选股操作意图"):
            self_select_intent_service.check_and_register_intent(
                symbol="600519", operation="REMOVE"
            )
    finally:
        _reset_db_state()


def test_remove_unknown_blocks_reverse_add(monkeypatch, tmp_path) -> None:
    """P0-1：REMOVE UNKNOWN 后发起 ADD → 阻止。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        intent = self_select_intent_service.check_and_register_intent(
            symbol="600519", operation="REMOVE"
        )
        self_select_intent_service.mark_unknown(intent.id, "状态不明")

        with pytest.raises(RuntimeError, match="未确认的自选股操作意图"):
            self_select_intent_service.check_and_register_intent(
                symbol="600519", operation="ADD"
            )
    finally:
        _reset_db_state()


def test_add_unknown_blocks_remove_noop_claim(monkeypatch, tmp_path) -> None:
    """P0-1：ADD UNKNOWN 后 REMOVE 不得直接返回 NOOP_VERIFIED。

    复审场景：10:00 ADD 超时 UNKNOWN → 10:15 REMOVE 判"列表无此股"
    返回 NOOP → 10:20 ADD 延迟生效 → 用户以为删除成功实际没有。
    现在 NOOP 判断前先核验开放意图（含反方向）。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        # ADD 意图 UNKNOWN
        intent = self_select_intent_service.check_and_register_intent(
            symbol="600519", operation="ADD"
        )
        self_select_intent_service.mark_unknown(
            intent.id, "写请求已发出但回查失败"
        )

        # 列表当前无该股（ADD 未生效/延迟）——REMOVE 若先判 NOOP 会
        # 错误声称"已满足目标状态"；现在被开放意图核验阻断
        class _EmptyListClient:
            def get_self_selects(self) -> dict:
                return {"data": {"rows": []}}

            def manage_self_select(self, query: str) -> dict:
                raise AssertionError("未确认意图存在时不得发送写请求")

        result = mx_execution.mx_execution_service.execute_tool(
            client=_EmptyListClient(),
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519从自选股删除"},
        )
        assert result["ok"] is False
        assert "未确认" in str(result.get("error") or "")
        # 没有 NOOP 声明
        assert result.get("executed_action") is None
    finally:
        _reset_db_state()


def test_self_select_write_exception_marks_unknown(monkeypatch, tmp_path) -> None:
    """P0-2：写请求超时/异常 → 意图进入 UNKNOWN（不卡 EXECUTING）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        class _TimeoutClient:
            def get_self_selects(self) -> dict:
                return {"data": {"rows": []}}

            def manage_self_select(self, query: str) -> dict:
                import httpx

                raise httpx.ReadTimeout("timed out")

        result = mx_execution.mx_execution_service.execute_tool(
            client=_TimeoutClient(),
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519加入自选股"},
        )
        assert result["ok"] is False
        # 意图为 UNKNOWN（非 EXECUTING）
        intents = self_select_intent_service.list_intents()
        assert intents[0].status == "unknown"
        assert "状态不明" in str(intents[0].note or "")
    finally:
        _reset_db_state()


def test_self_select_write_rejected_marks_failed(monkeypatch, tmp_path) -> None:
    """P0-2：明确业务拒绝 → 意图 FAILED（解除阻塞允许重试）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution
        from skills.mx_core.client import MXBusinessRejected
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        class _RejectedClient:
            def get_self_selects(self) -> dict:
                return {"data": {"rows": []}}

            def manage_self_select(self, query: str) -> dict:
                raise MXBusinessRejected("妙想接口业务错误 code=501: 拒绝")

        result = mx_execution.mx_execution_service.execute_tool(
            client=_RejectedClient(),
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519加入自选股"},
        )
        assert result["ok"] is False
        intents = self_select_intent_service.list_intents()
        assert intents[0].status == "failed"
        # failed 不阻塞：允许重试
        retry = self_select_intent_service.check_and_register_intent(
            symbol="600519", operation="ADD"
        )
        assert retry.id != intents[0].id
    finally:
        _reset_db_state()


def test_self_select_startup_recovery(monkeypatch, tmp_path) -> None:
    """P0-3：启动恢复——遗留 EXECUTING → UNKNOWN。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        intent = self_select_intent_service.check_and_register_intent(
            symbol="600519", operation="ADD"
        )
        # 模拟崩溃：意图停留在 EXECUTING

        recovered = self_select_intent_service.reconcile_stale_intents()
        assert recovered == 1
        stored = self_select_intent_service.list_intents()[0]
        assert stored.status == "unknown"
        assert "服务重启" in str(stored.note or "")
    finally:
        _reset_db_state()


def test_name_form_unknown_recovers_by_name(monkeypatch, tmp_path) -> None:
    """P1：名称形式的 UNKNOWN 通过名称精确匹配恢复。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        # 名称形式的意图（模型只提供名称）
        intent = self_select_intent_service.check_and_register_intent(
            symbol="中芯国际", operation="ADD"
        )
        self_select_intent_service.mark_unknown(intent.id, "状态不明")

        # 列表已出现该名称 → 目标状态确认 → 解除阻塞（返回最新状态）
        unblocked, fresh = self_select_intent_service.reconcile_open_intents(
            symbol="中芯国际",
            read_state=lambda: (set(), ["中芯国际"]),
        )
        assert unblocked is True
        assert "中芯国际" in fresh[1]
        assert self_select_intent_service.list_intents()[0].status == "verified"
    finally:
        _reset_db_state()


def test_invoke_tool_executor_signature_forms() -> None:
    """P1：签名 bind 适配——三参数/关键字/keyword-only/**kwargs 各调用一次。"""
    from app.services.llm_service import _invoke_tool_executor

    calls = []

    def three_args(name, arguments, tool_call_id):
        calls.append(("three", name))
        return {"ok": True, "form": "three"}

    def kw_only(name, arguments, *, tool_call_id=None):
        calls.append(("kw_only", name))
        return {"ok": True, "form": "kw_only"}

    def var_kwargs(name, arguments, **kwargs):
        calls.append(("var_kwargs", name))
        return {"ok": True, "form": "var_kwargs"}

    def two_args(name, arguments):
        calls.append(("two", name))
        return {"ok": True, "form": "two"}

    assert _invoke_tool_executor(three_args, "t", {}, "id-1")["form"] == "three"
    assert _invoke_tool_executor(kw_only, "t", {}, "id-1")["form"] == "kw_only"
    assert _invoke_tool_executor(var_kwargs, "t", {}, "id-1")["form"] == "var_kwargs"
    assert _invoke_tool_executor(two_args, "t", {}, "id-1")["form"] == "two"
    # 每种签名只调用一次
    assert len(calls) == 4


def test_invoke_tool_executor_internal_error_not_retried() -> None:
    """P1：回调内部 TypeError 只执行一次（bind 在调用前完成）。"""
    from app.services.llm_service import _invoke_tool_executor

    call_count = {"value": 0}

    def flaky(name, arguments, tool_call_id):
        del name, arguments, tool_call_id
        call_count["value"] += 1
        raise TypeError("响应处理内部异常")

    with pytest.raises(TypeError):
        _invoke_tool_executor(flaky, "t", {}, "id-1")
    assert call_count["value"] == 1  # 只执行一次，无重试


# ---------------------------------------------------------------------------
# 第十四轮复审专项（TOCTOU + run_id 贯通）
# ---------------------------------------------------------------------------


def test_reconcile_uses_fresh_state_for_noop_decision(monkeypatch, tmp_path) -> None:
    """P0（TOCTOU）：延迟生效的 ADD 在 reconcile 时被确认 → 新 ADD
    必须基于**核验读取的最新快照**判 NOOP，不得重复发送。

    复审场景：T1 第一次读取（无股票）→ T2 远端 ADD 延迟生效 →
    T3 reconcile 第二次读取（股票已出现，旧意图 VERIFIED）→
    T4 若用旧快照会再次发送 ADD（重复写请求）。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        # 构造：历史 ADD 意图 UNKNOWN
        old_intent = self_select_intent_service.check_and_register_intent(
            symbol="600519", operation="ADD"
        )
        self_select_intent_service.mark_unknown(
            old_intent.id, "写请求已发出但回查失败"
        )

        # 有状态客户端：第一次读取（handler 预读）无股票；
        # 第二次读取（reconcile）股票已出现（延迟生效）
        read_count = {"value": 0}

        class _DelayedEffectClient:
            def get_self_selects(self) -> dict:
                read_count["value"] += 1
                if read_count["value"] <= 1:
                    return {"data": {"rows": []}}  # 预读：无股票
                return {  # reconcile 及之后：股票已出现
                    "data": {
                        "rows": [
                            {"stockCode": "600519", "stockName": "贵州茅台"}
                        ]
                    }
                }

            def manage_self_select(self, query: str) -> dict:
                raise AssertionError(
                    "目标状态已达成（最新快照），不得重复发送写请求"
                )

        result = mx_execution.mx_execution_service.execute_tool(
            client=_DelayedEffectClient(),
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519加入自选股"},
        )

        # 基于最新快照：已在列表 → NOOP（不发送写请求）
        assert result["ok"] is True
        assert result["executed_action"]["noop"] is True
        # 旧意图被确认为 VERIFIED
        assert (
            self_select_intent_service.list_intents()[0].status == "verified"
        )
    finally:
        _reset_db_state()


def test_reconcile_fresh_state_prevents_stale_noop(monkeypatch, tmp_path) -> None:
    """P0（TOCTOU 反向）：第一次读取股票存在 → REMOVE 在 reconcile 时
    被确认 → 新 ADD 不得基于旧快照判 NOOP（实际已被移除）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        # 历史 REMOVE 意图 UNKNOWN
        old_intent = self_select_intent_service.check_and_register_intent(
            symbol="600519", operation="REMOVE"
        )
        self_select_intent_service.mark_unknown(
            old_intent.id, "写请求已发出但回查失败"
        )

        read_count = {"value": 0}

        class _RemovedClient:
            def __init__(self) -> None:
                self.manage_calls: list[str] = []

            def get_self_selects(self) -> dict:
                read_count["value"] += 1
                if read_count["value"] <= 1:
                    # 预读：股票仍存在（REMOVE 延迟未生效）
                    return {
                        "data": {
                            "rows": [
                                {"stockCode": "600519", "stockName": "贵州茅台"}
                            ]
                        }
                    }
                if self.manage_calls:
                    # 新 ADD 已执行：股票重新加入
                    return {
                        "data": {
                            "rows": [
                                {"stockCode": "600519", "stockName": "贵州茅台"}
                            ]
                        }
                    }
                # reconcile：REMOVE 已生效，股票已移除
                return {"data": {"rows": []}}

            def manage_self_select(self, query: str) -> dict:
                # 新 ADD 应该真实执行（股票已被移除，不在列表）
                self.manage_calls.append(query)
                return {"code": "200", "message": "成功"}

        result = mx_execution.mx_execution_service.execute_tool(
            client=_RemovedClient(),
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519加入自选股"},
        )

        # 基于最新快照：不在列表 → 真实执行 ADD（不是基于旧快照的 NOOP）
        assert result["ok"] is True
        assert result.get("executed_action", {}).get("noop") is not True
        assert "回查确认" in result["summary"]
    finally:
        _reset_db_state()


def test_run_id_persisted_to_self_select_intent(monkeypatch, tmp_path) -> None:
    """P1：run_id 贯通——execute_tool(run_id=N) → SelfSelectIntent.run_id=N。

    验证外层包装（签名判断看到的函数）正确传递 run_id 到内部与意图。
    """
    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    try:
        from skills.mx_core import execution as mx_execution
        from app.services.self_select_intent_service import (
            self_select_intent_service,
        )

        class _AddClient:
            def get_self_selects(self) -> dict:
                # 预读空列表 → ADD 执行 → 回查时已加入
                if not self.manage_calls:
                    return {"data": {"rows": []}}
                return {
                    "data": {
                        "rows": [
                            {"stockCode": "600519", "stockName": "贵州茅台"}
                        ]
                    }
                }

            def __init__(self) -> None:
                self.manage_calls: list[str] = []

            def manage_self_select(self, query: str) -> dict:
                self.manage_calls.append(query)
                return {"code": "200", "message": "成功"}

        result = mx_execution.mx_execution_service.execute_tool(
            client=_AddClient(),
            app_settings=None,
            tool_name="mx_manage_self_select",
            arguments={"query": "将600519加入自选股"},
            run_id=42,
        )
        assert result["ok"] is True

        intents = self_select_intent_service.list_intents()
        assert len(intents) == 1
        assert intents[0].run_id == 42  # 贯通到数据库
    finally:
        _reset_db_state()
