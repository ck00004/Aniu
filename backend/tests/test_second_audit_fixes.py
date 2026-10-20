"""二次审计修复的回归测试（F1/A1/A2）。

- F1：automation_dream_time 暴露到设置页——写入校验 HH:MM、服务层往返。
- A1：_extract_action_records 对非数值 quantity 防御性归零，不让运行崩溃。
- A2：模型返回非法 JSON 工具参数时，把错误回传给模型让其重新生成，
  而不是直接失败整次运行。
"""

import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.schemas.aniu import AppSettingsUpdate
from app.services.aniu_service import aniu_service
from app.services.llm_service import LLMService


def _use_temp_db(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None


def _reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# F1：梦境整理时间
# ---------------------------------------------------------------------------


def test_app_settings_update_validates_dream_time_format() -> None:
    assert AppSettingsUpdate(system_prompt="x").automation_dream_time == "00:30"
    assert (
        AppSettingsUpdate(
            system_prompt="x", automation_dream_time="01:15"
        ).automation_dream_time
        == "01:15"
    )
    assert (
        AppSettingsUpdate(
            system_prompt="x", automation_dream_time="0:30"
        ).automation_dream_time
        == "0:30"
    )

    for invalid in ("25:99", "abc", "0030", "12:5", "23:60", ""):
        with pytest.raises(ValidationError):
            AppSettingsUpdate(system_prompt="x", automation_dream_time=invalid)


def test_update_settings_roundtrips_dream_time(monkeypatch, tmp_path) -> None:
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            assert settings.automation_dream_time == "00:30"

        with session_scope() as db:
            updated = aniu_service.update_settings(
                db,
                AppSettingsUpdate(
                    system_prompt=str(getattr(settings, "system_prompt", "") or "x"),
                    automation_dream_time="01:15",
                ),
            )
            assert updated.automation_dream_time == "01:15"

        with session_scope() as db:
            reloaded = aniu_service.get_or_create_settings(db)
            assert reloaded.automation_dream_time == "01:15"
    finally:
        _reset_db_state()


# ---------------------------------------------------------------------------
# A1：数量解析防御
# ---------------------------------------------------------------------------


def test_extract_action_records_tolerates_non_numeric_quantity() -> None:
    tool_calls = [
        {
            "id": "call-1",
            "name": "mx_moni_trade",
            "arguments": {},
            "result": {
                "ok": True,
                "planned_action": {
                    "action": "BUY",
                    "symbol": "600030",
                    "quantity": "100股",
                },
            },
        },
        {
            "id": "call-2",
            "name": "mx_moni_trade",
            "arguments": {},
            "result": {
                "ok": True,
                "planned_action": {
                    "action": "SELL",
                    "symbol": "600027",
                    "quantity": {"value": 100},
                },
            },
        },
    ]

    records = aniu_service._extract_planned_actions(tool_calls)

    assert len(records) == 2
    assert records[0]["quantity"] == 0
    assert records[1]["quantity"] == 0


# ---------------------------------------------------------------------------
# A2：非法 JSON 工具参数回传模型重新生成
# ---------------------------------------------------------------------------


def test_agent_loop_invalid_json_arguments_feed_error_back_to_model(
    monkeypatch,
) -> None:
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
                                    "id": "call-bad",
                                    "type": "function",
                                    "function": {
                                        "name": "mx_get_positions",
                                        "arguments": '{"limit": ',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "已修正参数并给出结论"}}
            ]
        }

    monkeypatch.setattr(service, "_call_llm_stream", fake_call_llm_stream)

    executed: list[tuple[str, dict]] = []

    def tool_executor(tool_name: str, arguments: dict, tool_call_id=None) -> dict:
        executed.append((tool_name, arguments))
        return {"ok": True, "tool_name": tool_name, "summary": "ok", "result": {}}

    result = service._agent_loop(
        model="m",
        base_url="https://x",
        api_key="k",
        initial_messages=[{"role": "user", "content": "查询持仓"}],
        run_type="analysis",
        timeout_seconds=5,
        tool_executor=tool_executor,
    )

    # 坏参数调用未执行；运行以模型重新生成的结论正常完成
    assert executed == []
    assert result["final_answer"] == "已修正参数并给出结论"

    # 第二轮请求携带回传的错误工具消息，提示模型重新调用
    assert len(rounds) >= 2
    tool_messages = [
        m for m in rounds[1]["messages"] if m.get("role") == "tool"
    ]
    assert tool_messages, "第二轮请求必须携带工具结果消息"
    content = tool_messages[0]["content"]
    assert "工具参数不是合法 JSON" in content
    assert "重新调用" in content
    assert tool_messages[0]["tool_call_id"] == "call-bad"

    # 坏调用进入 tool_history 供运行记录追溯
    assert result["tool_history"][0]["name"] == "mx_get_positions"
    assert result["tool_history"][0]["result"]["ok"] is False
