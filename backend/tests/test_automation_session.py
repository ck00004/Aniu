from pathlib import Path
import sys
from types import SimpleNamespace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.rate_limit import _limiter
from app.db import database as database_module
from app.db.database import session_scope
from app.db.models import ChatMessageRecord, ChatSession, StrategyRun, StrategySchedule, TradeOrder
from app.main import create_app
from app.services.aniu_service import aniu_service
from app.services.llm_service import llm_service
from app.services.scheduler_service import scheduler_service
from app.services.token_estimator import estimate_messages_tokens, estimate_text_tokens
from app.services.trading_calendar_service import trading_calendar_service


def create_test_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(
        trading_calendar_service,
        "warm_up_months",
        lambda current: None,
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


def reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()
    get_settings.cache_clear()


def _prepare_schedule(db, *, name: str, run_type: str = "analysis") -> StrategySchedule:
    schedule = StrategySchedule(
        name=name,
        run_type=run_type,
        cron_expression="*/30 * * * *",
        task_prompt=f"{name} task prompt",
        timeout_seconds=180,
        enabled=True,
    )
    db.add(schedule)
    db.flush()
    return schedule


def _fake_run_result(final_answer: str, tool_calls=None):
    return (
        {
            "final_answer": final_answer,
            "tool_calls": tool_calls or [],
        },
        {"messages": []},
        {"responses": [], "final_message": {"content": final_answer}},
        {"messages": []},
    )


def _append_session_message(
    db,
    *,
    session_id: int,
    role: str,
    content: str,
    run_id: int,
) -> ChatMessageRecord:
    record = ChatMessageRecord(
        session_id=session_id,
        role=role,
        content=content,
        source="automation_run",
        run_id=run_id,
        message_kind="live_turn",
    )
    db.add(record)
    db.flush()
    return record


def test_schedule_runs_share_single_automation_session(monkeypatch, tmp_path) -> None:
    captured_messages: list[list[dict[str, object]]] = []

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del kwargs
        captured_messages.append(messages)
        return _fake_run_result("scheduled decision")

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)
    monkeypatch.setattr(
        aniu_service,
        "_prefetch_analysis_context",
        lambda **kwargs: (
            "[Jin10 当天新闻参考]\n1. [09:30:00] 新闻A",
            {"ok": True},
        ),
    )

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "llm-key"
            settings.llm_model = "demo-model"
            first = _prepare_schedule(db, name="盘前分析", run_type="analysis")
            second = _prepare_schedule(db, name="上午运行 1", run_type="trade")
            first_id = first.id
            second_id = second.id

        first_run = aniu_service.execute_run(trigger_source="schedule", schedule_id=first_id)
        second_run = aniu_service.execute_run(trigger_source="schedule", schedule_id=second_id)

        assert first_run.chat_session_id is not None
        assert second_run.chat_session_id == first_run.chat_session_id
        assert first_run.prompt_message_id is not None
        assert first_run.response_message_id is not None
        assert second_run.prompt_message_id is not None
        assert second_run.response_message_id is not None

        assert len(captured_messages) == 2
        assert any(msg["role"] == "user" for msg in captured_messages[0])
        assert any(
            "[Jin10 当天新闻参考]" in str(msg.get("content") or "")
            for msg in captured_messages[0]
        )
        second_history = captured_messages[1]
        assert any("时间：" in str(msg.get("content") or "") for msg in second_history)
        assert any("scheduled decision" in str(msg.get("content") or "") for msg in second_history)

        with session_scope() as db:
            sessions = db.query(ChatSession).filter(ChatSession.kind == "automation").all()
            assert len(sessions) == 1
            session = sessions[0]
            messages = (
                db.query(ChatMessageRecord)
                .filter(ChatMessageRecord.session_id == session.id)
                .order_by(ChatMessageRecord.id.asc())
                .all()
            )
            assert len(messages) == 4
            assert [item.role for item in messages] == ["user", "assistant", "user", "assistant"]
            assert session.slug == "automation-default"

    reset_db_state()


