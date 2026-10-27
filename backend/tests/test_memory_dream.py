"""记忆梦境夜间整理（P2-12 移植）的回归测试。"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.rate_limit import _limiter
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import MemoryDream, StrategyRun
from app.main import create_app
from app.services.aniu_service import aniu_service, now_utc
from app.services.llm_service import llm_service
from app.services.memory_dream_service import (
    _DREAM_INSTRUCTION,
    build_dream_tool_executor,
    build_dream_tools,
    memory_dream_service,
)
from app.services.memory_service import memory_service
from app.services.scheduler_service import scheduler_service
from app.services.trading_calendar_service import trading_calendar_service


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


def _wait_for(condition, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


def test_create_or_get_is_idempotent_per_date(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        target = date(2026, 10, 15)
        with session_scope() as db:
            first = memory_dream_service.create_or_get(db, target_date=target)
            second = memory_dream_service.create_or_get(db, target_date=target)
            assert first.id == second.id
            assert first.status == "pending"

            other = memory_dream_service.create_or_get(
                db, target_date=date(2026, 10, 14)
            )
            assert other.id != first.id
    reset_db_state()


def test_retry_if_finished_resets_terminal_dreams(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(
                db, target_date=date(2026, 10, 15)
            )
            dream.status = "completed"
            dream.result = "旧结果"
            db.add(dream)
            db.commit()

            reset = memory_dream_service.retry_if_finished(db, dream)
            assert reset.status == "pending"
            assert reset.result is None
            assert reset.failure_reason is None

            # pending 状态不受影响
            untouched = memory_dream_service.retry_if_finished(db, dream)
            assert untouched.status == "pending"
    reset_db_state()


# ---------------------------------------------------------------------------
# 梦境工具白名单
# ---------------------------------------------------------------------------


def test_dream_tools_contain_exactly_four_whitelist_entries() -> None:
    tools = build_dream_tools()
    names = {tool["function"]["name"] for tool in tools}
    assert names == {
        "dream_read_reports",
        "memory_list",
        "memory_read",
        "memory_write",
    }


def test_dream_executor_rejects_trading_tools(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        executor = build_dream_tool_executor(date(2026, 10, 15))
        result = executor("mx_moni_trade", {"action": "BUY"}, "call-1")
        assert result["ok"] is False
        assert "禁止" in result["error"]
    reset_db_state()


def test_dream_executor_reads_reports_and_memories(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        now = now_utc().replace(tzinfo=None)
        with session_scope() as db:
            db.add(
                StrategyRun(
                    status="completed",
                    run_type="analysis",
                    started_at=now,
                    finished_at=now + timedelta(minutes=1),
                    final_answer="当日结论：观望为主",
                )
            )
            memory_service.create_memory(
                db, content="放量滞涨应减仓", reason="量价背离"
            )
            db.commit()

        executor = build_dream_tool_executor(now_utc().date())
        reports = executor("dream_read_reports", {"offset": 0, "limit": 10}, None)
        assert reports["ok"] is True
        assert reports["total"] == 1
        assert "观望为主" in reports["runs"][0]["final_answer"]

        listed = executor("memory_list", {"offset": 0, "limit": 10}, None)
        assert listed["ok"] is True
        assert listed["total"] == 1
        assert "放量滞涨" in listed["items"][0]["content"]

        read = executor("memory_read", {"keywords": ["放量"]}, None)
        assert read["ok"] is True
        assert read["result_count"] == 1

        written = executor(
            "memory_write",
            {"action": "create", "content": "梦境写入的记忆", "reason": "整理"},
            None,
        )
        assert written["ok"] is True
    reset_db_state()


def test_dream_executor_memory_write_version_conflict(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            created = memory_service.create_memory(
                db, content="原始记忆", reason=None
            )
            memory_id, version = created.id, created.version

        executor = build_dream_tool_executor(date(2026, 10, 15))
        conflict = executor(
            "memory_write",
            {
                "action": "update",
                "memory_id": memory_id,
                "content": "新内容",
                "expected_version": version + 99,
            },
            None,
        )
        assert conflict["ok"] is False
        assert conflict.get("conflict") is True
    reset_db_state()


# ---------------------------------------------------------------------------
# Dream Agent 执行
# ---------------------------------------------------------------------------


def _configure_llm_settings() -> None:
    with session_scope() as db:
        settings = aniu_service.get_or_create_settings(db)
        settings.llm_base_url = "https://example.com/v1"
        settings.llm_api_key = "test-key"
        settings.llm_model = "test-model"
        settings.system_prompt = "全局系统提示"
        db.commit()


def test_execute_dream_completes_with_agent_result(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_with_messages(*, app_settings, client, messages, emit=None, tool_executor=None, **kwargs):
        captured["app_settings"] = app_settings
        captured["messages"] = messages
        captured["tool_executor"] = tool_executor
        # 白名单执行器在真实链路中可用
        probe = tool_executor("mx_moni_trade", {}, None)
        assert probe["ok"] is False
        return (
            {"final_answer": "本次整理：新增 2 条、更新 1 条记忆。", "tool_calls": []},
            {},
            {},
            {},
        )

    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)

    with create_test_client(monkeypatch, tmp_path):
        _configure_llm_settings()
        target = date(2026, 10, 15)
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(db, target_date=target)
            dream_id = dream.id

        result = memory_dream_service.execute_dream(dream_id)
        assert result is not None
        assert result.status == "completed"
        assert "新增 2 条" in (result.result or "")

        # 梦境设置：白名单工具 + dream run_type + 指令注入
        dream_settings = captured["app_settings"]
        assert dream_settings.run_type == "dream"
        override_tools = dream_settings.tools_override
        assert {t["function"]["name"] for t in override_tools} == {
            "dream_read_reports",
            "memory_list",
            "memory_read",
            "memory_write",
        }
        assert _DREAM_INSTRUCTION in dream_settings.system_prompt
        assert "全局系统提示" in dream_settings.system_prompt
        # 用户消息包含目标日期与整理要求
        user_content = str(captured["messages"][0]["content"])
        assert "2026-10-15" in user_content
        assert "expected_version" in user_content
    reset_db_state()


def test_execute_dream_failure_records_reason(monkeypatch, tmp_path) -> None:
    def failing_agent(**kwargs):
        raise RuntimeError("上游模型不可用")

    monkeypatch.setattr(llm_service, "run_agent_with_messages", failing_agent)

    with create_test_client(monkeypatch, tmp_path):
        _configure_llm_settings()
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(
                db, target_date=date(2026, 10, 15)
            )
            dream_id = dream.id

        result = memory_dream_service.execute_dream(dream_id)
        assert result is not None
        assert result.status == "failed"
        assert "上游模型不可用" in (result.failure_reason or "")
    reset_db_state()


def test_execute_dream_requires_llm_configuration(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        # 不配置 LLM → 明确报错
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(
                db, target_date=date(2026, 10, 15)
            )
            dream_id = dream.id

        result = memory_dream_service.execute_dream(dream_id)
        assert result is not None
        assert result.status == "failed"
        assert "未配置大模型接口" in (result.failure_reason or "")
    reset_db_state()


def test_execute_dream_skips_non_pending(monkeypatch, tmp_path) -> None:
    calls = {"count": 0}

    def unexpected_agent(**kwargs):
        calls["count"] += 1
        return ({"final_answer": "不应执行"}, {}, {}, {})

    monkeypatch.setattr(llm_service, "run_agent_with_messages", unexpected_agent)

    with create_test_client(monkeypatch, tmp_path):
        _configure_llm_settings()
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(
                db, target_date=date(2026, 10, 15)
            )
            dream.status = "running"
            db.add(dream)
            db.commit()
            dream_id = dream.id

        result = memory_dream_service.execute_dream(dream_id)
        assert result is not None
        assert result.status == "running"  # 保持不变
        assert calls["count"] == 0
    reset_db_state()


def test_execute_dream_lock_prevents_concurrent_runs(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(
                db, target_date=date(2026, 10, 15)
            )
            dream_id = dream.id

        # 手动持有锁模拟并发执行
        assert memory_dream_service._lock.acquire(blocking=False)
        try:
            with pytest.raises(RuntimeError, match="已有梦境任务正在执行"):
                memory_dream_service.execute_dream(dream_id)
        finally:
            memory_dream_service._lock.release()
    reset_db_state()


# ---------------------------------------------------------------------------
# 调度触发与启动恢复
# ---------------------------------------------------------------------------


def test_process_due_dream_before_time_no_trigger(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.automation_dream_time = "23:59"  # 当前时间必然早于
            db.commit()

        memory_dream_service.process_due_dream()

        with session_scope() as db:
            total = db.scalar(
                __import__("sqlalchemy").select(
                    __import__("sqlalchemy").func.count(MemoryDream.id)
                )
            )
            assert total == 0
    reset_db_state()


def test_process_due_dream_after_time_triggers_once(monkeypatch, tmp_path) -> None:
    executed: list[int] = []

    def fake_execute(dream_id: int):
        executed.append(dream_id)
        with session_scope() as db:
            dream = db.get(MemoryDream, dream_id)
            if dream is not None:
                dream.status = "completed"
                dream.result = "整理完成"
                dream.finished_at = now_utc()
                db.add(dream)
                db.commit()
        return None

    monkeypatch.setattr(memory_dream_service, "execute_dream", fake_execute)

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.automation_dream_time = "00:00"  # 当前时间必然晚于
            db.commit()

        memory_dream_service.process_due_dream()
        _wait_for(lambda: len(executed) >= 1)

        # 整理对象是昨天；轮询数据库终态（executed 记录先于 DB 写入，
        # 直接断言会有竞态）
        yesterday = datetime.now(timezone(timedelta(hours=8))).date() - timedelta(days=1)

        def _dream_finished() -> bool:
            with session_scope() as db:
                dream = db.scalar(
                    __import__("sqlalchemy").select(MemoryDream).where(
                        MemoryDream.target_date == yesterday
                    )
                )
                return dream is not None and dream.status == "completed"

        _wait_for(_dream_finished)
        with session_scope() as db:
            dream = db.scalar(
                __import__("sqlalchemy").select(MemoryDream).where(
                    MemoryDream.target_date == yesterday
                )
            )
            assert dream is not None
            assert dream.status == "completed"

        # 已存在（无论状态）不再重复触发
        executed.clear()
        memory_dream_service.process_due_dream()
        time.sleep(0.2)
        assert executed == []
    reset_db_state()


def test_fail_stale_running_dreams_at_startup(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(
                db, target_date=date(2026, 10, 15)
            )
            dream.status = "running"
            db.add(dream)
            db.commit()
            dream_id = dream.id

        with session_scope() as db:
            marked = memory_dream_service.fail_stale_running_dreams(db)
            assert marked == 1
            stale = db.get(MemoryDream, dream_id)
            assert stale.status == "failed"
            assert "手动重试" in (stale.failure_reason or "")
    reset_db_state()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_memory_dreams_api_lifecycle(monkeypatch, tmp_path) -> None:
    executed: list[int] = []

    def fake_execute(dream_id: int):
        executed.append(dream_id)
        with session_scope() as db:
            dream = db.get(MemoryDream, dream_id)
            if dream is not None:
                dream.status = "completed"
                dream.result = "手动整理结果"
                dream.finished_at = now_utc()
                db.add(dream)
                db.commit()
        return None

    monkeypatch.setattr(memory_dream_service, "execute_dream", fake_execute)

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = auth_headers(client)

        # 手动触发（默认昨天）
        run = client.post("/api/aniu/memory-dreams/run", headers=headers)
        assert run.status_code == 200
        payload = run.json()
        assert payload["started"] is True
        dream_id = payload["dream"]["id"]
        assert payload["dream"]["status"] in {"pending", "running"}

        _wait_for(lambda: len(executed) >= 1)

        # 列表与详情
        listed = client.get("/api/aniu/memory-dreams", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        detail = client.get(f"/api/aniu/memory-dreams/{dream_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "completed"
        assert detail.json()["result"] == "手动整理结果"

        # 未知 id → 404
        assert (
            client.get("/api/aniu/memory-dreams/999999", headers=headers).status_code
            == 404
        )

        # 删除
        deleted = client.delete(
            f"/api/aniu/memory-dreams/{dream_id}", headers=headers
        )
        assert deleted.status_code == 200
        assert (
            client.get(f"/api/aniu/memory-dreams/{dream_id}", headers=headers).status_code
            == 404
        )

        # 未认证被拒绝
        assert client.get("/api/aniu/memory-dreams").status_code == 401
    reset_db_state()


def test_memory_dreams_api_delete_running_conflict(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = auth_headers(client)
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(
                db, target_date=date(2026, 10, 15)
            )
            dream.status = "running"
            db.add(dream)
            db.commit()
            dream_id = dream.id

        response = client.delete(
            f"/api/aniu/memory-dreams/{dream_id}", headers=headers
        )
        assert response.status_code == 409
    reset_db_state()


def test_agent_loop_tools_override_reaches_payload(monkeypatch) -> None:
    """tools_override 精确控制模型可见的工具列表。"""
    from app.services.llm_service import LLMService

    service = LLMService()
    captured_payload: dict[str, object] = {}

    def fake_call_llm_stream(
        *, base_url, api_key, payload, timeout_seconds, emit=None, cancel_event=None
    ):
        captured_payload.update(payload)
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "梦境整理完成"}}
            ]
        }

    monkeypatch.setattr(service, "_call_llm_stream", fake_call_llm_stream)

    override = build_dream_tools()
    service._agent_loop(
        model="m",
        base_url="https://x",
        api_key="k",
        initial_messages=[{"role": "user", "content": "整理"}],
        run_type="analysis",  # 即便 run_type 是 analysis 也只暴露白名单
        timeout_seconds=5,
        tool_executor=lambda *a, **k: {"ok": True},
        tools_override=override,
    )

    tools = captured_payload.get("tools")
    assert isinstance(tools, list)
    assert {t["function"]["name"] for t in tools} == {
        "dream_read_reports",
        "memory_list",
        "memory_read",
        "memory_write",
    }


def test_execute_dream_uses_configured_dream_instruction(
    monkeypatch, tmp_path
) -> None:
    """梦境指令接入 prompt_templates：设置页可配置，未配置回退默认。"""
    captured: dict[str, object] = {}

    def fake_run_agent_with_messages(*, app_settings, client, messages, emit=None, tool_executor=None, **kwargs):
        captured["app_settings"] = app_settings
        return (
            {"final_answer": "整理完成。", "tool_calls": []},
            {},
            {},
            {},
        )

    monkeypatch.setattr(
        llm_service, "run_agent_with_messages", fake_run_agent_with_messages
    )

    with create_test_client(monkeypatch, tmp_path):
        _configure_llm_settings()
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.prompt_templates = {
                "dream_instruction_prompt": "自定义梦境指令：只沉淀量价背离类经验。"
            }
            db.commit()

        target = date(2026, 10, 15)
        with session_scope() as db:
            dream = memory_dream_service.create_or_get(db, target_date=target)
            dream_id = dream.id

        result = memory_dream_service.execute_dream(dream_id)
        assert result is not None
        assert result.status == "completed"

        dream_settings = captured["app_settings"]
        assert "自定义梦境指令：只沉淀量价背离类经验。" in dream_settings.system_prompt
        assert "夜间整理智能体" not in dream_settings.system_prompt
        # 真实模板字典透传给 agent（空结论重试等模板同样可配置）
        assert (
            dream_settings.prompt_templates.get("dream_instruction_prompt")
            == "自定义梦境指令：只沉淀量价背离类经验。"
        )
    reset_db_state()


def test_memory_search_finds_old_memories_beyond_recent_fifty(
    monkeypatch, tmp_path
) -> None:
    """memory_read 检索必须覆盖全库：旧实现只扫最近 50 条，
    记忆增长后较早的有效经验永远检索不到。"""
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            oldest = memory_service.create_memory(
                db,
                content="地量十字星后放量突破平台是可靠的中线买点",
                reason="2019 年多次验证的量价形态",
            )
            for index in range(60):
                memory_service.create_memory(
                    db,
                    content=f"常规经验条目 {index}：控制单票仓位上限",
                    reason="仓位管理",
                )
            # 把目标记忆的更新时间改为最早（模拟久远但仍有价值的经验）
            oldest.updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
            db.add(oldest)
            db.commit()

        result = memory_service.search_for_agent(
            keywords=["地量十字星"], match_mode="any", limit=5
        )

        assert result["ok"] is True
        assert result["result_count"] == 1
        assert "地量十字星" in result["items"][0]["content"]

        # all 模式：关键词分别命中 content 与 reason 的久远记忆
        result_all = memory_service.search_for_agent(
            keywords=["地量十字星", "量价形态"], match_mode="all", limit=5
        )
        assert result_all["result_count"] == 1

        # 不含关键词的常规条目不应命中
        result_none = memory_service.search_for_agent(
            keywords=["不存在的关键词xyz"], match_mode="any", limit=5
        )
        assert result_none["result_count"] == 0
    reset_db_state()
