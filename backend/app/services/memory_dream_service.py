"""记忆梦境夜间整理（移植自原版 Aniu business/dreams + dream_agent）。

每个整理日期一条幂等任务；Dream 智能体复用现有 agent 框架，工具白名单
仅 4 个（读当日报告 / 列记忆 / 检索记忆 / 写记忆），不接触任何交易工具。
单进程部署：用互斥锁防并发，启动时把遗留 running 任务收敛为失败。
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sa_func, select

from app.db.database import session_scope
from app.db.models import MemoryDream
from app.services.daily_report_service import daily_report_service
from app.services.llm_service import llm_service
from app.services.memory_service import memory_service

logger = logging.getLogger(__name__)

SHANGHAI_TZ = timezone(timedelta(hours=8))
DEFAULT_DREAM_TIME = "00:30"
_DREAM_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_DREAM_INSTRUCTION = (
    "你是交易记忆的夜间整理智能体。请整理指定日期的运行报告和长期记忆："
    "先分页阅读当日报告和全部当前记忆，再自主决定需要创建、更新、或软删除的记忆。"
    "更新和删除必须带上最近读取到的 expected_version，版本冲突时重新读取后再操作。"
    "只保存简洁、可长期复用的投资经验教训和形成原因；"
    "不得保存运行总结、短暂行情或泛泛原则。"
    "不要调用任何交易或股票工具；完成后汇报本次整理结果。"
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _parse_dream_time(value: Any) -> tuple[int, int]:
    text = str(value or DEFAULT_DREAM_TIME).strip()
    match = _DREAM_TIME_PATTERN.match(text)
    if not match:
        return 0, 30
    return int(match.group(1)), int(match.group(2))


def _dream_to_dict(dream: MemoryDream) -> dict[str, Any]:
    return {
        "id": dream.id,
        "target_date": dream.target_date.isoformat()
        if dream.target_date is not None
        else None,
        "status": dream.status,
        "result": dream.result,
        "failure_reason": dream.failure_reason,
        "created_at": dream.created_at.isoformat() if dream.created_at else None,
        "updated_at": dream.updated_at.isoformat() if dream.updated_at else None,
        "started_at": dream.started_at.isoformat() if dream.started_at else None,
        "finished_at": dream.finished_at.isoformat() if dream.finished_at else None,
    }


# ---------------------------------------------------------------------------
# Dream 工具（白名单：4 个，全部只读或仅操作记忆）
# ---------------------------------------------------------------------------


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = default
    return max(minimum, min(maximum, numeric))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_dream_tools() -> list[dict[str, Any]]:
    """梦境智能体的 4 个白名单工具（OpenAI function 格式）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "dream_read_reports",
                "description": "分页读取指定整理日期内各次运行的报告（结论、交易与证据）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "offset": {"type": "integer", "minimum": 0, "description": "起始下标"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "每页条数"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_list",
                "description": "分页读取当前所有未删除的长期记忆及其版本号。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "offset": {"type": "integer", "minimum": 0, "description": "起始下标"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "每页条数"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_read",
                "description": "按关键词检索长期记忆（整理时核对已有内容，避免重复创建）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关键词列表",
                        },
                        "match_mode": {"type": "string", "enum": ["any", "all"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["keywords"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_write",
                "description": "写入长期记忆：create 新建、update 更新、delete 软删除（带乐观锁）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["create", "update", "delete"]},
                        "memory_id": {"type": "integer"},
                        "content": {"type": "string"},
                        "reason": {"type": "string"},
                        "expected_version": {"type": "integer"},
                    },
                    "required": ["action"],
                },
            },
        },
    ]


_DREAM_TOOL_NAMES = {
    "dream_read_reports",
    "memory_list",
    "memory_read",
    "memory_write",
}


