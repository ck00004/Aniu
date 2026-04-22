from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Any

from fastapi import Request, status
from starlette.responses import JSONResponse

from app.core.config import get_settings


class _RateBucket:
    __slots__ = ("timestamps",)

    def __init__(self) -> None:
        self.timestamps: list[float] = []

    def hit(self, now: float, window: float, limit: int) -> bool:
        cutoff = now - window
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        if len(self.timestamps) >= limit:
            return False
        self.timestamps.append(now)
        return True


class RateLimiter:
    """Simple in-memory sliding-window rate limiter keyed by client IP + path."""

    def __init__(self) -> None:
        self._buckets: dict[str, _RateBucket] = defaultdict(_RateBucket)
        self._lock = Lock()
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = 300.0  # cleanup stale entries every 5 min

    def check(self, key: str, window: float, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup(now)
                self._last_cleanup = now
            return self._buckets[key].hit(now, window, limit)

    def _cleanup(self, now: float) -> None:
        stale_keys = [
            k
            for k, v in self._buckets.items()
            if not v.timestamps or v.timestamps[-1] < now - 600
        ]
        for k in stale_keys:
            del self._buckets[k]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._last_cleanup = time.monotonic()


_limiter = RateLimiter()

# Route-family rate limit rules:
# (bucket_name, exact_paths, path_prefixes, window_seconds, max_requests)
_ROUTE_LIMITS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], float, int], ...] = (
    ("login", ("/api/aniu/login",), (), 60.0, 10),
    ("run", ("/api/aniu/run", "/api/aniu/run-stream"), (), 60.0, 5),
    (
        "chat",
        ("/api/aniu/chat", "/api/aniu/chat-stream", "/api/aniu/chat/stream"),
        (),
        60.0,
        20,
    ),
    ("chat_uploads", (), ("/api/aniu/chat/uploads",), 60.0, 30),
    (
        "skill_imports",
        (
            "/api/aniu/skills/import-clawhub",
            "/api/aniu/skills/import-skillhub",
            "/api/aniu/skills/import-zip",
        ),
        (),
        60.0,
        10,
    ),
)


def _match_route_limit(path: str) -> tuple[str, float, int] | None:
    for bucket_name, exact_paths, path_prefixes, window, limit in _ROUTE_LIMITS:
        if path in exact_paths:
            return bucket_name, window, limit
        if any(path.startswith(prefix) for prefix in path_prefixes):
            return bucket_name, window, limit
    return None


def get_client_ip(request: Request) -> str:
    settings = get_settings()
    forwarded = request.headers.get("x-forwarded-for")
    if settings.trust_x_forwarded_for and forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_middleware(request: Request, call_next: Any) -> Any:
    path = request.url.path
    rule = _match_route_limit(path)
    if rule is not None:
        bucket_name, window, limit = rule
        client_ip = get_client_ip(request)
        key = f"{client_ip}:{bucket_name}"
        if not _limiter.check(key, window, limit):
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": "请求过于频繁，请稍后再试。",
                },
            )
    return await call_next(request)