def test_manual_runs_share_automation_session_with_scheduled_runs(monkeypatch, tmp_path) -> None:
    captured_messages: list[list[dict[str, object]]] = []

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del kwargs
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            return _fake_run_result("scheduled decision")
        return _fake_run_result("manual decision")

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)
    monkeypatch.setattr(
        aniu_service,
        "_prefetch_analysis_context",
        lambda **kwargs: (
            "[Jin10 当天新闻参考]\n1. [09:30:00] 新闻A",
            {"ok": True},
        ),
    )

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "llm-key"
            settings.llm_model = "demo-model"
            schedule = _prepare_schedule(db, name="收盘分析", run_type="analysis")
            schedule_id = schedule.id

        scheduled_run = aniu_service.execute_run(
            trigger_source="schedule",
            schedule_id=schedule_id,
        )
        manual_run = aniu_service.execute_run(trigger_source="manual", schedule_id=None)

        assert scheduled_run.chat_session_id is not None
        assert manual_run.chat_session_id == scheduled_run.chat_session_id
        assert manual_run.prompt_message_id is not None
        assert manual_run.response_message_id is not None
        assert len(captured_messages) == 2
        second_history = captured_messages[1]
        assert any("来源: 手动触发" in str(msg.get("content") or "") for msg in second_history)
        assert any("scheduled decision" in str(msg.get("content") or "") for msg in second_history)

        with session_scope() as db:
            sessions = db.query(ChatSession).filter(ChatSession.kind == "automation").all()
            assert len(sessions) == 1
            session = sessions[0]
            messages = (
                db.query(ChatMessageRecord)
                .filter(ChatMessageRecord.session_id == session.id)
                .order_by(ChatMessageRecord.id.asc())
                .all()
            )
            assert len(messages) == 4
            assert [item.role for item in messages] == ["user", "assistant", "user", "assistant"]
            assert any(record.run_id == manual_run.id for record in messages)

    reset_db_state()


