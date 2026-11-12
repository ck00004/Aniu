"""思考等级（reasoning effort）配置的回归测试。

背景（2026-11 排查）：代码里本来有"思考"分支，但
`_should_enable_deepseek_thinking()` 用 `startswith("deepseek-v4-pro")` 判断，
而网关返回的模型名统一带 provider 前缀（`cn:deepseek-v4-pro`），**永远匹配
不上** → 思考分支是死代码，线上从未开启过思考，且没有任何配置入口。

本次改动：
  1. 剥离 provider 前缀后再判断；
  2. app_settings.llm_reasoning_effort（auto/off/low/medium/high/max）可配；
  3. auto = 按运行类型默认（analysis → high，trade → off）；
  4. 上游 400 拒绝 reasoning 参数时自动剔除并重试（思考失效但不整轮失败）。
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
    _supports_reasoning,
    llm_service,
)


def _payload(model: str, *, tools: bool = True) -> dict:
    payload: dict = {"model": model, "temperature": 0.2, "messages": []}
    if tools:
        payload["tools"] = [{"type": "function", "function": {"name": "x"}}]
        payload["tool_choice"] = "auto"
    return payload


def test_provider_prefix_is_stripped():
    assert _strip_provider_prefix("cn:deepseek-v4-pro") == "deepseek-v4-pro"
    assert _strip_provider_prefix("CN:DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert _strip_provider_prefix("deepseek-v4-pro") == "deepseek-v4-pro"
    # 不能把 gpt-4o-mini 的连字符误当分隔符切掉
    assert _strip_provider_prefix("gpt-4o-mini") == "gpt-4o-mini"


def test_prefixed_thinking_model_enables_thinking():
    """线上真实写法 `cn:deepseek-v4-pro` 必须能打开思考（旧实现这里是 False）。"""
    out = _apply_reasoning_options(_payload("cn:deepseek-v4-pro"), run_type="analysis")
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == "high"
    # DeepSeek 思考模式不接受采样参数
    assert "temperature" not in out


def test_off_disables_everything():
    out = _apply_reasoning_options(
        _payload("cn:deepseek-v4-pro"), effort="off", run_type="analysis"
    )
    assert "thinking" not in out
    assert "reasoning_effort" not in out
    # 关闭思考后温度参数保留（普通采样）
    assert out["temperature"] == 0.2


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_explicit_levels_are_passed_through(level: str):
    out = _apply_reasoning_options(_payload("cn:deepseek-v4-pro"), effort=level)
    assert out["reasoning_effort"] == level


def test_explicit_level_applies_to_any_model():
    """用户显式选档时不再限制模型名单（不支持则由 400 降级兜底）。"""
    out = _apply_reasoning_options(_payload("cn:glm-5.3"), effort="high")
    assert out["reasoning_effort"] == "high"
    assert "thinking" not in out  # 非 deepseek 风格不加 thinking 字段


def test_auto_uses_run_type_default():
    analysis = _apply_reasoning_options(_payload("cn:deepseek-v4-pro"), run_type="analysis")
    trade = _apply_reasoning_options(_payload("cn:deepseek-v4-pro"), run_type="trade")
    assert analysis["reasoning_effort"] == "high"
    assert "reasoning_effort" not in trade
    assert "thinking" not in trade


def test_auto_skips_models_without_reasoning_support():
    """auto 不给未知模型塞参数（避免 400），但显式选档仍生效。"""
    assert _supports_reasoning("cn:deepseek-v4.1-flash") is True
    assert _supports_reasoning("cn:glm-5.3") is False
    out = _apply_reasoning_options(_payload("cn:glm-5.3"), run_type="analysis")
    assert "reasoning_effort" not in out


def test_mimo_still_enables_thinking_in_auto():
    out = _apply_reasoning_options(_payload("cn:mimo-v2.5-pro"), run_type="analysis")
    assert out["thinking"] == {"type": "enabled"}


def test_settings_value_flows_into_request_payload():
    """app_settings.llm_reasoning_effort 必须真的进入请求体。"""
    settings = SimpleNamespace(
        llm_model="cn:deepseek-v4.1-flash",
        system_prompt="system",
        run_type="analysis",
        prompt_templates=None,
        llm_reasoning_effort="max",
    )
    payload = llm_service.build_request_payload_from_messages(
        app_settings=settings, messages=[{"role": "user", "content": "hi"}]
    )
    assert payload["reasoning_effort"] == "max"


def test_settings_off_overrides_auto_default():
    settings = SimpleNamespace(
        llm_model="cn:deepseek-v4-pro",
        system_prompt="system",
        run_type="analysis",
        prompt_templates=None,
        llm_reasoning_effort="off",
    )
    payload = llm_service.build_request_payload_from_messages(
        app_settings=settings, messages=[{"role": "user", "content": "hi"}]
    )
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload


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
            _payload("cn:deepseek-v4-pro"), effort="high"
        ),
        timeout_seconds=5,
    )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert len(attempts) >= 2
    assert "reasoning_effort" in attempts[0]
    assert "reasoning_effort" not in attempts[-1]
    assert "thinking" not in attempts[-1]


def test_update_settings_persists_effort_and_affects_payload(monkeypatch):
    """端到端：写设置 → 读回 → 进入请求体。"""
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
                db, AppSettingsUpdate(system_prompt="s", llm_reasoning_effort="HIGH")
            )
            assert updated.llm_reasoning_effort == "high"

        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.run_type = "analysis"
            payload = llm_service.build_request_payload_from_messages(
                app_settings=settings, messages=[{"role": "user", "content": "hi"}]
            )
        assert payload["reasoning_effort"] == "high"

        # 改成 off 后请求体里不应再出现思考参数
        with session_scope() as db:
            aniu_service.update_settings(
                db, AppSettingsUpdate(system_prompt="s", llm_reasoning_effort="off")
            )
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.run_type = "analysis"
            payload = llm_service.build_request_payload_from_messages(
                app_settings=settings, messages=[{"role": "user", "content": "hi"}]
            )
        assert "reasoning_effort" not in payload
    finally:
        database_module._engine = None
        database_module._session_local = None
        get_settings.cache_clear()
