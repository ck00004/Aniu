from __future__ import annotations

import re
from typing import Any, Callable

from skills.mx_core.client import MXClient
from skills.mx_core.tool_specs import TOOL_PROFILES, TOOL_SPECS, build_tools


ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("401", "API Key 可能错误、失效或未正确配置，请检查 MX_APIKEY。"),
    ("API密钥不存在", "API Key 可能错误、失效或未正确配置，请检查 MX_APIKEY。"),
    ("code=113", "今日调用次数可能已达上限，请前往妙想 Skills 页面获取更多调用次数。"),
    ("今日调用次数已达上限", "今日调用次数可能已达上限，请前往妙想 Skills 页面获取更多调用次数。"),
    ("Connection refused", "当前网络可能无法访问东方财富妙想接口，请检查网络或稍后重试。"),
    ("connect:", "当前网络可能无法访问东方财富妙想接口，请检查网络或稍后重试。"),
    ("未绑定模拟组合账户", "当前账户可能尚未绑定模拟组合，请先在妙想 Skills 页面创建并绑定模拟账户。"),
    ("code=404", "当前账户可能尚未绑定模拟组合，请先在妙想 Skills 页面创建并绑定模拟账户。"),
    ("No dataTable found", "本次查询没有返回可用数据表，请放宽查询条件或到东方财富妙想 AI 页面确认查询方式。"),
    ("筛选结果为空", "本次筛选没有匹配到股票，请放宽选股条件。"),
)


