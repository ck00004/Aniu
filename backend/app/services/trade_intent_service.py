"""交易意图服务（补丁 C：跨轮/跨任务防重复交易的持久化挡板）。

设计要点（2026-10 外部审计）：
- signal_id 由后端按（上海交易日, 标的, 方向）派生，不由模型生成——
  同一天同一标的同一方向视为同一交易信号，15 分钟任务反复触发同一
  信号不会产生新键。
- tranche_id 区分同一信号下的合法分批：前一档已成交（FILLED）且冷却
  期已过才允许建立新档；每日每标的每方向有分批上限。撤单失败的档
  （CANCELED/FAILED）允许重试（新档，仍受上限约束）。
- UNKNOWN（超时/传输中断，远端可能已接受）阻塞同标的同方向的新
  意图，直到通过 mx_get_orders 核对出结果；无法确认时宁可继续阻塞，
  也不盲目重发（at-most-once 发送 + 状态核验）。
- 撤单意图按委托单号全局唯一（一单最多一个撤单意图）。
- 服务重启恢复：启动时把遗留 EXECUTING 意图收敛为 UNKNOWN（阻塞
  直到核对），不会因重启把未完成操作当作新操作重新执行。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.database import session_scope
from app.db.models import TradeIntent

logger = logging.getLogger(__name__)

SHANGHAI_TZ = timezone(timedelta(hours=8))

# 同一信号前一档成交后，允许新分批的最短间隔（分钟）。跨多个 15 分钟
# 决策轮次，机械区分"信号仍成立的重复触发"与"稍后的真实加仓"。
FILLED_REENTRY_COOLDOWN_MINUTES = 45
# 每日每标的每方向的分批上限（含失败重试档）。
MAX_TRANCHES_PER_SIGNAL = 3

# 阻塞新意图的状态：在途/未确认
_BLOCKING_STATUSES = {
    "planned",
    "executing",
    "submitted",
    "unknown",
    "partially_filled",
}
# 终态（可按规则重试或开新档）
_TERMINAL_STATUSES = {"filled", "canceled", "failed", "cancel_unknown"}

# 撤单意图的 signal_date 用固定哨兵值：撤单按委托单号全局唯一，
# 不按天滚动（委托不会跨天存在，但哨兵值让唯一性绝对成立）。
_CANCEL_SIGNAL_DATE = "PERM"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _normalize_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def extract_order_id(result: Any) -> str | None:
    """从交易/撤单返回中提取远端委托单号（尽力而为）。"""
    if not isinstance(result, dict):
        return None
    for container in (
        result,
        result.get("data") if isinstance(result.get("data"), dict) else {},
        result.get("data", {}).get("result")
        if isinstance(result.get("data"), dict)
        and isinstance(result.get("data", {}).get("result"), dict)
        else {},
    ):
        for key in ("orderID", "orderId", "order_id", "entrustNo", "entrust_no"):
            value = container.get(key)
            if value:
                return str(value).strip()
    return None


class TradeIntentService:
    # -- 意图注册 ---------------------------------------------------------

    def check_and_register_trade_intent(
        self,
        client: Any,
        *,
        symbol: str,
        side: str,
        quantity: int,
        run_id: int | None = None,
    ) -> TradeIntent:
        """交易意图去重检查 + 注册。被拦截时抛 RuntimeError。

        必须在 preflight（资金/持仓/未完成委托）之后、真实下单之前调用。
        """
        normalized_symbol = _normalize_symbol(symbol)
        normalized_side = str(side or "").strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise RuntimeError(f"交易意图方向只能是 BUY 或 SELL：{side}")
        signal_date = _now_shanghai().date().isoformat()

        # 先核对历史 UNKNOWN 意图（可能已被远端接受），能核对的收敛，
        # 核对不了的继续阻塞。
        self.reconcile_unknown_intents(client, symbol=normalized_symbol)

        with session_scope() as db:
            existing = list(
                db.scalars(
                    select(TradeIntent)
                    .where(
                        TradeIntent.signal_date == signal_date,
                        TradeIntent.symbol == normalized_symbol,
                        TradeIntent.side == normalized_side,
                    )
                    .order_by(TradeIntent.tranche_id.asc())
                ).all()
            )

            blocking = [
                item
                for item in existing
                if str(item.status or "") in _BLOCKING_STATUSES
            ]
            if blocking:
                latest = blocking[-1]
                raise RuntimeError(
                    f"{normalized_symbol} 当日已有未完成/未确认的 {normalized_side} "
                    f"交易意图（第 {latest.tranche_id} 档，状态 {latest.status}）。"
                    "同一信号不得重复提交；如需调整请先撤单或调用 mx_get_orders "
                    "核对该委托状态。"
                )

            tranche = len(existing) + 1
            latest_terminal = existing[-1] if existing else None
            if latest_terminal is not None:
                if str(latest_terminal.status or "") == "filled":
                    filled_at = latest_terminal.updated_at
                    if filled_at is not None:
                        filled_at = (
                            filled_at
                            if filled_at.tzinfo is not None
                            else filled_at.replace(tzinfo=timezone.utc)
                        )
                        cooldown_end = filled_at + timedelta(
                            minutes=FILLED_REENTRY_COOLDOWN_MINUTES
                        )
                        if _now_utc() < cooldown_end:
                            remaining = int(
                                (cooldown_end - _now_utc()).total_seconds() // 60
                            )
                            raise RuntimeError(
                                f"{normalized_symbol} 当日 {normalized_side} 信号"
                                f"（第 {latest_terminal.tranche_id} 档）刚成交，"
                                f"冷却期内禁止基于同一信号再次 {normalized_side}"
                                f"（剩余约 {remaining} 分钟）。只有冷却期后且具备"
                                "独立的新加仓依据，才允许建立新的分批。"
                            )
                if tranche > MAX_TRANCHES_PER_SIGNAL:
                    raise RuntimeError(
                        f"{normalized_symbol} 当日 {normalized_side} 交易意图已达"
                        f"分批上限（{MAX_TRANCHES_PER_SIGNAL} 档），禁止继续提交。"
                    )

            intent_key = f"{signal_date}:{normalized_symbol}:{normalized_side}:{tranche}"
            intent = TradeIntent(
                signal_date=signal_date,
                symbol=normalized_symbol,
                side=normalized_side,
                tranche_id=tranche,
                intent_key=intent_key,
                run_id=run_id,
                quantity=int(quantity or 0),
                status="executing",
            )
            db.add(intent)
            try:
                db.flush()
            except IntegrityError as exc:
                # 并发注册同一 (信号, 档位)：唯一约束兜底，第二个提交者被拒
                raise RuntimeError(
                    f"{normalized_symbol} {normalized_side} 交易意图被并发提交拦截"
                    "（唯一约束）：已有同信号同档位的意图存在。"
                ) from exc
            db.refresh(intent)
            db.expunge(intent)
            logger.info(
                "trade intent registered: %s qty=%s run_id=%s",
                intent_key,
                quantity,
                run_id,
            )
            return intent

    def check_and_register_cancel_intent(
        self,
        *,
        order_id: str,
        run_id: int | None = None,
    ) -> TradeIntent:
        """撤单意图：按委托单号全局唯一（一单最多一个撤单意图）。"""
        normalized_order = str(order_id or "").strip()
        if not normalized_order:
            raise RuntimeError("撤单意图缺少委托单号。")

        with session_scope() as db:
            existing = db.scalar(
                select(TradeIntent).where(
                    TradeIntent.signal_date == _CANCEL_SIGNAL_DATE,
                    TradeIntent.symbol == normalized_order,
                    TradeIntent.side == "CANCEL",
                )
            )
            if existing is not None and str(existing.status or "") != "failed":
                raise RuntimeError(
                    f"委托 {normalized_order} 已存在撤单意图"
                    f"（状态 {existing.status}），同一委托不得重复撤单；"
                    "请先调用 mx_get_orders 核对该委托的当前状态。"
                )
            if existing is not None:
                # 失败的撤单意图允许重试：复用原行（intent_key 唯一，
                # 不能新插同键行），重置为 executing。
                existing.status = "executing"
                existing.note = None
                existing.run_id = run_id
                existing.updated_at = _now_utc()
                db.add(existing)
                db.flush()
                db.refresh(existing)
                db.expunge(existing)
                return existing
            tranche = 1
            intent_key = f"{_CANCEL_SIGNAL_DATE}:{normalized_order}:CANCEL:1"
            intent = TradeIntent(
                signal_date=_CANCEL_SIGNAL_DATE,
                symbol=normalized_order,
                side="CANCEL",
                tranche_id=tranche,
                intent_key=intent_key,
                run_id=run_id,
                quantity=0,
                status="executing",
            )
            db.add(intent)
            try:
                db.flush()
            except IntegrityError as exc:
                raise RuntimeError(
                    f"委托 {normalized_order} 的撤单意图被并发提交拦截（唯一约束）。"
                ) from exc
            db.refresh(intent)
            db.expunge(intent)
            return intent

    # -- 状态流转 ---------------------------------------------------------

    def mark_intent_submitted(
        self, intent_id: int, provider_order_id: str | None = None
    ) -> None:
        with session_scope() as db:
            intent = db.get(TradeIntent, intent_id)
            if intent is None:
                return
            intent.status = "submitted"
            if provider_order_id:
                intent.provider_order_id = str(provider_order_id)
            intent.updated_at = _now_utc()
            db.add(intent)

    def mark_intent_unknown(self, intent_id: int, note: str) -> None:
        """超时/传输中断：远端可能��接受，状态未知（阻塞后续同向意图）。"""
        with session_scope() as db:
            intent = db.get(TradeIntent, intent_id)
            if intent is None:
                return
            intent.status = "unknown"
            intent.note = str(note or "")[:500] or None
            intent.updated_at = _now_utc()
            db.add(intent)

    def mark_intent_failed(self, intent_id: int, note: str) -> None:
        """确认未生效（远端明确拒绝/业务失败）：不阻塞，允许重试新档。"""
        with session_scope() as db:
            intent = db.get(TradeIntent, intent_id)
            if intent is None:
                return
            intent.status = "failed"
            intent.note = str(note or "")[:500] or None
            intent.updated_at = _now_utc()
            db.add(intent)

    # -- UNKNOWN 核对 -----------------------------------------------------

    def reconcile_unknown_intents(
        self, client: Any, *, symbol: str
    ) -> list[TradeIntent]:
        """核对指定标的的全部 UNKNOWN 意图。

        规则（基于 mx_get_orders 的可见证据）：
        - 委托列表中能找到该意图对应的委托（按单号或同标的同方向）→
          已被远端接受：按成交情况收敛为 submitted/filled/partially_filled。
        - 委托列表完全没有该标的该方向的委托痕迹 → 判定未提交成功，
          收敛为 failed（解除阻塞，允许重试）。
        - 无法判断（如委托列表读取失败）→ 保持 unknown，继续阻塞。
        """
        normalized_symbol = _normalize_symbol(symbol)
        try:
            orders_payload = client.get_orders()
        except Exception:  # noqa: BLE001
            logger.warning(
                "reconcile_unknown_intents: orders query failed for %s, "
                "keeping unknown intents blocking",
                normalized_symbol,
            )
            return []

        rows = orders_payload.get("data") if isinstance(orders_payload, dict) else None
        if isinstance(rows, dict):
            rows = (
                rows.get("orders")
                or rows.get("rows")
                or rows.get("list")
                or rows.get("data")
                or []
            )
        if not isinstance(rows, list):
            rows = []

        def _row_str(row: dict, *keys: str) -> str:
            for key in keys:
                value = row.get(key)
                if value is not None:
                    return str(value).strip()
            return ""

        symbol_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and _normalize_symbol(
                _row_str(row, "stockCode", "code", "SECURITY_CODE", "secCode")
            )
            == normalized_symbol
        ]

        with session_scope() as db:
            unknowns = list(
                db.scalars(
                    select(TradeIntent).where(
                        TradeIntent.symbol == normalized_symbol,
                        TradeIntent.status == "unknown",
                        TradeIntent.side.in_(["BUY", "SELL"]),
                    )
                ).all()
            )
            resolved: list[TradeIntent] = []
            for intent in unknowns:
                matched_row = None
                if intent.provider_order_id:
                    for row in symbol_rows:
                        if _row_str(
                            row, "orderID", "orderId", "order_id", "entrustNo"
                        ) == str(intent.provider_order_id):
                            matched_row = row
                            break
                if matched_row is None:
                    # 没有单号时按方向匹配（该标的当日该方向的委托）
                    side_rows = [
                        row
                        for row in symbol_rows
                        if _row_str(row, "orderDrt", "drt", "bsFlag", "side")
                        in ({"BUY": "1", "SELL": "2"}[intent.side], intent.side)
                    ]
                    matched_row = side_rows[0] if len(side_rows) == 1 else None

                if matched_row is not None:
                    ordered = _to_int(
                        _row_str(
                            matched_row, "orderCount", "count", "quantity", "orderQty"
                        )
                    )
                    dealt = _to_int(
                        _row_str(
                            matched_row,
                            "dealCount",
                            "tradeCount",
                            "filledCount",
                            "filledQty",
                        )
                    )
                    if ordered > 0 and dealt >= ordered:
                        intent.status = "filled"
                    elif dealt > 0:
                        intent.status = "partially_filled"
                    else:
                        intent.status = "submitted"
                    intent.note = "UNKNOWN 已通过委托列表核对收敛"
                elif not symbol_rows:
                    # 委托列表中该标的完全没有委托痕迹 → 未提交成功
                    intent.status = "failed"
                    intent.note = "UNKNOWN 核对：委托列表无该标的委托痕迹，判定未提交"
                # else：有该标的的委托但无法唯一对应 → 保持 unknown 继续阻塞
                intent.updated_at = _now_utc()
                db.add(intent)
                resolved.append(intent)
            return resolved

    # -- 启动恢复 ---------------------------------------------------------

    def reconcile_stale_executing_intents(self) -> int:
        """服务重启恢复：遗留 executing 意图收敛为 unknown（阻塞直到核对）。

        进程崩溃/重启时正在发送的意图状态不明，绝不能当作新操作重新执行。
        """
        with session_scope() as db:
            stale = list(
                db.scalars(
                    select(TradeIntent).where(TradeIntent.status == "executing")
                ).all()
            )
            for intent in stale:
                intent.status = "unknown"
                intent.note = "服务重启：发送状态不明，已收敛为 unknown 等待核对"
                intent.updated_at = _now_utc()
                db.add(intent)
            if stale:
                logger.warning(
                    "reconciled %s stale executing trade intents to unknown", len(stale)
                )
            return len(stale)

    # -- 查询 -------------------------------------------------------------

    def list_intents(
        self,
        *,
        symbol: str | None = None,
        signal_date: str | None = None,
        limit: int = 50,
    ) -> list[TradeIntent]:
        with session_scope() as db:
            stmt = select(TradeIntent).order_by(TradeIntent.id.desc()).limit(
                max(1, min(int(limit or 50), 200))
            )
            if symbol:
                stmt = stmt.where(TradeIntent.symbol == _normalize_symbol(symbol))
            if signal_date:
                stmt = stmt.where(TradeIntent.signal_date == signal_date)
            rows = list(db.scalars(stmt).all())
            for row in rows:
                db.expunge(row)
            return rows


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


trade_intent_service = TradeIntentService()
