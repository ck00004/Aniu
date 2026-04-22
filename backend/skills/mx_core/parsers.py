from __future__ import annotations

from typing import Any


def extract_candidates(
    screen_payload: dict[str, Any], limit: int = 10
) -> list[dict[str, str]]:
    data = (
        ((screen_payload.get("data") or {}).get("data") or {}).get("allResults") or {}
    ).get("result") or {}
    rows = data.get("dataList") or []
    candidates: list[dict[str, str]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        symbol = str(
            row.get("SECURITY_CODE") or row.get("stockCode") or row.get("code") or ""
        ).strip()
        name = str(
            row.get("SECURITY_SHORT_NAME")
            or row.get("name")
            or row.get("stockName")
            or ""
        ).strip()
        if symbol or name:
            candidates.append({"symbol": symbol, "name": name})
    return candidates


def extract_position_symbols(positions_payload: dict[str, Any]) -> set[str]:
    data = positions_payload.get("data")
    rows: list[Any] = []
    if isinstance(data, dict):
        rows = data.get("data") or data.get("rows") or data.get("list") or []
    elif isinstance(data, list):
        rows = data
    result: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(
            row.get("stockCode")
            or row.get("SECURITY_CODE")
            or row.get("securityCode")
            or row.get("code")
            or ""
        ).strip()
        if symbol:
            result.add(symbol)
    return result


def extract_available_balance(balance_payload: dict[str, Any]) -> float:
    data = balance_payload.get("data")
    if isinstance(data, dict):
        for key in ("availBalance", "availableBalance", "availableMoney", "balance"):
            value = data.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0
