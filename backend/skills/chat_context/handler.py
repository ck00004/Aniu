from __future__ import annotations

from datetime import date
from typing import Any

from app.db.database import session_scope
from app.schemas.aniu import RunDetailRead, RunSummaryRead
from app.skills.base import BaseSkill


def _bool_arg(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clamp_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = default
    return max(minimum, min(maximum, numeric))


def _truncate_text(value: Any, *, limit: int = 6000) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...(内容过长，已截断)"


def _summary_text(run: dict[str, Any]) -> str | None:
    return _truncate_text(
        run.get("analysis_summary")
        or run.get("output_markdown")
        or run.get("final_answer")
        or run.get("error_message")
    )


class Skill(BaseSkill):
    id = "chat_context"
    name = "聊天上下文"
    description = "账户摘要、持仓、委托与历史任务记录读取工具"
    run_types = ["chat"]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "chat_get_account_summary",
                "description": "读取当前账户总览摘要，包括总资产、现金、持仓市值、收益和仓位等信息。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "force_refresh": {
                            "type": "boolean",
                            "description": "是否强制实时刷新账户数据。默认 false；只有需要更实时数据时才设为 true。",
                        }
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "chat_get_positions",
                "description": "读取当前持仓明细，可限制返回数量。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "最多返回多少条持仓，默认 20，最大 100。",
                        },
                        "force_refresh": {
                            "type": "boolean",
                            "description": "是否强制实时刷新账户数据。默认 false。",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "chat_get_orders",
                "description": "读取当前委托明细，可限制返回数量。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "最多返回多少条委托，默认 20，最大 100。",
                        },
                        "force_refresh": {
                            "type": "boolean",
                            "description": "是否强制实时刷新账户数据。默认 false。",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "chat_list_runs",
                "description": "读取历史任务运行列表，用于先定位最近一次或某一天的任务，再决定是否展开详情。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "返回条数，默认 10，最大 50。",
                        },
                        "date": {
                            "type": "string",
                            "description": "可选，按日期过滤，格式 YYYY-MM-DD。",
                        },
                        "status": {
                            "type": "string",
                            "description": "可选，按状态过滤，例如 completed / failed / running。",
                        },
                        "before_id": {
                            "type": "integer",
                            "description": "可选，结合分页继续向更早的任务翻页。",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "chat_get_run_detail",
                "description": "按 run_id 读取某次任务的详细内容，包括输出摘要、最终结论、工具调用摘要和交易记录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "integer",
                            "description": "任务运行记录 ID。",
                        },
                        "include_tool_previews": {
                            "type": "boolean",
                            "description": "是否附带原始工具预览内容。默认 false，仅在用户明确要求时使用。",
                        },
                    },
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
            },
        },
    ]

    def do_chat_get_account_summary(self, *, arguments, context):
        del context
        from app.services.aniu_service import aniu_service

        overview = aniu_service.get_account_overview(
            force_refresh=_bool_arg(arguments.get("force_refresh", False))
        )
        account = {
            "open_date": overview.get("open_date"),
            "daily_profit_trade_date": overview.get("daily_profit_trade_date"),
            "operating_days": overview.get("operating_days"),
            "initial_capital": overview.get("initial_capital"),
            "total_assets": overview.get("total_assets"),
            "total_market_value": overview.get("total_market_value"),
            "cash_balance": overview.get("cash_balance"),
            "total_position_ratio": overview.get("total_position_ratio"),
            "holding_profit": overview.get("holding_profit"),
            "total_return_ratio": overview.get("total_return_ratio"),
            "nav": overview.get("nav"),
            "daily_profit": overview.get("daily_profit"),
            "daily_return_ratio": overview.get("daily_return_ratio"),
            "position_count": len(overview.get("positions") or []),
            "order_count": len(overview.get("orders") or []),
            "trade_summary_count": len(overview.get("trade_summaries") or []),
            "errors": overview.get("errors") or [],
        }
        return {
            "ok": True,
            "tool_name": "chat_get_account_summary",
            "summary": "已读取账户摘要。",
            "result": {"account": account},
        }

    def do_chat_get_positions(self, *, arguments, context):
        del context
        from app.services.aniu_service import aniu_service

        limit = _clamp_int(arguments.get("limit"), default=20, minimum=1, maximum=100)
        overview = aniu_service.get_account_overview(
            force_refresh=_bool_arg(arguments.get("force_refresh", False))
        )
        positions = list(overview.get("positions") or [])
        return {
            "ok": True,
            "tool_name": "chat_get_positions",
            "summary": f"已读取持仓明细，共 {len(positions)} 条，返回前 {min(len(positions), limit)} 条。",
            "result": {
                "total": len(positions),
                "items": positions[:limit],
                "errors": overview.get("errors") or [],
            },
        }

    def do_chat_get_orders(self, *, arguments, context):
        del context
        from app.services.aniu_service import aniu_service

        limit = _clamp_int(arguments.get("limit"), default=20, minimum=1, maximum=100)
        overview = aniu_service.get_account_overview(
            force_refresh=_bool_arg(arguments.get("force_refresh", False))
        )
        orders = list(overview.get("orders") or [])
        return {
            "ok": True,
            "tool_name": "chat_get_orders",
            "summary": f"已读取委托明细，共 {len(orders)} 条，返回前 {min(len(orders), limit)} 条。",
            "result": {
                "total": len(orders),
                "items": orders[:limit],
                "errors": overview.get("errors") or [],
            },
        }

    def do_chat_list_runs(self, *, arguments, context):
        del context
        from app.services.aniu_service import aniu_service

        run_date = None
        date_text = str(arguments.get("date") or "").strip()
        if date_text:
            try:
                run_date = date.fromisoformat(date_text)
            except ValueError:
                return {
                    "ok": False,
                    "tool_name": "chat_list_runs",
                    "error": "date 参数必须是 YYYY-MM-DD 格式。",
                }

        limit = _clamp_int(arguments.get("limit"), default=10, minimum=1, maximum=50)
        status = str(arguments.get("status") or "").strip() or None
        before_id = arguments.get("before_id")
        if before_id is not None:
            before_id = _clamp_int(before_id, default=1, minimum=1, maximum=10**9)

        with session_scope() as db:
            page = aniu_service.list_runs_page(
                db,
                limit=limit,
                run_date=run_date,
                status=status,
                before_id=before_id,
            )

        items = [
            self._serialize_run_summary(item)
            for item in page["items"]
        ]
        return {
            "ok": True,
            "tool_name": "chat_list_runs",
            "summary": f"已读取 {len(items)} 条任务记录。",
            "result": {
                "items": items,
                "has_more": bool(page.get("has_more")),
                "next_before_id": page.get("next_before_id"),
            },
        }

    def do_chat_get_run_detail(self, *, arguments, context):
        del context
        from app.services.aniu_service import aniu_service

        run_id = arguments.get("run_id")
        try:
            run_id = int(run_id)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "tool_name": "chat_get_run_detail",
                "error": "run_id 必须是整数。",
            }

        include_tool_previews = _bool_arg(arguments.get("include_tool_previews", False))
        with session_scope() as db:
            run = aniu_service.get_run(db, run_id)

        if run is None:
            return {
                "ok": False,
                "tool_name": "chat_get_run_detail",
                "error": f"运行记录不存在: {run_id}",
            }

        detail = RunDetailRead.model_validate(run).model_dump(mode="json")
        result = {
            "id": detail["id"],
            "trigger_source": detail["trigger_source"],
            "run_type": detail["run_type"],
            "schedule_name": detail["schedule_name"],
            "status": detail["status"],
            "started_at": detail["started_at"],
            "finished_at": detail["finished_at"],
            "analysis_summary": _truncate_text(detail.get("analysis_summary"), limit=2000),
            "final_answer": _truncate_text(detail.get("final_answer"), limit=12000),
            "output_markdown": _truncate_text(detail.get("output_markdown"), limit=12000),
            "error_message": _truncate_text(detail.get("error_message"), limit=2000),
            "api_call_count": detail["api_call_count"],
            "executed_trade_count": detail["executed_trade_count"],
            "input_tokens": detail["input_tokens"],
            "output_tokens": detail["output_tokens"],
            "total_tokens": detail["total_tokens"],
            "api_details": detail.get("api_details") or [],
            "trade_details": detail.get("trade_details") or [],
            "trade_orders": detail.get("trade_orders") or [],
        }

        if include_tool_previews:
            previews: list[dict[str, Any]] = []
            for item in detail.get("raw_tool_previews") or []:
                previews.append(
                    {
                        **item,
                        "preview": _truncate_text(item.get("preview"), limit=4000),
                    }
                )
            result["raw_tool_previews"] = previews

        return {
            "ok": True,
            "tool_name": "chat_get_run_detail",
            "summary": f"已读取任务 #{run_id} 的详细内容。",
            "result": result,
        }

    @staticmethod
    def _serialize_run_summary(run: Any) -> dict[str, Any]:
        item = RunSummaryRead.model_validate(run).model_dump(mode="json")
        item["analysis_summary"] = _truncate_text(item.get("analysis_summary"), limit=1000)
        item["error_message"] = _truncate_text(item.get("error_message"), limit=1000)
        item["content_preview"] = _summary_text(item)
        return item
