from __future__ import annotations

import json
from datetime import date, datetime, timezone

from typing import Any

from fastapi import Body, APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db, session_scope
from app.db.models import StrategyRun
from app.schemas.aniu import (
    AccountOverviewDebugRead,
    AccountOverviewRead,
    AppSettingsRead,
    AppSettingsUpdate,
    ChatAttachmentRead,
    ChatMessageRead,
    ChatRequest,
    ChatResponse,
    ChatSessionCreate,
    ChatSessionMessagesPageRead,
    ChatSessionRead,
    ChatSessionUpdate,
    PersistentSessionMessagesPageRead,
    PersistentSessionRead,
    ChatStreamRequest,
    LoginRequest,
    LoginResponse,
    MemoryActivityListRead,
    MemoryCreateRequest,
    MemoryDreamListRead,
    MemoryDreamRead,
    MemoryDreamRunResponse,
    MemoryItemRead,
    MemoryListRead,
    MemoryUpdateRequest,
    RunDetailRead,
    RawToolPreviewDetailRead,
    RunSummaryRead,
    RunSummaryPageRead,
    RuntimeOverviewRead,
    ScheduleRead,
    ScheduleUpdate,
    SkillImportClawHubRequest,
    SkillImportSkillHubRequest,
    SkillInfoRead,
    SkillListItemRead,
)
from app.services.aniu_service import aniu_service
from app.services.chat_session_service import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_UPLOAD_BYTES,
    chat_session_service,
)
from app.services.event_bus import event_bus
from app.services.daily_report_service import daily_report_service
from app.services.memory_dream_service import memory_dream_service
from app.services.memory_service import MemoryVersionConflict, memory_service
from app.services.run_abort_service import run_abort_registry
from app.services.skill_admin_service import (
    MAX_SKILL_ARCHIVE_BYTES,
    skill_admin_service,
)
from app.services.tool_invocation_log_service import tool_invocation_log_service

