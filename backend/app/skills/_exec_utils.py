"""Shared subprocess and truncation helpers for skill runtimes."""
from __future__ import annotations

MAX_EXEC_TIMEOUT = 60
MAX_READ_CHARS = 128_000


def truncate_text(text: str, *, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n...(truncated)", True


def safe_timeout(
    value,
    *,
    default: int = MAX_EXEC_TIMEOUT,
    maximum: int = MAX_EXEC_TIMEOUT,
) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = default
    return max(1, min(timeout, maximum))
