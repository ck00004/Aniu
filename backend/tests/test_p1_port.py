"""P1 移植功能的回归测试。

覆盖：长期记忆系统、上下文预算与溢出恢复、SSE 断线续传、每日运行报告。
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.rate_limit import _limiter
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import MemoryActivity, MemoryItem, StrategyRun, TradeOrder
from app.main import create_app
from app.services.aniu_service import aniu_service, now_utc
from app.services.context_compaction import (
    compact_messages_for_overflow,
    trim_messages_to_budget,
)
from app.services.daily_report_service import apply_evidence_budget
from app.services.daily_report_service import daily_report_service
from app.services.event_bus import EventBus
from app.services.llm_service import (
    LLMErrorCode,
    LLMService,
    LLMUpstreamError,
)
from app.services.memory_service import MemoryVersionConflict, memory_service
from app.services.scheduler_service import scheduler_service
from app.services.token_estimator import estimate_messages_tokens
from app.services.trading_calendar_service import trading_calendar_service
from app.services.skill_stack_service import skill_stack_service


def create_test_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(
        trading_calendar_service, "warm_up_months", lambda current: None
    )
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()
    aniu_service._account_overview_cache = None
    aniu_service._account_overview_cache_expires_at = None
    app = create_app()
    return TestClient(app)


def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/aniu/login", json={"password": "release-pass"})
    payload = response.json()
    return {"Authorization": f"Bearer {payload['token']}"}


def reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# P1-8：长期记忆系统
# ---------------------------------------------------------------------------


def test_memory_agent_search_any_and_all_modes(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            memory_service.create_memory(
                db, content="白酒板块高开低走时不宜追高", reason="多次验证"
            )
            memory_service.create_memory(
                db, content="半导体放量突破后回踩确认更稳", reason="2026-09 验证"
            )

        any_result = memory_service.search_for_agent(
            keywords=["白酒"], match_mode="any"
        )
        assert any_result["ok"] is True
        assert any_result["result_count"] == 1
        assert "白酒" in any_result["items"][0]["content"]

        all_result = memory_service.search_for_agent(
            keywords=["白酒", "高开"], match_mode="all"
        )
        assert all_result["result_count"] == 1

        miss = memory_service.search_for_agent(
            keywords=["白酒", "不存在"], match_mode="all"
        )
        assert miss["result_count"] == 0

        with session_scope() as db:
            activities = memory_service.list_activities(db)
            assert activities["total"] == 5  # 2 次 create + 3 次 read
    reset_db_state()


def test_memory_agent_write_with_version_conflict(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        created = memory_service.write_for_agent(
            action="create", content="追涨杀跌是大忌", reason="教训"
        )
        assert created["ok"] is True
        memory_id = created["item"]["id"]
        version = created["item"]["version"]

        # 错误版本号 → 冲突
        conflict = memory_service.write_for_agent(
            action="update",
            memory_id=memory_id,
            content="追涨杀跌是大忌（修订）",
            expected_version=version + 99,
        )
        assert conflict["ok"] is False
        assert conflict.get("conflict") is True

        # 正确版本号 → 更新成功且版本 +1
        updated = memory_service.write_for_agent(
            action="update",
            memory_id=memory_id,
            content="追涨杀跌是大忌（修订）",
            expected_version=version,
        )
        assert updated["ok"] is True
        assert updated["item"]["version"] == version + 1

        # 软删除后不可检索
        deleted = memory_service.write_for_agent(
            action="delete", memory_id=memory_id, expected_version=version + 1
        )
        assert deleted["ok"] is True
        search = memory_service.search_for_agent(keywords=["追涨杀跌"])
        assert search["result_count"] == 0
    reset_db_state()


def test_memory_skill_tools_via_runtime(monkeypatch, tmp_path) -> None:
    """memory_read / memory_write 经生产技能运行时端到端可用。"""
    with create_test_client(monkeypatch, tmp_path):
        write = skill_stack_service.runtime.execute_tool(
            tool_name="memory_write",
            arguments={
                "action": "create",
                "content": "放量滞涨应减仓",
                "reason": "量价背离信号",
            },
            context={},
        )
        assert write["ok"] is True

        read = skill_stack_service.runtime.execute_tool(
            tool_name="memory_read",
            arguments={"keywords": ["放量"]},
            context={},
        )
        assert read["ok"] is True
        assert read["result_count"] == 1
        assert "放量滞涨" in read["items"][0]["content"]
    reset_db_state()


def test_memory_api_crud_and_conflict(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = auth_headers(client)

        created = client.post(
            "/api/aniu/memories",
            json={"content": "记忆内容", "reason": "原因"},
            headers=headers,
        )
        assert created.status_code == 200
        item = created.json()
        assert item["version"] == 1
        memory_id = item["id"]

        listed = client.get("/api/aniu/memories", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        searched = client.get(
            "/api/aniu/memories?query=记忆", headers=headers
        )
        assert searched.json()["total"] == 1

        # 版本冲突 → 409
        conflict = client.put(
            f"/api/aniu/memories/{memory_id}",
            json={"content": "新内容", "expected_version": 99},
            headers=headers,
        )
        assert conflict.status_code == 409

        updated = client.put(
            f"/api/aniu/memories/{memory_id}",
            json={"content": "新内容", "expected_version": 1},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 2

        deleted = client.delete(
            f"/api/aniu/memories/{memory_id}?expected_version=2", headers=headers
        )
        assert deleted.status_code == 200

        # 软删除后列表为空
        assert client.get("/api/aniu/memories", headers=headers).json()["total"] == 0

        activities = client.get("/api/aniu/memory-activities", headers=headers)
        assert activities.status_code == 200
        actions = [item["action"] for item in activities.json()["items"]]
        assert "create" in actions and "update" in actions and "delete" in actions

        # 未认证被拒绝
        assert client.get("/api/aniu/memories").status_code == 401
    reset_db_state()


def test_memory_protocol_injected_into_run_prompt() -> None:
    """记忆协议随技能提示词补充注入运行系统提示词。"""
    from app.skills import skill_registry

    supplement = skill_registry.build_prompt_supplement(run_type="analysis")
    assert "memory_read" in supplement
    assert "memory_write" in supplement


# ---------------------------------------------------------------------------
# P1-9：上下文预算与溢出恢复
# ---------------------------------------------------------------------------


def test_compact_messages_under_budget_unchanged() -> None:
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]
    compacted, stats = compact_messages_for_overflow(messages, target_tokens=10000)
    assert compacted == messages
    assert stats == {"dropped": 0, "compacted": 0}


def test_compact_messages_compresses_tool_results_and_keeps_system() -> None:
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "第一个问题" * 100},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {"name": "mx_query_market", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "巨大的工具结果" * 2000},
        {"role": "user", "content": "最新问题"},
    ]
    compacted, stats = compact_messages_for_overflow(messages, target_tokens=500)
    assert stats["compacted"] >= 1
    # system 保留
    assert compacted[0]["role"] == "system"
    # 工具结果被压缩为占位符
    tool_messages = [m for m in compacted if m.get("role") == "tool"]
    for message in tool_messages:
        assert "工具结果已压缩" in str(message.get("content"))
    # 最新消息保留
    assert compacted[-1]["content"] == "最新问题"
    # 压缩后确实更小
    assert estimate_messages_tokens(compacted) < estimate_messages_tokens(messages)


def test_compact_messages_drops_oldest_when_still_over() -> None:
    messages = [{"role": "system", "content": "系统"}]
    for index in range(20):
        messages.append({"role": "user", "content": f"问题{index}" * 200})
    messages.append({"role": "user", "content": "最新"})
    compacted, stats = compact_messages_for_overflow(messages, target_tokens=1500)
    assert stats["dropped"] > 0
    assert compacted[0]["role"] == "system"
    assert compacted[-1]["content"] == "最新"
    assert estimate_messages_tokens(compacted) <= 1500 + 200  # 允许少量结构开销


def test_trim_messages_to_budget_drops_oldest() -> None:
    messages = [
        {"role": "user", "content": f"消息{index}" * 100} for index in range(10)
    ]
    trimmed, dropped = trim_messages_to_budget(messages, budget_tokens=500)
    assert dropped > 0
    assert len(trimmed) < len(messages)
    # 保留最后一条
    assert "消息9" in trimmed[-1]["content"]


def test_agent_loop_recovers_from_context_overflow(monkeypatch) -> None:
    """provider 报上下文溢出后：压缩历史并重试恰好一次。"""
    service = LLMService()
    calls: dict[str, object] = {"count": 0}
    stage_messages: list[str] = []

    def fake_emit(event_type, **data):
        if event_type == "stage":
            stage_messages.append(str(data.get("message") or ""))

    def fake_call_llm_stream(
        *, base_url, api_key, payload, timeout_seconds, emit=None, cancel_event=None
    ):
        calls["count"] = int(calls["count"]) + 1
        if int(calls["count"]) == 1:
            raise LLMUpstreamError(
                "This model's maximum context length is 4096 tokens",
                error_code=LLMErrorCode.CONTEXT_OVERFLOW,
            )
        calls["second_messages"] = payload["messages"]
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "恢复后的最终回答"}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    monkeypatch.setattr(service, "_call_llm_stream", fake_call_llm_stream)

    initial_messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "长问题" * 800},
        {"role": "tool", "content": "巨大工具结果" * 3000},
        {"role": "user", "content": "最新问题"},
    ]
    result = service._agent_loop(
        model="test-model",
        base_url="https://example.com/v1",
        api_key="key",
        initial_messages=initial_messages,
        run_type="analysis",
        timeout_seconds=10,
        tool_executor=lambda *a, **k: {"ok": True, "result": {}},
        emit=fake_emit,
        context_window_tokens=2000,
    )

    assert calls["count"] == 2
    assert result["final_answer"] == "恢复后的最终回答"
    second_messages = calls["second_messages"]
    assert estimate_messages_tokens(second_messages) < estimate_messages_tokens(
        initial_messages
    )
    assert any("压缩历史" in message for message in stage_messages)


def test_agent_loop_reraises_second_overflow() -> None:
    """第二次溢出不再重试（避免无限循环），直接抛出。"""
    service = LLMService()
    calls = {"count": 0}

    def fake_call_llm_stream(**kwargs):
        calls["count"] += 1
        raise LLMUpstreamError(
            "context length exceeded", error_code=LLMErrorCode.CONTEXT_OVERFLOW
        )

    import types

    service_with_mock = types.SimpleNamespace()
    monkeypatch_target = LLMService._agent_loop

    # 直接用 pytest 风格在函数内 monkeypatch 不便，这里手动替换再还原
    original = service._call_llm_stream
    service._call_llm_stream = fake_call_llm_stream
    try:
        with pytest.raises(LLMUpstreamError):
            service._agent_loop(
                model="m",
                base_url="https://x",
                api_key="k",
                initial_messages=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "q"},
                    {"role": "tool", "content": "r"},
                ],
                run_type="analysis",
                timeout_seconds=5,
                tool_executor=lambda *a, **k: {"ok": True},
                context_window_tokens=1000,
            )
        # 第一次 + 压缩重试一次 = 2 次调用
        assert calls["count"] == 2
    finally:
        service._call_llm_stream = original
    del monkeypatch_target


# ---------------------------------------------------------------------------
# P1-10：SSE 断线续传
# ---------------------------------------------------------------------------


def test_event_bus_assigns_monotonic_event_seq() -> None:
    bus = EventBus()
    bus.publish(1, "stage", {"message": "a"})
    bus.publish(1, "stage", {"message": "b"})
    sub, snapshot = bus.subscribe(1)
    seqs = [event["event_seq"] for event in snapshot]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_event_bus_after_seq_filters_history() -> None:
    bus = EventBus()
    for index in range(5):
        bus.publish(2, "stage", {"message": f"e{index}"})
    # 游标 = 3 → 只重放 4、5
    _, snapshot = bus.subscribe(2, after_seq=3)
    assert [event["event_seq"] for event in snapshot] == [4, 5]
    # 无游标 → 全量
    _, full = bus.subscribe(2)
    assert len(full) == 5


def test_event_bus_stream_resumes_from_cursor() -> None:
    import threading

    bus = EventBus()
    for index in range(3):
        bus.publish(3, "stage", {"message": f"e{index}"})
    # 用 stop_event 结束流（无终态事件时流会持续心跳）
    stop = threading.Event()
    stop.set()
    collected = list(bus.stream(3, after_seq=1, stop_event=stop))
    # 只收到 seq=2、3
    assert [event["event_seq"] for event in collected] == [2, 3]


def test_run_events_endpoint_accepts_after_seq(monkeypatch, tmp_path) -> None:
    from app.services.event_bus import event_bus

    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            db.add(StrategyRun(status="completed", run_type="analysis"))
            db.commit()
            run_id = (
                db.execute(
                    __import__("sqlalchemy").select(StrategyRun.id)
                    .order_by(StrategyRun.id.desc())
                    .limit(1)
                )
                .scalars()
                .one()
            )
        event_bus.publish(run_id, "stage", {"message": "第一"})
        event_bus.publish(run_id, "stage", {"message": "第二"})
        # 终态事件让流正常结束（否则 SSE 会无限心跳）
        event_bus.publish(run_id, "completed", {"message": "任务完成"})

        headers = auth_headers(client)
        response = client.get(
            f"/api/aniu/runs/{run_id}/events?after_seq=1", headers=headers
        )
        assert response.status_code == 200
        assert "第二" in response.text
        assert "第一" not in response.text
    reset_db_state()


# ---------------------------------------------------------------------------
# P1-11：每日运行报告（证据预算）
# ---------------------------------------------------------------------------


def test_evidence_budget_keeps_trade_evidence_never_drops() -> None:
    tool_calls = [
        {
            "name": "mx_query_market",
            "result": {"data": "查询结果" * 3000},
        },
        {
            "name": "mx_moni_trade",
            "result": {"ok": True, "executed_action": {"action": "BUY"}},
        },
    ]
    budgeted, omissions = apply_evidence_budget(tool_calls)
    kinds = [entry["kind"] for entry in budgeted]
    assert "trade" in kinds
    trade_entry = next(entry for entry in budgeted if entry["kind"] == "trade")
    assert trade_entry["tool_name"] == "mx_moni_trade"


def test_evidence_budget_drops_oldest_queries_with_omissions() -> None:
    tool_calls = [
        {"name": "mx_query_market", "result": {"data": "x" * 3000}}
        for _ in range(60)
    ]
    budgeted, omissions = apply_evidence_budget(tool_calls)
    assert len(omissions) > 0
    assert len(budgeted) < len(tool_calls)
    # 保留的是较新的查询（后缀）
    assert budgeted, "至少保留部分查询"


def test_daily_report_aggregates_runs_and_trades(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "report.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    now = now_utc().replace(tzinfo=None)
    with session_scope() as db:
        run = StrategyRun(
            status="completed",
            run_type="trade",
            started_at=now - timedelta(minutes=30),
            finished_at=now - timedelta(minutes=29),
            final_answer="当日分析结论",
            skill_payloads={
                "tool_calls": [
                    {"name": "mx_query_market", "result": {"data": "行情"}},
                    {
                        "name": "mx_moni_trade",
                        "result": {"ok": True, "executed_action": {"action": "BUY"}},
                    },
                ]
            },
        )
        db.add(run)
        db.flush()
        db.add(
            TradeOrder(
                run_id=run.id,
                symbol="600519",
                action="BUY",
                quantity=1000,
                price_type="MARKET",
                price=None,
                status="submitted",
            )
        )
        db.commit()

        report = daily_report_service.build_daily_report(db)
        assert report["run_count"] == 1
        assert report["completed_count"] == 1
        assert report["trade_count"] == 1
        assert report["trades"][0]["symbol"] == "600519"
        assert "当日分析结论" in report["markdown"]
        assert "600519" in report["markdown"]
        # 交易证据保留
        section = report["runs"][0]
        assert any(e["kind"] == "trade" for e in section["evidence"])

    reset_db_state()


def test_daily_report_api_endpoint(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = auth_headers(client)
        response = client.get(
            "/api/aniu/runs/daily-report?date=2026-10-15", headers=headers
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["date"] == "2026-10-15"
        assert "markdown" in payload
        assert payload["run_count"] == 0

        assert client.get("/api/aniu/runs/daily-report").status_code == 401
    reset_db_state()
