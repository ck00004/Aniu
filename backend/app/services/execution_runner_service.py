from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from app.db.database import session_scope
import json

from app.db.models import StrategyRun, StrategyRunAction, StrategyRunActionResult, TradeOrder
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
                    run_id=run_id,
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
                    # P1（2026-10 复审）：回填意图的 action_id，补足
                    # 交易 → 模型决策 → 计划动作的追溯链。
                    try:
                        self._backfill_intent_action_id(
                            run_id=run_id,
                            action_id=action.id,
                            executed_action=normalized_action,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "intent action_id backfill failed: %s", exc
                        )
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

    def retry_action(self, action_id: int) -> dict[str, Any]:
        """手动重试失败的动作（用户功能：工具执行失败后可手动再触发）。

        重试走全部安全挡板（意图注册、预检、开市门禁、自选股回查）——
        失败原因未消除时会再次被拦截（正确行为）；瞬时错误（超时/接口
        异常）重试可能成功。每次重试记录为新的尝试（attempt），成功后
        持久化交易委托、回填意图关联并重新汇总运行状态。
        """
        # ---- 1. 校验 + 原子抢占 ----
        with session_scope() as db:
            action = db.get(StrategyRunAction, action_id)
            if action is None:
                raise RuntimeError(f"动作 {action_id} 不存在")
            if str(action.status or "") not in ("failed_terminal", "failed_retryable"):
                raise RuntimeError(
                    f"动作当前状态为 {action.status}，仅失败动作允许手动重试"
                )
            run_id = action.run_id
            tool_name = str(action.tool_name or "")
            arguments = (
                dict(action.arguments_payload)
                if isinstance(action.arguments_payload, dict)
                else {}
            )
            # 已有尝试数（重试从 max+1 开始编号）
            existing_attempts = len(
                list(
                    db.scalars(
                        select(StrategyRunActionResult.id).where(
                            StrategyRunActionResult.action_id == action_id
                        )
                    ).all()
                )
            )

        claimed = None
        with session_scope() as db:
            claimed = db.execute(
                update(StrategyRunAction)
                .where(
                    StrategyRunAction.id == action_id,
                    StrategyRunAction.status.in_(
                        ("failed_terminal", "failed_retryable")
                    ),
                )
                .values(status="executing")
            )
            if claimed.rowcount != 1:
                raise RuntimeError("动作已被其他重试抢占或状态已变化")
        del claimed

        # ---- 2. 构建执行环境（真实客户端 + 设置） ----
        from types import SimpleNamespace

        from app.services.aniu_service import aniu_service

        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            mx_api_key = str(settings.mx_api_key or "")
            task_prompt = str(getattr(settings, "task_prompt", "") or "")

        if not mx_api_key:
            # 抢占回滚：无 Key 时把动作放回失败状态
            with session_scope() as db:
                db.execute(
                    update(StrategyRunAction)
                    .where(
                        StrategyRunAction.id == action_id,
                        StrategyRunAction.status == "executing",
                    )
                    .values(status="failed_terminal")
                )
            raise RuntimeError("未配置 MX API Key，无法重试")

        from skills.mx_core.client import MXClient

        app_settings = SimpleNamespace(task_prompt=task_prompt)
        client = MXClient(api_key=mx_api_key)
        try:
            # ---- 3. 执行（走全部挡板） ----
            result = mx_skill_service.execute_tool(
                client=client,
                app_settings=app_settings,
                tool_name=tool_name,
                arguments=arguments,
                run_id=run_id,
            )
            attempt_no = existing_attempts + 1
            success, normalized_action = self._record_attempt(
                action_id=action_id,
                attempt_no=attempt_no,
                result=result,
                max_attempts=1,  # 手动重试失败直接终态（可再次手动重试）
            )
            if success and isinstance(normalized_action, dict):
                # ---- 4. 意图关联回填 + 交易委托落库 ----
                try:
                    self._backfill_intent_action_id(
                        run_id=run_id,
                        action_id=action_id,
                        executed_action=normalized_action,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "retry intent action_id backfill failed: %s", exc
                    )
                if str(normalized_action.get("action") or "") in {"BUY", "SELL"}:
                    with session_scope() as db:
                        db.add(
                            TradeOrder(
                                run_id=run_id,
                                symbol=str(normalized_action.get("symbol") or ""),
                                action=str(normalized_action.get("action") or ""),
                                quantity=int(
                                    normalized_action.get("quantity") or 0
                                ),
                                price_type=str(
                                    normalized_action.get("price_type") or "MARKET"
                                ),
                                price=self._parse_float_or_none(
                                    normalized_action.get("price")
                                ),
                                status=str(
                                    normalized_action.get("status") or "submitted"
                                ),
                                response_payload=normalized_action.get("response"),
                            )
                        )
                # 追加到 run.executed_actions
                with session_scope() as db:
                    run = db.get(StrategyRun, run_id)
                    if run is not None:
                        try:
                            executed = json.loads(run.executed_actions or "[]")
                            if not isinstance(executed, list):
                                executed = []
                        except (TypeError, ValueError):
                            executed = []
                        executed.append(normalized_action)
                        run.executed_actions = executed
                        db.add(run)
        finally:
            client.close()

        # ---- 5. 重新汇总运行状态 ----
        self._refresh_run_summary(run_id)

        with session_scope() as db:
            action = db.get(StrategyRunAction, action_id)
            return {
                "action_id": action_id,
                "run_id": run_id,
                "tool_name": tool_name,
                "status": str(action.status or "") if action else "unknown",
                "ok": bool(result.get("ok")),
                "summary": result.get("summary"),
                "error": result.get("error"),
                "attempt_no": attempt_no,
            }

    def _refresh_run_summary(self, run_id: int) -> None:
        """重试后重新汇总运行状态（错误信息/状态/executed_actions 一致）。"""
        from app.services.execution_reconcile_service import (
            execution_reconcile_service,
        )

        status_counts, unresolved_count, error_message = self._summarize_actions(
            run_id
        )
        summary = {
            "total_planned": sum(status_counts.values()),
            "total_executed": status_counts.get("completed", 0),
            "unresolved_count": unresolved_count,
            "status_counts": status_counts,
            "error_message": error_message,
        }
        run_error = execution_reconcile_service.build_run_error_message(summary)
        with session_scope() as db:
            run = db.get(StrategyRun, run_id)
            if run is None:
                return
            run.error_message = run_error
            run.status = "completed" if not run_error else "failed"
            db.add(run)

    @staticmethod
    def _parse_float_or_none(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _backfill_intent_action_id(
        *, run_id: int, action_id: int, executed_action: dict[str, Any] | None
    ) -> None:
        """把成功的计划动作 id 回填到对应的交易意图（追溯链）。

        第三轮复审 P1：必须按 run_id + 标的 + 方向 联合匹配——
        原实现只按 run_id 批量回填，同一轮多笔交易会被错误关联到
        同一个 action_id，破坏事故追溯。
        """
        if not isinstance(executed_action, dict):
            return
        action_name = str(executed_action.get("action") or "").upper()
        if action_name not in {"BUY", "SELL", "CANCEL", "MANAGE_SELF_SELECT"}:
            return
        from sqlalchemy import and_, update as sa_update

        from app.db.models import TradeIntent

        symbol = str(
            executed_action.get("symbol")
            or executed_action.get("order_id")
            or ""
        ).strip()

        # P1（第六轮复审）：只按 intent_id + run_id 精确关联（一对一）。
        # - run_id 校验防止错误 intent_id 关联到其他运行的意图；
        # - 缺少 intent_id 不做模糊批量回填（标的+方向会串档），
        #   记录追溯告警即可。
        intent_id = executed_action.get("intent_id")
        if not (isinstance(intent_id, int) and intent_id > 0):
            logger.warning(
                "executed action without intent_id, skipping backfill: "
                "run_id=%s action_id=%s symbol=%s action=%s",
                run_id,
                action_id,
                symbol,
                action_name,
            )
            return
        with session_scope() as db:
            result = db.execute(
                sa_update(TradeIntent)
                .where(
                    and_(
                        TradeIntent.id == intent_id,
                        TradeIntent.run_id == run_id,
                        TradeIntent.action_id.is_(None),
                    )
                )
                .values(action_id=action_id)
            )
            if result.rowcount != 1:
                logger.warning(
                    "intent %s action_id backfill rowcount=%s (expected 1): "
                    "run_id=%s action_id=%s",
                    intent_id,
                    result.rowcount,
                    run_id,
                    action_id,
                )

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
            # 保留意图 id：执行记录 → 意图的一对一追溯链
            "intent_id": executed_action.get("intent_id"),
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