def build_dream_tool_executor(target_date: date) -> Any:
    """构建梦境工具白名单执行器：只允许 4 个记忆/报告工具。"""

    def _dream_read_reports(arguments: dict[str, Any]) -> dict[str, Any]:
        offset = _clamp_int(arguments.get("offset"), default=0, minimum=0, maximum=10000)
        limit = _clamp_int(arguments.get("limit"), default=10, minimum=1, maximum=20)
        with session_scope() as db:
            report = daily_report_service.build_daily_report(
                db, target_date=target_date
            )
        runs = report.get("runs") or []
        page = runs[offset : offset + limit]
        return {
            "ok": True,
            "target_date": target_date.isoformat(),
            "offset": offset,
            "limit": limit,
            "total": len(runs),
            "has_more": offset + limit < len(runs),
            "runs": page,
        }

    def _memory_list(arguments: dict[str, Any]) -> dict[str, Any]:
        offset = _clamp_int(arguments.get("offset"), default=0, minimum=0, maximum=100000)
        limit = _clamp_int(arguments.get("limit"), default=50, minimum=1, maximum=100)
        with session_scope() as db:
            payload = memory_service.list_memories(
                db, query=None, limit=limit, offset=offset
            )
        return {
            "ok": True,
            "offset": offset,
            "limit": limit,
            "total": payload["total"],
            "has_more": offset + limit < payload["total"],
            "items": payload["items"],
        }

    def _executor(
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str | None,
    ) -> dict[str, Any]:
        del tool_call_id
        if tool_name not in _DREAM_TOOL_NAMES:
            return {
                "ok": False,
                "tool_name": tool_name,
                "error": "梦境整理只允许使用记忆与报告工具，禁止调用交易或股票工具。",
            }
        try:
            if tool_name == "dream_read_reports":
                return _dream_read_reports(arguments)
            if tool_name == "memory_list":
                return _memory_list(arguments)
            if tool_name == "memory_read":
                keywords = arguments.get("keywords")
                if isinstance(keywords, str):
                    keywords = [keywords]
                return memory_service.search_for_agent(
                    keywords=keywords,
                    match_mode=str(arguments.get("match_mode") or "any"),
                    limit=_clamp_int(
                        arguments.get("limit"), default=5, minimum=1, maximum=10
                    ),
                )
            return memory_service.write_for_agent(
                action=str(arguments.get("action") or ""),
                memory_id=_optional_int(arguments.get("memory_id")),
                content=arguments.get("content"),
                reason=arguments.get("reason"),
                expected_version=_optional_int(arguments.get("expected_version")),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "tool_name": tool_name, "error": str(exc)}

    return _executor


# ---------------------------------------------------------------------------
# Dream 服务
# ---------------------------------------------------------------------------


