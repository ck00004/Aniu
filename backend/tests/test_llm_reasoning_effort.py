"""思考开关 / 思考等级 的回归测试。

设计（2026-11 按用户要求调整）：**两个独立设置项，代码不做任何替用户推断**。

  * `app_settings.llm_thinking_enabled`：是否开启思考（默认 False＝与历史行为一致）
  * `app_settings.llm_reasoning_effort`：low / medium / high / max（仅开启时生效）

同时修掉一个线上缺陷：原判断 `model.startswith("deepseek-v4-pro")` 在网关返回
`cn:deepseek-v4-pro` 时永远不成立，思考分支是死代码；现在先剥离 provider 前缀。
"""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.services.llm_service import (
    LLMService,
    LLMUpstreamError,
    _apply_reasoning_options,
    _strip_provider_prefix,
    llm_service,
)


def _payload(model: str) -> dict:
    return {
        "model": model,
        "temperature": 0.2,
        "messages": [],
        "tools": [{"type": "function", "function": {"name": "x"}}],
        "tool_choice": "auto",
    }


def test_provider_prefix_is_stripped():
    assert _strip_provider_prefix("cn:deepseek-v4-pro") == "deepseek-v4-pro"
    assert _strip_provider_prefix("CN:DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert _strip_provider_prefix("deepseek-v4-pro") == "deepseek-v4-pro"
    # 不能把 gpt-4o-mini 的连字符误当分隔符
    assert _strip_provider_prefix("gpt-4o-mini") == "gpt-4o-mini"


def test_default_is_thinking_disabled():
    """默认不开启：请求体里不出现任何思考参数（等于历史行为）。"""
    out = _apply_reasoning_options(_payload("cn:deepseek-v4.1-flash"))
    assert "thinking" not in out
    assert "reasoning_effort" not in out
    assert out["temperature"] == 0.2


def test_disabled_explicitly():
    out = _apply_reasoning_options(
        _payload("cn:deepseek-v4-pro"), thinking_enabled=False, effort="max"
    )
    assert "thinking" not in out
    assert "reasoning_effort" not in out


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_enabled_passes_chosen_level(level: str):
    out = _apply_reasoning_options(
        _payload("cn:deepseek-v4.1-flash"), thinking_enabled=True, effort=level
    )
    assert out["reasoning_effort"] == level


def test_enabled_without_level_defaults_to_high():
    out = _apply_reasoning_options(
        _payload("cn:deepseek-v4.1-flash"), thinking_enabled=True
    )
    assert out["reasoning_effort"] == "high"


def test_deepseek_thinking_model_gets_thinking_flag_and_drops_sampling():
    out = _apply_reasoning_options(
        _payload("cn:deepseek-v4-pro"), thinking_enabled=True, effort="medium"
    )
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == "medium"
    assert "temperature" not in out


def test_mimo_gets_thinking_flag():
    out = _apply_reasoning_options(
        _payload("cn:mimo-v2.5-pro"), thinking_enabled=True, effort="low"
    )
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == "low"


def test_legacy_effort_values_are_normalized():
    """旧值/脏值不报错：xhigh→max，auto/off/未知→high（关闭看开关）。"""
    cases = (("xhigh", "max"), ("auto", "high"), ("off", "high"), ("bogus", "high"))
    for legacy, expected in cases:
        out = _apply_reasoning_options(
            _payload("cn:deepseek-v4.1-flash"), thinking_enabled=True, effort=legacy
        )
        assert out["reasoning_effort"] == expected, legacy


def test_settings_flow_into_request_payload():
    settings = SimpleNamespace(
        llm_model="cn:deepseek-v4.1-flash",
        system_prompt="system",
        run_type="analysis",
        prompt_templates=None,
        llm_thinking_enabled=True,
        llm_reasoning_effort="max",
    )
    payload = llm_service.build_request_payload_from_messages(
        app_settings=settings, messages=[{"role": "user", "content": "hi"}]
    )
    assert payload["reasoning_effort"] == "max"


def test_settings_off_overrides_level():
    settings = SimpleNamespace(
        llm_model="cn:deepseek-v4-pro",
        system_prompt="system",
        run_type="analysis",
        prompt_templates=None,
        llm_thinking_enabled=False,
        llm_reasoning_effort="max",
    )
    payload = llm_service.build_request_payload_from_messages(
        app_settings=settings, messages=[{"role": "user", "content": "hi"}]
    )
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload


def test_missing_settings_fields_default_to_disabled():
    """老配置（没有这两个字段）必须与历史行为一致：不发送思考参数。"""
    settings = SimpleNamespace(
        llm_model="cn:deepseek-v4-pro",
        system_prompt="system",
        run_type="analysis",
        prompt_templates=None,
    )
    payload = llm_service.build_request_payload_from_messages(
        app_settings=settings, messages=[{"role": "user", "content": "hi"}]
    )
    assert "reasoning_effort" not in payload


def test_400_on_reasoning_params_retries_without_them(monkeypatch):
    """上游不认识 reasoning/thinking → 剔除后重试，不能让整轮失败。"""
    attempts: list[dict] = []

    def fake_consume(self, *, payload, **kwargs):
        attempts.append(dict(payload))
        if "reasoning_effort" in payload:
            raise LLMUpstreamError(
                "HTTP 400: Unsupported parameter: reasoning_effort",
                status_code=400,
            )
        return {"choices": [{"message": {"content": "ok"}}], "stream_meta": {}}

    monkeypatch.setattr(LLMService, "_consume_llm_stream", fake_consume)

    result = llm_service._call_llm_stream(
        base_url="http://127.0.0.1:1/v1",
        api_key="k",
        payload=_apply_reasoning_options(
            _payload("cn:deepseek-v4-pro"), thinking_enabled=True, effort="high"
        ),
        timeout_seconds=5,
    )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert len(attempts) >= 2
    assert "reasoning_effort" in attempts[0]
    assert "reasoning_effort" not in attempts[-1]
    assert "thinking" not in attempts[-1]


def test_update_settings_persists_both_fields(monkeypatch):
    """端到端：开关 + 等级都能写库并进入请求体。"""
    import os
    import tempfile

    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("SQLITE_DB_PATH", os.path.join(tmpdir, "eff.db"))
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "x")

    from app.core.config import get_settings
    from app.db import database as database_module
    from app.db.database import init_db, session_scope
    from app.schemas.aniu import AppSettingsUpdate
    from app.services.aniu_service import aniu_service

    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    try:
        with session_scope() as db:
            updated = aniu_service.update_settings(
                db,
                AppSettingsUpdate(
                    system_prompt="s",
                    llm_thinking_enabled=True,
                    llm_reasoning_effort="MEDIUM",
                ),
            )
            assert updated.llm_thinking_enabled is True
            assert updated.llm_reasoning_effort == "medium"

        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.run_type = "analysis"
            payload = llm_service.build_request_payload_from_messages(
                app_settings=settings, messages=[{"role": "user", "content": "hi"}]
            )
        assert payload["reasoning_effort"] == "medium"

        # 关掉开关后：等级保留，但请求体不再带思考参数
        with session_scope() as db:
            aniu_service.update_settings(
                db, AppSettingsUpdate(system_prompt="s", llm_thinking_enabled=False)
            )
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.run_type = "analysis"
            assert settings.llm_reasoning_effort == "medium"
            payload = llm_service.build_request_payload_from_messages(
                app_settings=settings, messages=[{"role": "user", "content": "hi"}]
            )
        assert "reasoning_effort" not in payload
    finally:
        database_module._engine = None
        database_module._session_local = None
        get_settings.cache_clear()
