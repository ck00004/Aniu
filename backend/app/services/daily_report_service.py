"""每日运行报告 + 证据预算（移植自原版 Aniu stages/stage_input.py 的确定性证据预算思路）。

证据预算规则：
- 交易/撤单调用与失败记录是**必需证据**，不可丢弃（只截断到 2000 字符）；
- 查询类工具结果先截断（4000 → 512 字符两级），总量仍超预算时从最旧
  开始丢弃，全部记入 omissions。

报告按上海日历日聚合当日全部运行，是后续"记忆梦境"夜间整理的输入。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.database import session_scope
from app.db.models import StrategyRun, TradeOrder
from app.services.aniu_service import now_shanghai
from app.services.run_payload_service import run_payload_service

_REQUIRED_EVIDENCE_LIMIT = 2000
_QUERY_EVIDENCE_LIMITS = (4000, 512)
_QUERY_TOTAL_BUDGET = 24_000
_FINAL_ANSWER_LIMIT = 8000

_TRADE_TOOL_NAMES = {"mx_moni_trade", "mx_moni_cancel"}


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + "…[已截断]"


def _tool_result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        import json

        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def apply_evidence_budget(
    tool_calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """对工具调用列表应用确定性证据预算。

    返回 (预算后列表, omissions 说明列表)。
    """
    omissions: list[str] = []
    budgeted: list[dict[str, Any]] = []
    query_total = 0

    # 第一遍：必需证据保留 + 查询结果截断到第一级
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("name") or call.get("tool_name") or "unknown")
        result_text = _tool_result_text(call.get("result"))

        if tool_name in _TRADE_TOOL_NAMES:
            budgeted.append(
                {
                    "tool_name": tool_name,
                    "kind": "trade",
                    "result": _truncate(result_text, _REQUIRED_EVIDENCE_LIMIT),
                }
            )
            continue

        trimmed = _truncate(result_text, _QUERY_EVIDENCE_LIMITS[0])
        budgeted.append(
            {
                "tool_name": tool_name,
                "kind": "query",
                "result": trimmed,
                "_size": len(trimmed),
            }
        )
        query_total += len(trimmed)

    # 第二遍：总量超预算时查询结果降到第二级截断
    if query_total > _QUERY_TOTAL_BUDGET:
        for entry in budgeted:
            if entry.get("kind") != "query":
                continue
            if entry["_size"] <= _QUERY_EVIDENCE_LIMITS[1]:
                continue
            new_text = _truncate(entry["result"], _QUERY_EVIDENCE_LIMITS[1])
            query_total -= entry["_size"] - len(new_text)
            entry["result"] = new_text
            entry["_size"] = len(new_text)

    # 第三遍：仍超预算则从最旧的查询开始丢弃
    if query_total > _QUERY_TOTAL_BUDGET:
        for index in range(len(budgeted)):
            entry = budgeted[index]
            if entry.get("kind") != "query":
                continue
            if query_total <= _QUERY_TOTAL_BUDGET:
                break
            omissions.append(
                f"查询结果 {entry['tool_name']} 因证据预算被丢弃"
            )
            query_total -= entry["_size"]
            entry["_dropped"] = True
        budgeted = [entry for entry in budgeted if not entry.get("_dropped")]

    for entry in budgeted:
        entry.pop("_size", None)
        entry.pop("_dropped", None)
    return budgeted, omissions


def _run_tool_calls(run: Any) -> list[dict[str, Any]]:
    skill_payloads = run["skill_payloads"]
    payload = skill_payloads if isinstance(skill_payloads, dict) else {}
    tool_calls = payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [call for call in tool_calls if isinstance(call, dict)]


def _list_run_rows_for_report(db: Any, target: date) -> list[Any]:
    """当日运行记录的窄列投影（不含 LLM 原始载荷）。"""
    shanghai = ZoneInfo("Asia/Shanghai")
    start_of_day = (
        datetime.combine(target, datetime.min.time(), tzinfo=shanghai)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    end_of_day = start_of_day + timedelta(days=1)
    stmt = (
        select(
            StrategyRun.id,
            StrategyRun.run_type,
            StrategyRun.status,
            StrategyRun.started_at,
            StrategyRun.finished_at,
            StrategyRun.final_answer,
            StrategyRun.error_message,
        )
        .where(
            StrategyRun.started_at >= start_of_day,
            StrategyRun.started_at < end_of_day,
        )
        .order_by(StrategyRun.started_at.desc(), StrategyRun.id.desc())
        .limit(50)
    )
    rows = db.execute(stmt).mappings().all()
    run_ids = [int(row["id"]) for row in rows]

    # 证据预算需要 skill 载荷：优先冷表（gzip 压缩，读取量约为原始的 1/8），
    # 未回填的历史行再批量回退热表旧字段。
    cold_payloads = run_payload_service.load_payloads_for_runs(
        db, run_ids, payload_types=("skill",)
    )
    missing = [
        run_id
        for run_id in run_ids
        if "skill" not in (cold_payloads.get(run_id) or {})
    ]
    legacy_payloads: dict[int, Any] = {}
    if missing:
        legacy_rows = db.execute(
            select(StrategyRun.id, StrategyRun.skill_payloads).where(
                StrategyRun.id.in_(missing)
            )
        ).mappings().all()
        legacy_payloads = {
            int(row["id"]): row["skill_payloads"]
            for row in legacy_rows
            if row["skill_payloads"] is not None
        }

    merged: list[dict[str, Any]] = []
    for row in rows:
        run_id = int(row["id"])
        payload = dict(row)
        payload["skill_payloads"] = (cold_payloads.get(run_id) or {}).get(
            "skill"
        ) or legacy_payloads.get(run_id)
        merged.append(payload)
    return merged


def _load_trade_orders_by_run(db: Any, run_ids: list[int]) -> dict[int, list[Any]]:
    """一次批量取回所有运行的交易记录，避免逐条 run 懒加载（N+1）。"""
    if not run_ids:
        return {}
    stmt = (
        select(TradeOrder)
        .where(TradeOrder.run_id.in_(run_ids))
        .order_by(TradeOrder.run_id.asc(), TradeOrder.id.asc())
    )
    grouped: dict[int, list[Any]] = {}
    for order in db.execute(stmt).scalars().all():
        grouped.setdefault(int(order.run_id), []).append(order)
    return grouped


def _run_section(run: Any, orders: list[Any]) -> dict[str, Any]:
    """单次运行的报告片段。

    run 现在是窄列投影的行（不是完整 ORM 对象），交易记录由调用方批量
    预取后传入——旧实现对 50 条 run 逐条触发 `run.trade_orders` 懒加载，
    形成 1 + 50 次查询的 N+1。
    """
    tool_calls = _run_tool_calls(run)
    evidence, omissions = apply_evidence_budget(tool_calls)

    trades = [
        {
            "symbol": order.symbol,
            "action": order.action,
            "quantity": order.quantity,
            "price_type": order.price_type,
            "price": order.price,
            "status": order.status,
        }
        for order in orders
    ]

    started_at = run["started_at"]
    finished_at = run["finished_at"]
    return {
        "run_id": int(run["id"]),
        "run_type": run["run_type"],
        "status": run["status"],
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "final_answer": _truncate(run["final_answer"], _FINAL_ANSWER_LIMIT),
        "trades": trades,
        "evidence": evidence,
        "omissions": omissions,
        "error_message": _truncate(run["error_message"], 500) or None,
    }


class DailyReportService:
    def build_daily_report(self, db: Any, *, target_date: date | None = None) -> dict[str, Any]:
        effective_date = target_date or now_shanghai().date()
        # 只投影报告真正需要的列（含 skill_payloads 用于证据预算），
        # 不读取 llm_request/response 等大载荷。
        runs = _list_run_rows_for_report(db, effective_date)
        orders_by_run = _load_trade_orders_by_run(
            db, [int(row["id"]) for row in runs]
        )

        sections = []
        all_trades: list[dict[str, Any]] = []
        all_omissions: list[str] = []
        for run in runs:
            section = _run_section(run, orders_by_run.get(int(run["id"]), []))
            sections.append(section)
            for trade in section["trades"]:
                all_trades.append({"run_id": int(run["id"]), **trade})
            all_omissions.extend(section["omissions"])

        completed = sum(1 for run in runs if run["status"] == "completed")
        failed = sum(1 for run in runs if run["status"] == "failed")

        return {
            "date": effective_date.isoformat(),
            "run_count": len(runs),
            "completed_count": completed,
            "failed_count": failed,
            "trade_count": len(all_trades),
            "runs": sections,
            "trades": all_trades,
            "omissions": all_omissions,
            "markdown": self._render_markdown(effective_date, sections, all_trades),
        }

    def _render_markdown(
        self,
        target_date: date,
        sections: list[dict[str, Any]],
        trades: list[dict[str, Any]],
    ) -> str:
        lines = [f"# {target_date.isoformat()} 每日运行报告", ""]
        if not sections:
            lines.append("当日无运行记录。")
            return "\n".join(lines)

        lines.append(
            f"共 {len(sections)} 次运行"
            f"（成功 {sum(1 for s in sections if s['status'] == 'completed')}"
            f" / 失败 {sum(1 for s in sections if s['status'] == 'failed')}），"
            f"执行交易 {len(trades)} 笔。"
        )
        lines.append("")

        for section in sections:
            status_text = {
                "completed": "成功",
                "failed": "失败",
                "running": "进行中",
            }.get(str(section["status"]), str(section["status"]))
            lines.append(f"## 运行 #{section['run_id']}（{section['run_type']}，{status_text}）")
            if section.get("trades"):
                for trade in section["trades"]:
                    lines.append(
                        f"- 交易：{trade['action']} {trade['symbol']}"
                        f" {trade['quantity']} 股（{trade['status']}）"
                    )
            if section.get("error_message"):
                lines.append(f"- 失败原因：{section['error_message']}")
            if section.get("final_answer"):
                lines.append("")
                lines.append(str(section["final_answer"]))
            lines.append("")

        if trades:
            lines.append("## 当日交易汇总")
            for trade in trades:
                lines.append(
                    f"- #{trade['run_id']} {trade['action']} "
                    f"{trade['symbol']} {trade['quantity']} 股"
                    f"（{trade['status']}）"
                )
        return "\n".join(lines)


daily_report_service = DailyReportService()
