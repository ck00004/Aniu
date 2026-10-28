"""P0 移植功能的回归测试。

覆盖：token 估算修正、LLM 错误分类与重试、usage 累计、运行中止、
交易预检、交易时段写门禁、工具调用日志。
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.rate_limit import _limiter
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import StrategyRun
from app.main import create_app
from app.services.aniu_service import aniu_service
from app.services.llm_service import (
    LLMErrorCode,
    LLMUpstreamError,
    _classify_error_text,
    _classify_http_status,
    _is_retryable_upstream_error,
    _retry_after_seconds_from_headers,
    _should_retry_header,
    accumulate_usage,
    llm_service,
)
from app.services.run_abort_service import RunAbortRegistry
from app.services.scheduler_service import scheduler_service
from app.services.token_estimator import (
    estimate_messages_tokens,
    estimate_text_tokens,
)
from app.services.tool_invocation_log_service import (
    instrument_invocation,
    record_invocation,
    tool_invocation_log_service,
)
from app.services.trading_calendar_service import trading_calendar_service
from skills.mx_core import execution as mx_execution
from skills.mx_core.execution import (
    is_market_open,
    preflight_cancel,
    preflight_trade,
)


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
# P0-6：token 估算修正
# ---------------------------------------------------------------------------


def test_token_estimator_counts_cjk_full() -> None:
    # 8 个汉字：旧算法 (8+3)//4=2，新算法全额 8
    assert estimate_text_tokens("贵州茅台五粮液") == 7  # 7 个汉字
    assert estimate_text_tokens("贵州茅台五粮液啊") == 8


def test_token_estimator_counts_ascii_quarter() -> None:
    assert estimate_text_tokens("abcdefgh") == 2
    assert estimate_text_tokens("abcd") == 1


def test_token_estimator_mixed_content() -> None:
    # 2 CJK + 4 ASCII = 2 + 1
    assert estimate_text_tokens("买入abcd") == 3


def test_token_estimator_messages_cjk_aware() -> None:
    messages = [{"role": "user", "content": "你好世界"}]
    estimate = estimate_messages_tokens(messages)
    # 4 个 CJK 全额计入，远大于旧算法的 1
    assert estimate >= 4


# ---------------------------------------------------------------------------
# P0-3：LLM 错误分类 + 重试
# ---------------------------------------------------------------------------


def test_classify_http_status() -> None:
    assert _classify_http_status(401) == LLMErrorCode.AUTHENTICATION
    assert _classify_http_status(403) == LLMErrorCode.AUTHENTICATION
    assert _classify_http_status(408) == LLMErrorCode.TIMEOUT
    assert _classify_http_status(429) == LLMErrorCode.RATE_LIMIT
    assert _classify_http_status(500) == LLMErrorCode.PROVIDER_5XX
    assert _classify_http_status(404) == LLMErrorCode.PROVIDER_4XX
    assert _classify_http_status(None) == LLMErrorCode.UNKNOWN


def test_classify_error_text_semantic_markers() -> None:
    assert _classify_error_text("Rate limit exceeded") == LLMErrorCode.RATE_LIMIT
    assert (
        _classify_error_text("This model's maximum context length is 4096 tokens")
        == LLMErrorCode.CONTEXT_OVERFLOW
    )
    assert _classify_error_text("The server is overloaded") == LLMErrorCode.PROVIDER_5XX
    assert _classify_error_text("some other error") is None


def test_rate_limit_and_timeout_are_retryable() -> None:
    """旧实现只认 5xx；429 限频与超时/网络错误现在必须可重试。"""
    rate_limited = LLMUpstreamError("限频", status_code=429)
    assert _is_retryable_upstream_error(rate_limited) is True

    timeout = LLMUpstreamError("超时", error_code=LLMErrorCode.TIMEOUT)
    assert _is_retryable_upstream_error(timeout) is True

    network = LLMUpstreamError("网络", error_code=LLMErrorCode.NETWORK)
    assert _is_retryable_upstream_error(network) is True


def test_context_overflow_and_auth_not_retryable() -> None:
    overflow = LLMUpstreamError(
        "maximum context length", error_code=LLMErrorCode.CONTEXT_OVERFLOW
    )
    assert _is_retryable_upstream_error(overflow) is False

    auth = LLMUpstreamError("key invalid", status_code=401)
    assert _is_retryable_upstream_error(auth) is False


def test_retryable_override_header() -> None:
    overridden = LLMUpstreamError(
        "bad request", status_code=400, retryable_override=True
    )
    assert _is_retryable_upstream_error(overridden) is True


class _FakeHeaders:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._values.get(name, default)


def test_retry_after_seconds_formats() -> None:
    assert _retry_after_seconds_from_headers(_FakeHeaders({"retry-after": "2.5"})) == 2.5
    assert (
        _retry_after_seconds_from_headers(_FakeHeaders({"retry-after-ms": "1500"}))
        == 1.5
    )
    assert _retry_after_seconds_from_headers(_FakeHeaders({})) is None
    assert _retry_after_seconds_from_headers(None) is None


def test_retry_after_http_date_format() -> None:
    from datetime import timedelta

    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    value = _retry_after_seconds_from_headers(_FakeHeaders({"retry-after": http_date}))
    assert value is not None
    assert 25 <= value <= 30


def test_should_retry_header() -> None:
    assert _should_retry_header(_FakeHeaders({"x-should-retry": "true"})) is True
    assert _should_retry_header(_FakeHeaders({"x-should-retry": "false"})) is False
    assert _should_retry_header(_FakeHeaders({})) is None


def test_raise_upstream_http_error_attaches_retry_after() -> None:
    import httpx

    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(
        429,
        request=request,
        headers={"retry-after": "7"},
        content=b'{"error":{"message":"rate limit"}}',
    )
    from app.services.llm_service import _raise_upstream_http_error

    with pytest.raises(LLMUpstreamError) as exc_info:
        _raise_upstream_http_error(response, response.content)
    assert exc_info.value.retry_after == 7.0
    assert exc_info.value.error_code == LLMErrorCode.RATE_LIMIT


# ---------------------------------------------------------------------------
# P0-4：usage 累计
# ---------------------------------------------------------------------------


def test_accumulate_usage_sums_across_responses() -> None:
    responses = [
        {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}},
        {"usage": {"prompt_tokens": 300, "completion_tokens": 80, "total_tokens": 380}},
        {"no_usage": True},
    ]
    totals = accumulate_usage(responses)
    assert totals == {
        "prompt_tokens": 400,
        "completion_tokens": 130,
        "total_tokens": 530,
    }


def test_accumulate_usage_handles_missing_total() -> None:
    totals = accumulate_usage(
        [{"usage": {"prompt_tokens": 10, "completion_tokens": 5}}]
    )
    assert totals["total_tokens"] == 15


def test_accumulate_usage_empty() -> None:
    assert accumulate_usage([]) is None
    assert accumulate_usage([{"no_usage": True}]) is None
    assert accumulate_usage(None) is None


def test_extract_usage_sums_across_run_responses() -> None:
    payload = {
        "responses": [
            {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}},
            {"usage": {"prompt_tokens": 200, "completion_tokens": 60, "total_tokens": 260}},
        ]
    }
    usage = aniu_service._extract_usage(payload)
    assert usage["prompt_tokens"] == 300
    assert usage["completion_tokens"] == 110
    assert usage["total_tokens"] == 410


def test_chat_usage_recorded_in_meta_payload(monkeypatch, tmp_path) -> None:
    """聊天路径的 usage 应随助手消息落库到 meta_payload。"""
    from app.db.models import ChatMessageRecord
    from app.schemas.aniu import ChatStreamRequest
    from app.services.chat_session_service import chat_session_service

    def fake_chat(*, messages, emit=None, **kwargs):
        if emit:
            emit("final_started")
            emit("final_delta", delta="回答")
            emit("completed", message="回答")
        return {
            "final_answer": "回答",
            "replay_messages": [],
            "responses": [
                {"usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}}
            ],
            "messages": [],
        }

    import app.services.chat_session_service as chat_module

    monkeypatch.setattr(chat_module.llm_service, "chat_with_details", fake_chat)

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "key"
            settings.llm_model = "model"
            session = chat_session_service.create_session(db, title="usage")
            session_id = session.id

        list(
            chat_session_service.stream_chat(
                ChatStreamRequest(session_id=session_id, content="问题")
            )
        )

        with session_scope() as db:
            records = (
                db.query(ChatMessageRecord)
                .filter(ChatMessageRecord.session_id == session_id)
                .order_by(ChatMessageRecord.id.asc())
                .all()
            )
            assistant_record = records[-1]
            assert assistant_record.meta_payload is not None
            assert assistant_record.meta_payload["usage"] == {
                "prompt_tokens": 30,
                "completion_tokens": 10,
                "total_tokens": 40,
            }

    reset_db_state()


# ---------------------------------------------------------------------------
# P0-5：运行中止
# ---------------------------------------------------------------------------


def test_run_abort_registry_lifecycle() -> None:
    registry = RunAbortRegistry()
    event = registry.register(1)
    assert not event.is_set()
    assert registry.abort(1) is True
    assert event.is_set()
    registry.unregister(1)
    assert registry.abort(1) is False


def test_abort_endpoint_returns_404_for_unknown_run(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = auth_headers(client)
        response = client.post("/api/aniu/runs/999999/abort", headers=headers)
        assert response.status_code == 404
    reset_db_state()


def test_abort_endpoint_returns_409_for_finished_run(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
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
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = auth_headers(client)
        response = client.post(f"/api/aniu/runs/{run_id}/abort", headers=headers)
        assert response.status_code == 409
    reset_db_state()


def test_abort_endpoint_marks_orphan_running_run_failed(monkeypatch, tmp_path) -> None:
    """running 状态但注册表无活动事件（如进程刚重启）→ 直接收敛为失败。"""
    with create_test_client(monkeypatch, tmp_path) as client:
        # 在客户端启动后创建 running 记录（启动时的僵尸收敛不会碰它）
        with session_scope() as db:
            db.add(StrategyRun(status="running", run_type="analysis"))
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

        headers = auth_headers(client)
        response = client.post(f"/api/aniu/runs/{run_id}/abort", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"run_id": run_id, "aborted": True}

        with session_scope() as db:
            run = db.get(StrategyRun, run_id)
            assert run.status == "failed"
            assert "中止" in run.error_message
    reset_db_state()


def test_run_body_abort_marks_run_failed(monkeypatch, tmp_path) -> None:
    """运行线程感知中止事件后：run 标记 failed 且错误信息明确。"""
    from app.services.aniu_service import LLMStreamCancelled
    from app.services.run_abort_service import run_abort_registry

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            db.commit()

        # 构造一个已中止的事件，_run_body 在预取后检查点感知
        cancel_event = run_abort_registry.register(999999)
        cancel_event.set()

        monkeypatch.setattr(
            aniu_service,
            "_prefetch_analysis_context",
            lambda **kwargs: ({}, {}),
        )

        run_id, settings_snapshot = aniu_service._prepare_run(
            "manual", None, "analysis"
        )
        run_abort_registry.unregister(999999)
        cancel_event = run_abort_registry.register(run_id)
        cancel_event.set()

        with pytest.raises(LLMStreamCancelled):
            aniu_service._run_body(
                run_id=run_id,
                settings_snapshot=settings_snapshot,
                trigger_source="manual",
                schedule_id=None,
                cancel_event=cancel_event,
            )
        run_abort_registry.unregister(run_id)

        with session_scope() as db:
            run = db.get(StrategyRun, run_id)
            assert run.status == "failed"
            assert "中止" in run.error_message

    reset_db_state()


# ---------------------------------------------------------------------------
# P0-1：交易预检
# ---------------------------------------------------------------------------


class _FakeMXClient:
    def __init__(
        self,
        *,
        balance: dict | None = None,
        positions: dict | None = None,
        orders: dict | None = None,
    ) -> None:
        self._balance = balance or {}
        self._positions = positions or {}
        self._orders = orders or {}

    def get_balance(self) -> dict:
        return self._balance

    def get_positions(self) -> dict:
        return self._positions

    def get_orders(self) -> dict:
        return self._orders


def test_preflight_buy_limit_insufficient_cash() -> None:
    client = _FakeMXClient(
        balance={"data": {"availBalance": 10000.0}},
    )
    with pytest.raises(RuntimeError, match="超过当前可用资金"):
        preflight_trade(
            client=client,
            action="BUY",
            symbol="600519",
            quantity=1000,
            price_type="LIMIT",
            price=20.0,
        )


def test_preflight_buy_limit_sufficient_cash() -> None:
    client = _FakeMXClient(balance={"data": {"availBalance": 50000.0}})
    preflight_trade(
        client=client,
        action="BUY",
        symbol="600519",
        quantity=1000,
        price_type="LIMIT",
        price=20.0,
    )


def test_preflight_sell_without_position() -> None:
    client = _FakeMXClient(positions={"data": {"rows": []}})
    with pytest.raises(RuntimeError, match="可卖数量 0"):
        preflight_trade(
            client=client,
            action="SELL",
            symbol="600519",
            quantity=100,
            price_type="MARKET",
            price=None,
        )


def test_preflight_sell_exceeds_available() -> None:
    client = _FakeMXClient(
        positions={
            "data": {
                "rows": [
                    {"stockCode": "600519", "count": 1000, "availCount": 500}
                ]
            }
        }
    )
    with pytest.raises(RuntimeError, match="可卖数量 500"):
        preflight_trade(
            client=client,
            action="SELL",
            symbol="600519",
            quantity=800,
            price_type="MARKET",
            price=None,
        )


def test_preflight_sell_accounts_for_pending_orders() -> None:
    """持仓 1000、在途卖出 400 → 可卖 600；卖 800 必须被拦下。"""
    client = _FakeMXClient(
        positions={"data": {"rows": [{"stockCode": "600519", "count": 1000}]}},
        orders={
            "data": {
                "rows": [
                    {
                        "orderId": "A1",
                        "stockCode": "600519",
                        "orderDrt": "2",
                        "orderStatus": "1",
                        "orderCount": 400,
                        "dealCount": 0,
                    }
                ]
            }
        },
    )
    with pytest.raises(RuntimeError, match="在途卖出委托占用 400"):
        preflight_trade(
            client=client,
            action="SELL",
            symbol="600519",
            quantity=800,
            price_type="MARKET",
            price=None,
        )


def test_preflight_cancel_unknown_order() -> None:
    client = _FakeMXClient(orders={"data": {"rows": []}})
    with pytest.raises(RuntimeError, match="未找到委托编号"):
        preflight_cancel(client=client, order_id="X1", stock_code=None)


def test_preflight_cancel_completed_order() -> None:
    client = _FakeMXClient(
        orders={
            "data": {
                "rows": [
                    {
                        "orderId": "A1",
                        "stockCode": "600519",
                        "orderStatus": "4",
                        "orderCount": 100,
                        "dealCount": 100,
                    }
                ]
            }
        }
    )
    with pytest.raises(RuntimeError, match="不可撤销"):
        preflight_cancel(client=client, order_id="A1", stock_code="600519")


def test_preflight_cancel_pending_order_passes() -> None:
    client = _FakeMXClient(
        orders={
            "data": {
                "rows": [
                    {
                        "orderId": "A1",
                        "stockCode": "600519",
                        "orderStatus": "1",
                        "orderCount": 100,
                        "dealCount": 0,
                    }
                ]
            }
        }
    )
    preflight_cancel(client=client, order_id="A1", stock_code="600519")


# ---------------------------------------------------------------------------
# P0-2：交易时段写门禁
# ---------------------------------------------------------------------------


class _FakeDateTime:
    """固定时间的 datetime 替身（is_market_open 内部调用 datetime.now）。"""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz=None) -> datetime:
        return self._fixed.astimezone(tz) if tz else self._fixed


def _set_market_time(
    monkeypatch, hour: int, minute: int, *, trading_day: bool = True
) -> None:
    # 2026-10-15 是周四（交易日），2026-10-17 是周六
    day = 15 if trading_day else 17
    fixed = datetime(2026, 10, day, hour, minute, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(mx_execution, "datetime", _FakeDateTime(fixed))
    monkeypatch.setattr(
        trading_calendar_service,
        "is_trading_day",
        lambda date: date.weekday() < 5,
    )


def test_market_open_during_morning_session(monkeypatch) -> None:
    _set_market_time(monkeypatch, 10, 30)
    assert is_market_open() is True


def test_market_open_during_afternoon_session(monkeypatch) -> None:
    _set_market_time(monkeypatch, 14, 0)
    assert is_market_open() is True


def test_market_closed_during_lunch_break(monkeypatch) -> None:
    _set_market_time(monkeypatch, 12, 0)
    assert is_market_open() is False


def test_market_closed_before_open(monkeypatch) -> None:
    _set_market_time(monkeypatch, 9, 0)
    assert is_market_open() is False


def test_market_closed_after_close(monkeypatch) -> None:
    _set_market_time(monkeypatch, 15, 30)
    assert is_market_open() is False


def test_market_closed_on_weekend(monkeypatch) -> None:
    _set_market_time(monkeypatch, 10, 30, trading_day=False)
    assert is_market_open() is False


def test_trade_handler_blocked_outside_market_hours(monkeypatch) -> None:
    """非交易时段调用交易工具：返回 ok=False 且带中文原因（不抛异常）。"""
    from skills.mx_core.execution import mx_execution_service

    _set_market_time(monkeypatch, 12, 0)
    result = mx_execution_service.execute_tool(
        client=None,
        app_settings=None,
        tool_name="mx_moni_trade",
        arguments={
            "action": "BUY",
            "symbol": "600519",
            "quantity": 1000,
            "price_type": "MARKET",
        },
    )
    assert result["ok"] is False
    assert "非交易时段" in result["error"]


# ---------------------------------------------------------------------------
# P0-7：工具调用日志
# ---------------------------------------------------------------------------


def test_record_and_list_tool_invocation_logs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "toollogs.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    record_invocation(
        operation="/api/claw/query",
        request_payload={"toolQuery": "上证指数"},
        status="success",
        status_code=200,
        duration_ms=120,
        response_size=2048,
    )
    record_invocation(
        operation="/api/claw/mockTrading/trade",
        request_payload={"type": "buy"},
        status="failed",
        duration_ms=80,
        error_summary="connection refused",
    )

    with session_scope() as db:
        payload = tool_invocation_log_service.list_logs(db, limit=10)
        assert payload["total"] == 2
        assert payload["items"][0]["operation"] == "/api/claw/mockTrading/trade"
        assert payload["items"][0]["status"] == "failed"
        assert payload["items"][1]["status"] == "success"

        summary = tool_invocation_log_service.summarize(db)
        assert summary["total"] == 2
        assert summary["failed"] == 2 - 1

    reset_db_state()


def test_instrument_invocation_records_failure() -> None:
    recorded: dict[str, object] = {}

    import app.services.tool_invocation_log_service as log_module

    original = log_module.record_invocation

    def fake_record(**kwargs):
        recorded.update(kwargs)

    log_module.record_invocation = fake_record
    try:
        with instrument_invocation("/test", {"a": 1}) as invocation:
            invocation.observe(status_code=200, response_size=10)
        assert recorded["status"] == "success"
        assert recorded["response_size"] == 10

        recorded.clear()
        try:
            with instrument_invocation("/test", None):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert recorded["status"] == "failed"
        assert "boom" in recorded["error_summary"]
    finally:
        log_module.record_invocation = original


def test_tool_invocation_logs_api_endpoint(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "toolapi.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None

    with create_test_client(monkeypatch, tmp_path) as client:
        record_invocation(
            operation="/api/claw/query",
            status="success",
            duration_ms=50,
        )
        headers = auth_headers(client)
        response = client.get(
            "/api/aniu/tool-invocation-logs?limit=10", headers=headers
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] >= 1
        assert payload["summary"]["total"] >= 1
        assert any(
            item["operation"] == "/api/claw/query" for item in payload["items"]
        )

        # 未认证请求被拒绝
        unauthenticated = client.get("/api/aniu/tool-invocation-logs")
        assert unauthenticated.status_code == 401

    reset_db_state()


def test_market_open_during_call_auction(monkeypatch) -> None:
    """09:15-09:25 集合竞价时段允许交易（2026-10 调整：上午起点 09:30→09:15）。"""
    _set_market_time(monkeypatch, 9, 15)
    assert is_market_open() is True

    _set_market_time(monkeypatch, 9, 20)
    assert is_market_open() is True


def test_market_closed_just_before_call_auction(monkeypatch) -> None:
    _set_market_time(monkeypatch, 9, 14)
    assert is_market_open() is False
