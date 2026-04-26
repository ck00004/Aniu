from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.mx_skill_service import mx_skill_service
from skills.mx_core.tool_specs import TOOL_SPECS


_MUTATION_TOOL_NAMES = {spec.name for spec in TOOL_SPECS if spec.mutation}


@dataclass(slots=True)
class PlannedActionDraft:
    sequence_no: int
    tool_name: str
    tool_call_id: str | None
    arguments: dict[str, Any]
    action_type: str
    planned_action: dict[str, Any]
    result_summary: str


class ExecutionPlanService:
    def is_mutation_tool(self, tool_name: str) -> bool:
        return str(tool_name or "").strip() in _MUTATION_TOOL_NAMES

    def execute_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str | None,
        context: dict[str, Any],
        sequence_no: int,
    ) -> tuple[dict[str, Any], PlannedActionDraft | None]:
        if not self.is_mutation_tool(tool_name):
            client = context.get("client")
            app_settings = context.get("app_settings")
            result = mx_skill_service.execute_tool(
                client=client,
                app_settings=app_settings,
                tool_name=tool_name,
                arguments=arguments,
            )
            return result, None

        planner = {
            "mx_manage_self_select": self._plan_manage_self_select,
            "mx_moni_trade": self._plan_trade,
            "mx_moni_cancel": self._plan_cancel,
        }.get(str(tool_name or "").strip())
        if planner is None:
            return {
                "ok": False,
                "tool_name": tool_name,
                "error": f"未知计划工具: {tool_name}",
            }, None

        return planner(
            arguments=arguments,
            tool_call_id=tool_call_id,
            sequence_no=sequence_no,
            app_settings=context.get("app_settings"),
        )

    def _plan_manage_self_select(
        self,
        *,
        arguments: dict[str, Any],
        tool_call_id: str | None,
        sequence_no: int,
        app_settings: Any,
    ) -> tuple[dict[str, Any], PlannedActionDraft]:
        query = mx_skill_service._resolve_query(arguments, app_settings)
        mx_skill_service._ensure_single_self_select_target(query)
        planned_action = {
            "action": "MANAGE_SELF_SELECT",
            "query": query,
        }
        summary = f"已生成自选股执行计划：{query}"
        draft = PlannedActionDraft(
            sequence_no=sequence_no,
            tool_name="mx_manage_self_select",
            tool_call_id=tool_call_id,
            arguments=dict(arguments),
            action_type="MANAGE_SELF_SELECT",
            planned_action=planned_action,
            result_summary=summary,
        )
        return {
            "ok": True,
            "tool_name": "mx_manage_self_select",
            "summary": summary,
            "result": {
                "planned": True,
                "stage": "planning",
                "message": summary,
            },
            "planned_action": planned_action,
        }, draft

    def _plan_trade(
        self,
        *,
        arguments: dict[str, Any],
        tool_call_id: str | None,
        sequence_no: int,
        app_settings: Any,
    ) -> tuple[dict[str, Any], PlannedActionDraft]:
        del app_settings
        action = str(arguments.get("action") or "").upper()
        symbol = str(arguments.get("symbol") or "").strip()
        price_type = str(arguments.get("price_type") or "MARKET").upper()
        quantity = int(arguments.get("quantity") or 0)
        price = arguments.get("price")
        reason = str(arguments.get("reason") or "").strip()
        name = str(arguments.get("name") or "").strip()

        if action not in {"BUY", "SELL"}:
            raise RuntimeError("模拟交易工具的 action 只能是 BUY 或 SELL。")
        mx_skill_service._ensure_single_trade_symbol(symbol)
        if not symbol:
            raise RuntimeError("模拟交易工具缺少股票代码。")
        if quantity <= 0:
            raise RuntimeError("模拟交易工具的 quantity 必须大于 0。")
        if quantity % 100 != 0:
            raise RuntimeError("A 股交易数量必须是 100 的整数倍。")
        if price_type not in {"MARKET", "LIMIT"}:
            raise RuntimeError("price_type 只能是 MARKET 或 LIMIT。")
        if price_type == "LIMIT":
            try:
                normalized_price = float(price)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("LIMIT 委托必须提供有效价格。") from exc
            if normalized_price <= 0:
                raise RuntimeError("LIMIT 委托价格必须大于 0。")
            price = normalized_price
        elif price is not None:
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = None

        planned_action = {
            "symbol": symbol,
            "name": name,
            "action": action,
            "quantity": quantity,
            "price_type": price_type,
            "price": price,
            "reason": reason,
        }
        summary = f"已生成交易执行计划：{action} {symbol} {quantity}股。"
        draft = PlannedActionDraft(
            sequence_no=sequence_no,
            tool_name="mx_moni_trade",
            tool_call_id=tool_call_id,
            arguments=dict(arguments),
            action_type=action,
            planned_action=planned_action,
            result_summary=summary,
        )
        return {
            "ok": True,
            "tool_name": "mx_moni_trade",
            "summary": summary,
            "result": {
                "planned": True,
                "stage": "planning",
                "message": summary,
            },
            "planned_action": planned_action,
        }, draft

    def _plan_cancel(
        self,
        *,
        arguments: dict[str, Any],
        tool_call_id: str | None,
        sequence_no: int,
        app_settings: Any,
    ) -> tuple[dict[str, Any], PlannedActionDraft]:
        del app_settings
        cancel_type = str(arguments.get("cancel_type") or "").strip().lower()
        order_id = str(arguments.get("order_id") or "").strip() or None
        stock_code = str(arguments.get("stock_code") or "").strip() or None
        reason = str(arguments.get("reason") or "").strip()

        if cancel_type != "order":
            raise RuntimeError("撤单一次只能按委托单号撤一笔，不允许 all 批量撤单。")
        if not order_id:
            raise RuntimeError("按委托编号撤单时必须提供 order_id。")

        planned_action = {
            "action": "CANCEL",
            "cancel_type": cancel_type,
            "order_id": order_id,
            "stock_code": stock_code,
            "reason": reason,
        }
        summary = f"已生成撤单执行计划：{order_id}"
        draft = PlannedActionDraft(
            sequence_no=sequence_no,
            tool_name="mx_moni_cancel",
            tool_call_id=tool_call_id,
            arguments=dict(arguments),
            action_type="CANCEL",
            planned_action=planned_action,
            result_summary=summary,
        )
        return {
            "ok": True,
            "tool_name": "mx_moni_cancel",
            "summary": summary,
            "result": {
                "planned": True,
                "stage": "planning",
                "message": summary,
            },
            "planned_action": planned_action,
        }, draft


execution_plan_service = ExecutionPlanService()