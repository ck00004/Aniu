from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from app.db.database import session_scope
from app.db.models import StrategyRunAction, StrategyRunActionResult
from app.services.mx_skill_service import mx_skill_service

logger = logging.getLogger(__name__)


DEFAULT_ACTION_MAX_ATTEMPTS = 2
# 只有这些状态的动作允许进入执行：已 completed/terminal 的动作再次执行
# 会重放真实交易（重复委托），必须拒绝。
_EXECUTABLE_ACTION_STATUSES = ("planned", "failed_retryable")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ExecutionRunSummary:
    executed_actions: list[dict[str, Any]]
    execution_tool_calls: list[dict[str, Any]]
    action_status_counts: dict[str, int]
    unresolved_count: int
    error_message: str | None


class ExecutionRunnerService:
    def replace_plan(
        self,
        *,
        run_id: int,
        planned_actions: list[dict[str, Any]],
    ) -> None:
        with session_scope() as db:
            existing = db.scalars(
                select(StrategyRunAction).where(StrategyRunAction.run_id == run_id)
            ).all()
            for action in existing:
                # 已发送（executing）或已出结果（completed/failed*）的动作
                # 是审计记录，不得被新计划覆盖删除（2026-10 外部审计补丁 D：
                # 删除后无法追溯真实发送过什么委托）。只允许替换仍处于
                # planned 状态、尚未发送的动作。
                if str(action.status or "planned") != "planned":
                    logger.warning(
                        "replace_plan kept non-planned action %s (status=%s) "
                        "for run %s: sent actions are immutable audit records",
                        action.id,
                        action.status,
                        run_id,
                    )
                    continue
                db.delete(action)

            for item in planned_actions:
                db.add(
                    StrategyRunAction(
                        run_id=run_id,
                        sequence_no=int(item.get("sequence_no") or 0),
                        phase="planned",
                        tool_name=str(item.get("tool_name") or ""),
                        action_type=str(item.get("action_type") or "UNKNOWN"),
                        status="planned",
                        tool_call_id=str(item.get("tool_call_id") or "") or None,
                        arguments_payload=item.get("arguments") if isinstance(item.get("arguments"), dict) else None,
                        planned_action_payload=item.get("planned_action") if isinstance(item.get("planned_action"), dict) else None,
                        result_summary=str(item.get("result_summary") or "").strip() or None,
                    )
                )

    def execute_plan(
        self,
        *,
        run_id: int,
        client: Any,
        app_settings: Any,
        emit: Any = None,
        max_attempts: int = DEFAULT_ACTION_MAX_ATTEMPTS,
        cancel_event: Any = None,
    ) -> ExecutionRunSummary:
        _emit = emit if callable(emit) else (lambda *_a, **_kw: None)
        action_ids = self._list_action_ids(run_id)
        executed_actions: list[dict[str, Any]] = []
        execution_tool_calls: list[dict[str, Any]] = []

        for action_id in action_ids:
            # 每个动作执行前检查中止：中止请求优先于后续真实交易。
            if cancel_event is not None and cancel_event.is_set():
                break
            action_snapshot = self._get_action(action_id)
            if action_snapshot is None:
                continue
            effective_max_attempts = self._resolve_action_max_attempts(
                action_snapshot,
                requested_max_attempts=max_attempts,
            )
            attempt = 0
            while attempt < effective_max_attempts:
                attempt += 1
                action = self._mark_action_executing(action_id)
                if action is None:
                    break

                arguments = (
                    dict(action.arguments_payload)
                    if isinstance(action.arguments_payload, dict)
                    else {}
                )
                _emit(
                    "tool_call",
                    phase="execution",
                    tool_name=action.tool_name,
                    tool_call_id=action.tool_call_id,
                    arguments=arguments,
                    status="running",
                    attempt=attempt,
                )
                result = mx_skill_service.execute_tool(
                    client=client,
                    app_settings=app_settings,
                    tool_name=action.tool_name,
                    arguments=arguments,
                )
                _emit(
                    "tool_call",
                    phase="execution",
                    tool_name=action.tool_name,
                    tool_call_id=action.tool_call_id,
                    arguments=arguments,
                    status="done",
                    ok=bool(result.get("ok")),
                    summary=result.get("summary"),
                    attempt=attempt,
                )
                execution_tool_calls.append(
                    {
                        "id": action.tool_call_id or f"exec-{action.id}-{attempt}",
                        "name": action.tool_name,
                        "arguments": arguments,
                        "result": result,
                    }
                )

                success, normalized_action = self._record_attempt(
                    action_id=action.id,
                    attempt_no=attempt,
                    result=result,
                    max_attempts=effective_max_attempts,
                )
                if success:
                    if normalized_action is not None:
                        executed_actions.append(normalized_action)
                    break

        status_counts, unresolved_count, error_message = self._summarize_actions(run_id)
        return ExecutionRunSummary(
            executed_actions=executed_actions,
            execution_tool_calls=execution_tool_calls,
            action_status_counts=status_counts,
            unresolved_count=unresolved_count,
            error_message=error_message,
        )

    def _list_action_ids(self, run_id: int) -> list[int]:
        with session_scope() as db:
            return list(
                db.scalars(
                    select(StrategyRunAction.id)
                    .where(
                        StrategyRunAction.run_id == run_id,
                        StrategyRunAction.status.in_(_EXECUTABLE_ACTION_STATUSES),
                    )
                    .order_by(StrategyRunAction.sequence_no.asc(), StrategyRunAction.id.asc())
                ).all()
            )

    def _get_action(self, action_id: int) -> StrategyRunAction | None:
        with session_scope() as db:
            action = db.get(StrategyRunAction, action_id)
            if action is None:
                return None
            db.expunge(action)
            return action

    def _resolve_action_max_attempts(
        self,
        action: StrategyRunAction,
        *,
        requested_max_attempts: int,
    ) -> int:
        # 写操作一律只允许一次自动尝试：撤单/自选股变更与买卖一样，
        # 重试会在状态不明时重复发送写请求（2026-10 外部审计补丁 F）。
        # 读取状态可以重试，写操作超时后必须先核对远端状态。
        action_type = str(action.action_type or "").upper()
        if action_type in {"BUY", "SELL", "CANCEL", "MANAGE_SELF_SELECT"}:
            return 1
        normalized_attempts = max(1, int(requested_max_attempts or 1))
        return normalized_attempts

    def _mark_action_executing(self, action_id: int) -> StrategyRunAction | None:
        with session_scope() as db:
            # 原子抢占：条件更新 + 受影响行数校验，防止两个执行者
            # 同时把同一动作置为 executing（读-判-写在并发下存在窗口）。
            claimed = db.execute(
                update(StrategyRunAction)
                .where(
                    StrategyRunAction.id == action_id,
                    StrategyRunAction.status.in_(_EXECUTABLE_ACTION_STATUSES),
                )
                .values(status="executing")
            )
            if claimed.rowcount != 1:
                return None
            action = db.get(StrategyRunAction, action_id)
            if action is None:
                return None
            db.expunge(action)
            return action

    def _record_attempt(
        self,
        *,
        action_id: int,
        attempt_no: int,
        result: dict[str, Any],
        max_attempts: int,
    ) -> tuple[bool, dict[str, Any] | None]:
        finished_at = _now_utc()
        ok = bool(result.get("ok"))
        executed_action = result.get("executed_action")
        success = ok and isinstance(executed_action, dict)
        normalized_action = self._normalize_executed_action(result) if success else None
        error_message = None if success else str(result.get("error") or "工具未返回可执行结果。")

        with session_scope() as db:
            action = db.get(StrategyRunAction, action_id)
            if action is None:
                return False, normalized_action

            db.add(
                StrategyRunActionResult(
                    action_id=action_id,
                    attempt_no=attempt_no,
                    status="success" if success else "failed",
                    response_payload=result,
                    error_message=error_message,
                    finished_at=finished_at,
                )
            )
            action.phase = "executed"
            action.updated_at = finished_at
            action.result_summary = str(result.get("summary") or action.result_summary or "").strip() or None
            if success:
                action.status = "completed"
                action.executed_action_payload = executed_action
                action.error_message = None
                action.executed_at = finished_at
            else:
                action.status = "failed_retryable" if attempt_no < max_attempts else "failed_terminal"
                action.error_message = error_message
            db.add(action)

        return success, normalized_action

    def _normalize_executed_action(self, result: dict[str, Any]) -> dict[str, Any] | None:
        executed_action = result.get("executed_action")
        if not isinstance(executed_action, dict):
            return None
        action_name = str(executed_action.get("action") or "").upper()
        entry = {
            "symbol": str(
                executed_action.get("symbol")
                or executed_action.get("stock_code")
                or ""
            ).strip(),
            "name": str(executed_action.get("name") or "").strip() or None,
            "action": action_name,
            "quantity": int(executed_action.get("quantity") or 0),
            "price_type": str(executed_action.get("price_type") or "MARKET"),
            "price": executed_action.get("price"),
            "reason": str(executed_action.get("reason") or "").strip(),
            "status": "submitted",
            "response": result.get("result"),
        }
        if action_name == "CANCEL":
            entry["price_type"] = "CANCEL"
            entry["status"] = "cancel_requested"
        if action_name == "MANAGE_SELF_SELECT":
            entry["price_type"] = "SELF_SELECT"
            entry["status"] = "completed"
            entry["query"] = str(executed_action.get("query") or "")
            entry["symbol"] = str(executed_action.get("query") or "")
        return entry

    def _summarize_actions(self, run_id: int) -> tuple[dict[str, int], int, str | None]:
        with session_scope() as db:
            actions = db.scalars(
                select(StrategyRunAction)
                .where(StrategyRunAction.run_id == run_id)
                .order_by(StrategyRunAction.sequence_no.asc(), StrategyRunAction.id.asc())
            ).all()

        counts: dict[str, int] = {}
        unresolved_messages: list[str] = []
        unresolved_count = 0
        for action in actions:
            status = str(action.status or "planned")
            counts[status] = counts.get(status, 0) + 1
            if status != "completed":
                unresolved_count += 1
                unresolved_messages.append(
                    f"{action.tool_name}#{action.sequence_no}: {action.error_message or status}"
                )

        return (
            counts,
            unresolved_count,
            "；".join(unresolved_messages[:5]) if unresolved_messages else None,
        )


execution_runner_service = ExecutionRunnerService()