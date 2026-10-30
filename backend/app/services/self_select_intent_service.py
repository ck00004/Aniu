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
            # 第十三轮复审 P0-1：同标的**任一方向**的开放意图都阻塞——
            # 未确认的 ADD 不得被 REMOVE 覆盖（反之亦然）。否则：
            # ADD UNKNOWN → REMOVE 判 NOOP（列表暂无该股）→ ADD 延迟
            # 生效 → 用户以为删除成功实际没有。
            open_intents = list(
                db.scalars(
                    select(SelfSelectIntent).where(
                        SelfSelectIntent.symbol == normalized_symbol,
                        SelfSelectIntent.status.in_(_OPEN_STATUSES),
                    )
                ).all()
            )
            if open_intents:
                latest = open_intents[-1]
                raise RuntimeError(
                    f"{normalized_symbol} 存在未确认的自选股操作意图"
                    f"（{latest.operation}，状态 {latest.status}，"
                    f"{latest.request_time} 发起）。写请求可能已提交但未获"
                    "回查确认，禁止同标的任何方向的重复操作；请先调用 "
                    "mx_get_self_selects 核对当前列表状态。"
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

    def reconcile_open_intents(
        self,
        *,
        symbol: str,
        read_state: Callable[[], tuple[set[str], list[str]] | None],
    ) -> bool:
        """核对同标的全部开放意图（含反方向）；全部确认后解除阻塞。

        第十三轮复审 P0-1：NOOP 判断前必须先处理所有未完成意图。
        read_state：读取自选股列表的回调（返回 (codes, names) 或 None）。
        返回 True 表示该标的已无开放意图（可继续本轮操作）。
        """
        normalized_symbol = _normalize_symbol(symbol)

        with session_scope() as db:
            open_intents = list(
                db.scalars(
                    select(SelfSelectIntent).where(
                        SelfSelectIntent.symbol == normalized_symbol,
                        SelfSelectIntent.status.in_(_OPEN_STATUSES),
                    )
                ).all()
            )
            if not open_intents:
                return True

        state = read_state()
        if state is None:
            # 列表不可读：开放意图继续阻塞
            return False
        codes, names = state
        # 名称形式的意图：按名称精确匹配判断在列状态
        is_code = normalized_symbol.isdigit()
        if is_code:
            in_list = normalized_symbol in codes
        else:
            in_list = any(
                row_name.strip() == normalized_symbol for row_name in names
            )

        with session_scope() as db:
            all_confirmed = True
            for intent in open_intents:
                achieved = (
                    intent.operation == "ADD" and in_list
                ) or (intent.operation == "REMOVE" and not in_list)
                if achieved:
                    intent.status = "verified"
                    intent.verified_at = _now_utc()
                    intent.note = "经重新读取确认目标状态已达成"
                    intent.updated_at = _now_utc()
                    db.add(intent)
                else:
                    # 目标状态未确认：保持开放（阻塞）
                    all_confirmed = False
            return all_confirmed

    # 兼容旧名
    def reconcile_unknown_intent(
        self,
        *,
        symbol: str,
        operation: str,
        read_state: Callable[[], tuple[set[str], list[str]] | None],
    ) -> bool:
        return self.reconcile_open_intents(symbol=symbol, read_state=read_state)

    def reconcile_stale_intents(self) -> int:
        """启动恢复（第十三轮复审 P0-3）：

        - 遗留 EXECUTING → UNKNOWN（写请求状态不明，阻塞直到核对）；
        - 遗留 SUBMITTED → 保持（下一轮操作前的 reconcile 会重新核验）。
        不能仅因进程重启判定 FAILED。
        """
        with session_scope() as db:
            stale = list(
                db.scalars(
                    select(SelfSelectIntent).where(
                        SelfSelectIntent.status == "executing"
                    )
                ).all()
            )
            for intent in stale:
                intent.status = "unknown"
                intent.note = "服务重启：写请求状态不明，已收敛为 unknown 等待核对"
                intent.updated_at = _now_utc()
                db.add(intent)
            if stale:
                logger.warning(
                    "reconciled %s stale self-select intents to unknown",
                    len(stale),
                )
            return len(stale)

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
