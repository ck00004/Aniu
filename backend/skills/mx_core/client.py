from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import get_settings

# 妙想接口可能以 HTTP 200 + 错误体返回业务错误（配额耗尽、未绑定模拟
# 组合等）。这些标记来自真实错误体的已知特征；命中时抛出包含响应体
# 文本的异常，让 ERROR_HINTS 的引导匹配真正可达。
_BODY_ERROR_MARKERS: tuple[str, ...] = (
    "API密钥不存在",
    "今日调用次数已达上限",
    "未绑定模拟组合账户",
    "No dataTable found",
    "筛选结果为空",
)
_MESSAGE_FIELDS: tuple[str, ...] = (
    "message",
    "msg",
    "error",
    "errorMsg",
    "errorMessage",
    "description",
)
_SUCCESS_CODES = (0, 200)


class MXClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.mx_apikey
        self.base_url = (base_url or settings.mx_api_url).rstrip("/")
        if not self.api_key:
            raise ValueError("未配置 MX_APIKEY，无法调用妙想接口。")
        self._client = httpx.Client(
            timeout=30.0,
            headers={
                "apikey": self.api_key,
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MXClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        # 工具调用日志：记录每次妙想接口调用的状态/耗时/响应规模，
        # 写入失败不影响调用主路径（移植自原版 Aniu stock_api_logs）。
        from app.services.tool_invocation_log_service import instrument_invocation

        with instrument_invocation(endpoint, payload) as invocation:
            response = self._client.post(url, json=payload)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"妙想接口返回了无法解析的响应: {response.text[:200]}"
                ) from exc
            invocation.observe(
                status_code=response.status_code,
                response_size=len(response.content),
            )
        self._raise_on_business_error(data)
        return data

    @staticmethod
    def _raise_on_business_error(data: Any) -> None:
        if isinstance(data, str):
            text = data.strip()
            if text and any(marker in text for marker in _BODY_ERROR_MARKERS):
                raise RuntimeError(f"妙想接口业务错误: {text[:200]}")
            return
        if not isinstance(data, dict):
            return

        # 数据类接口（query/news-search/stock-screen）用顶层 status 字段
        # 报错：status != 0 即失败，message 携带原因（官方 mx-data 规范）。
        status = data.get("status")
        if isinstance(status, bool):
            status = None
        if isinstance(status, int) and status != 0:
            raise RuntimeError(
                f"妙想接口业务错误 status={status}: "
                f"{MXClient._extract_error_text(data)}"
            )
        if isinstance(status, str) and status.strip().lstrip("-").isdigit():
            normalized_status = int(status.strip())
            if normalized_status != 0:
                raise RuntimeError(
                    f"妙想接口业务错误 status={normalized_status}: "
                    f"{MXClient._extract_error_text(data)}"
                )

        code = data.get("code")
        if isinstance(code, bool):
            code = None
        if isinstance(code, int) and code not in _SUCCESS_CODES:
            raise RuntimeError(
                f"妙想接口业务错误 code={code}: "
                f"{MXClient._extract_error_text(data)}"
            )
        if isinstance(code, str) and code.strip().lstrip("-").isdigit():
            normalized = int(code.strip())
            if normalized not in _SUCCESS_CODES:
                raise RuntimeError(
                    f"妙想接口业务错误 code={normalized}: "
                    f"{MXClient._extract_error_text(data)}"
                )

        message_text = MXClient._extract_error_text(data)
        if message_text and any(
            marker in message_text for marker in _BODY_ERROR_MARKERS
        ):
            raise RuntimeError(f"妙想接口业务错误: {message_text[:200]}")

    @staticmethod
    def _extract_error_text(data: dict[str, Any]) -> str:
        parts: list[str] = []
        for field in _MESSAGE_FIELDS:
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        if not parts:
            # 无消息字段时带上除 data 外的少量上下文，便于排障与 hint 匹配。
            context = {
                key: value
                for key, value in data.items()
                if key not in {"data"} and isinstance(value, (str, int, float, bool))
            }
            if context:
                parts.append(json.dumps(context, ensure_ascii=False))
        return " | ".join(parts)[:200]

    def query_market(self, query: str) -> dict[str, Any]:
        return self._post("/api/claw/query", {"toolQuery": query})

    def search_news(self, query: str) -> dict[str, Any]:
        return self._post("/api/claw/news-search", {"query": query})

    def screen_stocks(self, query: str) -> dict[str, Any]:
        return self._post("/api/claw/stock-screen", {"keyword": query})

    def get_positions(self) -> dict[str, Any]:
        return self._post("/api/claw/mockTrading/positions", {"moneyUnit": 1})

    def get_balance(self) -> dict[str, Any]:
        return self._post("/api/claw/mockTrading/balance", {"moneyUnit": 1})

    def get_orders(self) -> dict[str, Any]:
        return self._post(
            "/api/claw/mockTrading/orders", {"fltOrderDrt": 0, "fltOrderStatus": 0}
        )

    def get_self_selects(self) -> dict[str, Any]:
        return self._post("/api/claw/self-select/get", {})

    def manage_self_select(self, query: str) -> dict[str, Any]:
        return self._post("/api/claw/self-select/manage", {"query": query})

    def trade(
        self,
        *,
        action: str,
        symbol: str,
        quantity: int,
        price_type: str,
        price: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "buy" if action.upper() == "BUY" else "sell",
            "stockCode": symbol,
            "quantity": quantity,
            "useMarketPrice": price_type.upper() == "MARKET",
        }
        if price_type.upper() == "LIMIT" and price is not None:
            # 接口接收原始小数价格（官方 SKILL.md 文档示例 "price": 1780.00；
            # 生产 2026-10-19 验证：放大为整数会被拒 status=100
            # "输入价格不合理，超过限价"；上周五原始小数价格全部成交）。
            # 仅按市场规则规范化小数位：沪市（6/9 开头）不超过 2 位、
            # 深市（0/3 开头）不超过 3 位。
            decimal_places = 2 if str(symbol)[:1] in ("6", "9") else 3
            payload["price"] = round(float(price), decimal_places)
        return self._post("/api/claw/mockTrading/trade", payload)

    def cancel_order(
        self,
        *,
        cancel_type: str,
        order_id: str | None = None,
        stock_code: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": cancel_type}
        if cancel_type == "order":
            if not order_id:
                raise ValueError("order_id 不能为空。")
            payload["orderId"] = order_id
            if stock_code:
                payload["stockCode"] = stock_code
        return self._post("/api/claw/mockTrading/cancel", payload)
