"""工具结果对模型的可见性回归测试。

生产事故（2026-10-16~19）：记忆梦境连续多日报告"无运行报告、无交易结论"，
但当日明明有 14 次运行和成交。根因是 llm_service._slim_tool_result 按
mx 技能的规范形状 {ok, tool_name, summary, result} 统一投影所有工具结果，
而梦境/记忆工具返回 {ok, runs/items/...} 等自有形状——载荷被剥成
{"ok": true, "result": null}，模型只能误判"无数据"。记忆技能
（memory_read/memory_write）在所有智能体路径上同样不可见。

修复：规范形状照旧精简；非规范形状原样透传。本文件锁定该行为。
"""

from datetime import date
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.rate_limit import _limiter
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import StrategyRun
from app.main import create_app
from app.services.aniu_service import aniu_service, now_utc
from app.services.llm_service import LLMService, _slim_tool_result
from app.services.memory_dream_service import build_dream_tool_executor
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


def reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# _slim_tool_result 单元测试
# ---------------------------------------------------------------------------


def test_slim_tool_result_keeps_canonical_mx_shape_projected() -> None:
    """mx 技能/builtin_utils 的规范形状仍按元数据精简。"""
    slim = _slim_tool_result(
        {
            "ok": True,
            "tool_name": "mx_moni_trade",
            "summary": "已执行交易。",
            "result": {"orderId": "1"},
            "internal_field": "不应透传",
        }
    )

    assert slim == {
        "ok": True,
        "tool_name": "mx_moni_trade",
        "summary": "已执行交易。",
        "result": {"orderId": "1"},
    }


def test_slim_tool_result_preserves_error_in_canonical_shape() -> None:
    slim = _slim_tool_result(
        {
            "ok": False,
            "tool_name": "mx_moni_trade",
            "summary": "交易失败。",
            "result": None,
            "error": "可用资金不足",
        }
    )

    assert slim["ok"] is False
    assert slim["error"] == "可用资金不足"


def test_slim_tool_result_passes_dream_report_shape_through() -> None:
    """梦境报告形状（runs 载荷）必须原样到达模型。"""
    payload = {
        "ok": True,
        "target_date": "2026-10-19",
        "offset": 0,
        "limit": 10,
        "total": 1,
        "has_more": False,
        "runs": [{"run_id": 1763, "final_answer": "买入 600030"}],
    }

    slim = _slim_tool_result(payload)

    assert slim == payload
    assert slim["runs"][0]["final_answer"] == "买入 600030"


def test_slim_tool_result_passes_memory_shapes_through() -> None:
    """记忆工具形状（items/action/item）必须原样到达模型。"""
    listed = _slim_tool_result(
        {"ok": True, "total": 2, "items": [{"id": 1, "content": "记忆"}]}
    )
    assert listed["items"][0]["content"] == "记忆"

    written = _slim_tool_result(
        {"ok": True, "action": "create", "item": {"id": 3, "version": 1}}
    )
    assert written["item"]["id"] == 3


def test_slim_tool_result_passes_error_shape_through() -> None:
    """无 result/summary 的错误形状必须让模型看到 error 原因。"""
    slim = _slim_tool_result(
        {"ok": False, "tool_name": "memory_write", "error": "版本冲突"}
    )

    assert slim["ok"] is False
    assert slim["error"] == "版本冲突"


# ---------------------------------------------------------------------------
# 端到端：梦境工具结果必须真正到达模型
# ---------------------------------------------------------------------------


def test_agent_loop_delivers_dream_report_to_model(monkeypatch, tmp_path) -> None:
    """回归生产事故：模型调用 dream_read_reports 后，下一轮请求里的
    tool 消息必须包含当日运行的结论与交易，而不是 result: null。"""
    with create_test_client(monkeypatch, tmp_path):
        target = now_utc().date()
        now = now_utc().replace(tzinfo=None)
        with session_scope() as db:
            db.add(
                StrategyRun(
                    status="completed",
                    run_type="trade",
                    started_at=now,
                    finished_at=now,
                    final_answer="当日结论：尾盘买入 600030 共 1000 股",
                )
            )
            db.commit()

        service = LLMService()
        rounds: list[dict] = []

        def fake_call_llm_stream(
            *, base_url, api_key, payload, timeout_seconds, emit=None, cancel_event=None
        ):
            rounds.append(json.loads(json.dumps(payload)))
            if len(rounds) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "dream_read_reports",
                                            "arguments": '{"offset": 0, "limit": 10}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "整理完成"}}
                ]
            }

        monkeypatch.setattr(service, "_call_llm_stream", fake_call_llm_stream)

        service._agent_loop(
            model="m",
            base_url="https://x",
            api_key="k",
            initial_messages=[
                {"role": "user", "content": f"请整理 {target.isoformat()} 的报告"}
            ],
            run_type="dream",
            timeout_seconds=5,
            tool_executor=build_dream_tool_executor(target),
        )

        assert len(rounds) >= 2
        tool_messages = [
            m for m in rounds[1]["messages"] if m.get("role") == "tool"
        ]
        assert tool_messages, "第二轮请求必须携带工具结果消息"
        content = tool_messages[0]["content"]
        assert "尾盘买入 600030" in content, (
            "梦境报告载荷被剥掉（生产事故回归：模型只能看到 result: null）"
        )
        assert '"result": null' not in content
    reset_db_state()


def test_memory_skill_result_visible_to_model(monkeypatch, tmp_path) -> None:
    """记忆技能（所有 run_type 可用）的检索结果必须到达模型。"""
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            memory_service.create_memory(
                db, content="放量滞涨应减仓", reason="量价背离"
            )
            db.commit()

        service = LLMService()
        rounds: list[dict] = []

        def fake_call_llm_stream(
            *, base_url, api_key, payload, timeout_seconds, emit=None, cancel_event=None
        ):
            rounds.append(json.loads(json.dumps(payload)))
            if len(rounds) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "memory_read",
                                            "arguments": '{"keywords": ["放量"]}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "已参考记忆"}}
                ]
            }

        monkeypatch.setattr(service, "_call_llm_stream", fake_call_llm_stream)

        service._agent_loop(
            model="m",
            base_url="https://x",
            api_key="k",
            initial_messages=[{"role": "user", "content": "查一下记忆"}],
            run_type="trade",
            timeout_seconds=5,
            tool_executor=lambda tool_name, arguments, tool_call_id: (
                memory_service.search_for_agent(
                    keywords=arguments.get("keywords"),
                    match_mode=str(arguments.get("match_mode") or "any"),
                )
            ),
        )

        assert len(rounds) >= 2
        tool_messages = [
            m for m in rounds[1]["messages"] if m.get("role") == "tool"
        ]
        content = tool_messages[0]["content"]
        assert "放量滞涨应减仓" in content, (
            "记忆检索载荷被剥掉，交易智能体在所有路径上都以为记忆库为空"
        )
    reset_db_state()
