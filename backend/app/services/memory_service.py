"""长期交易记忆服务（移植自原版 Aniu business/memories + memory_repo）。

检索为 SQL contains 关键词匹配（无向量），写入带乐观锁版本控制，
每次读写同步落 activity 审计。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func as sa_func, or_, select

from app.db.database import session_scope
from app.db.models import MemoryActivity, MemoryItem

_MAX_KEYWORDS = 8
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 10


class MemoryVersionConflict(RuntimeError):
    """乐观锁版本冲突：记忆已被其他调用方修改。"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_keywords(keywords: Any) -> list[str]:
    if isinstance(keywords, str):
        parts = [keywords]
    elif isinstance(keywords, list):
        parts = keywords
    else:
        parts = []
    normalized: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        normalized.append(text)
        if len(normalized) >= _MAX_KEYWORDS:
            break
    return normalized


def _item_to_dict(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "content": item.content,
        "reason": item.reason,
        "version": int(item.version or 1),
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _log_activity(
    db: Any,
    *,
    action: str,
    keywords: list[str] | None = None,
    result_count: int | None = None,
    memory_id: int | None = None,
) -> None:
    db.add(
        MemoryActivity(
            action=action,
            keywords=keywords or None,
            result_count=result_count,
            memory_id=memory_id,
        )
    )


class MemoryService:
    # ------------------------------------------------------------------
    # Agent 工具路径
    # ------------------------------------------------------------------

    def search_for_agent(
        self,
        *,
        keywords: Any,
        match_mode: str = "any",
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """按关键词检索记忆并记录读取活动（Agent 的 memory_read 工具）。"""
        normalized = _normalize_keywords(keywords)
        if not normalized:
            return {"ok": True, "items": [], "result_count": 0, "keywords": []}

        mode = "all" if str(match_mode or "").strip().lower() == "all" else "any"
        normalized_limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))

        with session_scope() as db:
            stmt = (
                select(MemoryItem)
                .where(MemoryItem.deleted_at.is_(None))
                .order_by(MemoryItem.updated_at.desc())
                .limit(50)
            )
            rows = list(db.scalars(stmt).all())
            matched = []
            for row in rows:
                haystack = f"{row.content}\n{row.reason or ''}".lower()
                hits = sum(
                    1 for keyword in normalized if keyword.lower() in haystack
                )
                if (mode == "all" and hits == len(normalized)) or (
                    mode == "any" and hits > 0
                ):
                    matched.append(row)
                    if len(matched) >= normalized_limit:
                        break

            _log_activity(
                db,
                action="read",
                keywords=normalized,
                result_count=len(matched),
            )

            return {
                "ok": True,
                "items": [_item_to_dict(row) for row in matched],
                "result_count": len(matched),
                "keywords": normalized,
                "match_mode": mode,
            }

    def write_for_agent(
        self,
        *,
        action: str,
        memory_id: int | None = None,
        content: str | None = None,
        reason: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Agent 的 memory_write 工具：create/update/delete 三合一。"""
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"create", "update", "delete"}:
            return {
                "ok": False,
                "error": "memory_write 的 action 只能是 create、update 或 delete。",
            }

        with session_scope() as db:
            try:
                if normalized_action == "create":
                    item = self.create_memory(
                        db, content=content, reason=reason
                    )
                    return {"ok": True, "action": "create", "item": _item_to_dict(item)}

                if memory_id is None:
                    return {
                        "ok": False,
                        "error": "update/delete 必须提供 memory_id。",
                    }
                if normalized_action == "update":
                    item = self.update_memory(
                        db,
                        memory_id=memory_id,
                        content=content,
                        reason=reason,
                        expected_version=expected_version,
                    )
                    return {"ok": True, "action": "update", "item": _item_to_dict(item)}

                self.delete_memory(
                    db,
                    memory_id=memory_id,
                    expected_version=expected_version,
                )
                return {"ok": True, "action": "delete", "memory_id": memory_id}
            except MemoryVersionConflict as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "conflict": True,
                }

    # ------------------------------------------------------------------
    # 管理路径（API）
    # ------------------------------------------------------------------

    def create_memory(
        self, db: Any, *, content: str, reason: str | None = None
    ) -> MemoryItem:
        normalized_content = str(content or "").strip()
        if not normalized_content:
            raise ValueError("记忆内容不能为空。")
        if len(normalized_content) > 2000:
            raise ValueError("记忆内容过长（上限 2000 字符）。")
        item = MemoryItem(
            content=normalized_content,
            reason=str(reason or "").strip() or None,
            version=1,
        )
        db.add(item)
        db.flush()
        _log_activity(db, action="create", memory_id=item.id)
        db.commit()
        db.refresh(item)
        return item

    def update_memory(
        self,
        db: Any,
        *,
        memory_id: int,
        content: str | None = None,
        reason: str | None = None,
        expected_version: int | None = None,
    ) -> MemoryItem:
        item = db.get(MemoryItem, memory_id)
        if item is None or item.deleted_at is not None:
            raise LookupError(f"记忆 {memory_id} 不存在。")
        current_version = int(item.version or 1)
        if expected_version is None or int(expected_version) != current_version:
            raise MemoryVersionConflict(
                f"记忆 {memory_id} 版本冲突：当前版本 {current_version}，"
                f"请求携带 expected_version={expected_version}。请重新读取后重试。"
            )
        if content is not None:
            normalized_content = str(content or "").strip()
            if not normalized_content:
                raise ValueError("记忆内容不能为空。")
            if len(normalized_content) > 2000:
                raise ValueError("记忆内容过长（上限 2000 字符）。")
            item.content = normalized_content
        if reason is not None:
            item.reason = str(reason or "").strip() or None
        item.version = current_version + 1
        item.updated_at = _now_utc()
        db.add(item)
        _log_activity(db, action="update", memory_id=item.id)
        db.commit()
        db.refresh(item)
        return item

    def delete_memory(
        self, db: Any, *, memory_id: int, expected_version: int | None = None
    ) -> None:
        item = db.get(MemoryItem, memory_id)
        if item is None or item.deleted_at is not None:
            raise LookupError(f"记忆 {memory_id} 不存在。")
        current_version = int(item.version or 1)
        if expected_version is None or int(expected_version) != current_version:
            raise MemoryVersionConflict(
                f"记忆 {memory_id} 版本冲突：当前版本 {current_version}，"
                f"请求携带 expected_version={expected_version}。请重新读取后重试。"
            )
        item.deleted_at = _now_utc()
        item.version = current_version + 1
        db.add(item)
        _log_activity(db, action="delete", memory_id=item.id)
        db.commit()

    def list_memories(
        self,
        db: Any,
        *,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(int(limit or 20), 100))
        normalized_offset = max(0, int(offset or 0))
        keyword = str(query or "").strip()

        conditions = [MemoryItem.deleted_at.is_(None)]
        if keyword:
            conditions.append(
                or_(
                    MemoryItem.content.contains(keyword),
                    MemoryItem.reason.contains(keyword),
                )
            )
        base = select(MemoryItem).where(*conditions)
        count_stmt = select(sa_func.count(MemoryItem.id)).where(*conditions)

        total = int(db.scalar(count_stmt) or 0)
        rows = list(
            db.scalars(
                base.order_by(MemoryItem.updated_at.desc())
                .limit(normalized_limit)
                .offset(normalized_offset)
            ).all()
        )
        return {
            "total": total,
            "items": [_item_to_dict(row) for row in rows],
        }

    def list_activities(
        self, db: Any, *, limit: int = 30, offset: int = 0
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(int(limit or 30), 100))
        normalized_offset = max(0, int(offset or 0))
        total = int(db.scalar(select(sa_func.count(MemoryActivity.id))) or 0)
        rows = list(
            db.scalars(
                select(MemoryActivity)
                .order_by(MemoryActivity.id.desc())
                .limit(normalized_limit)
                .offset(normalized_offset)
            ).all()
        )
        return {
            "total": total,
            "items": [
                {
                    "id": row.id,
                    "action": row.action,
                    "keywords": row.keywords,
                    "result_count": row.result_count,
                    "memory_id": row.memory_id,
                    "created_at": row.created_at.isoformat()
                    if row.created_at
                    else None,
                }
                for row in rows
            ],
        }


memory_service = MemoryService()