def test_manual_schedule_run_uses_actual_market_day_type_in_context(
    monkeypatch, tmp_path
) -> None:
    captured_messages: list[list[dict[str, object]]] = []

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del kwargs
        captured_messages.append(messages)
        return _fake_run_result("manual scheduled decision")

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)
    monkeypatch.setattr(
        aniu_service,
        "_prefetch_analysis_context",
        lambda **kwargs: (None, None),
    )

    from app.services import aniu_service as aniu_service_module

    monkeypatch.setattr(
        aniu_service_module,
        "now_shanghai",
        lambda: datetime(2026, 4, 11, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr(
        trading_calendar_service,
        "is_trading_day",
        lambda current: False if current.isoformat() == "2026-04-11" else current.weekday() < 5,
    )

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "llm-key"
            settings.llm_model = "demo-model"
            schedule = _prepare_schedule(db, name="盘前分析", run_type="analysis")
            schedule_id = schedule.id

        run = aniu_service.execute_run(trigger_source="manual", schedule_id=schedule_id)

        assert run.market_day_type == "non_trading_day"
        assert len(captured_messages) == 1
        assert any("今日类型: 非交易日" in str(msg.get("content") or "") for msg in captured_messages[0])

    reset_db_state()


def test_analysis_schedule_followup_adds_self_select_when_new_candidates_are_claimed(
    monkeypatch,
    tmp_path,
) -> None:
    captured_messages: list[list[dict[str, object]]] = []

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del kwargs
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            return (
                {
                    "final_answer": (
                        "五、条件选股筛选后的新增候选结论\n"
                        "其中，我认为今天最值得重点跟踪的新增候选只有3个：\n"
                        "1. 亨通光电\n2. 三环集团\n3. 中船防务"
                    ),
                    "tool_calls": [
                        {"name": "mx_get_self_selects", "result": {"ok": True, "summary": "已查询自选股列表。"}},
                        {"name": "mx_search_news", "result": {"ok": True, "summary": "已查询资讯。"}},
                        {"name": "mx_screen_stocks", "result": {"ok": True, "summary": "已执行选股。"}},
                    ],
                },
                {"messages": []},
                {"responses": [], "final_message": {"content": "first"}},
                {"messages": messages},
            )
        return (
            {
                "final_answer": "已将亨通光电加入自选，作为新增重点跟踪标的。",
                "tool_calls": [
                    {
                        "name": "mx_manage_self_select",
                        "result": {
                            "ok": True,
                            "summary": "已加入自选。",
                            "executed_action": {
                                "action": "MANAGE_SELF_SELECT",
                                "query": "将亨通光电加入自选股",
                            },
                        },
                    }
                ],
            },
            {"messages": []},
            {"responses": [], "final_message": {"content": "followup"}},
            {"messages": messages},
        )

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)
    monkeypatch.setattr(
        aniu_service,
        "_prefetch_analysis_context",
        lambda **kwargs: ("[Jin10 当天新闻参考]\n1. [09:30:00] 新闻A", {"ok": True}),
    )

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "llm-key"
            settings.llm_model = "demo-model"
            schedule = _prepare_schedule(db, name="盘前分析", run_type="analysis")
            schedule_id = schedule.id

        run = aniu_service.execute_run(trigger_source="schedule", schedule_id=schedule_id)

        assert len(captured_messages) == 2
        assert "新增候选" in str(captured_messages[1][-1].get("content") or "")
        assert run.executed_actions is not None
        assert any(str(item.get("action") or "") == "MANAGE_SELF_SELECT" for item in run.executed_actions)
        assert run.decision_payload is not None
        assert run.decision_payload.get("original_final_answer")
        assert "一致性检查修正说明" in str(run.final_answer or "")

    reset_db_state()


def test_trade_schedule_followup_executes_trade_when_final_answer_claims_sell(
    monkeypatch,
    tmp_path,
) -> None:
    captured_messages: list[list[dict[str, object]]] = []

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del kwargs
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            return (
                {
                    "final_answer": "09:30这一轮直接结论：\n- 先卖立讯精密\n\n执行：\n- 卖出 立讯精密 5000股",
                    "tool_calls": [],
                },
                {"messages": []},
                {"responses": [], "final_message": {"content": "first"}},
                {"messages": messages},
            )
        return (
            {
                "final_answer": "已实际提交卖出立讯精密 5000股委托。",
                "tool_calls": [
                    {
                        "name": "mx_moni_trade",
                        "result": {
                            "ok": True,
                            "summary": "已提交卖出委托。",
                            "executed_action": {
                                "action": "SELL",
                                "symbol": "002475",
                                "name": "立讯精密",
                                "quantity": 5000,
                                "price_type": "MARKET",
                            },
                            "result": {"order_id": "SELL-1"},
                        },
                    }
                ],
            },
            {"messages": []},
            {"responses": [], "final_message": {"content": "followup"}},
            {"messages": messages},
        )

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)
    monkeypatch.setattr(
        aniu_service,
        "_prefetch_analysis_context",
        lambda **kwargs: (None, None),
    )

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "llm-key"
            settings.llm_model = "demo-model"
            schedule = _prepare_schedule(db, name="上午运行1号", run_type="trade")
            schedule_id = schedule.id

        run = aniu_service.execute_run(trigger_source="schedule", schedule_id=schedule_id)

        assert len(captured_messages) == 2
        assert "未实际交易" in str(captured_messages[1][-1].get("content") or "")
        assert run.executed_actions is not None
        assert any(str(item.get("action") or "") == "SELL" for item in run.executed_actions)
        with session_scope() as db:
            stored_run = db.get(StrategyRun, run.id)
            orders = db.query(TradeOrder).filter(TradeOrder.run_id == run.id).all()
            assert stored_run is not None
            assert len(orders) == 1
            assert orders[0].action == "SELL"
        assert "一致性检查修正说明" in str(run.final_answer or "")

    reset_db_state()


