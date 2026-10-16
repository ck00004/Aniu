"""工具调用日志服务（移植自原版 Aniu business/stock_api_logs 的思路）。

记录每次上游（妙想）接口调用，让调用可观测、可排障。日志写入失败
绝不影响交易主路径。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import func as sa_func, select

from app.db.database import session_scope
from app.db.models import ToolInvocationLog

logger = logging.getLogger(__name__)

_MAX_REQUEST_SUMMARY_CHARS = 2000
_MAX_ERROR_CHARS = 500


def _trim(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 3] + "..."
    return value


def record_invocation(
    *,
    operation: str,
    request_payload: dict[str, Any] | None = None,
    status: str = "success",
    status_code: int | None = None,
    duration_ms: int | None = None,
    response_size: int | None = None,
    error_summary: str | None = None,
    provider: str = "mx",
) -> None:
    """记录一次上游调用。任何异常只记日志，不影响调用方。"""
    try:
        with session_scope() as db:
            db.add(
                ToolInvocationLog(
                    provider=provider,
                    operation=str(operation or "unknown")[:128],
                    request_summary=_trim(request_payload, _MAX_REQUEST_SUMMARY_CHARS)
                    if isinstance(request_payload, dict)
                    else None,
                    status=str(status or "success")[:16],
                    status_code=status_code,
                    duration_ms=duration_ms,
                    response_size=response_size,
                    error_summary=_trim(
                        str(error_summary or "") or None, _MAX_ERROR_CHARS
                    ),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool invocation log write failed: %s", exc)


class ToolInvocationLogService:
    def list_logs(
        self,
        db: Any,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(int(limit or 50), 200))
        normalized_offset = max(0, int(offset or 0))
        normalized_status = str(status or "").strip().lower() or None

        stmt = select(ToolInvocationLog)
        count_stmt = select(sa_func.count(ToolInvocationLog.id))
        if normalized_status:
            stmt = stmt.where(ToolInvocationLog.status == normalized_status)
            count_stmt = count_stmt.where(ToolInvocationLog.status == normalized_status)

        total = int(db.scalar(count_stmt) or 0)
        rows = list(
            db.scalars(
                stmt.order_by(ToolInvocationLog.id.desc())
                .limit(normalized_limit)
                .offset(normalized_offset)
            ).all()
        )

        return {
            "total": total,
            "items": [
                {
                    "id": row.id,
                    "provider": row.provider,
                    "operation": row.operation,
                    "request_summary": row.request_summary,
                    "status": row.status,
                    "status_code": row.status_code,
                    "duration_ms": row.duration_ms,
                    "response_size": row.response_size,
                    "error_summary": row.error_summary,
                    "created_at": row.created_at.isoformat()
                    if row.created_at is not None
                    else None,
                }
                for row in rows
            ],
        }

    def summarize(self, db: Any) -> dict[str, Any]:
        total = int(db.scalar(select(sa_func.count(ToolInvocationLog.id))) or 0)
        failed = int(
            db.scalar(
                select(sa_func.count(ToolInvocationLog.id)).where(
                    ToolInvocationLog.status != "success"
                )
            )
            or 0
        )
        avg_duration = db.scalar(
            select(sa_func.avg(ToolInvocationLog.duration_ms))
        )
        return {
            "total": total,
            "failed": failed,
            "avg_duration_ms": round(float(avg_duration), 1)
            if avg_duration is not None
            else None,
        }


tool_invocation_log_service = ToolInvocationLogService()


class instrument_invocation:
    """上下文管理器：为一次上游调用计时并落日志。"""

    def __init__(self, operation: str, request_payload: dict[str, Any] | None) -> None:
        self._operation = operation
        self._request_payload = request_payload
        self._started_at = time.monotonic()
        self._status_code: int | None = None
        self._response_size: int | None = None

    def __enter__(self) -> "instrument_invocation":
        return self

    def observe(
        self,
        *,
        status_code: int | None = None,
        response_size: int | None = None,
    ) -> None:
        self._status_code = status_code
        self._response_size = response_size

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration_ms = int((time.monotonic() - self._started_at) * 1000)
        if exc is None:
            record_invocation(
                operation=self._operation,
                request_payload=self._request_payload,
                status="success",
                status_code=self._status_code,
                duration_ms=duration_ms,
                response_size=self._response_size,
            )
        else:
            record_invocation(
                operation=self._operation,
                request_payload=self._request_payload,
                status="failed",
                status_code=self._status_code,
                duration_ms=duration_ms,
                response_size=self._response_size,
                error_summary=str(exc),
            )
        return False
