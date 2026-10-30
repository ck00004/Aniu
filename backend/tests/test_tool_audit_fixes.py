"""工具调用审计修复的回归测试（2026-10 外部审计，补丁 A/B/C/D/F）。

覆盖审计回归清单中可自动化的项：
- 买入预检拦截同标的未完成买单（清单 #1/#2 的预检层）
- 交易超时进入"状态未知"，不自动重发（#4）
- 写操作（撤单/自选股）单次尝试（#9 部分）
- 动作抢占为原子条件更新（#11 单进程模拟）
- replace_plan 不得删除已发送动作（#10 部分）
- 执行时权限复检：分析任务调用交易工具被拒（#14 部分）
- HTTP 200 + 业务失败的调用落成 failed 日志（日志缺陷）
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import StrategyRunAction, ToolInvocationLog
from app.services import execution_plan_service
from app.services.execution_runner_service import execution_runner_service
from app.services.skill_stack_service import skill_stack_service
from app.services.tool_invocation_log_service import instrument_invocation
from skills.mx_core.client import MXClient
from skills.mx_core.execution import preflight_trade


def _use_temp_db(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None


def _reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


class _OrdersClient:
    """预检桩：get_orders 返回固定委托列表。"""

    def __init__(self, orders: list[dict[str, Any]], balance: dict | None = None) -> None:
        self._orders = {"data": {"orders": orders}}
        self._balance = balance or {"data": {"availBalance": 10_000_000.0}}

    def get_orders(self) -> dict:
        return self._orders

    def get_balance(self) -> dict:
        return self._balance


# ---------------------------------------------------------------------------
# 买入预检：同标的未完成买单拦截
# ---------------------------------------------------------------------------


def test_preflight_buy_blocked_by_pending_same_symbol_buy() -> None:
    """同标的存在未完成买单时，再次买入被拦截（跨轮重复买入挡板）。"""
    client = _OrdersClient(
        orders=[
            {
                "stockCode": "600519",
                "orderDrt": "1",  # 买入方向
                "orderStatus": "1",  # 已报（pending）
                "orderCount": 100,
                "dealCount": 0,
            }
        ]
    )
    with pytest.raises(RuntimeError, match="未完成买入委托"):
        preflight_trade(
            client=client,
            action="BUY",
            symbol="600519",
            quantity=100,
            price_type="LIMIT",
            price=20.0,
        )


def test_preflight_buy_allowed_when_pending_order_is_sell_side() -> None:
    """在途的是卖单，不拦截买入。"""
    client = _OrdersClient(
        orders=[
            {
                "stockCode": "600519",
                "orderDrt": "2",  # 卖出方向
                "orderStatus": "1",
                "orderCount": 100,
                "dealCount": 0,
            }
        ]
    )
    preflight_trade(
        client=client,
        action="BUY",
        symbol="600519",
        quantity=100,
        price_type="LIMIT",
        price=20.0,
    )


def test_preflight_buy_allowed_when_pending_buy_fully_filled() -> None:
    """买单已全部成交（remaining=0）不拦截。"""
    client = _OrdersClient(
        orders=[
            {
                "stockCode": "600519",
                "orderDrt": "1",
                "orderStatus": "1",
                "orderCount": 100,
                "dealCount": 100,
            }
        ]
    )
    preflight_trade(
        client=client,
        action="BUY",
        symbol="600519",
        quantity=100,
        price_type="LIMIT",
        price=20.0,
    )


def test_preflight_buy_blocked_by_pending_buy_of_different_symbol_only_reports_that_symbol() -> None:
    """其他标的的未完成买单不影响本标的买入。"""
    client = _OrdersClient(
        orders=[
            {
                "stockCode": "000001",
                "orderDrt": "1",
                "orderStatus": "1",
                "orderCount": 100,
                "dealCount": 0,
            }
        ]
    )
    preflight_trade(
        client=client,
        action="BUY",
        symbol="600519",
        quantity=100,
        price_type="LIMIT",
        price=20.0,
    )


# ---------------------------------------------------------------------------
# 交易超时：状态未知，不自动重发
# ---------------------------------------------------------------------------


class _TimeoutTradeStub:
    """MXClient 桩：trade 抛超时；超时后委托列表出现该买单
    （模拟"远端已接受委托但响应超时"的 UNKNOWN 场景）。"""

    def __init__(self) -> None:
        self.trade_calls = 0

    def get_orders(self) -> dict:
        if self.trade_calls == 0:
            # 超时前：无在途委托（预检通过）
            return {"data": {"orders": []}}
        # 超时后：远端实际已接受该委托
        return {
            "data": {
                "orders": [
                    {
                        "stockCode": "600519",
                        "orderDrt": "1",
                        "orderStatus": "1",
                        "orderCount": 100,
                        "dealCount": 0,
                        "orderID": "TIMEOUT-1",
                    }
                ]
            }
        }

    def get_balance(self) -> dict:
        return {"data": {"availBalance": 10_000_000.0}}

    def trade(self, **kwargs) -> dict:
        self.trade_calls += 1
        raise httpx.ReadTimeout("timed out")


def test_trade_timeout_reports_unknown_state_and_does_not_resend(
    monkeypatch, tmp_path
) -> None:
    from skills.mx_core import execution as mx_execution

    _use_temp_db(monkeypatch, tmp_path)
    init_db()
    monkeypatch.setattr(mx_execution, "is_market_open", lambda: True)
    stub = _TimeoutTradeStub()

    try:
        result = mx_execution.mx_execution_service.execute_tool(
            client=stub,
            app_settings=SimpleNamespace(),
            tool_name="mx_moni_trade",
            arguments={
                "action": "BUY",
                "symbol": "600519",
                "quantity": 100,
                "price_type": "LIMIT",
                "price": 20.0,
            },
        )

        # 状态未知错误被透传给模型（含核对指引），且只发送过一次、无自动重发
        assert result["ok"] is False
        assert "委托状态未知" in str(result.get("error") or "")
        assert "mx_get_orders" in str(result.get("error") or "")
        assert stub.trade_calls == 1

        # 补丁 C：意图被标记为 UNKNOWN，阻塞同标的同方向的后续提交
        from app.services.trade_intent_service import trade_intent_service

        with pytest.raises(RuntimeError, match="未完成/未确认"):
            trade_intent_service.check_and_register_trade_intent(
                client=stub, symbol="600519", side="BUY", quantity=100
            )
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 执行器：原子抢占 + replace_plan 保护 + 写操作单次尝试
# ---------------------------------------------------------------------------


def _seed_action(db, *, status: str = "planned", run_id: int = 1) -> int:
    action = StrategyRunAction(
        run_id=run_id,
        sequence_no=1,
        phase="planned",
        tool_name="mx_moni_cancel",
        action_type="CANCEL",
        status=status,
        tool_call_id="call-1",
        arguments_payload={"order_id": "123"},
    )
    db.add(action)
    db.flush()
    return action.id


def test_mark_action_executing_is_atomic_claim(monkeypatch, tmp_path) -> None:
    """抢占是条件更新：非 planned 状态的动作无法被置为 executing。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        with session_scope() as db:
            planned_id = _seed_action(db, status="planned")
            completed_id = _seed_action(db, status="completed")

        assert execution_runner_service._mark_action_executing(planned_id) is not None
        # 已完成的动作抢占失败（返回 None），且状态不变
        assert execution_runner_service._mark_action_executing(completed_id) is None
        with session_scope() as db:
            assert db.get(StrategyRunAction, completed_id).status == "completed"
            # 已抢占的动作不能被再次抢占（executing 不在可执行状态集）
        assert execution_runner_service._mark_action_executing(planned_id) is None
    finally:
        _reset_db_state()


