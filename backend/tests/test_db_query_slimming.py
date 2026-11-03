"""阶段 B 数据库读取瘦身回归测试。

覆盖：
1. 运行列表/概览/每日报告不得读取 llm_request_payload 等大 JSON 列；
2. 运行分页改为 (started_at, id) 双字段游标后不漏行、不重复；
3. 自动化会话历史读取必须把 LIMIT 下推到 SQL；
4. 每日报告不再产生 trade_orders 的 N+1 查询；
5. run 收尾统计快照列的写入与读取一致性。
"""

from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core import rate_limit as rate_limit_module
from app.core.config import get_settings
from app.db import database as database_module
from app.db.database import get_engine, session_scope
from app.db.models import ChatMessageRecord, ChatSession, StrategyRun, TradeOrder
from app.main import create_app
from app.services import daily_report_service
from app.services.aniu_service import aniu_service
from app.services.scheduler_service import scheduler_service
from app.services.trading_calendar_service import trading_calendar_service

_BIG_COLUMN_MARKERS = (
    "llm_request_payload",
    "llm_response_payload",
    "skill_payloads",
    "decision_payload",
    "executed_actions",
)


def create_test_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "slim.db"))
    monkeypatch.setattr(trading_calendar_service, "warm_up_months", lambda current: None)
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    rate_limit_module._limiter.reset()
    aniu_service._account_overview_cache = None
    aniu_service._account_overview_cache_expires_at = None
    return TestClient(create_app())


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/aniu/login", json={"password": "release-pass"})
    return {"Authorization": f"Bearer {response.json()['token']}"}