router = APIRouter(prefix="/api/aniu", tags=["aniu"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    try:
        return aniu_service.authenticate_login(payload.password)
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.get("/settings", response_model=AppSettingsRead)
def get_settings(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> AppSettingsRead:
    return aniu_service.get_or_create_settings(db)


@router.put("/settings", response_model=AppSettingsRead)
def update_settings(
    payload: AppSettingsUpdate,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> AppSettingsRead:
    return aniu_service.update_settings(db, payload)


@router.get("/skills", response_model=list[SkillListItemRead])
def list_skills(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[SkillListItemRead]:
    return skill_admin_service.list_skills(db)


@router.post("/skills/import-clawhub", response_model=SkillInfoRead)
def import_clawhub_skill(
    payload: SkillImportClawHubRequest,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    try:
        return skill_admin_service.import_from_clawhub(db, payload.slug_or_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/skills/import-skillhub", response_model=SkillInfoRead)
def import_skillhub_skill(
    payload: SkillImportSkillHubRequest,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    try:
        return skill_admin_service.import_from_skillhub(db, payload.slug_or_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/skills/import-zip", response_model=SkillInfoRead)
def import_zip_skill(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    archive_bytes = file.file.read(MAX_SKILL_ARCHIVE_BYTES + 1)
    if len(archive_bytes) > MAX_SKILL_ARCHIVE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"技能压缩包过大，当前最多支持 "
                f"{MAX_SKILL_ARCHIVE_BYTES // (1024 * 1024)}MB。"
            ),
        )
    try:
        return skill_admin_service.import_from_zip(
            db,
            filename=file.filename or "skill.zip",
            archive_bytes=archive_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/skills/reload", response_model=list[SkillListItemRead])
def reload_skills(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[SkillListItemRead]:
    return skill_admin_service.reload(db)


@router.post("/skills/{skill_id}/enable", response_model=SkillInfoRead)
def enable_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    try:
        return skill_admin_service.set_enabled(db, skill_id=skill_id, enabled=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/skills/{skill_id}/disable", response_model=SkillInfoRead)
def disable_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    try:
        return skill_admin_service.set_enabled(db, skill_id=skill_id, enabled=False)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/skills/{skill_id}", status_code=204)
def delete_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> None:
    try:
        skill_admin_service.delete_skill(db, skill_id=skill_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/schedule", response_model=list[ScheduleRead])
def get_schedule(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[ScheduleRead]:
    return aniu_service.list_schedules(db)


@router.put("/schedule", response_model=list[ScheduleRead])
def update_schedule(
    payload: list[ScheduleUpdate],
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[ScheduleRead]:
    return aniu_service.replace_schedules(db, payload)


@router.post("/run", response_model=RunDetailRead)
def run_once(
    schedule_id: int | None = Query(default=None, ge=1),
    run_type: Literal["analysis", "trade"] | None = Query(default=None),
    _user: str = Depends(get_current_user),
) -> RunDetailRead:
    try:
        return aniu_service.execute_run(
            trigger_source="manual",
            schedule_id=schedule_id,
            manual_run_type=run_type,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/run-stream")
def run_stream(
    schedule_id: int | None = Query(default=None, ge=1),
    run_type: Literal["analysis", "trade"] | None = Query(default=None),
    _user: str = Depends(get_current_user),
) -> dict:
    """Launch a run in the background and return run_id immediately.

    Subscribe to progress events via GET /api/aniu/runs/{run_id}/events.
    """
    try:
        run_id = aniu_service.start_run_async(
            trigger_source="manual",
            schedule_id=schedule_id,
            manual_run_type=run_type,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"run_id": run_id}


@router.post("/runs/{run_id}/abort")
def abort_run(
    run_id: int,
    _user: str = Depends(get_current_user),
) -> dict:
    """请求中止一个进行中的运行。

    运行线程在 LLM 迭代、工具调用与交易执行等检查点感知中止并停止；
    中止请求优先于后续真实交易。
    """
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行记录不存在。")
        if str(run.status or "") != "running":
            raise HTTPException(status_code=409, detail="该运行已结束，无法中止。")

    if not run_abort_registry.abort(run_id):
        # run 已是 running 状态但注册表无活动事件（如进程刚重启），
        # 直接把记录收敛为失败，避免僵尸运行。
        with session_scope() as db:
            run = db.get(StrategyRun, run_id)
            if run is not None and str(run.status or "") == "running":
                run.status = "failed"
                run.error_message = "运行已被用户中止。"
                run.finished_at = datetime.now(timezone.utc)
                db.add(run)
    return {"run_id": run_id, "aborted": True}


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: int,
    after_seq: int | None = Query(default=None, ge=0),
    _user: str = Depends(get_current_user),
) -> StreamingResponse:
    # 订阅不存在的 run 会得到只有心跳的无限流；先校验存在性。
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行记录不存在。")

    def _generator():
        try:
            for event in event_bus.stream(run_id, after_seq=after_seq):
                event_type = str(event.get("type") or "message")
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = json.dumps({"type": "failed", "message": str(exc)}, ensure_ascii=False)
            yield f"event: failed\ndata: {err}\n\n"

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/memories", response_model=MemoryListRead)
def list_memories(
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> MemoryListRead:
    payload = memory_service.list_memories(
        db, query=query, limit=limit, offset=offset
    )
    return MemoryListRead(**payload)


@router.post("/memories", response_model=MemoryItemRead)
def create_memory(
    payload: MemoryCreateRequest,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> MemoryItemRead:
    try:
        item = memory_service.create_memory(
            db, content=payload.content, reason=payload.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MemoryItemRead.model_validate(item, from_attributes=True)


@router.put("/memories/{memory_id}", response_model=MemoryItemRead)
def update_memory(
    memory_id: int,
    payload: MemoryUpdateRequest,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> MemoryItemRead:
    try:
        item = memory_service.update_memory(
            db,
            memory_id=memory_id,
            content=payload.content,
            reason=payload.reason,
            expected_version=payload.expected_version,
        )
    except MemoryVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MemoryItemRead.model_validate(item, from_attributes=True)


@router.delete("/memories/{memory_id}")
def delete_memory(
    memory_id: int,
    expected_version: int = Query(ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> dict:
    try:
        memory_service.delete_memory(
            db, memory_id=memory_id, expected_version=expected_version
        )
    except MemoryVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True, "memory_id": memory_id}


@router.get("/trade-intents", response_model=list[dict[str, Any]])
def list_trade_intents(
    symbol: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _user: str = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """交易意图列表（含 UNKNOWN/SUBMITTED_UNTRACKED 待人工核验项）。"""
    from app.services.trade_intent_service import trade_intent_service

    intents = trade_intent_service.list_intents(
        symbol=symbol,
        limit=limit,
    )
    return [
        {
            "id": item.id,
            "signal_date": item.signal_date,
            "symbol": item.symbol,
            "side": item.side,
            "tranche_id": item.tranche_id,
            "intent_key": item.intent_key,
            "run_id": item.run_id,
            "action_id": item.action_id,
            "provider_order_id": item.provider_order_id,
            "quantity": item.quantity,
            "status": item.status,
            "note": item.note,
            "unknown_checks": item.unknown_checks,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }
        for item in intents
    ]


@router.post(
    "/trade-intents/{intent_id}/bind-order", response_model=dict[str, Any]
)
def bind_trade_intent_order(
    intent_id: int,
    payload: dict[str, Any] = Body(...),
    _user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """人工核验后为 submitted_untracked/unknown 意图绑定远端单号。

    绑定后状态同步恢复单号精确匹配；不改变阻塞语义（第七轮复审 P1）。
    """
    from app.services.trade_intent_service import trade_intent_service

    order_id = str(payload.get("provider_order_id") or "").strip()
    if not order_id:
        raise HTTPException(status_code=422, detail="缺少 provider_order_id。")
    try:
        intent = trade_intent_service.bind_provider_order_id(
            intent_id,
            order_id,
            expected_symbol=payload.get("symbol"),
            expected_side=payload.get("side"),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if intent is None:
        raise HTTPException(status_code=404, detail="交易意图不存在。")
    return {
        "bound": True,
        "intent_id": intent.id,
        "provider_order_id": intent.provider_order_id,
        "status": intent.status,
    }


@router.get("/memory-dreams", response_model=MemoryDreamListRead)
def list_memory_dreams(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> MemoryDreamListRead:
    payload = memory_dream_service.list_dreams(db, limit=limit, offset=offset)
    return MemoryDreamListRead(**payload)


@router.get("/memory-dreams/{dream_id}", response_model=MemoryDreamRead)
def get_memory_dream(
    dream_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> MemoryDreamRead:
    dream = memory_dream_service.get_dream(db, dream_id)
    if dream is None:
        raise HTTPException(status_code=404, detail="梦境任务不存在。")
    return MemoryDreamRead.model_validate(dream, from_attributes=True)


@router.post("/memory-dreams/run", response_model=MemoryDreamRunResponse)
def run_memory_dream(
    target_date: date | None = Query(default=None, alias="date"),
    _user: str = Depends(get_current_user),
) -> MemoryDreamRunResponse:
    """手动触发记忆梦境（默认整理昨天）；已完成/失败的任务会重置重跑。"""
    try:
        dream, started = memory_dream_service.run_dream_async(target_date=target_date)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if dream is None:
        raise HTTPException(status_code=500, detail="创建梦境任务失败。")
    return MemoryDreamRunResponse(
        dream=MemoryDreamRead.model_validate(dream, from_attributes=True),
        started=started,
    )


@router.delete("/memory-dreams/{dream_id}")
def delete_memory_dream(
    dream_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> dict:
    try:
        deleted = memory_dream_service.delete_dream(db, dream_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="梦境任务不存在。")
    return {"deleted": True, "dream_id": dream_id}


@router.get("/memory-activities", response_model=MemoryActivityListRead)
def list_memory_activities(
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> MemoryActivityListRead:
    payload = memory_service.list_activities(db, limit=limit, offset=offset)
    return MemoryActivityListRead(**payload)


@router.get("/tool-invocation-logs")
def list_tool_invocation_logs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> dict:
    """上游工具调用日志（每次妙想接口调用的状态/耗时/响应规模）。"""
    payload = tool_invocation_log_service.list_logs(
        db,
        limit=limit,
        offset=offset,
        status=status,
    )
    payload["summary"] = tool_invocation_log_service.summarize(db)
    return payload


@router.get("/runs/daily-report")
def get_daily_run_report(
    run_date: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> dict:
    """每日运行报告（上海日历日聚合，含证据预算与当日交易汇总）。"""
    return daily_report_service.build_daily_report(db, target_date=run_date)


@router.get("/runs", response_model=list[RunSummaryRead])
def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    run_date: date | None = Query(default=None, alias="date"),
    status: str | None = Query(default=None),
    before_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[RunSummaryRead]:
    return aniu_service.list_runs(
        db,
        limit=limit,
        run_date=run_date,
        status=status,
        before_id=before_id,
    )


@router.get("/runs-feed", response_model=RunSummaryPageRead)
def list_runs_feed(
    limit: int = Query(default=20, ge=1, le=100),
    run_date: date | None = Query(default=None, alias="date"),
    status: str | None = Query(default=None),
    before_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> RunSummaryPageRead:
    return aniu_service.list_runs_page(
        db,
        limit=limit,
        run_date=run_date,
        status=status,
        before_id=before_id,
    )


@router.get("/runs/{run_id}", response_model=RunDetailRead)
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> RunDetailRead:
    run = aniu_service.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在。")
    return run


@router.get(
    "/runs/{run_id}/raw-tool-previews/{preview_index}",
    response_model=RawToolPreviewDetailRead,
)
def get_run_raw_tool_preview(
    run_id: int,
    preview_index: int = Path(ge=0),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> RawToolPreviewDetailRead:
    try:
        return aniu_service.get_run_raw_tool_preview(db, run_id, preview_index)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(
    run_id: int,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> None:
    try:
        aniu_service.delete_run(db, run_id, force=force)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runtime-overview", response_model=RuntimeOverviewRead)
def get_runtime_overview(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> RuntimeOverviewRead:
    return aniu_service.get_runtime_overview(db)


@router.get("/account", response_model=AccountOverviewRead)
def get_account(
    force_refresh: bool = Query(default=False),
    _user: str = Depends(get_current_user),
) -> AccountOverviewRead:
    try:
        return aniu_service.get_account_overview(force_refresh=force_refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/account/debug", response_model=AccountOverviewDebugRead)
def get_account_debug(
    force_refresh: bool = Query(default=False),
    _user: str = Depends(get_current_user),
) -> AccountOverviewDebugRead:
    try:
        return aniu_service.get_account_overview(
            include_raw=True,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    _user: str = Depends(get_current_user),
) -> ChatResponse:
    try:
        return aniu_service.chat(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chat-stream")
def chat_stream(
    payload: ChatRequest,
    _user: str = Depends(get_current_user),
) -> StreamingResponse:
    try:
        event_iter = aniu_service.chat_stream(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _generator():
        try:
            for event in event_iter:
                event_type = str(event.get("type") or "message")
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = json.dumps({"type": "failed", "message": str(exc)}, ensure_ascii=False)
            yield f"event: failed\ndata: {err}\n\n"

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/chat/sessions", response_model=list[ChatSessionRead])
def list_chat_sessions(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[ChatSessionRead]:
    return chat_session_service.list_sessions(db)


@router.post("/chat/sessions", response_model=ChatSessionRead)
def create_chat_session(
    payload: ChatSessionCreate | None = None,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> ChatSessionRead:
    title = payload.title if payload else None
    result = chat_session_service.create_session(db, title=title)
    db.commit()
    return result


@router.patch("/chat/sessions/{session_id}", response_model=ChatSessionRead)
def rename_chat_session(
    session_id: int,
    payload: ChatSessionUpdate,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> ChatSessionRead:
    try:
        result = chat_session_service.rename_session(
            db, session_id, title=payload.title
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return result


@router.delete("/chat/sessions/{session_id}", status_code=204)
def delete_chat_session(
    session_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> None:
    try:
        chat_session_service.delete_session(db, session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()


@router.get(
    "/chat/sessions/{session_id}/messages",
    response_model=ChatSessionMessagesPageRead,
)
def list_chat_messages(
    session_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> ChatSessionMessagesPageRead:
    try:
        session, messages, next_before_id, has_more = chat_session_service.list_messages(
            db,
            session_id,
            limit=limit,
            before_id=before_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "session": session.model_dump(mode="json"),
        "messages": [m.model_dump(mode="json") for m in messages],
        "next_before_id": next_before_id,
        "has_more": has_more,
    }


@router.get("/persistent-session", response_model=PersistentSessionRead)
def get_persistent_session(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> PersistentSessionRead:
    return aniu_service.get_persistent_session(db)


@router.post("/persistent-session/reset", response_model=PersistentSessionRead)
def reset_persistent_session(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> PersistentSessionRead:
    return aniu_service.reset_persistent_session(db)


@router.get(
    "/persistent-session/messages",
    response_model=PersistentSessionMessagesPageRead,
)
def list_persistent_session_messages(
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> PersistentSessionMessagesPageRead:
    session, messages, next_before_id, has_more = aniu_service.list_persistent_session_messages(
        db,
        limit=limit,
        before_id=before_id,
    )
    return {
        "session": session.model_dump(mode="json"),
        "messages": [m.model_dump(mode="json") for m in messages],
        "next_before_id": next_before_id,
        "has_more": has_more,
    }


@router.post("/chat/uploads", response_model=ChatAttachmentRead)
def upload_chat_attachment(
    file: UploadFile = File(...),
    session_id: int | None = Form(default=None),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> ChatAttachmentRead:
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大，最大允许 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB。",
        )
    try:
        result = chat_session_service.save_attachment(
            db,
            filename=file.filename or "upload.bin",
            mime_type=file.content_type or "application/octet-stream",
            data=data,
            session_id=session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/chat/uploads/{attachment_id}")
def download_chat_attachment(
    attachment_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> FileResponse:
    try:
        path, mime_type, filename = chat_session_service.get_attachment_file(
            db, attachment_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(str(path), media_type=mime_type, filename=filename)


@router.post("/chat/stream")
def chat_session_stream(
    payload: ChatStreamRequest,
    _user: str = Depends(get_current_user),
) -> StreamingResponse:
    try:
        event_iter = chat_session_service.stream_chat(payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _generator():
        try:
            for event in event_iter:
                event_type = str(event.get("type") or "message")
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = json.dumps(
                {"type": "failed", "message": str(exc)}, ensure_ascii=False
            )
            yield f"event: failed\ndata: {err}\n\n"

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers=headers,
    )