def test_schedule_run_injects_jin10_news_diagnosis_into_messages_and_payload(
    monkeypatch,
    tmp_path,
) -> None:
    captured_messages: list[list[dict[str, object]]] = []

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del kwargs
        captured_messages.append(messages)
        return _fake_run_result("scheduled decision")

    def fake_fetch_news_items(**kwargs):
        del kwargs
        return (
            [
                {
                    "time": "09:31:00",
                    "title": "央行公开市场操作",
                    "content": "流动性边际改善。",
                },
                {
                    "time": "10:02:00",
                    "title": "科技产业政策推进",
                    "content": "自主可控方向继续强化。",
                },
            ],
            {
                "ok": True,
                "url": "http://127.0.0.1:3000/api/news",
                "params": {
                    "date": "2026-04-22",
                    "startTime": "00:00:00",
                    "endTime": "14:30:00",
                    "limit": "30",
                },
                "item_count": 2,
                "total": 2,
                "has_more": False,
            },
        )

    def fake_generate_text(**kwargs):
        del kwargs
        return (
            "市场总览：政策偏暖，风险偏好改善。\nA股重点方向：券商、科技自主可控。\n金融市场联动：汇率压力缓和。\n交易观察：关注量价验证。\n风险提示：海外波动。",
            {"messages": []},
            {"choices": [{"message": {"content": "ok"}}]},
        )

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)
    monkeypatch.setattr(
        "app.services.jin10_news_service.jin10_news_service.fetch_news_items",
        fake_fetch_news_items,
    )
    monkeypatch.setattr(llm_service, "generate_text", fake_generate_text)

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "llm-key"
            settings.llm_model = "demo-model"
            schedule = _prepare_schedule(db, name="盘前分析", run_type="analysis")
            schedule_id = schedule.id

        run = aniu_service.execute_run(trigger_source="schedule", schedule_id=schedule_id)

        assert len(captured_messages) == 1
        assert any(
            "[Jin10 新闻诊断]" in str(msg.get("content") or "")
            for msg in captured_messages[0]
        )
        assert run.skill_payloads is not None
        prefetched = run.skill_payloads.get("prefetched_context")
        assert isinstance(prefetched, dict)
        assert "analysis_text" in prefetched
        assert "A股重点方向" in str(prefetched.get("analysis_text") or "")

    reset_db_state()


def test_scheduled_runs_can_read_prior_manual_history(monkeypatch, tmp_path) -> None:
    captured_messages: list[list[dict[str, object]]] = []

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del kwargs
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            return _fake_run_result("manual decision")
        return _fake_run_result("scheduled decision")

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "llm-key"
            settings.llm_model = "demo-model"
            schedule = _prepare_schedule(db, name="收盘分析", run_type="analysis")
            schedule_id = schedule.id

        manual_run = aniu_service.execute_run(trigger_source="manual", schedule_id=None)
        scheduled_run = aniu_service.execute_run(
            trigger_source="schedule",
            schedule_id=schedule_id,
        )

        assert manual_run.chat_session_id is not None
        assert scheduled_run.chat_session_id == manual_run.chat_session_id
        assert len(captured_messages) == 2
        second_history = captured_messages[1]
        assert any("来源: 手动触发" in str(msg.get("content") or "") for msg in second_history)
        assert any("manual decision" in str(msg.get("content") or "") for msg in second_history)

    reset_db_state()


