from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

import httpx

from app.core.prompt_templates import resolve_prompt_template
from app.skills import skill_registry
from app.services.context_compaction import compact_messages_for_overflow
from app.services.mx_service import MXClient
from app.services.skill_stack_service import skill_stack_service

logger = logging.getLogger(__name__)

_LLM_TEMPERATURE = 0.2
_MAX_TOOL_ITERATIONS = 100
_FINAL_STREAM_CHUNK_SIZE = 96
_EMPTY_FINAL_ANSWER_RETRY_LIMIT = 1
_LLM_UPSTREAM_MAX_RETRIES = 3
_LLM_UPSTREAM_RETRY_DELAY_SECONDS = 1.0
_MIMO_THINKING_MODEL_PREFIXES = (
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "mimo-v2-pro",
    "mimo-v2-omni",
    "mimo-v2-flash",
)
_DEEPSEEK_THINKING_MODEL_PREFIXES = (
    "deepseek-v4-pro",
)
_DEEPSEEK_IGNORED_SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
)

class LLMStreamCancelled(RuntimeError):
    """Raised when a streaming chat/run should stop because the client disconnected."""


class LLMErrorCode:
    """LLM 上游错误分类（移植自原版 Aniu llm/errors.py）。"""

    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_4XX = "provider_4xx"
    PROVIDER_5XX = "provider_5xx"
    EMPTY_RESPONSE = "empty_response"
    CONTEXT_OVERFLOW = "context_overflow"
    UNKNOWN = "unknown"


_CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length exceeded",
    "maximum context length",
    "model_context_window_exceeded",
    "prompt too long; exceeded",
    "too large for model with",
    "exceed context limit",
)


def _classify_http_status(status_code: int | None) -> str:
    if status_code is None:
        return LLMErrorCode.UNKNOWN
    if status_code in {401, 403}:
        return LLMErrorCode.AUTHENTICATION
    if status_code == 408:
        return LLMErrorCode.TIMEOUT
    if status_code == 429:
        return LLMErrorCode.RATE_LIMIT
    if 500 <= status_code <= 599:
        return LLMErrorCode.PROVIDER_5XX
    if 400 <= status_code <= 499:
        return LLMErrorCode.PROVIDER_4XX
    return LLMErrorCode.UNKNOWN


def _classify_error_text(text: str) -> str | None:
    lowered = str(text or "").lower()
    if not lowered:
        return None
    if "rate_limit_error" in lowered or "rate limit exceeded" in lowered:
        return LLMErrorCode.RATE_LIMIT
    if "overloaded" in lowered:
        return LLMErrorCode.PROVIDER_5XX
    if any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS):
        return LLMErrorCode.CONTEXT_OVERFLOW
    return None


