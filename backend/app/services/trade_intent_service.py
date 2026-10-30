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

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.db.database import session_scope
from app.db.models import TradeIntent
from skills.mx_core.order_schema import (
    CANCELED_ORDER_STATUSES,
    PENDING_ORDER_STATUSES,
    ROWS_ERROR,
    ROWS_INVALID,
    get_order_side,
    get_order_status,
    get_order_symbol,
    order_terminal_status,
    parse_order_snapshot,
    row_int_optional,
    row_str,
)

logger = logging.getLogger(__name__)

SHANGHAI_TZ = timezone(timedelta(hours=8))

# 同一信号前一档成交后，允许新分批的最短间隔（分钟）。跨多个 15 分钟
# 决策轮次，机械区分"信号仍成立的重复触发"与"稍后的真实加仓"。
FILLED_REENTRY_COOLDOWN_MINUTES = 45
# 每日每标的每方向的分批上限。
# 第六轮复审特别建议：45 分钟冷却尚无独立新信号验证能力（时间规则
# 而非行情确认），在信号生命周期机制落地前降为 1（保守模式），
# 防止"时间到了就允许二次进攻"。恢复分批需后端可验证的新行情确认。
MAX_TRANCHES_PER_SIGNAL = 1

# 阻塞新意图的状态：在途/未确认
_BLOCKING_STATUSES = {
    "planned",
    "executing",
    "submitted",
    "submitted_untracked",
    "unknown",
    "partially_filled",
}
# 终态（可按规则重试或开新档）。canceled_partial：部分成交后剩余已
# 撤销——委托不再在途（第七轮复审 P0-2），已成交部分已形成持仓变化。
_TERMINAL_STATUSES = {
    "filled",
    "canceled",
    "canceled_partial",
    "failed",
    "cancel_unknown",
}

# 撤单意图的 signal_date 用固定哨兵值：撤单按委托单号全局唯一，
# 不按天滚动（委托不会跨天存在，但哨兵值让唯一性绝对成立）。
_CANCEL_SIGNAL_DATE = "PERM"

# 历史遗留说明（第七轮复审清理）：UNKNOWN 曾设计"连续多次核验 + 等待期
# 后自动收敛 failed"，该逻辑已在第五轮复审后移除——活动委托列表查不到
# 不能证明未提交，UNKNOWN 一律保持阻塞，等待单号精确匹配或人工核验。
# 保留 unknown_checks / first_unknown_at 字段仅为记录核验轨迹。
# 以下两个常量已无行为作用（保留供历史测试引用，勿在新代码使用）。
UNKNOWN_MIN_CHECKS = 2
UNKNOWN_WAIT_SECONDS = 90

# 技术性重试（确认未提交的 failed 档）单独计数，不消耗真实分批档位。
MAX_TECHNICAL_RETRIES = 3