def test_replace_plan_keeps_sent_actions(monkeypatch, tmp_path) -> None:
    """已发送（executing/completed/failed）的动作是审计记录，replace_plan
    不得删除；只允许替换仍处于 planned 的动作。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        with session_scope() as db:
            planned_id = _seed_action(db, status="planned")
            executing_id = _seed_action(db, status="executing")
            completed_id = _seed_action(db, status="completed")

        execution_runner_service.replace_plan(
            run_id=1, planned_actions=[]
        )

        with session_scope() as db:
            assert db.get(StrategyRunAction, planned_id) is None
            assert db.get(StrategyRunAction, executing_id) is not None
            assert db.get(StrategyRunAction, completed_id) is not None
    finally:
        _reset_db_state()


def test_write_actions_have_single_attempt(monkeypatch, tmp_path) -> None:
    """撤单/自选股动作与买卖一样只允许一次自动尝试。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        for action_type in ("CANCEL", "MANAGE_SELF_SELECT", "BUY", "SELL"):
            action = SimpleNamespace(action_type=action_type)
            assert (
                execution_runner_service._resolve_action_max_attempts(
                    action, requested_max_attempts=2
                )
                == 1
            ), action_type
        # 读取类动作仍可重试
        read_action = SimpleNamespace(action_type="QUERY")
        assert (
            execution_runner_service._resolve_action_max_attempts(
                read_action, requested_max_attempts=2
            )
            == 2
        )
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 执行时权限复检
# ---------------------------------------------------------------------------


