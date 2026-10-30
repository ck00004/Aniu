"""自选股操作意图服务（第十二轮复审 P1：跨轮持久化防重复）。

与交易意图（trade_intent_service）分离——自选股操作没有档位/信号语义。
核心目标：写请求已发出但回查失败（UNKNOWN）时，下一轮任务不得因
"列表暂时读不到目标状态"而重复发送 ADD/REMOVE。

状态机：
    EXECUTING → SUBMITTED → VERIFIED / NOOP / UNKNOWN / FAILED

UNKNOWN 的跨轮处理（复审指定语义）：
- 下一轮先重新读取自选股列表；
- 已达到目标状态 → VERIFIED（解除阻塞）；
- 仍无法确认 → 继续阻塞（同标的同方向不得重复提交）；
- 只有远端确认未执行（人工核验）后才允许 FAILED 重试。
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Callable

from sqlalchemy import select

from app.db.database import session_scope
from app.db.models import SelfSelectIntent

logger = logging.getLogger(__name__)

# 阻塞新意图的开放状态
_OPEN_STATUSES = ("executing", "submitted", "unknown")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


class SelfSelectIntentService:
    def check_and_register_intent(
        self,
        *,
        symbol: str,
        operation: str,
        run_id: int | None = None,
    ) -> SelfSelectIntent:
        """注册自选股操作意图；存在开放/UNKNOWN 意图时阻塞。

        必须在发送写请求之前调用。UNKNOWN 阻塞的解除只能通过
        reconcile_unknown_intent（目标状态已确认）或人工核验。
        """
        normalized_symbol = _normalize_symbol(symbol)
        normalized_op = str(operation or "").strip().upper()
        if normalized_op not in {"ADD", "REMOVE"}:
            raise RuntimeError(f"自选股操作只能是 ADD 或 REMOVE：{operation}")

        with session_scope() as db:
            open_intents = list(
                db.scalars(
                    select(SelfSelectIntent).where(
                        SelfSelectIntent.symbol == normalized_symbol,
                        SelfSelectIntent.operation == normalized_op,
                        SelfSelectIntent.status.in_(_OPEN_STATUSES),
                    )
                ).all()
            )
            if open_intents:
                latest = open_intents[-1]
                raise RuntimeError(
                    f"{normalized_symbol} 存在未确认的 {normalized_op} 操作意图"
                    f"（状态 {latest.status}，{latest.request_time} 发起）。"
                    "写请求可能已提交但未获回查确认，禁止重复发送；"
                    "请先调用 mx_get_self_selects 核对当前列表状态。"
                )

            intent = SelfSelectIntent(
                symbol=normalized_symbol,
                operation=normalized_op,
                run_id=run_id,
                status="executing",
            )
            db.add(intent)
            db.flush()
            db.refresh(intent)
            db.expunge(intent)
            logger.info(
                "self-select intent registered: %s %s run_id=%s",
                normalized_op,
                normalized_symbol,
                run_id,
            )
            return intent

    def mark_submitted(self, intent_id: int) -> None:
        with session_scope() as db:
            intent = db.get(SelfSelectIntent, intent_id)
            if intent is None:
                raise RuntimeError(f"自选股意图不存在：{intent_id}")
            intent.status = "submitted"
            intent.updated_at = _now_utc()
            db.add(intent)

    def mark_verified(self, intent_id: int, note: str | None = None) -> None:
        with session_scope() as db:
            intent = db.get(SelfSelectIntent, intent_id)
            if intent is None:
                raise RuntimeError(f"自选股意图不存在：{intent_id}")
            intent.status = "verified"
            intent.verified_at = _now_utc()
            intent.note = note
            intent.updated_at = _now_utc()
            db.add(intent)

    def mark_noop(self, intent_id: int, note: str | None = None) -> None:
        with session_scope() as db:
            intent = db.get(SelfSelectIntent, intent_id)
            if intent is None:
                raise RuntimeError(f"自选股意图不存在：{intent_id}")
            intent.status = "noop"
            intent.verified_at = _now_utc()
            intent.note = note
            intent.updated_at = _now_utc()
            db.add(intent)

    def mark_unknown(self, intent_id: int, note: str) -> None:
        """写请求已发出但回查失败：持久化 UNKNOWN（跨轮阻塞）。"""
        with session_scope() as db:
            intent = db.get(SelfSelectIntent, intent_id)
            if intent is None:
                raise RuntimeError(f"自选股意图不存在：{intent_id}")
            intent.status = "unknown"
            intent.note = str(note or "")[:500] or None
            intent.updated_at = _now_utc()
            db.add(intent)

    def mark_failed(self, intent_id: int, note: str) -> None:
        """远端明确拒绝/未执行：解除阻塞，允许重试。"""
        with session_scope() as db:
            intent = db.get(SelfSelectIntent, intent_id)
            if intent is None:
                raise RuntimeError(f"自选股意图不存在：{intent_id}")
            intent.status = "failed"
            intent.note = str(note or "")[:500] or None
            intent.updated_at = _now_utc()
            db.add(intent)

    def reconcile_unknown_intent(
        self,
        *,
        symbol: str,
        operation: str,
        read_state: Callable[[], tuple[set[str], list[str]] | None],
    ) -> bool:
        """核对 UNKNOWN 意图：目标状态已确认 → verified（解除阻塞）。

        read_state：读取自选股列表的回调（返回 (codes, names) 或 None）。
        返回 True 表示该标的+方向已解除阻塞（可继续本轮操作）。
        """
        normalized_symbol = _normalize_symbol(symbol)
        normalized_op = str(operation or "").strip().upper()

        with session_scope() as db:
            unknowns = list(
                db.scalars(
                    select(SelfSelectIntent).where(
                        SelfSelectIntent.symbol == normalized_symbol,
                        SelfSelectIntent.operation == normalized_op,
                        SelfSelectIntent.status == "unknown",
                    )
                ).all()
            )
            if not unknowns:
                return True

        state = read_state()
        if state is None:
            # 列表仍不可读：UNKNOWN 继续阻塞
            return False
        codes, _names = state
        in_list = normalized_symbol in codes
        achieved = (normalized_op == "ADD" and in_list) or (
            normalized_op == "REMOVE" and not in_list
        )
        if not achieved:
            # 目标状态未确认（例如 ADD 后列表仍无该标的——可能未生效
            # 或列表延迟）：保持 UNKNOWN 阻塞，等待人工核验
            return False

        with session_scope() as db:
            for intent in unknowns:
                intent.status = "verified"
                intent.verified_at = _now_utc()
                intent.note = "UNKNOWN 经重新读取确认目标状态已达成"
                intent.updated_at = _now_utc()
                db.add(intent)
        return True

    def list_intents(
        self, *, symbol: str | None = None, limit: int = 50
    ) -> list[SelfSelectIntent]:
        with session_scope() as db:
            stmt = (
                select(SelfSelectIntent)
                .order_by(SelfSelectIntent.id.desc())
                .limit(max(1, min(int(limit or 50), 200)))
            )
            if symbol:
                stmt = stmt.where(
                    SelfSelectIntent.symbol == _normalize_symbol(symbol)
                )
            rows = list(db.scalars(stmt).all())
            for row in rows:
                db.expunge(row)
            return rows


self_select_intent_service = SelfSelectIntentService()
