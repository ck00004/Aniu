from __future__ import annotations

from typing import Any


class ExecutionReconcileService:
    def summarize(
        self,
        *,
        planned_actions: list[dict[str, Any]],
        executed_actions: list[dict[str, Any]],
        unresolved_count: int,
        action_status_counts: dict[str, int] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        total_planned = len(planned_actions)
        total_executed = len(executed_actions)
        status_counts = dict(action_status_counts or {})
        return {
            "total_planned": total_planned,
            "total_executed": total_executed,
            "fully_executed": total_planned == total_executed and unresolved_count == 0,
            "unresolved_count": int(unresolved_count or 0),
            "status_counts": status_counts,
            "error_message": str(error_message or "").strip() or None,
        }

    def build_run_error_message(self, summary: dict[str, Any] | None) -> str | None:
        payload = summary if isinstance(summary, dict) else {}
        if bool(payload.get("fully_executed")):
            return None
        unresolved_count = int(payload.get("unresolved_count") or 0)
        error_message = str(payload.get("error_message") or "").strip()
        if unresolved_count <= 0 and not error_message:
            return None
        if error_message:
            return f"执行计划未完全落地：{error_message}"
        return f"执行计划未完全落地，仍有 {unresolved_count} 条动作未完成。"


execution_reconcile_service = ExecutionReconcileService()