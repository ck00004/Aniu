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


def test_effort_is_passed_through_verbatim():
    """等级是手动输入：只做去空白/小写/别名归一，其它值原样发送。"""
    cases = (
        ("HIGH", "high"),
        (" max ", "max"),
        ("xhigh", "max"),
        ("minimal", "minimal"),
        ("veryhigh", "veryhigh"),
        ("最高", "max"),
    )
    for raw, expected in cases:
        out = _apply_reasoning_options(
            _payload("cn:deepseek-v4.1-flash"), thinking_enabled=True, effort=raw
        )
        assert out["reasoning_effort"] == expected, raw


def test_empty_effort_falls_back_to_high():
    for empty in (None, "", "   "):
        out = _apply_reasoning_options(
            _payload("cn:deepseek-v4.1-flash"), thinking_enabled=True, effort=empty
        )
        assert out["reasoning_effort"] == "high"


def test_schema_keeps_manual_value():
    """写入路径不能把用户手输的等级改写成 high。"""
    from app.schemas.aniu import AppSettingsBase

    for raw, expected in (
        ("minimal", "minimal"),
        ("VERYHIGH", "veryhigh"),
        (" max ", "max"),
        ("", "high"),
    ):
        model = AppSettingsBase(system_prompt="s", llm_reasoning_effort=raw)
        assert model.llm_reasoning_effort == expected, raw


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


def test_unknown_level_503_from_gateway_degrades_instead_of_failing(monkeypatch):
    """实测：未知等级被网关拒时返回 503（no_healthy_account），
    必须剔除参数重试，而不是让整轮交易分析失败。"""
    attempts: list[dict] = []

    def fake_consume(self, *, payload, **kwargs):
        attempts.append(dict(payload))
        if "reasoning_effort" in payload:
            raise LLMUpstreamError(
                'HTTP 503: {"error":{"code":"no_healthy_account",'
                '"gateway_hint":"request parameters were rejected by the model"}}',
                status_code=503,
            )
        return {"choices": [{"message": {"content": "ok"}}], "stream_meta": {}}

    monkeypatch.setattr(LLMService, "_consume_llm_stream", fake_consume)

    events: list[tuple] = []
    result = llm_service._call_llm_stream(
        base_url="http://127.0.0.1:1/v1",
        api_key="k",
        payload=_apply_reasoning_options(
            _payload("cn:deepseek-v4.1-flash"), thinking_enabled=True, effort="veryhigh"
        ),
        timeout_seconds=5,
        emit=lambda *a, **kw: events.append((a, kw)),
    )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert attempts[0]["reasoning_effort"] == "veryhigh"
    assert "reasoning_effort" not in attempts[-1]
    # 用户可见的降级提示
    assert any("思考等级" in str(kw.get("message", "")) for _, kw in events)


def test_irrelevant_5xx_keeps_reasoning_params(monkeypatch):
    """与思考参数无关的 5xx 不应被误判成参数问题（仍按原样重试）。"""
    calls: list[dict] = []

    def fake_consume(self, *, payload, **kwargs):
        calls.append(dict(payload))
        if len(calls) == 1:
            raise LLMUpstreamError("HTTP 502: upstream connect error", status_code=502)
        return {"choices": [{"message": {"content": "ok"}}], "stream_meta": {}}

    monkeypatch.setattr(LLMService, "_consume_llm_stream", fake_consume)

    result = llm_service._call_llm_stream(
        base_url="http://127.0.0.1:1/v1",
        api_key="k",
        payload=_apply_reasoning_options(
            _payload("cn:deepseek-v4.1-flash"), thinking_enabled=True, effort="high"
        ),
        timeout_seconds=5,
    )
    assert result["choices"][0]["message"]["content"] == "ok"
    assert calls[-1]["reasoning_effort"] == "high"


def test_chat_forwards_thinking_settings(monkeypatch):
    """聊天路径此前没接思考开关（即使打开了也不会思考），必须透传。"""
    captured: dict = {}

    def fake_agent_loop(self, **kwargs):
        captured.update(kwargs)
        return {"final_answer": "ok", "messages": [], "responses": [], "tool_calls": []}

    monkeypatch.setattr(LLMService, "_agent_loop", fake_agent_loop)

    llm_service.chat(
        model="cn:deepseek-v4.1-flash",
        base_url="http://127.0.0.1:1/v1",
        api_key="k",
        system_prompt="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_enabled=True,
        reasoning_effort="max",
    )

    assert captured["thinking_enabled"] is True
    assert captured["reasoning_effort"] == "max"


def test_generate_text_forwards_thinking_settings(monkeypatch):
    """新闻诊断等辅助生成路径同样透传（否则开关对它们无效）。"""
    captured: dict = {}

    def fake_call_llm_stream(self, *, payload, **kwargs):
        captured.update(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(LLMService, "_call_llm_stream", fake_call_llm_stream)

    llm_service.generate_text(
        model="cn:deepseek-v4.1-flash",
        base_url="http://127.0.0.1:1/v1",
        api_key="k",
        system_prompt="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_enabled=True,
        reasoning_effort="medium",
    )
    assert captured["reasoning_effort"] == "medium"


def test_single_global_settings_drives_all_payload_builders():
    """全局唯一：同一份 app_settings 驱动调度运行与聊天两条构造路径。"""
    settings = SimpleNamespace(
        llm_model="cn:deepseek-v4.1-flash",
        system_prompt="system",
        task_prompt="task",
        run_type="analysis",
        prompt_templates=None,
        llm_thinking_enabled=True,
        llm_reasoning_effort="max",
    )

    scheduled = llm_service.build_initial_request_payload(settings)
    chat = llm_service.build_request_payload_from_messages(
        app_settings=settings, messages=[{"role": "user", "content": "hi"}]
    )

    assert scheduled["reasoning_effort"] == "max"
    assert chat["reasoning_effort"] == "max"

    # 关掉同一份配置，两条路径同时失效
    settings.llm_thinking_enabled = False
    assert "reasoning_effort" not in llm_service.build_initial_request_payload(settings)
    assert "reasoning_effort" not in llm_service.build_request_payload_from_messages(
        app_settings=settings, messages=[{"role": "user", "content": "hi"}]
    )
