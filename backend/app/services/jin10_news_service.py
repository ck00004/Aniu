from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_LIMIT = 30
_MAX_PAGE_SIZE = 200
_MAX_FETCH_ITEMS = 1000
_CONTENT_PREVIEW_MAX_CHARS = 180
_ANALYSIS_PREVIEW_MAX_CHARS = 160
_CONTEXT_MAX_ITEMS = 200


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class Jin10NewsService:
    def fetch_news_context(
        self,
        *,
        base_url: str | None,
        target_day: date,
        current_time: datetime,
        limit: int = _DEFAULT_LIMIT,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[str | None, dict[str, Any] | None]:
        endpoint_base = str(base_url or "").strip().rstrip("/")
        if not endpoint_base:
            return None, None

        page_size = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_PAGE_SIZE))
        params = {
            "date": target_day.isoformat(),
            "startTime": "00:00:00",
            "endTime": current_time.strftime("%H:%M:%S"),
            "limit": str(page_size),
            "includeAnalysis": "1",
            "importantOnly": "1",
        }
        url = endpoint_base + "/api/news"

        try:
            payload = self._fetch_all_pages(
                url=url,
                params=params,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("jin10 news fetch failed: %s", exc)
            return None, {
                "ok": False,
                "url": url,
                "error": str(exc),
                "params": params,
            }

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            logger.warning("jin10 news payload missing items list")
            return None, {
                "ok": False,
                "url": url,
                "error": "响应缺少 items 列表",
                "params": params,
            }

        usable_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("skipped") or item.get("deleted"):
                continue
            usable_items.append(item)

        context = self._build_context_text(
            usable_items,
            target_day=target_day,
            current_time=current_time,
        )
        return context, {
            "ok": True,
            "url": url,
            "params": params,
            "item_count": len(usable_items),
            "total": payload.get("total") if isinstance(payload, dict) else None,
            "has_more": payload.get("hasMore") if isinstance(payload, dict) else None,
        }

    def _fetch_all_pages(
        self,
        *,
        url: str,
        params: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        aggregated_items: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        next_before: str | None = None
        request_count = 0
        last_total: int | None = None

        while True:
            page_params = dict(params)
            if next_before:
                page_params["before"] = next_before

            response = httpx.get(url, params=page_params, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            request_count += 1
            if not isinstance(payload, dict):
                raise RuntimeError("Jin10 响应格式无效")

            items = payload.get("items")
            if not isinstance(items, list):
                raise RuntimeError("Jin10 响应缺少 items 列表")

            for item in items:
                if not isinstance(item, dict):
                    continue
                item_key = self._item_key(item)
                if item_key in seen_keys:
                    continue
                seen_keys.add(item_key)
                aggregated_items.append(item)
                if len(aggregated_items) >= _MAX_FETCH_ITEMS:
                    break

            total_value = payload.get("total")
            if isinstance(total_value, int):
                last_total = total_value
            elif isinstance(total_value, float) and total_value.is_integer():
                last_total = int(total_value)

            page_has_more = bool(payload.get("hasMore"))
            if len(aggregated_items) >= _MAX_FETCH_ITEMS or not page_has_more:
                break

            next_cursor = self._extract_before_cursor(items)
            if not next_cursor or next_cursor == next_before:
                break
            next_before = next_cursor

        has_more = bool(last_total and len(aggregated_items) < last_total)
        return {
            "items": aggregated_items,
            "total": last_total if last_total is not None else len(aggregated_items),
            "hasMore": has_more,
            "requestCount": request_count,
        }

    def _item_key(self, item: dict[str, Any]) -> str:
        candidates = [
            item.get("id"),
            item.get("flashId"),
            item.get("createdAt"),
        ]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return str(item)

    def _extract_before_cursor(self, items: list[Any]) -> str | None:
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            created_at = item.get("createdAt")
            if isinstance(created_at, bool):
                continue
            if isinstance(created_at, (int, float)):
                numeric = int(created_at)
            else:
                text = str(created_at or "").strip()
                if not text.isdigit():
                    continue
                numeric = int(text)
            if numeric > 0:
                return str(numeric)
        return None

    def _build_context_text(
        self,
        items: list[dict[str, Any]],
        *,
        target_day: date,
        current_time: datetime,
    ) -> str | None:
        if not items:
            return None

        lines = [
            "[Jin10 当天新闻参考]",
            (
                f"日期：{target_day.isoformat()}，截至"
                f" {current_time.strftime('%H:%M:%S')}，"
                f"共获取 {len(items)} 条重要新闻。"
            ),
            "以下新闻仅作为辅助参考，请结合市场数据、行情、持仓和量价信号综合判断。",
            "",
        ]

        for index, item in enumerate(items[:_CONTEXT_MAX_ITEMS], start=1):
            time_text = str(item.get("time") or "--").strip()
            title = _compact_text(item.get("title") or item.get("content") or "", limit=120)
            content = _compact_text(item.get("content") or "", limit=_CONTENT_PREVIEW_MAX_CHARS)
            lines.append(f"{index}. [{time_text}] {title}")
            if content and content != title:
                lines.append(f"   内容：{content}")
            analysis = _compact_text(item.get("analysis") or "", limit=_ANALYSIS_PREVIEW_MAX_CHARS)
            if analysis:
                lines.append(f"   Jin10解读：{analysis}")

        if len(items) > _CONTEXT_MAX_ITEMS:
            lines.append("")
            lines.append(
                f"其余 {len(items) - _CONTEXT_MAX_ITEMS} 条新闻已省略，请优先关注已列出的重要资讯。"
            )

        return "\n".join(lines).strip()


jin10_news_service = Jin10NewsService()