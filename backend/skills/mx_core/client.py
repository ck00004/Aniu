from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings


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
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        return response.json()

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
            payload["price"] = price
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