class MemoryDreamService:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    # -- 状态机 -----------------------------------------------------------

    def create_or_get(self, db: Any, *, target_date: date) -> MemoryDream:
        existing = db.scalar(
            select(MemoryDream).where(MemoryDream.target_date == target_date)
        )
        if existing is not None:
            return existing
        dream = MemoryDream(target_date=target_date, status="pending")
        db.add(dream)
        db.commit()
        db.refresh(dream)
        return dream

    def retry_if_finished(self, db: Any, dream: MemoryDream) -> MemoryDream:
        """已完成/失败的任务允许手动重跑：重置为 pending。"""
        if str(dream.status or "") in {"completed", "failed"}:
            dream.status = "pending"
            dream.result = None
            dream.failure_reason = None
            dream.started_at = None
            dream.finished_at = None
            db.add(dream)
            db.commit()
            db.refresh(dream)
        return dream

    # -- 执行 -------------------------------------------------------------

    def execute_dream(self, dream_id: int) -> MemoryDream | None:
        """同步执行一次梦境整理（状态机 pending → running → completed/failed）。"""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("已有梦境任务正在执行，请稍后再试。")
        try:
            with session_scope() as db:
                dream = db.get(MemoryDream, dream_id)
                if dream is None or str(dream.status or "") != "pending":
                    return dream
                dream.status = "running"
                dream.started_at = _now_utc()
                dream.failure_reason = None
                db.add(dream)
                db.commit()
                target_date = dream.target_date

            try:
                result_text = self._run_dream_agent(target_date)
                with session_scope() as db:
                    dream = db.get(MemoryDream, dream_id)
                    if dream is None:
                        return None
                    dream.status = "completed"
                    dream.result = (result_text or "").strip() or None
                    dream.finished_at = _now_utc()
                    db.add(dream)
                    db.commit()
                    db.refresh(dream)
                    return dream
            except Exception as exc:  # noqa: BLE001
                logger.exception("memory dream failed: dream_id=%s", dream_id)
                with session_scope() as db:
                    dream = db.get(MemoryDream, dream_id)
                    if dream is None:
                        return None
                    dream.status = "failed"
                    dream.failure_reason = str(exc)
                    dream.finished_at = _now_utc()
                    db.add(dream)
                    db.commit()
                    db.refresh(dream)
                    return dream
        finally:
            self._lock.release()

    def _run_dream_agent(self, target_date: date) -> str:
        from types import SimpleNamespace

        with session_scope() as db:
            from app.services.aniu_service import aniu_service

            settings = aniu_service.get_or_create_settings(db)
            if not settings.llm_base_url or not settings.llm_api_key:
                raise RuntimeError("未配置大模型接口，无法执行记忆梦境。")
            llm_model = settings.llm_model
            llm_base_url = str(settings.llm_base_url)
            llm_api_key = str(settings.llm_api_key)
            system_prompt = str(settings.system_prompt or "").strip()

        dream_settings = SimpleNamespace(
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            system_prompt=(
                f"{system_prompt}\n\n{_DREAM_INSTRUCTION}".strip()
                if system_prompt
                else _DREAM_INSTRUCTION
            ),
            prompt_templates={},
            run_type="dream",
            timeout_seconds=600,
            automation_context_window_tokens=None,
            tools_override=build_dream_tools(),
        )

        decision, _request, _response, _trace = llm_service.run_agent_with_messages(
            app_settings=dream_settings,
            client=None,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"请整理 {target_date.isoformat()} 这一天的运行报告和长期记忆。"
                        "请先分页阅读当日报告和全部当前记忆，再自主决定需要创建、"
                        "更新、或软删除的记忆。更新和删除必须带上最近读取到的 "
                        "expected_version。不要调用任何交易或股票工具；完成后汇报本次整理结果。"
                    ),
                }
            ],
            emit=None,
            tool_executor=build_dream_tool_executor(target_date),
        )
        return str(decision.get("final_answer") or "")

    # -- 触发 -------------------------------------------------------------

    def run_dream_async(
        self, *, target_date: date | None = None
    ) -> tuple[MemoryDream | None, bool]:
        """手动触发：默认整理昨天。返回 (dream, 是否已启动线程)。"""
        effective_date = target_date or (_now_shanghai().date() - timedelta(days=1))
        with session_scope() as db:
            dream = self.create_or_get(db, target_date=effective_date)
            dream = self.retry_if_finished(db, dream)
            dream_id = dream.id
            snapshot = _dream_to_dict(dream)

        if str(snapshot["status"] or "") == "running":
            return dream, False

        thread = threading.Thread(
            target=self._run_in_thread,
            args=(dream_id,),
            name=f"memory-dream-{dream_id}",
            daemon=True,
        )
        thread.start()
        return dream, True

    def _run_in_thread(self, dream_id: int) -> None:
        try:
            self.execute_dream(dream_id)
        except Exception:  # noqa: BLE001
            logger.exception("memory dream thread failed: dream_id=%s", dream_id)

    def process_due_dream(self) -> None:
        """调度循环钩子：过了每日整理时间且当日任务不存在时触发一次。"""
        try:
            now = _now_shanghai()
            with session_scope() as db:
                from app.services.aniu_service import aniu_service

                settings = aniu_service.get_or_create_settings(db)
                dream_hour, dream_minute = _parse_dream_time(
                    getattr(settings, "automation_dream_time", None)
                )
                # 整理对象是"昨天"，因此触发日 = 整理日期 + 1 天
                target_date = now.date() - timedelta(days=1)
                existing = db.scalar(
                    select(MemoryDream).where(
                        MemoryDream.target_date == target_date
                    )
                )

            minutes_now = now.hour * 60 + now.minute
            minutes_dream = dream_hour * 60 + dream_minute
            if minutes_now < minutes_dream:
                return
            if existing is not None:
                # 已存在（含失败/完成）则不重复触发，重跑走手动入口
                return

            dream, started = self.run_dream_async(target_date=target_date)
            if started:
                logger.info(
                    "memory dream triggered by schedule: target_date=%s dream_id=%s",
                    target_date.isoformat(),
                    dream.id if dream is not None else None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("process_due_dream failed: %s", exc)

    # -- 恢复与查询 -------------------------------------------------------

    def fail_stale_running_dreams(self, db: Any) -> int:
        """启动时把遗留 running 的梦境任务收敛为失败（不自动重试）。"""
        stale = list(
            db.scalars(
                select(MemoryDream).where(MemoryDream.status == "running")
            ).all()
        )
        for dream in stale:
            dream.status = "failed"
            dream.failure_reason = "应用进程中断，未自动重试，请确认后手动重试。"
            dream.finished_at = _now_utc()
            db.add(dream)
        if stale:
            db.commit()
            logger.warning(
                "marked %s stale running dream(s) as failed at startup", len(stale)
            )
        return len(stale)

    def list_dreams(
        self, db: Any, *, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(int(limit or 20), 100))
        normalized_offset = max(0, int(offset or 0))
        total = int(db.scalar(select(sa_func.count(MemoryDream.id))) or 0)
        rows = list(
            db.scalars(
                select(MemoryDream)
                .order_by(MemoryDream.target_date.desc())
                .limit(normalized_limit)
                .offset(normalized_offset)
            ).all()
        )
        return {"total": total, "items": [_dream_to_dict(row) for row in rows]}

    def get_dream(self, db: Any, dream_id: int) -> MemoryDream | None:
        return db.get(MemoryDream, dream_id)

    def delete_dream(self, db: Any, dream_id: int) -> bool:
        dream = db.get(MemoryDream, dream_id)
        if dream is None:
            return False
        if str(dream.status or "") == "running":
            raise RuntimeError("运行中的梦境任务不可删除。")
        db.delete(dream)
        db.commit()
        return True


memory_dream_service = MemoryDreamService()
