from __future__ import annotations

from datetime import date, datetime, time as dt_time
import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_LIMIT = 200
_MAX_PAGE_SIZE = 2000
_RAW_NEWS_ITEM_MAX_CHARS = 4000
_CONTENT_PREVIEW_MAX_CHARS = 180
_ANALYSIS_CHUNK_MAX_CHARS = 24000
_RAW_CONTEXT_MAX_ITEMS = 120


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class ClsNewsService:
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
        start_dt = datetime.combine(target_day, dt_time.min, tzinfo=current_time.tzinfo)
        start_ctime = int(start_dt.timestamp())
        end_ctime = int(current_time.timestamp())
        params = {
            "limit": str(page_size),
            "startCtime": str(start_ctime),
            "endCtime": str(end_ctime),
            "format": "json",
        }
        url = endpoint_base + "/api/cls/export"

        try:
            payload = self._fetch_all_pages(
                url=url,
                params=params,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cls news fetch failed: %s", exc)
            return [], {
                "ok": False,
                "url": url,
                "error": str(exc),
                "params": params,
            }

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            logger.warning("cls news payload missing items list")
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
            "[CLS 当天新闻原文摘录]",
            (
                f"日期：{target_day.isoformat()}，截至"
                f" {current_time.strftime('%H:%M:%S')}，"
                f"共拉取 {len(items)} 条电报。"
            ),
            "以下为 CLS 原始电报摘录，请结合市场数据和量价信号交叉验证。",
            "",
        ]

        for index, item in enumerate(items[:_RAW_CONTEXT_MAX_ITEMS], start=1):
            lines.append(self._format_raw_context_item(item, index=index))

        if len(items) > _RAW_CONTEXT_MAX_ITEMS:
            lines.extend(["", f"其余 {len(items) - _RAW_CONTEXT_MAX_ITEMS} 条原始电报已省略。"])

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
        next_before_ctime: str | None = None
        next_before_id: str | None = None
        request_count = 0
        last_total: int | None = None
        max_retries = 3

        while True:
            page_params = dict(params)
            if next_before_ctime:
                page_params["beforeCtime"] = next_before_ctime
            if next_before_id:
                page_params["beforeId"] = next_before_id

            payload = None
            for attempt in range(max_retries):
                try:
                    response = httpx.get(url, params=page_params, timeout=timeout_seconds)
                    response.raise_for_status()
                    payload = response.json()
                    break
                except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        raise
                    if attempt < max_retries - 1:
                        logger.warning(
                            "CLS 请求失败，准备重试 (%s/%s): %s",
                            attempt + 1,
                            max_retries,
                            exc,
                        )
                    else:
                        raise

            request_count += 1
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise RuntimeError("CLS 响应格式无效")

            export = payload.get("export")
            if not isinstance(export, dict):
                raise RuntimeError("CLS 响应缺少 export 数据")

            items = export.get("items")
            if not isinstance(items, list):
                raise RuntimeError("CLS 响应缺少 items 列表")

            for item in items:
                if not isinstance(item, dict):
                    continue
                item_key = self._item_key(item)
                if item_key in seen_keys:
                    continue
                seen_keys.add(item_key)
                aggregated_items.append(item)

            total_value = export.get("total")
            if isinstance(total_value, int):
                last_total = total_value
            elif isinstance(total_value, float) and total_value.is_integer():
                last_total = int(total_value)

            page_has_more = bool(export.get("hasMore"))
            next_cursor = export.get("nextCursor") if isinstance(export.get("nextCursor"), dict) else None
            if not page_has_more or not next_cursor:
                break

            next_before_ctime = str(next_cursor.get("beforeCtime") or "").strip() or None
            next_before_id = str(next_cursor.get("beforeId") or "").strip() or None
            if not next_before_ctime:
                break

        has_more = bool(last_total and len(aggregated_items) < last_total)
        return {
            "items": aggregated_items,
            "total": last_total if last_total is not None else len(aggregated_items),
            "hasMore": has_more,
            "requestCount": request_count,
        }

    def _item_key(self, item: dict[str, Any]) -> str:
        candidates = [item.get("id"), item.get("ctime")]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return str(item)

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, str] | None:
        ctime_value = item.get("ctime")
        try:
            ctime_int = int(ctime_value)
        except (TypeError, ValueError):
            return None
        item_time = datetime.fromtimestamp(ctime_int, tz=datetime.now().astimezone().tzinfo)
        time_text = item_time.strftime("%H:%M:%S")
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
            "level": str(item.get("level") or "").strip(),
            "important": "true" if bool(item.get("important")) else "false",
            "source_id": str(item.get("id") or ""),
        }

    def _format_analysis_item_line(self, item: dict[str, str], *, index: int) -> str:
        title = item.get("title") or item.get("content") or "--"
        content = item.get("content") or title
        level = str(item.get("level") or "").strip()
        important = item.get("important") == "true"
        prefix_parts = [f"{index}. [{item.get('time') or '--'}]"]
        if level:
            prefix_parts.append(f"等级 {level}")
        if important:
            prefix_parts.append("重要")
        prefix = " ".join(prefix_parts)
        if title == content:
            return f"{prefix} {title}".strip()
        return f"{prefix} 标题：{title}\n正文：{content}".strip()

    def _format_raw_context_item(self, item: dict[str, str], *, index: int) -> str:
        title = _compact_text(item.get("title") or item.get("content") or "", limit=120)
        content = _compact_text(item.get("content") or "", limit=_CONTENT_PREVIEW_MAX_CHARS)
        extra = []
        level = str(item.get("level") or "").strip()
        if level:
            extra.append(f"等级 {level}")
        if item.get("important") == "true":
            extra.append("重要")
        suffix = f" ({' / '.join(extra)})" if extra else ""
        lines = [f"{index}. [{item.get('time') or '--'}] {title}{suffix}"]
        if content and content != title:
            lines.append(f"内容：{content}")
        return "\n".join(lines)


cls_news_service = ClsNewsService()