def test_execute_tool_rejects_trade_tool_in_analysis_run(monkeypatch, tmp_path) -> None:
    """分析任务中调用交易工具必须被运行时权限检查拒绝（即使模型幻觉
    构造了工具调用）。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        result = skill_stack_service.runtime.execute_tool(
            tool_name="mx_moni_trade",
            arguments={
                "action": "BUY",
                "symbol": "600519",
                "quantity": 100,
                "price_type": "MARKET",
            },
            context={"run_type": "analysis", "client": None, "app_settings": None},
        )
        assert result["ok"] is False
        assert "不允许在 analysis 任务中执行" in str(result.get("error") or "")
    finally:
        _reset_db_state()


def test_execute_tool_allows_trade_tool_in_trade_run(monkeypatch, tmp_path) -> None:
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        result = skill_stack_service.runtime.execute_tool(
            tool_name="mx_moni_trade",
            arguments={
                "action": "BUY",
                "symbol": "600519",
                "quantity": 100,
                "price_type": "MARKET",
            },
            context={"run_type": "trade", "client": None, "app_settings": None},
        )
        # 权限通过（后续因非交易时段/缺 client 失败是另一层挡板）
        assert "不允许在 trade 任务中执行" not in str(result.get("error") or "")
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 日志：HTTP 200 + 业务失败必须记为 failed
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        import json as _json

        self.status_code = 200
        self.content = _json.dumps(payload).encode()
        self.text = self.content.decode()

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        import json as _json

        return _json.loads(self.text)


def test_post_business_error_records_failed_log(monkeypatch, tmp_path) -> None:
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    class _FakeHttp:
        def post(self, url: str, json: dict) -> _FakeResponse:
            return _FakeResponse({"status": 100, "message": "输入价格不合理"})

    try:
        client = MXClient.__new__(MXClient)
        client.api_key = "k"
        client.base_url = "https://example.com"
        client._client = _FakeHttp()

        with pytest.raises(RuntimeError, match="status=100"):
            client._post("/mockTrading/trade", {"a": 1})

        with session_scope() as db:
            logs = (
                db.query(ToolInvocationLog)
                .order_by(ToolInvocationLog.id.desc())
                .limit(1)
                .all()
            )
            assert len(logs) == 1
            assert logs[0].status == "failed"
            assert "status=100" in str(logs[0].error_summary or "")
    finally:
        _reset_db_state()


def test_instrument_invocation_error_summary_marks_failed(monkeypatch, tmp_path) -> None:
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        with instrument_invocation("/mockTrading/trade", {"a": 1}) as invocation:
            invocation.observe(status_code=200, response_size=10)
            invocation.observe(status_code=200, response_size=10, error_summary="业务失败")

        with session_scope() as db:
            logs = (
                db.query(ToolInvocationLog)
                .order_by(ToolInvocationLog.id.desc())
                .limit(1)
                .all()
            )
            assert logs[0].status == "failed"
            assert logs[0].error_summary == "业务失败"
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# 补丁 B：自选股操作意图解析 + 前后状态核验
# ---------------------------------------------------------------------------


class _SelfSelectClient:
    """自选股桩：维护内存列表，模拟远端状态。"""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        manage_fails: bool = False,
        read_fails: bool = False,
        manage_no_effect: bool = False,
    ) -> None:
        self.rows = rows or []
        self.manage_calls: list[str] = []
        self._manage_fails = manage_fails
        self._read_fails = read_fails
        self._manage_no_effect = manage_no_effect

    def get_self_selects(self) -> dict:
        if self._read_fails:
            raise RuntimeError("读取失败")
        return {"data": {"rows": self.rows}}

    def manage_self_select(self, query: str) -> dict:
        self.manage_calls.append(query)
        if self._manage_fails:
            raise RuntimeError("写失败")
        if self._manage_no_effect:
            # 模拟审计场景：接口返回成功但列表实际未变更
            return {"code": "200", "message": "成功"}
        # 模拟远端真实生效：按解析出的代码变更内存列表
        import re as _re

        code = (_re.findall(r"\d{6}", query) or [""])[0]
        is_add = any(m in query for m in ("加入", "添加", "放入", "纳入"))
        if code:
            if is_add:
                if not any(r.get("stockCode") == code for r in self.rows):
                    self.rows.append({"stockCode": code, "stockName": ""})
            else:
                self.rows = [r for r in self.rows if r.get("stockCode") != code]
        return {"code": "200", "message": "成功"}


def test_manage_self_select_add_verified_by_state_readback() -> None:
    from skills.mx_core.execution import mx_execution_service

    client = _SelfSelectClient(rows=[])
    result = mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "将600519加入自选股"},
    )

    assert result["ok"] is True
    assert "回查确认" in result["summary"]
    assert result["executed_action"]["verified"] is True
    assert len(client.manage_calls) == 1


def test_manage_self_select_add_noop_when_already_in_list() -> None:
    """目标已在自选股列表：NOOP，不发送写请求（幂等）。"""
    from skills.mx_core.execution import mx_execution_service

    client = _SelfSelectClient(
        rows=[{"stockCode": "600519", "stockName": "贵州茅台"}]
    )
    result = mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "将600519加入自选股"},
    )

    assert result["ok"] is True
    assert result["executed_action"]["noop"] is True
    assert client.manage_calls == []  # 未发送写请求


def test_manage_self_select_remove_noop_when_not_in_list() -> None:
    from skills.mx_core.execution import mx_execution_service

    client = _SelfSelectClient(rows=[])
    result = mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "将600519从自选股删除"},
    )

    assert result["ok"] is True
    assert result["executed_action"]["noop"] is True
    assert client.manage_calls == []


def test_manage_self_select_unverified_state_reports_unknown() -> None:
    """接口返回成功但回查未确认目标状态：不得声称完成（UNKNOWN）。"""
    from skills.mx_core.execution import mx_execution_service

    # manage 不真正生效（列表不变）：ADD 后回查仍不在列表
    client = _SelfSelectClient(rows=[], manage_no_effect=True)
    result = mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "将600519加入自选股"},
    )

    assert result["ok"] is False
    assert "回查未确认目标状态" in str(result.get("error") or "")
    assert "mx_get_self_selects" in str(result.get("error") or "")
    assert len(client.manage_calls) == 1  # 写请求只发过一次


def test_manage_self_select_readback_failure_reports_unknown() -> None:
    """写请求已发出但回查读取失败：UNKNOWN，不自动重试。"""
    from skills.mx_core.execution import mx_execution_service

    client = _SelfSelectClient(rows=[], read_fails=True)
    result = mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "将600519加入自选股"},
    )

    assert result["ok"] is False
    assert "回查失败" in str(result.get("error") or "")
    assert len(client.manage_calls) == 1


def test_manage_self_select_ambiguous_intent_rejected_without_write() -> None:
    """意图无法唯一确定（既非新增也非删除）：拒绝且不发送任何写请求。"""
    from skills.mx_core.execution import mx_execution_service

    client = _SelfSelectClient(rows=[])
    result = mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "操作一下600519的自选股"},
    )

    assert result["ok"] is False
    assert "无法唯一确定" in str(result.get("error") or "")
    assert client.manage_calls == []


def test_manage_self_select_name_and_code_resolve_to_same_target() -> None:
    """名称与代码两种写法解析为同一意图（审计清单 #8）。"""
    from skills.mx_core.execution import mx_execution_service

    by_code = mx_execution_service._parse_self_select_intent("将600519加入自选股")
    by_name = mx_execution_service._parse_self_select_intent("把贵州茅台加入自选股")

    assert by_code is not None and by_code[0] == "ADD"
    assert by_name is not None and by_name[0] == "ADD"
    # 代码写法提取 600519；名称写法提取名称（回查时按名称匹配）
    assert by_code[1] == "600519"
    assert by_name[2] == "贵州茅台"


def test_manage_self_select_add_remove_add_sequence_allowed() -> None:
    """先 ADD、再 REMOVE、又因新原因 ADD：合法状态变更不被误拦（清单 #7）。"""
    from skills.mx_core.execution import mx_execution_service

    client = _SelfSelectClient(rows=[])
    r1 = mx_execution_service.execute_tool(
        client=client, app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "将600519加入自选股"},
    )
    assert r1["ok"] is True
    client.rows = [{"stockCode": "600519", "stockName": "贵州茅台"}]

    r2 = mx_execution_service.execute_tool(
        client=client, app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "将600519从自选股删除"},
    )
    assert r2["ok"] is True
    client.rows = []

    r3 = mx_execution_service.execute_tool(
        client=client, app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "将600519加入自选股"},
    )
    assert r3["ok"] is True
    assert len(client.manage_calls) == 3