class _SqlRecorder:
    """记录测试期间执行的 SQL，用于断言"没有读取大字段"。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> "_SqlRecorder":
        self._engine = get_engine()

        def _before(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)

        self._listener = _before
        event.listen(self._engine, "before_cursor_execute", _before)
        return self

    def __exit__(self, *exc_info) -> None:
        event.remove(self._engine, "before_cursor_execute", self._listener)

    def select_statements(self) -> list[str]:
        return [sql for sql in self.statements if sql.lstrip().upper().startswith("SELECT")]

    def assert_no_big_columns(self) -> None:
        statements = self.select_statements()
        assert any("strategy_runs" in sql for sql in statements), (
            "未捕获到 strategy_runs 查询，断言会失效"
        )
        for sql in statements:
            for marker in _BIG_COLUMN_MARKERS:
                assert marker not in sql, f"查询仍在读取大字段 {marker}: {sql}"


def _seed_runs(count: int, *, base: datetime | None = None) -> list[int]:
    base = base or datetime(2026, 11, 3, 2, 0)
    ids: list[int] = []
    with session_scope() as db:
        for index in range(count):
            run = StrategyRun(
                trigger_source="schedule",
                run_type="analysis",
                schedule_name="盘前分析",
                status="completed",
                analysis_summary=f"摘要-{index}",
                api_call_count=index,
                executed_trade_count=0,
                input_tokens=index * 10,
                output_tokens=index,
                total_tokens=index * 11,
                started_at=base - timedelta(minutes=30 * index),
                finished_at=base - timedelta(minutes=30 * index - 5),
                llm_request_payload={"messages": ["x" * 20000]},
                llm_response_payload={"choices": ["y" * 20000]},
                skill_payloads={"tool_calls": [{"name": "mx_query_market"}]},
                decision_payload={"tool_calls": [{"name": "mx_query_market"}]},
                executed_actions=[{"action": "BUY", "symbol": "600000"}],
            )
            db.add(run)
            db.flush()
            ids.append(int(run.id))
    return ids


def test_runs_feed_never_reads_large_payload_columns(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        _seed_runs(5)
        headers = _auth_headers(client)
        with _SqlRecorder() as recorder:
            response = client.get("/api/aniu/runs-feed?limit=5", headers=headers)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 5
    recorder.assert_no_big_columns()


def test_runtime_overview_never_reads_large_payload_columns(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        _seed_runs(3)
        headers = _auth_headers(client)
        with _SqlRecorder() as recorder:
            response = client.get("/api/aniu/runtime-overview", headers=headers)

    assert response.status_code == 200
    recorder.assert_no_big_columns()


def test_runs_feed_dual_cursor_paginates_without_gaps(monkeypatch, tmp_path) -> None:
    """started_at 相同的两行必须靠 id 决出先后，旧游标会漏掉其中一行。"""
    with create_test_client(monkeypatch, tmp_path) as client:
        same_time = datetime(2026, 11, 3, 1, 0)
        _seed_runs(2, base=same_time)
        with session_scope() as db:
            db.add(
                StrategyRun(
                    trigger_source="manual",
                    run_type="analysis",
                    status="completed",
                    started_at=same_time,
                    finished_at=same_time,
                )
            )
        headers = _auth_headers(client)

        first = client.get("/api/aniu/runs-feed?limit=2", headers=headers).json()
        assert first["has_more"] is True
        assert first["next_cursor_id"] is not None
        assert first["next_cursor_started_at"] is not None

        second = client.get(
            "/api/aniu/runs-feed",
            params={
                "limit": 2,
                "before_id": first["next_cursor_id"],
                "before_started_at": first["next_cursor_started_at"],
            },
            headers=headers,
        ).json()

    seen = [item["id"] for item in first["items"]] + [item["id"] for item in second["items"]]
    assert len(seen) == len(set(seen)), "双字段游标翻页出现重复行"
    assert len(seen) == 3, "双字段游标翻页漏行"


def test_persistent_session_history_query_pushes_limit_down(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            session = ChatSession(title="自动化", kind="automation", slug="automation-default")
            db.add(session)
            db.flush()
            for index in range(40):
                db.add(
                    ChatMessageRecord(
                        session_id=session.id,
                        role="user" if index % 2 == 0 else "assistant",
                        content=f"第{index}条",
                        message_kind="live_turn",
                    )
                )
            db.flush()
            session_id = session.id

        with session_scope() as db:
            with _SqlRecorder() as recorder:
                records = aniu_service._list_persistent_session_history_records(
                    db=db, session_id=session_id, recent_limit=6
                )

    assert len(records) == 6
    assert records[-1].content == "第39条"
    history_sql = [
        sql for sql in recorder.select_statements() if "chat_messages" in sql
    ]
    assert history_sql, "未捕获到 chat_messages 查询"
    assert any("LIMIT" in sql.upper() for sql in history_sql), (
        "会话历史查询没有把 LIMIT 下推到 SQL"
    )


def test_daily_report_has_no_trade_order_n_plus_one(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        target_day = datetime(2026, 11, 3, 2, 0)
        with session_scope() as db:
            for index in range(6):
                run = StrategyRun(
                    trigger_source="schedule",
                    run_type="analysis",
                    status="completed",
                    final_answer="结论",
                    skill_payloads={"tool_calls": [{"name": "mx_query_market", "result": {"ok": True, "result": {}}}]},
                    started_at=target_day + timedelta(minutes=index),
                    finished_at=target_day + timedelta(minutes=index, seconds=30),
                )
                db.add(run)
                db.flush()
                db.add(
                    TradeOrder(
                        run_id=run.id,
                        symbol="600000",
                        action="BUY",
                        quantity=100,
                        price_type="MARKET",
                        status="submitted",
                    )
                )

        with session_scope() as db:
            with _SqlRecorder() as recorder:
                report = daily_report_service.DailyReportService().build_daily_report(
                    db, target_date=date(2026, 11, 3)
                )

    assert report["run_count"] == 6
    assert report["trade_count"] == 6
    assert len(report["trades"]) == 6
    trade_order_sql = [
        sql for sql in recorder.select_statements() if "trade_orders" in sql
    ]
    assert len(trade_order_sql) == 1, (
        f"trade_orders 出现 {len(trade_order_sql)} 次查询，N+1 未消除"
    )


def test_run_metrics_snapshot_is_written_from_payloads(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        run = SimpleNamespace(
            llm_response_payload={
                "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}
            },
            llm_request_payload=None,
            skill_payloads={
                "tool_calls": [
                    {"name": "mx_query_market"},
                    {"name": "mx_moni_trade"},
                ]
            },
            decision_payload=None,
            executed_actions=[{"action": "BUY", "symbol": "600000"}],
        )
        aniu_service._hydrate_run_summary_metrics(run)

    assert run.api_call_count == 1
    assert run.executed_trade_count == 1
    assert (run.input_tokens, run.output_tokens, run.total_tokens) == (12, 34, 46)


def _settings_with_mx_key() -> None:
    with session_scope() as db:
        settings = aniu_service.get_or_create_settings(db)
        settings.mx_api_key = "test-key"
        db.add(settings)


def _seed_account_snapshot_run() -> None:
    with session_scope() as db:
        db.add(
            StrategyRun(
                status="completed",
                skill_payloads={
                    "tool_calls": [
                        {"name": "mx_get_balance", "result": {"ok": True, "result": {"total_assets": 100.0}}},
                        {"name": "mx_get_positions", "result": {"ok": True, "result": {"positions": []}}},
                        {"name": "mx_get_orders", "result": {"ok": True, "result": {"orders": []}}},
                    ]
                },
                llm_request_payload={"messages": ["x" * 5000]},
                llm_response_payload={"choices": ["y" * 5000]},
            )
        )


def test_account_overview_skips_history_scan_when_live_api_succeeds(
    monkeypatch, tmp_path
) -> None:
    """远端接口可用时不得扫描历史运行（旧实现在每次 /account 都读 20 条整行）。"""

    class _FakeClient:
        def __init__(self, api_key: str | None = None) -> None:
            self.api_key = api_key

        def close(self) -> None:
            return None

    with create_test_client(monkeypatch, tmp_path):
        _settings_with_mx_key()
        _seed_account_snapshot_run()
        monkeypatch.setattr("app.services.aniu_service.MXClient", _FakeClient)
        monkeypatch.setattr(
            aniu_service,
            "_fetch_live_account_payloads",
            lambda client: {
                "balance": {"ok": True, "result": {"total_assets": 1.0}},
                "positions": {"ok": True, "result": {"positions": []}},
                "orders": {"ok": True, "result": {"orders": []}},
            },
        )
        aniu_service._account_overview_cache = None
        with _SqlRecorder() as recorder:
            overview = aniu_service.get_account_overview(force_refresh=True)

    # 远端可用时不应出现"使用缓存"的提示，也不应读取历史运行表。
    assert overview["errors"] == []
    assert all("strategy_runs" not in sql.lower() for sql in recorder.select_statements())


def test_account_overview_falls_back_to_history_snapshot_when_live_fails(
    monkeypatch, tmp_path
) -> None:
    class _BoomClient:
        def __init__(self, api_key: str | None = None) -> None:
            raise RuntimeError("mx client unavailable")

    with create_test_client(monkeypatch, tmp_path):
        _settings_with_mx_key()
        _seed_account_snapshot_run()
        monkeypatch.setattr("app.services.aniu_service.MXClient", _BoomClient)
        aniu_service._account_overview_cache = None
        overview = aniu_service.get_account_overview(force_refresh=True)

    # 远端失败时必须回落到历史快照，并给出"展示缓存"的提示。
    assert any("缓存" in message for message in overview["errors"])
