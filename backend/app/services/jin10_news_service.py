from __future__ import annotations

from datetime import date, datetime
import logging
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_LIMIT = 30
_MAX_PAGE_SIZE = 200
_RAW_NEWS_ITEM_MAX_CHARS = 4000
_CONTENT_PREVIEW_MAX_CHARS = 180
_ANALYSIS_PREVIEW_MAX_CHARS = 160
_CONTEXT_MAX_ITEMS = 200
_ANALYSIS_CHUNK_MAX_CHARS = 24000
_RAW_CONTEXT_MAX_ITEMS = 80


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class Jin10NewsService:
    def fetch_news_items(
        self,
        *,
        base_url: str | None,
        target_day: date,
        current_time: datetime,
        limit: int = _DEFAULT_LIMIT,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
        endpoint_base = str(base_url or "").strip().rstrip("/")
        if not endpoint_base:
            return [], None

        page_size = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_PAGE_SIZE))
        params = {
            "date": target_day.isoformat(),
            "startTime": "00:00:00",
            "endTime": current_time.strftime("%H:%M:%S"),
            "limit": str(page_size),
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
            return [], {
                "ok": False,
                "url": url,
                "error": str(exc),
                "params": params,
            }

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            logger.warning("jin10 news payload missing items list")
            return [], {
                "ok": False,
                "url": url,
                "error": "响应缺少 items 列表",
                "params": params,
            }

        usable_items: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("skipped") or item.get("deleted"):
                continue
            normalized = self._normalize_item(item)
            if normalized is None:
                continue
            usable_items.append(normalized)

        return usable_items, {
            "ok": True,
            "url": url,
            "params": params,
            "item_count": len(usable_items),
            "total": payload.get("total") if isinstance(payload, dict) else None,
            "has_more": payload.get("hasMore") if isinstance(payload, dict) else None,
            "request_count": payload.get("requestCount") if isinstance(payload, dict) else None,
        }

    def build_analysis_chunks(self, items: list[dict[str, str]]) -> list[str]:
        chunks: list[str] = []
        current_lines: list[str] = []
        current_chars = 0

        for index, item in enumerate(items, start=1):
            line = self._format_analysis_item_line(item, index=index)
            if current_lines and current_chars + len(line) + 1 > _ANALYSIS_CHUNK_MAX_CHARS:
                chunks.append("\n\n".join(current_lines))
                current_lines = []
                current_chars = 0
            current_lines.append(line)
            current_chars += len(line) + 2

        if current_lines:
            chunks.append("\n\n".join(current_lines))

        return chunks

    def build_raw_context_text(
        self,
        items: list[dict[str, str]],
        *,
        target_day: date,
        current_time: datetime,
    ) -> str | None:
        if not items:
            return None

        lines = [
            "[Jin10 当天新闻原文摘录]",
            (
                f"日期：{target_day.isoformat()}，截至"
                f" {current_time.strftime('%H:%M:%S')}，"
                f"共拉取 {len(items)} 条新闻。"
            ),
            "以下为 Jin10 原始新闻摘录，请结合市场数据和量价信号交叉验证。",
            "",
        ]

        for index, item in enumerate(items[:_RAW_CONTEXT_MAX_ITEMS], start=1):
            lines.append(self._format_raw_context_item(item, index=index))

        if len(items) > _RAW_CONTEXT_MAX_ITEMS:
            lines.extend(
                [
                    "",
                    f"其余 {len(items) - _RAW_CONTEXT_MAX_ITEMS} 条原始新闻已省略。",
                ]
            )

        return "\n\n".join(lines).strip()

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
        max_retries = 3
        retry_delay = 1.0

        while True:
            page_params = dict(params)
            if next_before:
                page_params["before"] = next_before

            payload = None
            for attempt in range(max_retries):
                try:
                    response = httpx.get(
                        url, params=page_params, timeout=timeout_seconds
                    )
                    response.raise_for_status()
                    payload = response.json()
                    break
                except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        raise
                    if attempt < max_retries - 1:
                        logger.warning(
                            "Jin10 请求失败，准备重试 (%s/%s): %s",
                            attempt + 1,
                            max_retries,
                            exc,
                        )
                        time.sleep(retry_delay)
                    else:
                        raise

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

            total_value = payload.get("total")
            if isinstance(total_value, int):
                last_total = total_value
            elif isinstance(total_value, float) and total_value.is_integer():
                last_total = int(total_value)

            page_has_more = bool(payload.get("hasMore"))
            if not page_has_more:
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

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, str] | None:
        time_text = str(item.get("time") or "--").strip() or "--"
        title = " ".join(str(item.get("title") or "").split()).strip()
        content = " ".join(str(item.get("content") or "").split()).strip()
        if not title and not content:
            return None
        merged = content
        if len(merged) > _RAW_NEWS_ITEM_MAX_CHARS:
            merged = merged[: _RAW_NEWS_ITEM_MAX_CHARS - 3] + "..."
        return {
            "time": time_text,
            "title": title,
            "content": merged,
        }

    def _format_analysis_item_line(self, item: dict[str, str], *, index: int) -> str:
        title = item.get("title") or item.get("content") or "--"
        content = item.get("content") or title
        if title == content:
            return f"{index}. [{item.get('time') or '--'}] {title}"
        return (
            f"{index}. [{item.get('time') or '--'}] 标题：{title}\n"
            f"正文：{content}"
        )

    def _format_raw_context_item(self, item: dict[str, str], *, index: int) -> str:
        title = _compact_text(item.get("title") or item.get("content") or "", limit=120)
        content = _compact_text(item.get("content") or "", limit=_CONTENT_PREVIEW_MAX_CHARS)
        lines = [f"{index}. [{item.get('time') or '--'}] {title}"]
        if content and content != title:
            lines.append(f"内容：{content}")
        return "\n".join(lines)


jin10_news_service = Jin10NewsService()
