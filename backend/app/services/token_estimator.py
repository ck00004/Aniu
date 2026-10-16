from __future__ import annotations

import json
from typing import Any


def _normalize_content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _count_tokens(text: str) -> int:
    """按字符类别估算 token：ASCII 约 4 字符/token，非 ASCII（中文等）
    主流 tokenizer 下约 1 字符/token 全额计入。

    移植自原版 Aniu `llm/context.py:32-39`。旧的 (len+3)//4 对中文
    系统性低估 2.5-4 倍，导致压缩触发时机偏晚。
    """
    ascii_chars = 0
    non_ascii_chars = 0
    for character in text:
        if character.isascii():
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return (ascii_chars + 3) // 4 + non_ascii_chars


def estimate_text_tokens(text: str | None) -> int:
    content = str(text or "")
    if not content:
        return 0
    return max(1, _count_tokens(content))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    combined_parts: list[str] = []
    structural_overhead = 0

    for message in messages:
        if not isinstance(message, dict):
            continue
        combined_parts.append(_normalize_content_text(message.get("content")))
        combined_parts.append(_normalize_content_text(message.get("tool_calls")))
        combined_parts.append(str(message.get("role") or ""))
        structural_overhead += 16

    return max(0, _count_tokens("".join(combined_parts)) + structural_overhead // 4)
