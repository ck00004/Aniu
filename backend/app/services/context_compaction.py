"""确定性上下文压缩（移植自原版 Aniu kernel/context_compaction.py 的思路）。

provider 返回上下文溢出后的被动恢复：保留 system 消息与最近对话后缀，
将较早的工具结果替换为占位符、较早的文本截断，仍超预算则从最旧开始
丢弃。纯确定性算法（不依赖额外的摘要 LLM 调用），保证重试必然比
上次请求更小。
"""

from __future__ import annotations

import json
from typing import Any

from app.services.token_estimator import estimate_messages_tokens

# 压缩后保留的最近消息预算占目标的比例
_RECENT_SUFFIX_RATIO = 0.6
# 较早消息的文本截断上限
_LEGACY_TEXT_LIMIT = 512
# 单条消息的硬截断上限（防止异常巨大的单条消息撑爆预算）
_HARD_MESSAGE_LIMIT = 8000


def _message_size(message: dict[str, Any]) -> int:
    try:
        return len(json.dumps(message, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(message))


def _truncate_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[: limit - 20].rstrip() + "\n…[已截断]"


def _compact_single_message(message: dict[str, Any]) -> dict[str, Any]:
    """压缩单条历史消息：工具结果→占位符，长文本→截断。"""
    compacted = dict(message)
    role = str(message.get("role") or "")

    if role == "tool":
        original = message.get("content")
        size = len(str(original or ""))
        compacted["content"] = f"[工具结果已压缩：原 {size} 字符]"
        return compacted

    content = message.get("content")
    if isinstance(content, str):
        compacted["content"] = _truncate_text(content, _LEGACY_TEXT_LIMIT)
    elif isinstance(content, list):
        compacted["content"] = [
            _truncate_text(part.get("text"), _LEGACY_TEXT_LIMIT)
            if isinstance(part, dict) and isinstance(part.get("text"), str)
            else part
            for part in content
        ]

    if isinstance(message.get("tool_calls"), list):
        compacted["tool_calls"] = [
            {
                **call,
                "function": {
                    **(call.get("function") or {}),
                    "arguments": _truncate_text(
                        (call.get("function") or {}).get("arguments"),
                        _LEGACY_TEXT_LIMIT,
                    ),
                },
            }
            if isinstance(call, dict)
            else call
            for call in message["tool_calls"]
        ]
    return compacted


def _hard_trim(message: dict[str, Any]) -> dict[str, Any]:
    """单条消息硬截断（防异常大消息）。"""
    trimmed = dict(message)
    content = trimmed.get("content")
    if isinstance(content, str) and len(content) > _HARD_MESSAGE_LIMIT:
        trimmed["content"] = content[:_HARD_MESSAGE_LIMIT] + "…[已截断]"
    return trimmed


def compact_messages_for_overflow(
    messages: list[dict[str, Any]],
    *,
    target_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """把消息列表压缩到 target_tokens 以内。

    返回 (压缩后消息, 统计)。策略（确定性，从旧到新逐级加压）：
    1. system 消息始终保留（不压缩、不丢弃）。
    2. 保留最近的消息后缀（按 target 的 60% 预算，从末尾向前按整条累计）。
    3. 后缀之外的消息：压缩（工具结果→占位符、文本→512 截断）。
    4. 仍超预算：从最旧的非 system 消息开始丢弃，直到达标或只剩后缀。
    """
    if not messages:
        return list(messages), {"dropped": 0, "compacted": 0}

    target = max(1024, int(target_tokens or 1024))
    current = estimate_messages_tokens(messages)
    if current <= target:
        return list(messages), {"dropped": 0, "compacted": 0}

    # 1. 划分 system 与非 system
    system_messages = [m for m in messages if str(m.get("role") or "") == "system"]
    body = [m for m in messages if str(m.get("role") or "") != "system"]

    # 2. 从末尾向前保留最近后缀（预算 = target * 0.6）
    suffix_budget = int(target * _RECENT_SUFFIX_RATIO)
    suffix: list[dict[str, Any]] = []
    suffix_tokens = 0
    for message in reversed(body):
        message_tokens = estimate_messages_tokens([message])
        if suffix and suffix_tokens + message_tokens > suffix_budget:
            break
        suffix.insert(0, message)
        suffix_tokens += message_tokens

    prefix = body[: len(body) - len(suffix)]

    # 3. 压缩前缀
    compacted_prefix = [_compact_single_message(m) for m in prefix]
    compacted_count = len(compacted_prefix)

    # 4. 仍超预算则从最旧开始丢弃
    dropped = 0
    while (
        estimate_messages_tokens(system_messages + compacted_prefix + suffix) > target
        and compacted_prefix
    ):
        compacted_prefix.pop(0)
        dropped += 1

    result = system_messages + compacted_prefix + suffix
    # 最后兜底：单条异常大消息硬截断
    result = [_hard_trim(m) for m in result]
    return result, {"dropped": dropped, "compacted": compacted_count}


def trim_messages_to_budget(
    messages: list[dict[str, Any]],
    *,
    budget_tokens: int,
    keep_last: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    """按 token 预算从最旧开始裁剪消息（保留最后 keep_last 条）。

    用于聊天历史构建：固定条数上限之外的 token 预算防线。
    """
    budget = max(1024, int(budget_tokens or 1024))
    if estimate_messages_tokens(messages) <= budget:
        return list(messages), 0

    working = list(messages)
    dropped = 0
    while (
        len(working) > keep_last
        and estimate_messages_tokens(working) > budget
    ):
        working.pop(0)
        dropped += 1
    return working, dropped
