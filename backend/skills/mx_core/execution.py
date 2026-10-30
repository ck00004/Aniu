from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from skills.mx_core.client import (
    MXBusinessRejected,
    MXClient,
    MXResponseUnknown,
)
from skills.mx_core.tool_specs import TOOL_PROFILES, TOOL_SPECS, build_tools

logger = logging.getLogger(__name__)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
# A 股交易时段（分钟数，含边界）。上午从 09:15 起：09:15-09:25 集合竞价
# 可挂单参与撮合，09:25-09:30 可挂单等待开盘连续撮合，定时任务可从
# 09:15 开始触发（2026-10 调整，原起点 09:30）。
_MARKET_SESSIONS: tuple[tuple[int, int], ...] = (
    (9 * 60 + 15, 11 * 60 + 30),
    (13 * 60, 15 * 60),
)
# 委托仍占用资金/份额的状态（已报/未成交/处理中）
_PENDING_ORDER_STATUSES = {"1", "2", "8", "100"}

# 自选股操作互斥锁：读取 → 判断 → 写入 → 回查必须串行执行，防止两个
# 并发任务同时读到"不存在"然后同时发送 ADD（2026-10 外部审计）。
_SELF_SELECT_OP_LOCK = threading.Lock()

# 自选股操作的目标片段提取模式（意图解析与单目标校验共用）
_SELF_SELECT_TARGET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:把|将)(.+?)(?:加入|添加到?|放入|纳入)(?:我的)?自选(?:股|列表)?"),
    re.compile(r"(?:把|将)(.+?)(?:从)?(?:我的)?自选(?:股|列表)?(?:中)?(?:删除|移除|移出|去掉)"),
    re.compile(r"(?:把|将)(.+?)(?:删除|移除|移出|去掉)(?:出)?(?:我的)?自选(?:股|列表)?"),
    re.compile(r"(.+?)(?:加入|添加到?|放入|纳入)(?:我的)?自选(?:股|列表)?"),
    re.compile(r"(.+?)(?:从)?(?:我的)?自选(?:股|列表)?(?:中)?(?:删除|移除|移出|去掉)"),
)


def _normalize_symbol(value: Any) -> str:
    """归一化标的代码：去掉市场后缀（600519.SH → 600519）。"""
    return str(value or "").strip().split(".")[0].strip()


