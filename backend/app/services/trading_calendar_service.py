from __future__ import annotations

import calendar
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

_MAX_PROBE_DAYS = 30
_CALENDAR_FETCH_RETRIES = 3
_CALENDAR_SOURCE = "ciis_transaction_calendar"
_CALENDAR_API_URL = (
    "https://www.ciis.com.hk/hongkong/transactionCalendar/"
    "queryTransactionCalendar.do"
)
_CALENDAR_EXCHANGE = "SSE"
_CALENDAR_REQUEST_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://www.ciis.com.hk",
    "Referer": "https://www.ciis.com.hk/hongkong/sc/aboutus/"
    "transactioncalendar/index.shtml",
    "X-Requested-With": "XMLHttpRequest",
}


class TradingCalendarService:
    def __init__(self) -> None:
        self._data_path = Path(__file__).resolve().parents[1] / "data" / (
            "trading_calendar.json"
        )
        self._calendar: dict[str, object] | None = None
        self._month_days_cache: dict[str, set[str]] = {}

    def _load_calendar(self) -> dict[str, object]:
        if self._calendar is None:
            if self._data_path.exists():
                payload = json.loads(
                    self._data_path.read_text(encoding="utf-8")
                )
                if not isinstance(payload, dict):
                    raise RuntimeError("交易日历缓存结构异常")
                months_data = payload.get("months")
                if not isinstance(months_data, dict):
                    raise RuntimeError(
                        "交易日历缓存结构异常：缺少 months 字段"
                    )
                self._calendar = payload
            else:
                self._calendar = {
                    "version": 1,
                    "source": _CALENDAR_SOURCE,
                    "months": {},
                }
        return self._calendar

    def _save_calendar(self) -> None:
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        self._data_path.write_text(
            json.dumps(self._calendar, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _month_key(self, year: int, month: int) -> str:
        return f"{year:04d}-{month:02d}"

    def _parse_month_key(self, month_key: str) -> tuple[int, int]:
        if len(month_key) != 7 or month_key[4] != "-":
            raise RuntimeError(f"非法交易日历月份标识: {month_key}")
        year_text, month_text = month_key.split("-", 1)
        if not (year_text.isdigit() and month_text.isdigit()):
            raise RuntimeError(f"非法交易日历月份标识: {month_key}")
        year = int(year_text)
        month = int(month_text)
        if month < 1 or month > 12:
            raise RuntimeError(f"非法交易日历月份标识: {month_key}")
        return year, month

    def _next_month_key(self, month_key: str) -> str:
        year, month = self._parse_month_key(month_key)
        if month == 12:
            return self._month_key(year + 1, 1)
        return self._month_key(year, month + 1)

    def _fetch_month(self, month_key: str) -> list[str]:
        last_error: RuntimeError | None = None
        for attempt in range(_CALENDAR_FETCH_RETRIES + 1):
            try:
                return self._fetch_month_once(month_key)
            except RuntimeError as exc:
                last_error = exc
                if attempt == _CALENDAR_FETCH_RETRIES:
                    raise RuntimeError(
                        f"{exc}；已重试 {_CALENDAR_FETCH_RETRIES} 次仍失败"
                    ) from exc

        raise RuntimeError(
            "missing trading calendar data for month "
            f"{month_key}: {last_error}"
        )

    def _fetch_month_once(self, month_key: str) -> list[str]:
        year, month = self._parse_month_key(month_key)
        payload = {"year": month_key}

        try:
            response = httpx.post(
                _CALENDAR_API_URL,
                data=payload,
                headers=_CALENDAR_REQUEST_HEADERS,
                timeout=30.0,
                verify=False,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"交易日历远程接口返回错误 ({exc.response.status_code})"
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("交易日历远程接口请求超时") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"交易日历远程接口请求失败: {exc}") from exc

        try:
            result = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("交易日历远程接口返回了无效 JSON") from exc

        if not isinstance(result, dict):
            raise RuntimeError("交易日历远程接口返回结构异常")

        rest_days = self._extract_rest_days(result, month_key)
        unique_days = self._build_trading_days(year, month, rest_days)
        if not unique_days:
            raise RuntimeError(
                f"missing trading calendar data for month {month_key}"
            )
        return unique_days

    def _extract_rest_days(
        self, payload: dict[str, Any], month_key: str
    ) -> set[str]:
        calendars = payload.get("transactionCalendars")
        if not isinstance(calendars, list):
            raise RuntimeError("交易日历远程接口缺少 transactionCalendars 字段")
        if not calendars:
            raise RuntimeError(
                f"交易日历远程接口暂未发布 {month_key} 月数据"
            )

        rest_days: set[str] = set()
        seen_sse_payload = False
        for group in calendars:
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, dict):
                    continue
                market_type = str(item.get("marketType") or "").strip()
                market_type_sc = str(item.get("marketType_sc") or "").strip()
                if (
                    market_type != _CALENDAR_EXCHANGE
                    and market_type_sc != "上海证券交易所"
                ):
                    continue
                seen_sse_payload = True
                rest_date = item.get("restDate")
                if rest_date is None:
                    continue
                normalized = self._normalize_calendar_date(str(rest_date))
                if normalized.startswith(f"{month_key}-"):
                    rest_days.add(normalized)

        if not seen_sse_payload:
            raise RuntimeError("交易日历远程接口未返回上交所日历数据")
        return rest_days

    def _build_trading_days(
        self, year: int, month: int, rest_days: set[str]
    ) -> list[str]:
        trading_days: list[str] = []
        _, month_days = calendar.monthrange(year, month)
        for day in range(1, month_days + 1):
            current = date(year, month, day)
            if current.weekday() >= 5:
                continue
            current_iso = current.isoformat()
            if current_iso in rest_days:
                continue
            trading_days.append(current_iso)
        return trading_days

    def _normalize_calendar_date(self, value: str) -> str:
        text = value.strip()
        if len(text) == 8 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return text
        raise RuntimeError(f"交易日历远程接口返回了非法日期: {value}")

    def ensure_months(self, month_keys: list[str]) -> None:
        calendar_payload = self._load_calendar()
        months_data = calendar_payload.get("months")
        if not isinstance(months_data, dict):
            raise RuntimeError("交易日历缓存结构异常")
        changed = False
        for month_key in month_keys:
            normalized_key = self._month_key(*self._parse_month_key(month_key))
            if normalized_key in months_data:
                continue
            queried_days = self._fetch_month(normalized_key)
            if not queried_days:
                raise RuntimeError(
                    "missing trading calendar data for month "
                    f"{normalized_key}"
                )
            months_data[normalized_key] = {"trading_days": queried_days}
            changed = True
        if changed:
            calendar_payload["source"] = _CALENDAR_SOURCE
            self._save_calendar()
            self._month_days_cache.clear()

    def warm_up_months(self, current: date) -> None:
        current_month_key = self._month_key(current.year, current.month)
        self.ensure_months([current_month_key])
        if not self.is_trading_day(current):
            return
        month_days = self._month_days(current)
        if not month_days or current.isoformat() != max(month_days):
            return
        try:
            self.ensure_months([self._next_month_key(current_month_key)])
        except RuntimeError:
            # Missing next-month data should not block
            # current-month scheduling.
            pass

    def _month_days(self, current: date) -> set[str]:
        month_key = self._month_key(current.year, current.month)
        if month_key in self._month_days_cache:
            return self._month_days_cache[month_key]
        self.ensure_months([month_key])
        calendar_payload = self._load_calendar()
        months_data = calendar_payload.get("months", {})
        if isinstance(months_data, dict):
            month_payload = months_data.get(month_key, {})
        else:
            month_payload = {}
        if not isinstance(month_payload, dict):
            result: set[str] = set()
        else:
            trading_days = month_payload.get("trading_days", [])
            result = {str(item) for item in trading_days}
        self._month_days_cache[month_key] = result
        return result

    def is_trading_day(self, current: date) -> bool:
        return current.isoformat() in self._month_days(current)

    def next_trading_day(self, current: date) -> date:
        probe = current
        for _ in range(_MAX_PROBE_DAYS):
            if self.is_trading_day(probe):
                return probe
            probe += timedelta(days=1)
        raise RuntimeError(
            f"在 {current} 之后 {_MAX_PROBE_DAYS} 天内未找到交易日，请检查交易日历数据。"
        )


trading_calendar_service = TradingCalendarService()
