"""订单数据 Schema 公共模块（第六轮复审 P0：统一解析器）。

execution.py 与 trade_intent_service.py 必须对同一份订单数据得出
相同结论——此前两套独立解析器（宽松过滤非法行、缺失数量默认 0）
会让意图状态同步与交易预检对同一响应做出不同判断。

所有订单数量解析必须经 get_order_quantities()：缺失/格式异常/非整数
一律返回 None，绝不静默转 0（缺失值 ≠ 真实 0）。
"""

from __future__ import annotations

import re
from typing import Any

# 已知方向值（白名单——真实接口验证前不扩大）
_ORDER_SIDE_VALUES = {"1", "2", "BUY", "SELL", "B", "S"}
_SIDE_CODE_MAP = {"1": "BUY", "2": "SELL", "B": "BUY", "S": "SELL"}

# 委托/持仓列表的候选行键（与历史 _payload_rows 对齐）
_LIST_ROW_KEYS = (
    "rows",
    "list",
    "data",
    "orders",
    "orderList",
    "posList",
    "positionList",
)

# 待处理状态码（已报/未成交/处理中）
PENDING_ORDER_STATUSES = {"1", "2", "8", "100"}
# 已知终局状态码白名单（撤单/废单/已结）——未知码一律不可判定
CANCELED_ORDER_STATUSES = {"3", "4", "5", "6", "9", "-1"}

# 解析结果四态
ROWS_VALID_DATA = "VALID_DATA"
ROWS_VALID_EMPTY = "VALID_EMPTY"
ROWS_INVALID = "INVALID"
ROWS_ERROR = "ERROR"


def row_str(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def row_int_optional(row: dict[str, Any], *keys: str) -> int | None:
    """数量字段解析：缺失/格式异常/非整数返回 None。

    int(float(value)) 会把 100.9 静默截断为 100，掩盖数据格式异常——
    订单数量必须是整数，非整数按不可判定处理。
    """
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        text = str(value).strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            return None
        return int(text)
    return None


def get_order_symbol(row: dict[str, Any]) -> str:
    return row_str(row, "stockCode", "code", "SECURITY_CODE", "secCode")


def get_order_status(row: dict[str, Any]) -> str:
    """订单状态码：缺失返回空串（调用方必须显式处理缺失）。"""
    return row_str(row, "orderStatus", "status", "dbStatus")


def get_order_side(row: dict[str, Any]) -> str | None:
    """订单方向 → BUY/SELL；缺失或不在白名单返回 None。"""
    raw = row_str(row, "orderDrt", "drt", "bsFlag", "side", "tradeType")
    if not raw:
        return None
    normalized = raw.upper()
    if normalized in _SIDE_CODE_MAP:
        return _SIDE_CODE_MAP[normalized]
    if normalized in ("BUY", "SELL"):
        return normalized
    return None


def get_order_quantities(row: dict[str, Any]) -> tuple[int | None, int | None]:
    """(委托数量, 成交数量)：任一缺失/异常返回 (None, None)。"""
    ordered = row_int_optional(
        row, "orderCount", "count", "quantity", "orderQty"
    )
    dealt = row_int_optional(
        row, "dealCount", "tradeCount", "filledCount", "filledQty"
    )
    return ordered, dealt


def validate_order_row(row: dict[str, Any]) -> bool:
    """订单行级 Schema 校验（第六轮复审 P0-1/P0-2）。

    必须满足：
    - 股票代码存在；
    - 状态码显式存在；
    - 方向字段存在且在白名单内；
    - 待处理订单：委托数量 > 0、成交数量 0 ≤ dealt ≤ ordered。
    任一不满足 → 整份委托列表 INVALID（不得当作有效空列表放行交易）。
    """
    if not get_order_symbol(row):
        return False
    if not get_order_status(row):
        return False
    if get_order_side(row) is None:
        return False
    status = get_order_status(row)
    if status in PENDING_ORDER_STATUSES:
        ordered, dealt = get_order_quantities(row)
        if ordered is None or dealt is None:
            return False
        if ordered <= 0:
            return False
        if dealt < 0 or dealt > ordered:
            return False
    return True


def parse_order_snapshot(payload: Any) -> tuple[list[dict[str, Any]], str]:
    """解析委托列表载荷 → (行, 状态四态)。

    - 结构级：载荷非 dict → ERROR；已知列表键缺失 → INVALID；
      列表含非 dict 元素 → INVALID。
    - 行级：任一行字段不完整 → INVALID。
    - 只有结构可识别且全行为空 → VALID_EMPTY。
    """
    if not isinstance(payload, dict):
        return [], ROWS_ERROR
    data_value = payload.get("data")
    if isinstance(data_value, list):
        rows = data_value
    else:
        source = data_value if isinstance(data_value, dict) else payload
        if not isinstance(source, dict):
            return [], ROWS_INVALID
        rows = None
        for key in _LIST_ROW_KEYS:
            value = source.get(key)
            if isinstance(value, list):
                rows = value
                break
        if rows is None:
            return [], ROWS_INVALID
    if any(not isinstance(row, dict) for row in rows):
        return [], ROWS_INVALID
    parsed = [row for row in rows if isinstance(row, dict)]
    if parsed and any(not validate_order_row(row) for row in parsed):
        return [], ROWS_INVALID
    return parsed, (ROWS_VALID_DATA if parsed else ROWS_VALID_EMPTY)


def order_terminal_status(row: dict[str, Any]) -> str:
    """按委托行计算意图终态（数量缺失/异常 → unknown，不默认成交）。

    filled / partially_filled / submitted / canceled / unknown。
    """
    ordered, dealt = get_order_quantities(row)
    status = get_order_status(row) or "0"
    if ordered is None or ordered <= 0:
        return "unknown"
    if dealt is None or dealt < 0 or dealt > ordered:
        return "unknown"
    if dealt == ordered:
        return "filled"
    if dealt > 0:
        return "partially_filled"
    if status in PENDING_ORDER_STATUSES:
        return "submitted"
    if status in CANCELED_ORDER_STATUSES:
        return "canceled"
    return "unknown"