def test_schedule_run_failure_persists_failed_assistant_message(monkeypatch, tmp_path) -> None:
    def fake_run_agent_with_messages(**kwargs):
        del kwargs
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.mx_api_key = "mx-key"
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "llm-key"
            settings.llm_model = "demo-model"
            schedule = _prepare_schedule(db, name="午间复盘", run_type="analysis")
            schedule_id = schedule.id

        try:
            aniu_service.execute_run(trigger_source="schedule", schedule_id=schedule_id)
        except RuntimeError as exc:
            assert str(exc) == "llm unavailable"
        else:
            raise AssertionError("expected scheduled run to fail")

        with session_scope() as db:
            run = db.query(StrategyRun).order_by(StrategyRun.id.desc()).first()
            assert run is not None
            assert run.status == "failed"
            assert run.chat_session_id is not None
            session = db.get(ChatSession, run.chat_session_id)
            assert session is not None
            messages = (
                db.query(ChatMessageRecord)
                .filter(ChatMessageRecord.session_id == session.id)
                .order_by(ChatMessageRecord.id.asc())
                .all()
            )
            assert len(messages) == 2
            assert messages[0].role == "user"
            assert messages[1].role == "assistant"
            assert "执行失败：llm unavailable" in messages[1].content
            assert run.response_message_id == messages[1].id

    reset_db_state()