def _retry_after_seconds_from_headers(headers: Any) -> float | None:
    """解析 Retry-After：支持 retry-after-ms 头、秒数与 HTTP 日期三种格式
    （移植自原版 Aniu llm/errors.py `_retry_after_seconds`）。"""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None

    def _header(name: str) -> str | None:
        value = getter(name)
        return str(value).strip() if value is not None else None

    milliseconds = _header("retry-after-ms")
    if milliseconds:
        try:
            return max(0.0, float(milliseconds) / 1000.0)
        except ValueError:
            pass
    value = _header("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


def _should_retry_header(headers: Any) -> bool | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter("x-should-retry")
    if value == "true":
        return True
    if value == "false":
        return False
    return None


class LLMUpstreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retry_after: float | None = None,
        retryable_override: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code or _classify_http_status(status_code)
        self.retry_after = retry_after
        self.retryable_override = retryable_override


def accumulate_usage(responses: Any) -> dict[str, int] | None:
    """跨轮累计 usage（移植自原版 Aniu llm/usage.py 的归一化思路）。

    多轮工具调用每轮 prompt 都在增长，真实消耗是各轮之和；只取最后
    一轮会系统性少记。
    """
    if not isinstance(responses, list):
        return None

    def _usage_int(usage: dict[str, Any], key: str) -> int:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(0, int(value))

    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    found_any = False
    for item in responses:
        if not isinstance(item, dict):
            continue
        usage = item.get("usage")
        if not isinstance(usage, dict):
            continue
        found_any = True
        totals["prompt_tokens"] += _usage_int(usage, "prompt_tokens")
        totals["completion_tokens"] += _usage_int(usage, "completion_tokens")
        totals["total_tokens"] += _usage_int(usage, "total_tokens")
    if not found_any:
        return None
    if totals["total_tokens"] == 0:
        totals["total_tokens"] = totals["prompt_tokens"] + totals["completion_tokens"]
    return totals


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise LLMStreamCancelled("客户端连接已断开。")


def _format_error_message(prefix: str, detail: str) -> str:
    detail_text = str(detail or "").strip()
    if detail_text:
        return f"{prefix}: {detail_text}"
    return f"{prefix}。"


def _is_retryable_upstream_error(exc: LLMUpstreamError) -> bool:
    """错误分类驱动的可重试判定（移植自原版 Aniu llm/errors.py）。

    旧实现只认 status>=500，429 限频与超时/网络错误都不重试——
    恰恰是瞬态错误中最常见的两类。上下文溢出明确不重试（重试必然
    再溢出，应尽快失败让上层裁剪上下文）。
    """
    if exc.error_code == LLMErrorCode.CONTEXT_OVERFLOW:
        return False
    if exc.retryable_override is not None:
        return exc.retryable_override
    if exc.error_code in {
        LLMErrorCode.RATE_LIMIT,
        LLMErrorCode.TIMEOUT,
        LLMErrorCode.NETWORK,
        LLMErrorCode.PROVIDER_5XX,
        LLMErrorCode.EMPTY_RESPONSE,
    }:
        return True
    return exc.status_code in {408, 409, 425, 429}


def _should_fallback_to_non_stream(exc: LLMUpstreamError) -> bool:
    return exc.status_code == 400


def _should_enable_mimo_thinking(model: Any) -> bool:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return False
    return normalized.startswith(_MIMO_THINKING_MODEL_PREFIXES)


def _should_enable_deepseek_thinking(model: Any) -> bool:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return False
    return normalized.startswith(_DEEPSEEK_THINKING_MODEL_PREFIXES)


def _normalize_reasoning_effort(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"low", "medium", "high"}:
        return "high"
    if normalized in {"xhigh", "max"}:
        return "max"
    return None


def _default_deepseek_reasoning_effort(payload: dict[str, Any]) -> str:
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        return "max"
    return "high"


def _apply_reasoning_options(payload: dict[str, Any]) -> dict[str, Any]:
    adjusted = dict(payload)
    if _should_enable_deepseek_thinking(adjusted.get("model")):
        thinking_payload = adjusted.get("thinking")
        thinking_type = "enabled"
        if isinstance(thinking_payload, dict):
            normalized_type = str(thinking_payload.get("type") or "").strip().lower()
            if normalized_type in {"enabled", "disabled"}:
                thinking_type = normalized_type
        adjusted["thinking"] = {"type": thinking_type}

        reasoning_effort = _normalize_reasoning_effort(adjusted.get("reasoning_effort"))
        if thinking_type == "enabled":
            adjusted["reasoning_effort"] = (
                reasoning_effort or _default_deepseek_reasoning_effort(adjusted)
            )
            for key in _DEEPSEEK_IGNORED_SAMPLING_KEYS:
                adjusted.pop(key, None)
        else:
            adjusted.pop("reasoning_effort", None)
        return adjusted

    if _should_enable_mimo_thinking(adjusted.get("model")):
        adjusted["thinking"] = {"type": "enabled"}
    return adjusted


def _normalize_tool_call_for_replay(tool_call: Any) -> dict[str, Any] | None:
    if not isinstance(tool_call, dict):
        return None

    function_payload = tool_call.get("function")
    if not isinstance(function_payload, dict):
        function_payload = {}

    tool_name = str(function_payload.get("name") or "").strip()
    arguments = function_payload.get("arguments")
    arguments_text = arguments if isinstance(arguments, str) else ""
    if not tool_name and not arguments_text:
        return None

    return {
        "id": str(tool_call.get("id") or "").strip() or None,
        "type": str(tool_call.get("type") or "function").strip() or "function",
        "function": {
            "name": tool_name,
            "arguments": arguments_text,
        },
    }


def _sleep_before_retry(
    delay_seconds: float,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    if delay_seconds <= 0:
        _raise_if_cancelled(cancel_event)
        return
    if cancel_event is not None:
        if cancel_event.wait(delay_seconds):
            raise LLMStreamCancelled("客户端连接已断开。")
        return
    time.sleep(delay_seconds)


def _extract_error_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            part = _extract_error_text(item)
            if part:
                parts.append(part)
        return "; ".join(parts)
    if isinstance(value, dict):
        for key in ("message", "detail", "msg", "error_description", "reason"):
            part = _extract_error_text(value.get(key))
            if part:
                return part
        return _safe_json_dumps(value)
    return str(value).strip()


def _extract_error_detail(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = _extract_error_text(payload.get("error"))
        if detail:
            return detail
        for key in ("message", "detail", "msg", "error_description"):
            detail = _extract_error_text(payload.get(key))
            if detail:
                return detail
    return _extract_error_text(payload)


def _decode_response_body(response: httpx.Response, raw_body: bytes) -> str:
    if not raw_body:
        return ""
    encoding = response.encoding or "utf-8"
    try:
        return raw_body.decode(encoding, errors="replace").strip()
    except LookupError:
        return raw_body.decode("utf-8", errors="replace").strip()


def _extract_response_error_detail(response: httpx.Response, raw_body: bytes) -> str:
    body_text = _decode_response_body(response, raw_body)
    if not body_text:
        return ""
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return body_text[:500]
    detail = _extract_error_detail(payload)
    return (detail or body_text)[:500]


def _raise_upstream_http_error(response: httpx.Response, raw_body: bytes) -> None:
    status = int(response.status_code)
    detail = _extract_response_error_detail(response, raw_body)
    error_code = _classify_error_text(detail) or _classify_http_status(status)
    headers = getattr(response, "headers", None)
    retry_after = _retry_after_seconds_from_headers(headers)
    retryable_override = _should_retry_header(headers)
    if status == 401:
        raise LLMUpstreamError(
            _format_error_message("大模型 API Key 无效或已过期 (401)", detail),
            status_code=status,
            error_code=error_code,
            retry_after=retry_after,
            retryable_override=retryable_override,
        )
    if status == 400:
        raise LLMUpstreamError(
            _format_error_message("大模型请求参数错误 (400)", detail),
            status_code=status,
            error_code=error_code,
            retry_after=retry_after,
            retryable_override=retryable_override,
        )
    if status == 429:
        raise LLMUpstreamError(
            _format_error_message("大模型接口请求频率超限 (429)", detail),
            status_code=status,
            error_code=error_code,
            retry_after=retry_after,
            retryable_override=retryable_override,
        )
    raise LLMUpstreamError(
        _format_error_message(f"大模型接口返回错误 ({status})", detail),
        status_code=status,
        error_code=error_code,
        retry_after=retry_after,
        retryable_override=retryable_override,
    )


def _to_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _safe_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _slim_tool_result(tool_result: dict[str, Any]) -> dict[str, Any]:
    """Pass raw tool payloads to the model while keeping minimal metadata.

    mx 技能与 builtin_utils 返回 {ok, tool_name, summary, result} 规范形状，
    按元数据精简；梦境/记忆等内置工具返回各自的结构化形状（runs/items/
    action/error 等键），必须原样透传。统一按规范形状投影会把非规范载荷
    剥成 {"ok": true, "result": null}，模型只能误判"无数据"——生产
    2026-10-16~19 记忆梦境连续空转、记忆技能在所有智能体路径不可见的根因。
    """
    if "result" in tool_result or "summary" in tool_result:
        slim = {
            "ok": tool_result.get("ok"),
            "tool_name": tool_result.get("tool_name"),
            "summary": tool_result.get("summary"),
            "result": tool_result.get("result"),
        }
        if "error" in tool_result:
            slim["error"] = tool_result.get("error")
        return slim
    return dict(tool_result)


def _iter_text_chunks(content: str, chunk_size: int = _FINAL_STREAM_CHUNK_SIZE):
    text = str(content or "")
    if not text:
        return

    for block in text.splitlines(keepends=True):
        if len(block) <= chunk_size:
            yield block
            continue

        start = 0
        while start < len(block):
            yield block[start : start + chunk_size]
            start += chunk_size


def _to_stream_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _to_reasoning_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
                continue
            if isinstance(item.get("content"), str):
                parts.append(item["content"])
        return "".join(parts)
    return ""


def _merge_stream_tool_call(
    tool_calls: dict[int, dict[str, Any]],
    delta_payload: dict[str, Any],
) -> None:
    index = int(delta_payload.get("index") or 0)
    entry = tool_calls.setdefault(
        index,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )

    call_id = delta_payload.get("id")
    if isinstance(call_id, str) and call_id:
        entry["id"] = call_id

    call_type = delta_payload.get("type")
    if isinstance(call_type, str) and call_type:
        entry["type"] = call_type

    function_payload = delta_payload.get("function")
    if not isinstance(function_payload, dict):
        return

    function_entry = entry.setdefault("function", {"name": "", "arguments": ""})
    name = function_payload.get("name")
    if isinstance(name, str) and name:
        function_entry["name"] += name

    arguments = function_payload.get("arguments")
    if isinstance(arguments, str) and arguments:
        function_entry["arguments"] += arguments


class LLMService:
    # 模块级累计函数挂为静态方法，供 chat_session_service 等调用方
    # 通过 llm_service 实例访问。
    accumulate_usage = staticmethod(accumulate_usage)

    def _create_http_client(self, timeout_seconds: int) -> httpx.Client:
        return httpx.Client(timeout=float(timeout_seconds))

    def close(self) -> None:
        return None

    @staticmethod
    def normalize_replay_messages(messages: Any) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            return []

        normalized_messages: list[dict[str, Any]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            if role not in {"assistant", "tool"}:
                continue

            content = item.get("content")
            if role == "tool":
                tool_call_id = str(item.get("tool_call_id") or "").strip()
                if not tool_call_id:
                    continue
                normalized_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(content or ""),
                    }
                )
                continue

            entry: dict[str, Any] = {
                "role": "assistant",
                "content": content if isinstance(content, str) else _to_text_content(content),
            }
            reasoning_content = _to_reasoning_content(item.get("reasoning_content"))
            if reasoning_content:
                entry["reasoning_content"] = reasoning_content

            raw_tool_calls = item.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                normalized_tool_calls = [
                    tool_call
                    for tool_call in (
                        _normalize_tool_call_for_replay(raw_tool_call)
                        for raw_tool_call in raw_tool_calls
                    )
                    if tool_call is not None
                ]
                if normalized_tool_calls:
                    entry["tool_calls"] = normalized_tool_calls

            normalized_messages.append(entry)
        return normalized_messages

    @classmethod
    def extract_turn_replay_messages(
        cls,
        messages: Any,
        *,
        initial_message_count: int = 0,
    ) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            return []
        start_index = max(0, int(initial_message_count or 0))
        return cls.normalize_replay_messages(messages[start_index:])

    def chat_with_details(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        system_prompt: str | None,
        prompt_templates: Any = None,
        messages: list[dict[str, str]],
        timeout_seconds: int = 60,
        tool_context: dict[str, Any] | None = None,
        emit: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        payload_messages: list[dict[str, Any]] = []
        effective_system_prompt = self._augment_system_prompt(
            system_prompt,
            run_type="chat",
            prompt_templates=prompt_templates,
        )
        if effective_system_prompt:
            payload_messages.append(
                {"role": "system", "content": effective_system_prompt}
            )
        payload_messages.extend(messages)
        chat_tool_context = {**(tool_context or {}), "run_type": "chat"}

        def _chat_tool_executor(
            tool_name: str,
            arguments: dict[str, Any],
            tool_call_id: str | None,
        ) -> dict[str, Any]:
            del tool_call_id
            return skill_stack_service.runtime.execute_tool(
                tool_name=tool_name,
                arguments=arguments,
                context=chat_tool_context,
            )

        result = self._agent_loop(
            model=model,
            base_url=base_url,
            api_key=api_key,
            initial_messages=payload_messages,
            run_type="chat",
            timeout_seconds=timeout_seconds,
            tool_executor=_chat_tool_executor,
            emit=emit,
            cancel_event=cancel_event,
        )
        result["initial_message_count"] = len(payload_messages)
        result["replay_messages"] = self.extract_turn_replay_messages(
            result.get("messages"),
            initial_message_count=len(payload_messages),
        )
        return result

    def chat(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        system_prompt: str | None,
        prompt_templates: Any = None,
        messages: list[dict[str, str]],
        timeout_seconds: int = 60,
        tool_context: dict[str, Any] | None = None,
        emit: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        result = self.chat_with_details(
            model=model,
            base_url=base_url,
            api_key=api_key,
            system_prompt=system_prompt,
            prompt_templates=prompt_templates,
            messages=messages,
            timeout_seconds=timeout_seconds,
            tool_context=tool_context,
            emit=emit,
            cancel_event=cancel_event,
        )
        return result["final_answer"] or "模型本轮未返回可展示内容。"

    def generate_text(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        system_prompt: str | None,
        prompt_templates: Any = None,
        messages: list[dict[str, str]],
        timeout_seconds: int = 60,
        run_type: str | None = None,
        emit: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        payload_messages: list[dict[str, Any]] = []
        effective_system_prompt = self._augment_system_prompt(
            system_prompt,
            run_type=run_type,
            prompt_templates=prompt_templates,
        )
        if effective_system_prompt:
            payload_messages.append(
                {"role": "system", "content": effective_system_prompt}
            )
        payload_messages.extend(dict(message) for message in messages)

        payload = _apply_reasoning_options({
            "model": model,
            "temperature": _LLM_TEMPERATURE,
            "messages": payload_messages,
        })
        response_payload = self._call_llm_stream(
            base_url=base_url,
            api_key=api_key,
            payload=payload,
            timeout_seconds=timeout_seconds,
            emit=emit,
            cancel_event=cancel_event,
        )
        choices = response_payload.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        final_text = _to_text_content(message.get("content")) or "模型本轮未返回可展示内容。"
        return final_text, payload, response_payload

    def build_initial_request_payload(self, app_settings: Any) -> dict[str, Any]:
        run_type = str(getattr(app_settings, "run_type", "analysis") or "analysis")
        system_prompt = self._augment_system_prompt(
            app_settings.system_prompt,
            run_type=run_type,
            prompt_templates=getattr(app_settings, "prompt_templates", None),
        )
        return _apply_reasoning_options({
            "model": app_settings.llm_model,
            "temperature": _LLM_TEMPERATURE,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": getattr(app_settings, "task_prompt", "")},
            ],
            "tools": skill_stack_service.runtime.build_tools(run_type=run_type),
            "tool_choice": "auto",
        })

    def build_request_payload_from_messages(
        self,
        *,
        app_settings: Any,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        run_type = str(getattr(app_settings, "run_type", "analysis") or "analysis")
        system_prompt = self._augment_system_prompt(
            app_settings.system_prompt,
            run_type=run_type,
            prompt_templates=getattr(app_settings, "prompt_templates", None),
        )
        payload_messages: list[dict[str, Any]] = []
        if system_prompt:
            payload_messages.append({"role": "system", "content": system_prompt})
        payload_messages.extend(dict(message) for message in messages)
        return _apply_reasoning_options({
            "model": app_settings.llm_model,
            "temperature": _LLM_TEMPERATURE,
            "messages": payload_messages,
            "tools": skill_stack_service.runtime.build_tools(run_type=run_type),
            "tool_choice": "auto",
        })

    @staticmethod
    def _augment_system_prompt(
        base_prompt: str | None,
        *,
        run_type: str | None = None,
        prompt_templates: Any = None,
    ) -> str:
        supplement = skill_registry.build_prompt_supplement(run_type=run_type)
        prompt_parts = [
            str(base_prompt or "").strip(),
            str(supplement or "").strip(),
        ]
        if str(run_type or "").strip() == "chat":
            prompt_parts.append(
                resolve_prompt_template(
                    prompt_templates,
                    "chat_confirmation_append_prompt",
                )
            )
        return "\n\n".join(part for part in prompt_parts if part)

    def run_agent(
        self,
        app_settings: Any,
        client: MXClient,
        emit: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        return self.run_agent_with_messages(
            app_settings=app_settings,
            client=client,
            messages=[
                {
                    "role": "user",
                    "content": getattr(app_settings, "task_prompt", ""),
                }
            ],
            emit=emit,
        )

    def run_agent_with_messages(
        self,
        *,
        app_settings: Any,
        client: MXClient,
        messages: list[dict[str, Any]],
        emit: Any = None,
        tool_executor: Callable[[str, dict[str, Any], str | None], dict[str, Any]] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        request_payload = self.build_request_payload_from_messages(
            app_settings=app_settings,
            messages=messages,
        )
        if not app_settings.llm_base_url or not app_settings.llm_api_key:
            raise RuntimeError("未配置大模型接口，无法执行 AI 调度。")

        run_type = str(getattr(app_settings, "run_type", "analysis") or "analysis")

        def _run_tool_executor(
            tool_name: str,
            arguments: dict[str, Any],
            tool_call_id: str | None,
        ) -> dict[str, Any]:
            del tool_call_id
            return skill_stack_service.runtime.execute_tool(
                tool_name=tool_name,
                arguments=arguments,
                context={
                    "client": client,
                    "app_settings": app_settings,
                    "run_type": run_type,
                },
            )

        result = self._agent_loop(
            model=app_settings.llm_model,
            base_url=app_settings.llm_base_url,
            api_key=app_settings.llm_api_key,
            initial_messages=[dict(m) for m in request_payload["messages"]],
            run_type=run_type,
            timeout_seconds=getattr(app_settings, "timeout_seconds", 60),
            tool_executor=tool_executor or _run_tool_executor,
            prompt_templates=getattr(app_settings, "prompt_templates", None),
            emit=emit,
            cancel_event=cancel_event,
            context_window_tokens=getattr(
                app_settings, "automation_context_window_tokens", None
            ),
            tools_override=getattr(app_settings, "tools_override", None),
        )

        return (
            {
                "final_answer": result["final_answer"],
                "tool_calls": result["tool_history"],
            },
            request_payload,
            {
                "responses": result["responses"],
                "final_message": result["final_message"],
            },
            {
                "messages": result["messages"],
                "initial_message_count": len(request_payload["messages"]),
                "replay_messages": self.extract_turn_replay_messages(
                    result["messages"],
                    initial_message_count=len(request_payload["messages"]),
                ),
            },
        )

    def _agent_loop(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        initial_messages: list[dict[str, Any]],
        run_type: str,
        timeout_seconds: int,
        tool_executor: Callable[[str, dict[str, Any], str | None], dict[str, Any]],
        prompt_templates: Any = None,
        emit: Any = None,
        cancel_event: threading.Event | None = None,
        context_window_tokens: int | None = None,
        tools_override: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        _emit = emit if callable(emit) else (lambda *_a, **_kw: None)
        messages: list[dict[str, Any]] = [dict(m) for m in initial_messages]
        response_history: list[dict[str, Any]] = []
        tool_history: list[dict[str, Any]] = []
        empty_final_answer_retries = 0
        # 上下文溢出被动恢复（移植自原版 Aniu agent_loop 的
        # _generate_turn_with_context_recovery）：provider 报溢出后压缩
        # 历史并重试恰好一次。
        context_overflow_recovered = False
        effective_context_window = int(context_window_tokens or 0) or 32768
        # 工具白名单覆盖（如梦境智能体只暴露 4 个只读/写记忆工具）；
        # None 时使用技能栈按 run_type 构建的全量工具。
        effective_tools = (
            tools_override
            if isinstance(tools_override, list)
            else None
        )

        for iteration in range(_MAX_TOOL_ITERATIONS):
            _raise_if_cancelled(cancel_event)
            iteration_payload = _apply_reasoning_options({
                "model": model,
                "temperature": _LLM_TEMPERATURE,
                "messages": messages,
                "tools": effective_tools
                if effective_tools is not None
                else skill_stack_service.runtime.build_tools(run_type=run_type),
                "tool_choice": "auto",
            })
            _emit("llm_request", iteration=iteration + 1, model=model)
            try:
                response_payload = self._call_llm_stream(
                    base_url=base_url,
                    api_key=api_key,
                    payload=iteration_payload,
                    timeout_seconds=timeout_seconds,
                    emit=_emit,
                    cancel_event=cancel_event,
                )
            except LLMUpstreamError as exc:
                if (
                    exc.error_code == LLMErrorCode.CONTEXT_OVERFLOW
                    and not context_overflow_recovered
                    and len(messages) > 1
                ):
                    context_overflow_recovered = True
                    compacted, stats = compact_messages_for_overflow(
                        messages,
                        target_tokens=int(effective_context_window * 0.6),
                    )
                    messages = compacted
                    _emit(
                        "stage",
                        stage="llm",
                        message=(
                            "上下文超出模型窗口，已压缩历史"
                            f"（丢弃 {stats['dropped']} 条、压缩 {stats['compacted']} 条）后重试"
                        ),
                    )
                    logger.warning(
                        "context overflow recovered by compaction: "
                        "dropped=%s compacted=%s",
                        stats["dropped"],
                        stats["compacted"],
                    )
                    continue
                raise
            response_history.append(response_payload)

            choices = response_payload.get("choices") or []
            if not choices:
                raise RuntimeError("大模型未返回 choices。")

            message = choices[0].get("message") or {}
            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content") or "",
            }
            reasoning_content = _to_reasoning_content(
                message.get("reasoning_content")
            )
            if reasoning_content:
                assistant_entry["reasoning_content"] = reasoning_content
            if message.get("tool_calls"):
                assistant_entry["tool_calls"] = message["tool_calls"]
            messages.append(assistant_entry)

            assistant_text = _to_text_content(message.get("content"))
            tool_calls = message.get("tool_calls") or []
            if assistant_text and tool_calls:
                _emit("llm_message", iteration=iteration + 1, content=assistant_text)

            if not tool_calls:
                if (
                    not str(assistant_text or "").strip()
                    and tool_history
                    and empty_final_answer_retries < _EMPTY_FINAL_ANSWER_RETRY_LIMIT
                ):
                    empty_final_answer_retries += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": resolve_prompt_template(
                                prompt_templates,
                                "empty_final_answer_followup_prompt",
                            ),
                        }
                    )
                    _emit(
                        "stage",
                        stage="llm",
                        message="模型未输出最终结论，正在请求补充结论",
                    )
                    continue

                final_message = assistant_text or "模型本轮未返回可展示内容。"
                stream_meta = response_payload.get("stream_meta")
                final_streamed = (
                    isinstance(stream_meta, dict)
                    and bool(stream_meta.get("final_streamed"))
                )
                if not final_streamed:
                    self._emit_final_answer_stream(final_message, emit=_emit)
                return {
                    "final_answer": final_message,
                    "tool_history": tool_history,
                    "responses": response_history,
                    "final_message": message,
                    "messages": messages,
                }

            for tool_call in tool_calls:
                _raise_if_cancelled(cancel_event)
                if not isinstance(tool_call, dict):
                    continue
                function_payload = tool_call.get("function") or {}
                tool_name = str(function_payload.get("name") or "").strip()
                arguments_text = function_payload.get("arguments") or "{}"
                try:
                    arguments = json.loads(arguments_text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"工具参数不是合法 JSON: {exc}") from exc

                _emit(
                    "tool_call",
                    phase="llm",
                    tool_name=tool_name,
                    tool_call_id=tool_call.get("id"),
                    arguments=arguments,
                    status="running",
                )
                try:
                    tool_result = tool_executor(
                        tool_name,
                        arguments,
                        tool_call.get("id"),
                    )
                except TypeError:
                    tool_result = tool_executor(tool_name, arguments)  # type: ignore[misc, call-arg]
                _emit(
                    "tool_call",
                    phase="llm",
                    tool_name=tool_name,
                    tool_call_id=tool_call.get("id"),
                    arguments=arguments,
                    status="done",
                    ok=bool(tool_result.get("ok")),
                    summary=tool_result.get("summary"),
                )
                tool_history.append(
                    {
                        "id": tool_call.get("id"),
                        "name": tool_name,
                        "arguments": arguments,
                        "result": tool_result,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": _safe_json_dumps(_slim_tool_result(tool_result)),
                    }
                )

        raise RuntimeError("大模型工具调用轮次超限，已中止。")

    def _emit_final_answer_stream(self, content: str, *, emit: Callable[..., Any]) -> None:
        final_text = str(content or "").strip()
        emit("final_started", char_count=len(final_text))

        streamed = 0
        for chunk in _iter_text_chunks(final_text):
            streamed += len(chunk)
            emit(
                "final_delta",
                delta=chunk,
                streamed_chars=streamed,
            )

        emit("final_finished", content=final_text, char_count=len(final_text))

    def _call_llm_stream(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: int,
        emit: Callable[..., Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        stream_payload = dict(payload)
        stream_payload["stream"] = True

        last_error: LLMUpstreamError | None = None
        for attempt in range(_LLM_UPSTREAM_MAX_RETRIES):
            last_error = None
            for include_usage in (True, False):
                _raise_if_cancelled(cancel_event)
                attempt_payload = dict(stream_payload)
                if include_usage:
                    attempt_payload["stream_options"] = {"include_usage": True}
                try:
                    return self._consume_llm_stream(
                        base_url=base_url,
                        api_key=api_key,
                        payload=attempt_payload,
                        timeout_seconds=timeout_seconds,
                        emit=emit,
                        cancel_event=cancel_event,
                    )
                except LLMUpstreamError as exc:
                    last_error = exc
                    if include_usage and exc.status_code == 400:
                        continue
                    break

            if last_error is None:
                continue
            if _should_fallback_to_non_stream(last_error):
                if callable(emit):
                    emit(
                        "stage",
                        stage="llm",
                        message="流式请求被上游拒绝，正在切换为非流式请求重试",
                    )
                logger.warning(
                    "LLM stream request rejected with 400, falling back to non-stream request: %s",
                    last_error,
                )
                fallback_payload = self._call_llm(
                    base_url=base_url,
                    api_key=api_key,
                    payload=dict(payload),
                    timeout_seconds=timeout_seconds,
                )
                if isinstance(fallback_payload, dict):
                    stream_meta = fallback_payload.get("stream_meta")
                    if not isinstance(stream_meta, dict):
                        stream_meta = {}
                    stream_meta.setdefault("fallback_mode", "non_stream")
                    stream_meta.setdefault("final_streamed", False)
                    fallback_payload["stream_meta"] = stream_meta
                return fallback_payload
            if (
                not _is_retryable_upstream_error(last_error)
                or attempt >= _LLM_UPSTREAM_MAX_RETRIES - 1
            ):
                raise last_error

            if callable(emit):
                emit(
                    "stage",
                    stage="llm",
                    message=(
                        "大模型上游暂时不可用，正在重试 "
                        f"({attempt + 1}/{_LLM_UPSTREAM_MAX_RETRIES})"
                    ),
                )
            logger.warning(
                "LLM upstream temporary failure, retrying (%s/%s): %s",
                attempt + 1,
                _LLM_UPSTREAM_MAX_RETRIES,
                last_error,
            )
            # 上游通过 Retry-After 指明等待时间时优先遵循（上限 30 秒，
            # 防止异常大的值把运行卡死）。
            retry_delay = _LLM_UPSTREAM_RETRY_DELAY_SECONDS
            if last_error.retry_after is not None:
                retry_delay = min(float(last_error.retry_after), 30.0)
            _sleep_before_retry(
                retry_delay,
                cancel_event=cancel_event,
            )

        if last_error is not None:
            raise last_error
        raise RuntimeError("大模型流式请求失败。")

    def _consume_llm_stream(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: int,
        emit: Callable[..., Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        _emit = emit if callable(emit) else (lambda *_a, **_kw: None)
        _raise_if_cancelled(cancel_event)

        url = base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            with self._create_http_client(timeout_seconds) as http_client:
                with http_client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.is_error:
                        _raise_upstream_http_error(response, response.read())
                    return self._parse_llm_stream_response(
                        lines=response.iter_lines(),
                        emit=_emit,
                        cancel_event=cancel_event,
                    )
        except LLMUpstreamError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMUpstreamError(
                f"大模型接口请求超时 ({timeout_seconds}s)，请检查网络或增加超时时间。",
                error_code=LLMErrorCode.TIMEOUT,
            ) from exc
        except httpx.RequestError as exc:
            raise LLMUpstreamError(
                f"大模型接口请求失败: {exc}",
                error_code=LLMErrorCode.NETWORK,
            ) from exc

    def _parse_llm_stream_response(
        self,
        *,
        lines: Iterable[str],
        emit: Callable[..., Any],
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        data_lines: list[str] = []
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        response_id: str | None = None
        response_model: str | None = None
        response_created: Any = None
        response_object: str | None = None
        chunk_count = 0
        stream_mode: str | None = None
        final_started = False

        def _flush_payload(raw_payload: str) -> None:
            nonlocal usage
            nonlocal finish_reason
            nonlocal response_id
            nonlocal response_model
            nonlocal response_created
            nonlocal response_object
            nonlocal chunk_count
            nonlocal stream_mode
            nonlocal final_started

            if not raw_payload:
                return
            if raw_payload == "[DONE]":
                return

            chunk = json.loads(raw_payload)
            chunk_count += 1

            if "error" in chunk:
                raise LLMUpstreamError(
                    _format_error_message(
                        "大模型流式响应错误",
                        _extract_error_detail(chunk),
                    )
                )

            if any(key in chunk for key in ("message", "detail")) and not chunk.get(
                "choices"
            ):
                raise LLMUpstreamError(
                    _format_error_message(
                        "大模型流式响应错误",
                        _extract_error_detail(chunk),
                    )
                )

            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]

            if response_id is None and isinstance(chunk.get("id"), str):
                response_id = chunk["id"]
            if response_model is None and isinstance(chunk.get("model"), str):
                response_model = chunk["model"]
            if response_object is None and isinstance(chunk.get("object"), str):
                response_object = chunk["object"]
            if response_created is None and chunk.get("created") is not None:
                response_created = chunk.get("created")

            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                return

            choice = choices[0] if isinstance(choices[0], dict) else {}
            finish_value = choice.get("finish_reason")
            if isinstance(finish_value, str) and finish_value:
                finish_reason = finish_value

            delta = choice.get("delta")
            if not isinstance(delta, dict):
                return

            delta_reasoning = _to_reasoning_content(delta.get("reasoning_content"))
            if delta_reasoning:
                reasoning_parts.append(delta_reasoning)

            delta_tool_calls = delta.get("tool_calls")
            if isinstance(delta_tool_calls, list) and delta_tool_calls:
                if stream_mode is None:
                    stream_mode = "tool"
                for item in delta_tool_calls:
                    if isinstance(item, dict):
                        _merge_stream_tool_call(tool_calls, item)

            delta_text = _to_stream_text_content(delta.get("content"))
            if delta_text:
                content_parts.append(delta_text)
                if stream_mode is None:
                    stream_mode = "final"
                if stream_mode == "final":
                    if not final_started:
                        emit("final_started")
                        final_started = True
                    emit("final_delta", delta=delta_text)

        for raw_line in lines:
            _raise_if_cancelled(cancel_event)
            line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8")
            line = line.rstrip("\r\n")
            if not line:
                payload = "\n".join(data_lines)
                data_lines.clear()
                _flush_payload(payload)
                if payload == "[DONE]":
                    break
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        if data_lines:
            _flush_payload("\n".join(data_lines))

        final_text = "".join(content_parts)
        if stream_mode != "tool":
            if not final_started:
                emit("final_started", char_count=len(final_text))
                final_started = True
            emit("final_finished", content=final_text, char_count=len(final_text))

        message: dict[str, Any] = {
            "role": "assistant",
            "content": final_text,
        }
        reasoning_content = "".join(reasoning_parts)
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        ordered_tool_calls = [tool_calls[idx] for idx in sorted(tool_calls)]
        if ordered_tool_calls:
            message["tool_calls"] = ordered_tool_calls

        response_payload: dict[str, Any] = {
            "id": response_id,
            "object": response_object or "chat.completion",
            "created": response_created,
            "model": response_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "stream_meta": {
                "chunk_count": chunk_count,
                "final_streamed": stream_mode != "tool",
            },
        }
        if usage is not None:
            response_payload["usage"] = usage
        return response_payload

    def _call_llm(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        url = base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        last_error: LLMUpstreamError | None = None
        for attempt in range(_LLM_UPSTREAM_MAX_RETRIES):
            try:
                with self._create_http_client(timeout_seconds) as http_client:
                    response = http_client.post(url, headers=headers, json=payload)
                    if response.is_error:
                        _raise_upstream_http_error(response, response.content)
                    return response.json()
            except LLMUpstreamError as exc:
                last_error = exc
                if (
                    not _is_retryable_upstream_error(exc)
                    or attempt >= _LLM_UPSTREAM_MAX_RETRIES - 1
                ):
                    raise
                logger.warning(
                    "LLM upstream temporary failure, retrying (%s/%s): %s",
                    attempt + 1,
                    _LLM_UPSTREAM_MAX_RETRIES,
                    exc,
                )
                time.sleep(_LLM_UPSTREAM_RETRY_DELAY_SECONDS)
            except httpx.TimeoutException as exc:
                last_error = LLMUpstreamError(
                    f"大模型接口请求超时 ({timeout_seconds}s)，请检查网络或增加超时时间。",
                    error_code=LLMErrorCode.TIMEOUT,
                )
                if attempt >= _LLM_UPSTREAM_MAX_RETRIES - 1:
                    raise last_error from exc
                logger.warning(
                    "LLM upstream timeout, retrying (%s/%s): %s",
                    attempt + 1,
                    _LLM_UPSTREAM_MAX_RETRIES,
                    exc,
                )
                time.sleep(_LLM_UPSTREAM_RETRY_DELAY_SECONDS)
            except httpx.RequestError as exc:
                last_error = LLMUpstreamError(
                    f"大模型接口请求失败: {exc}",
                    error_code=LLMErrorCode.NETWORK,
                )
                if attempt >= _LLM_UPSTREAM_MAX_RETRIES - 1:
                    raise last_error from exc
                logger.warning(
                    "LLM request transport failure, retrying (%s/%s): %s",
                    attempt + 1,
                    _LLM_UPSTREAM_MAX_RETRIES,
                    exc,
                )
                time.sleep(_LLM_UPSTREAM_RETRY_DELAY_SECONDS)

        if last_error is not None:
            raise last_error
        raise RuntimeError("大模型请求失败。")


llm_service = LLMService()