# 委托列表的候选行键（与 skills.mx_core.execution._payload_rows 对齐，
# 含 orderList——2026-10 复审 P0：两处解析器不一致会让 orderList 响应
# 被误判为"无委托痕迹"从而错误解除 UNKNOWN 阻塞）。
_ORDER_ROW_KEYS = (
    "rows",
    "list",
    "data",
    "orders",
    "orderList",
    "posList",
    "positionList",
)
_PENDING_ORDER_STATUS_VALUES = {"1", "2", "8", "100"}







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

            # 技术性重试（确认未提交的 failed 档）不消耗真实分批档位，
            # 但单独设上限，防止接口故障导致无限重试（2026-10 复审）。
            failed_count = sum(
                1 for item in existing if str(item.status or "") == "failed"
            )
            if failed_count >= MAX_TECHNICAL_RETRIES:
                raise RuntimeError(
                    f"{normalized_symbol} 当日 {normalized_side} 的技术性重试已达"
                    f"上限（{MAX_TECHNICAL_RETRIES} 次），请人工核对妙想接口与"
                    "委托状态后再继续。"
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
                # 真实分批上限只数非 failed 档（failed 是技术重试）
                real_tranches = sum(
                    1 for item in existing if str(item.status or "") != "failed"
                )
                if real_tranches >= MAX_TRANCHES_PER_SIGNAL:
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
        """提交成功。无远端单号 → submitted_untracked（第七轮复审 P1）。

        submitted_untracked 同样阻塞同向新委托（订单确认提交过），
        但无法自动状态同步——需要人工核验后绑定单号
        （bind_provider_order_id），禁止模糊匹配自动认领。
        """
        with session_scope() as db:
            intent = db.get(TradeIntent, intent_id)
            if intent is None:
                return
            if provider_order_id:
                intent.status = "submitted"
                intent.provider_order_id = str(provider_order_id)
            else:
                intent.status = "submitted_untracked"
                intent.note = (
                    "提交成功但未能从响应提取远端单号：无法自动同步状态，"
                    "请人工核验后绑定单号（POST /api/aniu/trade-intents/"
                    f"{intent_id}/bind-order）"
                )
            intent.updated_at = _now_utc()
            db.add(intent)

    def bind_provider_order_id(
        self,
        intent_id: int,
        provider_order_id: str,
        *,
        expected_symbol: str | None = None,
        expected_side: str | None = None,
    ) -> TradeIntent | None:
        """人工核验后绑定远端单号（submitted_untracked → submitted）。

        第八轮复审安全加固：
        - 仅 submitted_untracked / unknown 状态可绑定（重复绑定拒绝）；
        - 可选校验标的与方向与意图一致（防误绑到其他意图的订单）；
        - 单号全局唯一：已被其他意图绑定的单号拒绝（数据库唯一约束兜底）；
        - 绑定后状态为 submitted，恢复单号精确匹配同步。
        """
        normalized = str(provider_order_id or "").strip()
        if not normalized:
            raise RuntimeError("远端委托单号不能为空。")
        with session_scope() as db:
            intent = db.get(TradeIntent, intent_id)
            if intent is None:
                return None
            if str(intent.status or "") not in (
                "submitted_untracked",
                "unknown",
            ):
                raise RuntimeError(
                    f"意图当前状态为 {intent.status}，仅 submitted_untracked/"
                    "unknown 状态允许人工绑定单号（重复绑定被拒绝）。"
                )
            if expected_symbol is not None and _normalize_symbol(
                expected_symbol
            ) != _normalize_symbol(intent.symbol):
                raise RuntimeError(
                    f"单号所属标的与意图不一致（意图 {intent.symbol}，"
                    f"请求 {expected_symbol}），绑定被拒绝。"
                )
            if (
                expected_side is not None
                and str(expected_side).upper() != str(intent.side).upper()
            ):
                raise RuntimeError(
                    f"单号所属方向与意图不一致（意图 {intent.side}，"
                    f"请求 {expected_side}），绑定被拒绝。"
                )
            # 单号唯一性：同一单号不得绑定到两个意图
            existing = db.scalar(
                select(TradeIntent).where(
                    TradeIntent.provider_order_id == normalized,
                    TradeIntent.id != intent_id,
                )
            )
            if existing is not None:
                raise RuntimeError(
                    f"委托单号 {normalized} 已被意图 #{existing.id} 绑定，"
                    "同一订单不得绑定到两个交易意图。"
                )
            intent.provider_order_id = normalized
            intent.status = "submitted"
            intent.note = f"人工核验绑定单号：{normalized}"
            intent.updated_at = _now_utc()
            db.add(intent)
            db.flush()
            db.refresh(intent)
            db.expunge(intent)
            logger.info(
                "trade intent %s manually bound to order %s",
                intent_id,
                normalized,
            )
            return intent

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

    def reconcile_open_trade_intents(
        self, client: Any, *, symbol: str
    ) -> list[TradeIntent]:
        """核对指定标的的全部在途意图（submitted/partially_filled/unknown）。

        P0-1/P0-3（2026-10 复审）：
        - 委托列表解析与 execution._payload_rows 同源（含 orderList），
          结构无法识别视为"不可判定"，保持阻塞。
        - 按远端单号优先匹配；无单号时按 标的+方向 唯一匹配。
        - "查不到痕迹"≠"未提交"：需连续 UNKNOWN_MIN_CHECKS 次独立核验
          且超过等待期才收敛 failed；此前保持 UNKNOWN 阻塞。
        - 正常 SUBMITTED/PARTIALLY_FILLED 意图同步为远端真实状态
          （filled/partially_filled/canceled/submitted）。
        """
        normalized_symbol = _normalize_symbol(symbol)
        try:
            orders_payload = client.get_orders()
        except Exception:  # noqa: BLE001
            logger.warning(
                "reconcile_open_trade_intents: orders query failed for %s, "
                "keeping intents blocking",
                normalized_symbol,
            )
            return []

        rows, rows_state = parse_order_snapshot(orders_payload)
        if rows_state in (ROWS_INVALID, ROWS_ERROR):
            logger.warning(
                "reconcile_open_trade_intents: unparseable orders payload for %s, "
                "keeping intents blocking",
                normalized_symbol,
            )
            return []

        symbol_rows = [
            row
            for row in rows
            if _normalize_symbol(
                get_order_symbol(row)
            )
            == normalized_symbol
        ]

        with session_scope() as db:
            open_intents = list(
                db.scalars(
                    select(TradeIntent).where(
                        TradeIntent.symbol == normalized_symbol,
                        TradeIntent.status.in_(
                            [
                                "submitted",
                                "submitted_untracked",
                                "partially_filled",
                                "unknown",
                            ]
                        ),
                        TradeIntent.side.in_(["BUY", "SELL"]),
                    )
                ).all()
            )
            resolved: list[TradeIntent] = []
            now = _now_utc()
            for intent in open_intents:
                # P0-2（第三轮复审）：有单号时只做单号精确匹配——
                # 单号找不到绝不退化为"标的+方向"匹配，防止把同标的的
                # 其他委托认作本意图（错误收敛为 submitted/filled）。
                #
                # P0-5（第六轮复审）：无单号时**禁止**标的+方向模糊匹配。
                # 超时订单（UNKNOWN）没有单号，唯一候选也可能是历史订单
                # 而非本次请求——无法证明因果关联，一律保持 UNKNOWN 等
                # 待人工核验。只有单号精确匹配才允许同步状态。
                if intent.provider_order_id:
                    matched_row = next(
                        (
                            row
                            for row in symbol_rows
                            if row_str(
                                row, "orderID", "orderId", "order_id", "entrustNo"
                            )
                            == str(intent.provider_order_id)
                        ),
                        None,
                    )
                else:
                    matched_row = None

                if matched_row is not None:
                    intent.status = order_terminal_status(matched_row)
                    intent.note = "已按远端委托列表同步状态"
                    intent.unknown_checks = 0
                    intent.first_unknown_at = None
                    intent.updated_at = now
                    db.add(intent)
                    resolved.append(intent)
                    continue

                # ---- 查不到对应委托：按意图状态分别处理 ----
                # P0-1b（第三轮复审）：委托从列表消失 ≠ 未提交。订单可能
                # 已成交、已撤销或进入历史委托列表。
                # - unknown：允许经可见性等待后判定"未提交"（该意图本来
                #   就是因为发送状态不明才进入 UNKNOWN，无单号时这是
                #   唯一可用证据）。
                # - submitted/partially_filled：订单确认提交过——消失只
                #   能说明"当前列表查不到"，绝不能判 failed 解除阻塞；
                #   保持原状态，等待委托重新可见或人工核对。
                if intent.status in ("submitted", "partially_filled"):
                    intent.note = (
                        "委托已确认提交但当前委托列表中不可见（可能已成交/"
                        "撤销或转入历史列表），保持阻塞等待重新可见或人工核对"
                    )
                    intent.updated_at = now
                    db.add(intent)
                    resolved.append(intent)
                    continue

                # unknown：不自动解除（第五轮复审 P0-3 重设计）
                #
                # 安全原则：找到订单 → 按精确匹配更新状态；没找到订单
                # → 不代表没有提交。一条历史订单 ≠ 完整历史视图（接口
                # 可能分页/过滤/延迟，partially_filled 也可能仍在活动
                # 列表）。在妙想提供可验证的完整历史委托视图（明确指定
                # 交易日期、分页总数、has_more、查询成功状态）之前，
                # UNKNOWN 一律保持阻塞，等待人工核验或订单重新可见。
                checks = int(intent.unknown_checks or 0) + 1
                first_at = intent.first_unknown_at
                if first_at is None:
                    first_at = now
                    intent.first_unknown_at = now
                if first_at.tzinfo is None:
                    first_at = first_at.replace(tzinfo=timezone.utc)
                intent.unknown_checks = checks
                intent.note = (
                    f"委托列表暂无痕迹（第 {checks} 次核验）。活动委托列表"
                    "查不到不能证明未提交（订单可能已成交离开列表）；"
                    "在确认妙想委托查询返回完整历史视图前，本意图保持"
                    "UNKNOWN 阻塞，请通过 mx_get_orders 人工核对或等待"
                    "委托重新可见后重试。"
                )
                intent.updated_at = now
                db.add(intent)
                resolved.append(intent)
            return resolved

    def reconcile_cancel_intents(self, client: Any) -> list[TradeIntent]:
        """撤单意图状态同步（第三轮复审 P1：撤单需要独立闭环）。

        按原委托单号核对：
        - 原委托已撤销 → verified（撤单确认生效）
        - 原委托已成交 → filled（撤单太迟，订单已执行）
        - 原委托仍待撤 → submitted（撤单处理中，继续阻塞重复撤单）
        - 原委托状态未知 → unknown（保持阻塞）
        - 原委托从列表消失 → unknown（已撤销/成交/转历史，不可再撤，
          阻塞重复撤单是安全默认）
        """
        try:
            orders_payload = client.get_orders()
        except Exception:  # noqa: BLE001
            return []
        rows, rows_state = parse_order_snapshot(orders_payload)
        if rows_state in (ROWS_INVALID, ROWS_ERROR):
            return []

        def _row_order_id(row: dict[str, Any]) -> str:
            return row_str(row, "orderID", "orderId", "order_id", "entrustNo")

        with session_scope() as db:
            open_cancels = list(
                db.scalars(
                    select(TradeIntent).where(
                        TradeIntent.side == "CANCEL",
                        TradeIntent.status.in_(
                            ["executing", "submitted", "cancel_pending", "unknown"]
                        ),
                    )
                ).all()
            )
            resolved: list[TradeIntent] = []
            now = _now_utc()
            for intent in open_cancels:
                order_id = str(intent.symbol or "")
                matched = next(
                    (row for row in rows if _row_order_id(row) == order_id),
                    None,
                )
                if matched is None:
                    intent.status = "unknown"
                    intent.note = (
                        f"原委托 {order_id} 已从委托列表消失（已撤销/成交/"
                        "转历史），不可再撤；保持阻塞防止重复撤单"
                    )
                else:
                    terminal = order_terminal_status(matched)
                    if terminal in ("canceled", "canceled_partial"):
                        intent.status = "verified"
                        if terminal == "canceled_partial":
                            intent.note = (
                                f"原委托 {order_id} 部分成交后剩余已撤销，"
                                "撤单确认生效（已成交部分已形成持仓变化）"
                            )
                        else:
                            intent.note = f"原委托 {order_id} 已撤销，撤单确认生效"
                    elif terminal == "filled":
                        intent.status = "filled"
                        intent.note = f"原委托 {order_id} 已全部成交，撤单未生效"
                    elif terminal == "partially_filled":
                        # P1（第四/五轮复审）：部分成交 ≠ 全部成交——剩余
                        # 委托仍在途，撤单仍在处理中。使用独立
                        # cancel_pending 状态（区别于"撤单请求已提交但
                        # 原单仍待撤"的 submitted），不归 filled（否则剩余
                        # 部分被永久锁死无法再撤）。
                        intent.status = "cancel_pending"
                        intent.note = (
                            f"原委托 {order_id} 部分成交、剩余仍在途，"
                            "撤单处理中（CANCEL_PENDING）"
                        )
                    elif terminal == "submitted":
                        intent.status = "submitted"
                        intent.note = f"原委托 {order_id} 仍待撤，撤单处理中"
                    else:
                        intent.status = "unknown"
                        intent.note = f"原委托 {order_id} 状态不可判定"
                intent.updated_at = now
                db.add(intent)
                resolved.append(intent)
            return resolved

    def reconcile_all_open_intents(self, client: Any) -> dict[str, int]:
        """每轮 Trade 任务开始前同步全部在途意图（P0-3 状态闭环）。

        SUBMITTED / PARTIALLY_FILLED / UNKNOWN 意图按远端委托列表更新为
        真实状态：全部成交 → filled（进入冷却期）、部分成交 →
        partially_filled（继续阻塞）、已撤/废单 → canceled（按策略放行）。
        返回 {状态: 数量} 摘要。
        """
        with session_scope() as db:
            symbols = list(
                db.scalars(
                    select(TradeIntent.symbol)
                    .where(
                        TradeIntent.status.in_(
                            [
                                "submitted",
                                "submitted_untracked",
                                "partially_filled",
                                "unknown",
                            ]
                        ),
                        TradeIntent.side.in_(["BUY", "SELL"]),
                    )
                    .distinct()
                ).all()
            )

        status_counts: dict[str, int] = {}
        for symbol in symbols:
            for intent in self.reconcile_open_trade_intents(client, symbol=symbol):
                status_counts[intent.status] = (
                    status_counts.get(intent.status, 0) + 1
                )
        # 撤单意图独立同步（按单号，不按标的）
        for intent in self.reconcile_cancel_intents(client):
            status_counts[f"cancel:{intent.status}"] = (
                status_counts.get(f"cancel:{intent.status}", 0) + 1
            )
        if status_counts:
            logger.info("open trade intents reconciled: %s", status_counts)
        return status_counts

    # 兼容旧名：UNKNOWN 核对已并入在途意图同步
    def reconcile_unknown_intents(
        self, client: Any, *, symbol: str
    ) -> list[TradeIntent]:
        return self.reconcile_open_trade_intents(client, symbol=symbol)

    # -- 启动恢复 ---------------------------------------------------------

    def reconcile_stale_executing_intents(self) -> int:
        """服务重启恢复：遗留 executing 意图收敛为 unknown（阻塞直到核对）。

        进程崩溃/重启时正在发送的意图状态不明，绝不能当作新操作重新执行。
        多 worker 守卫：只收敛所属 run 已不在 running 状态的意图——另一
        个 worker 正在执行的 run（DB 状态仍为 running）不会被误判为遗留。
        """
        from app.db.models import StrategyRun

        with session_scope() as db:
            stale = list(
                db.scalars(
                    select(TradeIntent)
                    .where(TradeIntent.status == "executing")
                    .join(
                        StrategyRun,
                        TradeIntent.run_id == StrategyRun.id,
                        isouter=True,
                    )
                    .where(
                        or_(
                            StrategyRun.status != "running",
                            StrategyRun.id.is_(None),
                        )
                    )
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