def test_chat_session_list_excludes_automation_sessions(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        response = client.post("/api/aniu/login", json={"password": "release-pass"})
        headers = {"Authorization": f"Bearer {response.json()['token']}"}

        with session_scope() as db:
            db.add(ChatSession(title="Automation", kind="automation", slug="automation-default"))
            db.add(ChatSession(title="User", kind="user"))

        result = client.get("/api/aniu/chat/sessions", headers=headers)
        assert result.status_code == 200
        payload = result.json()
        assert len(payload) == 1
        assert payload[0]["title"] == "User"
        assert payload[0]["kind"] == "user"

    reset_db_state()


def test_automation_context_estimate_does_not_double_count_archived_summary(
    monkeypatch, tmp_path
) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.system_prompt = "系统提示"
            session = ChatSession(
                title="Automation",
                kind="automation",
                slug="automation-default",
                archived_summary="已归档摘要",
            )
            db.add(session)
            db.flush()

            messages = aniu_service._build_persistent_session_prompt_messages(
                session=session,
                history_messages=[{"role": "user", "content": "本轮任务"}],
                memory_messages=[],
            )
            assert messages == [{"role": "user", "content": "本轮任务"}]
            estimate = aniu_service._estimate_persistent_session_context_tokens(
                session=session,
                settings=settings,
                messages=messages,
            )

            assert estimate == estimate_messages_tokens(messages) + estimate_text_tokens(
                settings.system_prompt
            )

    reset_db_state()


def test_auto_compaction_ignores_already_archived_history_until_new_messages_accumulate(
    monkeypatch, tmp_path
) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.automation_enable_auto_compaction = True
            settings.automation_recent_message_limit = 4
            settings.automation_idle_summary_hours = 999
            session = ChatSession(
                title="Automation",
                kind="automation",
                slug="automation-default",
                archived_summary="历史摘要",
                summary_revision=1,
                last_message_at=datetime.now(timezone.utc),
            )
            db.add(session)
            db.flush()

            old_records: list[ChatMessageRecord] = []
            for run_id in range(1, 4):
                old_records.append(
                    _append_session_message(
                        db,
                        session_id=session.id,
                        role="user",
                        content=f"旧任务 {run_id}",
                        run_id=run_id,
                    )
                )
                old_records.append(
                    _append_session_message(
                        db,
                        session_id=session.id,
                        role="assistant",
                        content=f"旧结论 {run_id}",
                        run_id=run_id,
                    )
                )

            session.last_compacted_message_id = old_records[-1].id
            session.last_compacted_run_id = old_records[-1].run_id
            db.add(session)
            db.flush()

            new_user = _append_session_message(
                db,
                session_id=session.id,
                role="user",
                content="新任务 4",
                run_id=4,
            )
            new_assistant = _append_session_message(
                db,
                session_id=session.id,
                role="assistant",
                content="新结论 4",
                run_id=4,
            )
            session.last_message_at = datetime.now(timezone.utc)
            db.add(session)
            db.flush()

            history_records = aniu_service._list_persistent_session_history_records(
                db=db,
                session_id=session.id,
                recent_limit=4,
            )
            assert [record.id for record in history_records] == [
                new_user.id,
                new_assistant.id,
            ]

            original_last_compacted_message_id = session.last_compacted_message_id
            summary, version = aniu_service._maybe_compact_persistent_session(
                db=db,
                session=session,
                settings=settings,
                estimated_tokens=0,
            )

            assert summary == "历史摘要"
            assert version == 1
            assert session.summary_revision == 1
            assert session.last_compacted_message_id == original_last_compacted_message_id

            summary_messages = (
                db.query(ChatMessageRecord)
                .filter(ChatMessageRecord.session_id == session.id)
                .filter(ChatMessageRecord.message_kind == "context_compaction_summary")
                .all()
            )
            assert summary_messages == []

    reset_db_state()


def test_repeated_compaction_only_archives_new_history_and_preserves_previous_summary(
    monkeypatch, tmp_path
) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.automation_enable_auto_compaction = True
            settings.automation_recent_message_limit = 8
            settings.automation_idle_summary_hours = 999
            session = ChatSession(
                title="Automation",
                kind="automation",
                slug="automation-default",
                archived_summary="历史摘要",
                summary_revision=1,
                last_message_at=datetime.now(timezone.utc),
            )
            db.add(session)
            db.flush()

            old_records: list[ChatMessageRecord] = []
            for run_id in range(1, 3):
                old_records.append(
                    _append_session_message(
                        db,
                        session_id=session.id,
                        role="user",
                        content=f"旧任务 {run_id}",
                        run_id=run_id,
                    )
                )
                old_records.append(
                    _append_session_message(
                        db,
                        session_id=session.id,
                        role="assistant",
                        content=f"旧结论 {run_id}",
                        run_id=run_id,
                    )
                )

            session.last_compacted_message_id = old_records[-1].id
            session.last_compacted_run_id = old_records[-1].run_id
            db.add(session)
            db.flush()

            new_records: list[ChatMessageRecord] = []
            for offset in range(5):
                run_id = 100 + offset
                new_records.append(
                    _append_session_message(
                        db,
                        session_id=session.id,
                        role="user",
                        content=f"新任务 {run_id}",
                        run_id=run_id,
                    )
                )
                new_records.append(
                    _append_session_message(
                        db,
                        session_id=session.id,
                        role="assistant",
                        content=f"新的分析结论 {run_id}",
                        run_id=run_id,
                    )
                )

            session.last_message_at = datetime.now(timezone.utc)
            db.add(session)
            db.flush()

            summary, version = aniu_service._maybe_compact_persistent_session(
                db=db,
                session=session,
                settings=settings,
                estimated_tokens=0,
            )

            assert version == 2
            assert session.summary_revision == 2
            assert summary is not None
            assert "历史摘要" in summary
            assert "run_id 100" in summary
            assert session.last_compacted_message_id == new_records[1].id

            summary_messages = (
                db.query(ChatMessageRecord)
                .filter(ChatMessageRecord.session_id == session.id)
                .filter(ChatMessageRecord.message_kind == "context_compaction_summary")
                .order_by(ChatMessageRecord.id.asc())
                .all()
            )
            assert len(summary_messages) == 1
            assert summary_messages[0].role == "system"
            assert summary_messages[0].content.startswith("[上下文压缩摘要]\n")
            assert "run_id 100" in summary_messages[0].content

            history_records = aniu_service._list_persistent_session_history_records(
                db=db,
                session_id=session.id,
                recent_limit=8,
            )
            assert [record.id for record in history_records] == [
                record.id for record in new_records[2:]
            ]

    reset_db_state()