def _payload_rows(payload: Any) -> list[dict[str, Any]]:
    source = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(source, dict):
        rows = (
            source.get("rows")
            or source.get("list")
            or source.get("data")
            or source.get("orderList")
            or source.get("orders")
            # 持仓接口的真实列表键是 posList（生产 2026-10-19 排查确认：
            # 漏掉该键导致卖出预检全部误判"未找到持仓"，当日零成交）
            or source.get("posList")
            or source.get("positionList")
            or []
        )
    elif isinstance(source, list):
        rows = source
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _row_int(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0


def _row_str(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def _is_sell_side(row: dict[str, Any]) -> bool:
    side = _row_str(row, "orderDrt", "drt", "bsFlag", "side", "tradeType")
    return side in {"2", "SELL", "sell", "S"}


def _order_status(row: dict[str, Any]) -> str:
    status = _row_str(row, "orderStatus", "status", "dbStatus")
    return status or "0"


def is_market_open() -> bool:
    """A 股是否处于可交易时段（上海时区，上午含集合竞价）。

    交易日历不可用时退化为"周一至周五 + 时段"启发式并记录告警，
    避免日历故障完全阻断交易（节假日废单由远端拒绝兜底）。
    """
    now = datetime.now(_SHANGHAI_TZ)
    try:
        from app.services.trading_calendar_service import trading_calendar_service

        if not trading_calendar_service.is_trading_day(now.date()):
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-open check fell back to weekday heuristic: %s", exc)
        if now.weekday() >= 5:
            return False
    minutes = now.hour * 60 + now.minute
    return any(start <= minutes <= end for start, end in _MARKET_SESSIONS)


def preflight_trade(
    *,
    client: MXClient,
    action: str,
    symbol: str,
    quantity: int,
    price_type: str,
    price: float | None,
) -> None:
    """交易预检（移植自原版 Aniu mx_agent_tools.py `_preflight_trade`）。

    买入（限价）：核对可用资金；买入（全部）：核对同标的是否存在
    未完成买单——防止跨轮重复委托被动放大敞口（2026-10 外部审计
    P0：同一标的已有在途买单时再次买入必须先撤单或等待成交）；
    卖出：核对可卖数量（持仓 - 在途卖出委托占用）。市价买入因价格
    未知不做资金预检，由远端拒绝兜底。
    """
    normalized_symbol = _normalize_symbol(symbol)

    if action.upper() == "BUY":
        pending_buy = None
        for row in _payload_rows(client.get_orders()):
            if _normalize_symbol(
                _row_str(row, "stockCode", "code", "SECURITY_CODE", "secCode")
            ) != normalized_symbol:
                continue
            if _is_sell_side(row):
                continue
            status = _order_status(row)
            remaining = max(
                _row_int(row, "orderCount", "count", "quantity", "orderQty")
                - _row_int(row, "dealCount", "tradeCount", "filledCount", "filledQty"),
                0,
            )
            if status in _PENDING_ORDER_STATUSES and remaining > 0:
                pending_buy = row
                break
        if pending_buy is not None:
            raise RuntimeError(
                f"{symbol} 已存在未完成买入委托（在途买入），禁止重复提交同方向"
                "委托；如需调整仓位请先撤单或等待该委托成交后再评估"
            )
        if price_type.upper() == "LIMIT" and price is not None:
            balance_payload = client.get_balance()
            balance = (
                balance_payload.get("data")
                if isinstance(balance_payload, dict)
                else {}
            )
            available_cash = 0.0
            if isinstance(balance, dict):
                for key in ("availBalance", "availableBalance", "availableMoney", "balance"):
                    value = balance.get(key)
                    if value is None:
                        continue
                    try:
                        available_cash = float(value)
                        break
                    except (TypeError, ValueError):
                        continue
            required_cash = float(price) * int(quantity)
            if required_cash > available_cash:
                raise RuntimeError(
                    f"买入所需资金 {required_cash:.2f} 元超过当前可用资金 "
                    f"{available_cash:.2f} 元"
                )
        return

    # SELL：可卖 = 持仓 - 在途卖出委托占用
    positions_payload = client.get_positions()
    position_row = next(
        (
            row
            for row in _payload_rows(positions_payload)
            if _normalize_symbol(
                _row_str(row, "stockCode", "code", "SECURITY_CODE", "secCode")
            )
            == normalized_symbol
        ),
        None,
    )
    if position_row is None:
        raise RuntimeError(f"卖出数量 {quantity} 超过 {symbol} 可卖数量 0（未找到该标的持仓）")

    available = _row_int(position_row, "availCount", "availableCount", "enableQty")
    if available > 0:
        if quantity > available:
            raise RuntimeError(
                f"卖出数量 {quantity} 超过 {symbol} 可卖数量 {available}"
            )
        return

    # 持仓未提供可卖字段时，用 持仓数量 - 在途卖出占用 推算
    held = _row_int(position_row, "count", "quantity", "orderQty", "volume")
    orders_payload = client.get_orders()
    occupied = 0
    for row in _payload_rows(orders_payload):
        if not _is_sell_side(row):
            continue
        if _normalize_symbol(
            _row_str(row, "stockCode", "secCode", "code")
        ) != normalized_symbol:
            continue
        status = _order_status(row)
        remaining = max(
            _row_int(row, "orderCount", "count", "quantity", "orderQty")
            - _row_int(row, "dealCount", "tradeCount", "filledCount", "filledQty"),
            0,
        )
        if status in _PENDING_ORDER_STATUSES:
            occupied += remaining
        elif status == "0" and remaining > 0:
            raise RuntimeError(
                f"无法确认 {symbol} 可卖数量；请求卖出 {quantity} 股，"
                "请先确认未完成卖出委托"
            )
    available = max(held - occupied, 0)
    if quantity > available:
        raise RuntimeError(
            f"卖出数量 {quantity} 超过 {symbol} 可卖数量 {available}"
            f"（含在途卖出委托占用 {occupied}）"
        )


def preflight_cancel(*, client: MXClient, order_id: str, stock_code: str | None) -> None:
    """撤单预检（移植自原版 Aniu mx_agent_tools.py `_preflight_cancel`）。"""
    orders_payload = client.get_orders()
    order_row = next(
        (
            row
            for row in _payload_rows(orders_payload)
            if _row_str(row, "orderId", "entrustNo", "id") == str(order_id).strip()
        ),
        None,
    )
    if order_row is None:
        raise RuntimeError(f"未找到委托编号 {order_id}；请先查询委托确认编号。")

    if stock_code:
        order_symbol = _normalize_symbol(
            _row_str(order_row, "stockCode", "secCode", "code")
        )
        if order_symbol and order_symbol != _normalize_symbol(stock_code):
            raise RuntimeError(
                f"委托编号 {order_id} 对应股票 {order_symbol}，与 {stock_code} 不一致"
            )

    status = _order_status(order_row)
    order_qty = _row_int(order_row, "orderCount", "count", "quantity", "orderQty")
    filled_qty = _row_int(
        order_row, "dealCount", "tradeCount", "filledCount", "filledQty"
    )
    # 非待撤状态（已成交/已撤单/已完成/未知）一律不可撤——
    # 旧条件额外要求"已全部成交"，已撤单（零成交）会被漏判为可撤
    # （生产结构回归测试发现）。
    if status not in _PENDING_ORDER_STATUSES:
        del order_qty, filled_qty
        raise RuntimeError(
            f"委托编号 {order_id} 当前状态为 {status}，不可撤销"
        )


ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("401", "API Key 可能错误、失效或未正确配置，请检查 MX_APIKEY。"),
    ("API密钥不存在", "API Key 可能错误、失效或已过期，请前往妙想 Skills 页面重新获取。"),
    ("code=114", "API Key 可能错误、失效或已过期，请前往妙想 Skills 页面重新获取并更新。"),
    ("code=113", "今日调用次数可能已达上限，请前往妙想 Skills 页面获取更多调用次数。"),
    ("今日调用次数已达上限", "今日调用次数可能已达上限，请前往妙想 Skills 页面获取更多调用次数。"),
    ("Connection refused", "当前网络可能无法访问东方财富妙想接口，请检查网络或稍后重试。"),
    ("connect:", "当前网络可能无法访问东方财富妙想接口，请检查网络或稍后重试。"),
    ("未绑定模拟组合账户", "当前账户可能尚未绑定模拟组合，请先在妙想 Skills 页面创建并绑定模拟账户。"),
    ("code=404", "当前账户可能尚未绑定模拟组合，请先在妙想 Skills 页面创建并绑定模拟账户。"),
    ("No dataTable found", "本次查询没有返回可用数据表，请放宽查询条件或到东方财富妙想 AI 页面确认查询方式。"),
    ("筛选结果为空", "本次筛选没有匹配到股票，请放宽选股条件。"),
    ("不可交易", "委托失败可能与交易时段有关，请在 A 股连续竞价时段（9:30-11:30、13:00-15:00）重试。"),
    (
        "一次只能添加或删除一只自选股",
        "请把每只股票拆成一次单独的 mx_manage_self_select 调用，不要在同一次调用里包含多个股票。",
    ),
    (
        "一次只能交易一只股票",
        "请把每只股票拆成一次单独的 mx_moni_trade 调用，不要在同一次调用里包含多个股票。",
    ),
    (
        "撤单一次只能按委托单号撤一笔",
        "如需撤多笔，请多次调用 mx_moni_cancel，每次只传一个 order_id。",
    ),
)


def _format_query_market_result(result: Any) -> dict[str, Any]:
    """把 mx_query_market 的原始响应格式化为紧凑的结构化结果。

    移植自官方 mx-data 参考实现（table_to_rows + format_terminal）：
    dataTableDTOList 的 table 结构为「headName=时间/维度列 + 每个指标键
    对应等长数组」，nameMap 把指标编码映射为业务中文名。原始 JSON 直接
    交给模型既浪费上下文又难以解析，格式化为 Markdown 表格后可读性
    与 token 效率都显著更好。
    """
    if not isinstance(result, dict):
        return {"ok": False, "error": "查询返回结构异常。"}

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    search_result = (
        inner.get("searchDataResultDTO")
        if isinstance(inner.get("searchDataResultDTO"), dict)
        else {}
    )
    dto_list = search_result.get("dataTableDTOList")
    if not isinstance(dto_list, list) or not dto_list:
        return {
            "ok": False,
            "error": "接口返回中无 dataTableDTOList，请放宽查询条件或确认查询内容。",
        }

    def _flatten(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _label(key: str, name_map: dict, code_map: dict) -> str:
        mapped = name_map.get(key)
        if mapped is None and key.isdigit():
            mapped = name_map.get(int(key))
        if mapped not in (None, ""):
            return _flatten(mapped)
        mapped_code = code_map.get(key)
        if mapped_code not in (None, ""):
            return _flatten(mapped_code)
        return "" if key.isdigit() else key

    tables: list[dict[str, Any]] = []
    conditions: list[str] = []
    total_rows = 0
    for index, dto in enumerate(dto_list):
        if not isinstance(dto, dict):
            continue
        title = str(
            dto.get("title") or dto.get("inputTitle") or dto.get("entityName")
            or f"表{index + 1}"
        )[:80]
        condition = dto.get("condition")
        if condition not in (None, ""):
            conditions.append(f"[{dto.get('entityName') or title}] {condition}")

        table = dto.get("table") if isinstance(dto.get("table"), dict) else {}
        name_map = dto.get("nameMap") if isinstance(dto.get("nameMap"), dict) else {}
        if isinstance(name_map, list):
            name_map = {str(i): v for i, v in enumerate(name_map)}
        code_map: dict = {}
        for key in ("returnCodeMap", "returnCodeNameMap", "codeMap"):
            raw = dto.get(key)
            if isinstance(raw, dict):
                code_map = {str(k): _flatten(v) for k, v in raw.items()}
                break

        headers = table.get("headName")
        if not isinstance(headers, list) or not headers:
            continue
        order = [
            key
            for key in table.keys()
            if key != "headName"
        ]
        indicator_order = dto.get("indicatorOrder")
        if isinstance(indicator_order, list):
            preferred = [str(k) for k in indicator_order]
            order = sorted(
                order,
                key=lambda key: (
                    preferred.index(str(key)) if str(key) in preferred else len(preferred)
                ),
            )

        columns = ["日期"]
        for key in order:
            label = _label(str(key), name_map, code_map)
            if label:
                columns.append(label)

        rows: list[list[str]] = []
        for row_index, head in enumerate(headers):
            row = [_flatten(head)]
            for key in order:
                label = _label(str(key), name_map, code_map)
                if not label:
                    continue
                values = table.get(key)
                value = (
                    values[row_index]
                    if isinstance(values, list) and row_index < len(values)
                    else ""
                )
                row.append(_flatten(value))
            rows.append(row)

        preview_rows = rows[:20]
        markdown_lines = [
            f"**{title}**（共 {len(rows)} 行，预览前 {len(preview_rows)} 行）",
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join(["---"] * len(columns)) + " |",
        ]
        for row in preview_rows:
            markdown_lines.append("| " + " | ".join(row) + " |")
        if len(rows) > len(preview_rows):
            markdown_lines.append(f"| …（其余 {len(rows) - len(preview_rows)} 行已省略）" + " |" * (len(columns) - 1))

        tables.append(
            {
                "title": title,
                "columns": columns,
                "row_count": len(rows),
                "truncated": len(rows) > len(preview_rows),
                "markdown": "\n".join(markdown_lines),
            }
        )
        total_rows += len(rows)

    if not tables:
        return {
            "ok": False,
            "error": "dataTableDTOList 中无有效 table 数据，请放宽查询条件。",
        }

    entities = []
    entity_tags = search_result.get("entityTagDTOList")
    if isinstance(entity_tags, list):
        for tag in entity_tags:
            if not isinstance(tag, dict):
                continue
            entities.append(
                {
                    "name": str(tag.get("fullName") or ""),
                    "code": str(tag.get("secuCode") or ""),
                    "type": str(tag.get("entityTypeName") or ""),
                }
            )

    return {
        "ok": True,
        "question_id": str(search_result.get("questionId") or ""),
        "entities": entities,
        "table_count": len(tables),
        "total_rows": total_rows,
        "conditions": conditions,
        "tables": tables,
    }


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
        run_id: int | None = None,
    ) -> dict[str, Any]:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return {
                "ok": False,
                "tool_name": tool_name,
                "error": f"未知工具调用: {tool_name}",
            }

        try:
            try:
                return handler(
                    client=client,
                    app_settings=app_settings,
                    arguments=arguments,
                    run_id=run_id,
                )
            except TypeError:
                # 其余 handler 不接受 run_id 参数
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
        # 官方 mx-data 规范：原始 JSON 体积大且结构深，格式化为紧凑的
        # Markdown 表格再交给模型（token 效率与可读性显著更好）。
        formatted = _format_query_market_result(result)
        if formatted.get("ok"):
            return {
                "ok": True,
                "tool_name": "mx_query_market",
                "summary": f"已查询市场数据：{query}。",
                "result": formatted,
            }
        return {
            "ok": False,
            "tool_name": "mx_query_market",
            "error": str(formatted.get("error") or "查询未返回有效数据。"),
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
        with _SELF_SELECT_OP_LOCK:
            return self._handle_manage_self_select_unlocked(
                client=client, app_settings=app_settings, arguments=arguments
            )

    def _handle_manage_self_select_unlocked(
        self, *, client: MXClient, app_settings: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        query = self._resolve_query(arguments, app_settings)
        self._ensure_single_self_select_target(query)

        # 补丁 B（2026-10 外部审计）：自选股操作必须解析出唯一意图
        # （ADD/REMOVE + 代码或名称），并做操作前后状态核验——
        # "接口返回成功"不等于"列表已变更"，未回查确认不得声称完成。
        intent = self._parse_self_select_intent(query)
        if intent is None:
            # 无法唯一确定意图：拒绝执行，不得把模糊表述发给远端。
            raise RuntimeError(
                f"自选股操作指令无法唯一确定新增或移除意图：{query}。"
                "请使用明确的表述（如\"将600519加入自选股\"或"
                "\"将600519从自选股删除\"）重新调用。"
            )
        operation, code, name = intent

        current = self._read_self_select_state(client)
        if current is not None:
            in_list = self._target_in_state(code, name, current)
            if (operation == "ADD" and in_list) or (
                operation == "REMOVE" and not in_list
            ):
                return {
                    "ok": True,
                    "tool_name": "mx_manage_self_select",
                    "summary": (
                        f"{name or code} 已处于目标状态"
                        f"（{'已在' if operation == 'ADD' else '不在'}自选股列表），"
                        "未发送写请求（NOOP_VERIFIED）。"
                    ),
                    "result": {"verified": True, "noop": True},
                    "executed_action": {
                        "action": "MANAGE_SELF_SELECT",
                        "query": query,
                        "verified": True,
                        "noop": True,
                    },
                }

        result = client.manage_self_select(query)

        verified = self._read_self_select_state(client)
        if verified is None:
            raise RuntimeError(
                f"自选股操作已提交但回查失败，状态未知（UNKNOWN）：{query}。"
                "请调用 mx_get_self_selects 核对当前列表，确认前不得重复提交。"
            )
        in_list = self._target_in_state(code, name, verified)
        if (operation == "ADD" and not in_list) or (operation == "REMOVE" and in_list):
            raise RuntimeError(
                f"自选股操作已提交但回查未确认目标状态（UNKNOWN）：{query}。"
                "请调用 mx_get_self_selects 核对当前列表，确认前不得重复提交。"
            )

        return {
            "ok": True,
            "tool_name": "mx_manage_self_select",
            "summary": f"已执行自选股操作并回查确认：{query}",
            "result": result,
            "executed_action": {
                "action": "MANAGE_SELF_SELECT",
                "query": query,
                "verified": True,
            },
        }

    @staticmethod
    def _target_in_state(
        code: str, name: str, state: tuple[set[str], list[str]]
    ) -> bool:
        codes, names = state
        if code:
            return code in codes
        return bool(name) and any(name in row_name for row_name in names)

    def _parse_self_select_intent(
        self, query: str
    ) -> tuple[str, str, str] | None:
        """解析自选股操作意图 → (ADD/REMOVE, 六位代码或空, 名称或空)。

        无法唯一确定方向时返回 None（由调用方拒绝执行）。
        """
        normalized = re.sub(r"\s+", "", str(query or ""))
        has_add = any(
            marker in normalized for marker in ("加入", "添加", "放入", "纳入")
        )
        has_remove = any(
            marker in normalized for marker in ("删除", "移除", "移出", "去掉")
        )
        if has_add == has_remove:  # 两者都有（矛盾）或都没有（模糊）
            return None

        # 目标片段：与 _ensure_single_self_select_target 相同的模式提取，
        # 避免对名称做逐字剔除（"中芯国际"的"中"不能被当结构词删掉）。
        target_fragment = normalized
        for pattern in _SELF_SELECT_TARGET_PATTERNS:
            matched = pattern.search(normalized)
            if matched:
                target_fragment = matched.group(1)
                break

        codes = re.findall(r"\d{6}", target_fragment)
        code = codes[0] if len(codes) == 1 else ""
        name = re.sub(r"\d{6}", "", target_fragment).strip("，。,.;；:：()（）") or ""
        if not code and not name:
            return None
        return ("ADD" if has_add else "REMOVE", code, name)

    def _read_self_select_state(
        self, client: MXClient
    ) -> tuple[set[str], list[str]] | None:
        """读取当前自选股列表 → (代码集合, 名称列表)；读取失败返回 None。"""
        try:
            payload = client.get_self_selects()
        except Exception:  # noqa: BLE001
            return None
        rows = _payload_rows(payload)
        if not isinstance(rows, list):
            return None
        codes: set[str] = set()
        names: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_code = _normalize_symbol(
                _row_str(row, "stockCode", "code", "SECURITY_CODE", "secCode")
            )
            if row_code:
                codes.add(row_code)
            row_name = _row_str(row, "stockName", "secName", "name", "stock_name")
            if row_name:
                names.append(row_name)
        return codes, names

    def _handle_moni_trade(
        self,
        *,
        client: MXClient,
        app_settings: Any,
        arguments: dict[str, Any],
        run_id: int | None = None,
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

        # 交易时段写门禁（移植自原版 Aniu agent_runner.py 的 authorize）：
        # 非交易时段直接阻止交易/撤单写操作，避免闭市委托必然失败。
        if not is_market_open():
            raise RuntimeError(
                "非交易时段，交易和撤单写操作已被阻止；仍可继续分析。"
            )
        preflight_trade(
            client=client,
            action=action,
            symbol=symbol,
            quantity=quantity,
            price_type=price_type,
            price=price,
        )

        # 补丁 C：跨轮交易意图挡板（同信号去重 / 成交后冷却 / 分批上限 /
        # UNKNOWN 核对）。被拦截时抛错，模型在工具结果中看到拦截原因。
        from app.services.trade_intent_service import (
            extract_order_id,
            trade_intent_service,
        )

        intent = trade_intent_service.check_and_register_trade_intent(
            client=client,
            symbol=symbol,
            side=action,
            quantity=quantity,
            run_id=run_id,
        )

        try:
            result = client.trade(
                action=action,
                symbol=symbol,
                quantity=quantity,
                price_type=price_type,
                price=price,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # 超时/传输中断时远端可能已接受委托（状态未知）：绝不自动
            # 重发。意图置为 UNKNOWN 阻塞同标的同方向的后续提交，直到
            # 通过 mx_get_orders 核对收敛。
            trade_intent_service.mark_intent_unknown(
                intent.id, f"交易请求超时/传输中断：{exc}"
            )
            raise RuntimeError(
                f"委托状态未知（{type(exc).__name__}）：{symbol} {action} "
                f"{quantity} 股的请求已发出但未收到确认，可能已提交也可能"
                "未提交。请先调用 mx_get_orders 核对该委托状态，确认前"
                "不得重复提交同方向委托。"
            ) from exc
        except MXBusinessRejected as exc:
            # 远端明确拒绝（业务错误 status/code）：意图确认未生效，
            # 不阻塞后续重试。
            trade_intent_service.mark_intent_failed(
                intent.id, f"远端明确拒绝该委托：{exc}"
            )
            raise
        except MXResponseUnknown as exc:
            # HTTP 200 但响应体无法解析：远端可能已接受，状态不确定。
            trade_intent_service.mark_intent_unknown(
                intent.id, f"交易响应无法解析，状态不确定：{exc}"
            )
            raise RuntimeError(
                f"委托状态未知（响应无法解析）：{symbol} {action} "
                f"{quantity} 股的请求已发出但响应体无法确认结果。"
                "请先调用 mx_get_orders 核对该委托状态，确认前"
                "不得重复提交同方向委托。"
            ) from exc
        except Exception as exc:
            # 未分类异常（请求已发出之后）：不能证明远端拒绝——
            # 一律按状态未知处理，绝不放行重试（第三轮复审 P0）。
            trade_intent_service.mark_intent_unknown(
                intent.id, f"交易请求未分类异常，状态不确定：{exc}"
            )
            raise RuntimeError(
                f"委托状态未知（未分类异常 {type(exc).__name__}）：{symbol} "
                f"{action} {quantity} 股的请求已发出但结果无法确认。"
                "请先调用 mx_get_orders 核对该委托状态，确认前"
                "不得重复提交同方向委托。"
            ) from exc

        trade_intent_service.mark_intent_submitted(
            intent.id, extract_order_id(result)
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
        self,
        *,
        client: MXClient,
        app_settings: Any,
        arguments: dict[str, Any],
        run_id: int | None = None,
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

        # 交易时段写门禁 + 撤单预检（委托存在性/一致性/可撤状态）。
        if not is_market_open():
            raise RuntimeError(
                "非交易时段，交易和撤单写操作已被阻止；仍可继续分析。"
            )
        preflight_cancel(client=client, order_id=order_id, stock_code=stock_code)

        # 补丁 C：撤单意图按委托单号全局唯一（一单最多一个撤单意图，
        # 防止多个 15 分钟任务对同一委托重复撤单）。
        from app.services.trade_intent_service import (
            extract_order_id,
            trade_intent_service,
        )

        intent = trade_intent_service.check_and_register_cancel_intent(
            order_id=order_id,
            run_id=run_id,
        )

        try:
            result = client.cancel_order(
                cancel_type=cancel_type,
                order_id=order_id,
                stock_code=stock_code,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            trade_intent_service.mark_intent_unknown(
                intent.id, f"撤单请求超时/传输中断：{exc}"
            )
            raise RuntimeError(
                f"撤单状态未知（{type(exc).__name__}）：委托 {order_id} 的撤单"
                "请求已发出但未收到确认。请调用 mx_get_orders 核对该委托"
                "状态，确认前不得重复提交撤单。"
            ) from exc
        except MXBusinessRejected as exc:
            trade_intent_service.mark_intent_failed(
                intent.id, f"远端明确拒绝撤单：{exc}"
            )
            raise
        except MXResponseUnknown as exc:
            trade_intent_service.mark_intent_unknown(
                intent.id, f"撤单响应无法解析，状态不确定：{exc}"
            )
            raise RuntimeError(
                f"撤单状态未知（响应无法解析）：委托 {order_id} 的撤单请求"
                "已发出但响应体无法确认结果。请调用 mx_get_orders 核对该"
                "委托状态，确认前不得重复提交撤单。"
            ) from exc
        except Exception as exc:
            # 未分类异常（请求已发出之后）：一律按状态未知处理
            trade_intent_service.mark_intent_unknown(
                intent.id, f"撤单请求未分类异常，状态不确定：{exc}"
            )
            raise RuntimeError(
                f"撤单状态未知（未分类异常 {type(exc).__name__}）：委托 "
                f"{order_id} 的撤单请求已发出但结果无法确认。请调用 "
                "mx_get_orders 核对该委托状态，确认前不得重复提交撤单。"
            ) from exc

        trade_intent_service.mark_intent_submitted(
            intent.id, extract_order_id(result)
        )
        # 撤单结果核验（尽力而为）：撤单请求 ≠ 撤单完成。
        verify_note = self._verify_cancel_result(client, order_id)
        return {
            "ok": True,
            "tool_name": "mx_moni_cancel",
            "summary": f"已提交撤单请求：{order_id}。{verify_note}",
            "result": result,
            "executed_action": {
                "action": "CANCEL",
                "cancel_type": cancel_type,
                "order_id": order_id,
                "stock_code": stock_code,
                "reason": reason,
            },
        }

    def _verify_cancel_result(self, client: MXClient, order_id: str) -> str:
        """撤单后回查委托列表，尽力确认委托是否已离开可撤状态。"""
        try:
            orders_payload = client.get_orders()
        except Exception:  # noqa: BLE001
            return "（回查失败，请调用 mx_get_orders 自行核对该委托状态。）"
        rows = _payload_rows(orders_payload)
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_order_id = _row_str(row, "orderID", "orderId", "order_id", "entrustNo")
            if row_order_id and row_order_id == str(order_id).strip():
                status = _order_status(row)
                if status in _PENDING_ORDER_STATUSES:
                    return "（回查：该委托仍处于待撤状态，请稍后通过 mx_get_orders 核对。）"
                return "（回查：该委托已离开可撤状态——已撤单或已成交。）"
        return "（回查：委托列表中已找不到该委托，通常表示撤单已生效。）"

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

        target_fragment = normalized
        for pattern in _SELF_SELECT_TARGET_PATTERNS:
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