class MXExecutionService:
    def __init__(self) -> None:
        self._tool_specs = TOOL_SPECS
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "mx_query_market": self._handle_query_market,
            "mx_search_news": self._handle_search_news,
            "mx_screen_stocks": self._handle_screen_stocks,
            "mx_get_positions": self._handle_get_positions,
            "mx_get_balance": self._handle_get_balance,
            "mx_get_orders": self._handle_get_orders,
            "mx_get_self_selects": self._handle_get_self_selects,
            "mx_manage_self_select": self._handle_manage_self_select,
            "mx_moni_trade": self._handle_moni_trade,
            "mx_moni_cancel": self._handle_moni_cancel,
        }

    def build_tools(self, run_type: str | None = None) -> list[dict[str, Any]]:
        return build_tools(run_type=run_type)

    def execute_tool(
        self,
        *,
        client: MXClient,
        app_settings: Any,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return {
                "ok": False,
                "tool_name": tool_name,
                "error": f"未知工具调用: {tool_name}",
            }

        try:
            return handler(
                client=client, app_settings=app_settings, arguments=arguments
            )
        except Exception as exc:
            guidance = self._build_error_guidance(str(exc))
            return {
                "ok": False,
                "tool_name": tool_name,
                "error": f"{str(exc)}{guidance}",
            }

    def _handle_query_market(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        query = self._resolve_query(arguments, app_settings)
        result = client.query_market(query)
        return {
            "ok": True,
            "tool_name": "mx_query_market",
            "summary": f"已查询市场数据：{query}。",
            "result": result,
        }

    def _handle_search_news(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        query = self._resolve_query(arguments, app_settings)
        result = client.search_news(query)
        return {
            "ok": True,
            "tool_name": "mx_search_news",
            "summary": f"已查询资讯：{query}。",
            "result": result,
        }

    def _handle_screen_stocks(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        query = self._resolve_query(arguments, app_settings)
        result = client.screen_stocks(query)
        return {
            "ok": True,
            "tool_name": "mx_screen_stocks",
            "summary": f"已执行选股：{query}。",
            "result": result,
        }

    def _handle_get_positions(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del app_settings, arguments
        result = client.get_positions()
        return {
            "ok": True,
            "tool_name": "mx_get_positions",
            "summary": "已查询持仓。",
            "result": result,
        }

    def _handle_get_balance(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del app_settings, arguments
        result = client.get_balance()
        return {
            "ok": True,
            "tool_name": "mx_get_balance",
            "summary": "已查询账户资金。",
            "result": result,
        }

    def _handle_get_orders(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del app_settings, arguments
        result = client.get_orders()
        return {
            "ok": True,
            "tool_name": "mx_get_orders",
            "summary": "已查询委托记录。",
            "result": result,
        }

    def _handle_get_self_selects(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del app_settings, arguments
        result = client.get_self_selects()
        return {
            "ok": True,
            "tool_name": "mx_get_self_selects",
            "summary": "已查询自选股列表。",
            "result": result,
        }

    def _handle_manage_self_select(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        query = self._resolve_query(arguments, app_settings)
        self._ensure_single_self_select_target(query)
        result = client.manage_self_select(query)
        return {
            "ok": True,
            "tool_name": "mx_manage_self_select",
            "summary": f"已执行自选股操作：{query}",
            "result": result,
            "executed_action": {
                "action": "MANAGE_SELF_SELECT",
                "query": query,
            },
        }

    def _handle_moni_trade(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del app_settings
        action = str(arguments.get("action") or "").upper()
        symbol = str(arguments.get("symbol") or "").strip()
        price_type = str(arguments.get("price_type") or "MARKET").upper()
        quantity = int(arguments.get("quantity") or 0)
        price = arguments.get("price")
        reason = str(arguments.get("reason") or "").strip()

        if action not in {"BUY", "SELL"}:
            raise RuntimeError("模拟交易工具的 action 只能是 BUY 或 SELL。")
        self._ensure_single_trade_symbol(symbol)
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

        result = client.trade(
            action=action,
            symbol=symbol,
            quantity=quantity,
            price_type=price_type,
            price=price,
        )
        return {
            "ok": True,
            "tool_name": "mx_moni_trade",
            "summary": f"已提交{action}委托：{symbol} {quantity} 股。",
            "result": result,
            "executed_action": {
                "symbol": symbol,
                "name": str(arguments.get("name") or "").strip(),
                "action": action,
                "quantity": quantity,
                "price_type": price_type,
                "price": price,
                "reason": reason,
            },
        }

    def _handle_moni_cancel(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del app_settings
        cancel_type = str(arguments.get("cancel_type") or "").strip().lower()
        order_id = str(arguments.get("order_id") or "").strip() or None
        stock_code = str(arguments.get("stock_code") or "").strip() or None
        reason = str(arguments.get("reason") or "").strip()

        if cancel_type != "order":
            raise RuntimeError("撤单一次只能按委托单号撤一笔，不允许 all 批量撤单。")
        if not order_id:
            raise RuntimeError("按委托编号撤单时必须提供 order_id。")

        result = client.cancel_order(
            cancel_type=cancel_type,
            order_id=order_id,
            stock_code=stock_code,
        )
        return {
            "ok": True,
            "tool_name": "mx_moni_cancel",
            "summary": f"已提交撤单请求：{order_id}",
            "result": result,
            "executed_action": {
                "action": "CANCEL",
                "cancel_type": cancel_type,
                "order_id": order_id,
                "stock_code": stock_code,
                "reason": reason,
            },
        }

    def _resolve_query(self, arguments: dict[str, Any], app_settings: Any) -> str:
        query = str(arguments.get("query") or "").strip()
        if query:
            return query
        fallback = str(getattr(app_settings, "task_prompt", "") or "").strip()
        if fallback:
            return fallback
        raise RuntimeError("缺少 query 参数。")

    def _ensure_single_self_select_target(self, query: str) -> None:
        normalized = re.sub(r"\s+", "", str(query or ""))
        if not normalized:
            raise RuntimeError("自选股操作缺少目标股票。")

        patterns = [
            re.compile(r"(?:把|将)(.+?)(?:加入|添加到?|放入|纳入)(?:我的)?自选(?:股|列表)?"),
            re.compile(r"(?:把|将)(.+?)(?:从)?(?:我的)?自选(?:股|列表)?(?:中)?(?:删除|移除|移出|去掉)"),
            re.compile(r"(?:把|将)(.+?)(?:删除|移除|移出|去掉)(?:出)?(?:我的)?自选(?:股|列表)?"),
            re.compile(r"(.+?)(?:加入|添加到?|放入|纳入)(?:我的)?自选(?:股|列表)?"),
            re.compile(r"(.+?)(?:从)?(?:我的)?自选(?:股|列表)?(?:中)?(?:删除|移除|移出|去掉)"),
        ]

        target_fragment = normalized
        for pattern in patterns:
            matched = pattern.search(normalized)
            if matched:
                target_fragment = matched.group(1)
                break

        if re.search(r"(?:、|,|，|;|；|/|\\|和|及|以及|与)", target_fragment):
            raise RuntimeError(
                "一次只能添加或删除一只自选股；如需操作多只股票，请多次调用 mx_manage_self_select。"
            )

        stock_codes = {
            item.group(0)
            for item in re.finditer(r"\d{6}(?:\.(?:SH|SZ))?", target_fragment, re.IGNORECASE)
        }
        if len(stock_codes) > 1:
            raise RuntimeError(
                "一次只能添加或删除一只自选股；如需操作多只股票，请多次调用 mx_manage_self_select。"
            )

    def _ensure_single_trade_symbol(self, symbol: str) -> None:
        text = str(symbol or "").strip()
        if not text:
            return

        if re.search(r"(?:、|,|，|;|；|/|\\)\s*", text):
            raise RuntimeError(
                "一次只能交易一只股票；如需交易多只股票，请多次调用 mx_moni_trade。"
            )

        stock_codes = {
            item.group(0)
            for item in re.finditer(r"\d{6}(?:\.(?:SH|SZ))?", text, re.IGNORECASE)
        }
        if len(stock_codes) > 1:
            raise RuntimeError(
                "一次只能交易一只股票；如需交易多只股票，请多次调用 mx_moni_trade。"
            )

    def _build_error_guidance(self, message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return ""
        for needle, hint in ERROR_HINTS:
            if needle in text:
                return f"；建议：{hint}"
        return ""


mx_execution_service = MXExecutionService()
