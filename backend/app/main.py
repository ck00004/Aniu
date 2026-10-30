from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Thread
from zoneinfo import ZoneInfo
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.router import router as aniu_router
from app.core.config import get_settings
from app.core.rate_limit import rate_limit_middleware
from app.db.database import init_db
from app.db.database import session_scope
from app.services.aniu_service import aniu_service
from app.services.memory_dream_service import memory_dream_service
from app.services.scheduler_service import scheduler_service
from app.services.skill_admin_service import skill_admin_service
from app.services.trading_calendar_service import trading_calendar_service
from app.skills import skill_registry

logger = logging.getLogger(__name__)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# Directory where the Vite build output is placed (only exists in Docker image)
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _configure_app_logging() -> None:
    """保证应用日志在 uvicorn 下可见。

    uvicorn 默认只配置自己的 logger（uvicorn/uvicorn.error/uvicorn.access），
    应用 logger 的 INFO 输出没有 handler 会完全不可见——启动分步日志
    也就无法用于排查。仅在根 logger 无任何 handler 时挂载（pytest 的
    caplog 已自带 handler，不干预以免重复输出）。
    """
    if logging.getLogger().handlers:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(levelname)s:     %(name)s : %(message)s")
    )
    for logger_name in ("app", "skills"):
        target = logging.getLogger(logger_name)
        if not target.handlers:
            target.addHandler(handler)
        target.setLevel(logging.INFO)


def _run_with_lock_retry(label: str, action, attempts: int = 3) -> None:
    """启动步骤的数据库锁重试。

    典型场景：--reload 重载或旧实例退出时，旧进程的写入尚未释放，
    新进程启动即撞上写锁。busy timeout（30s）已兜底大部分情况，
    这里再叠加一层显式重试，避免极端窗口下启动直接失败。
    """
    for attempt in range(1, attempts + 1):
        try:
            action()
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= attempts:
                raise
            logger.warning(
                "app startup: %s hit database lock (attempt %d/%d), retrying in 2s: %s",
                label,
                attempt,
                attempts,
                exc,
            )
            time.sleep(2)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    # 每个启动步骤都带计时 INFO 日志：生产环境 "Waiting for application
    # startup." 卡住时可直接从日志看到卡在哪一步、耗时多少。
    started_at = time.monotonic()

    def _log_step(label: str, step_started: float) -> float:
        now = time.monotonic()
        logger.info("app startup: %s done in %.2fs", label, now - step_started)
        return now

    step = time.monotonic()
    _run_with_lock_retry("init_db", init_db)
    step = _log_step("init_db", step)

    skill_registry.reload()
    step = _log_step("skill_registry.reload", step)

    def _restore_runtime_state() -> None:
        with session_scope() as db:
            skill_admin_service.apply_persisted_state(db)
            # 进程重启后遗留的 running 状态运行在此收敛为 failed，
            # 避免僵尸记录阻塞删除并让前端事件流永久挂起。
            try:
                aniu_service.fail_stale_running_runs(db)
            except Exception as exc:
                logger.warning("failed to mark stale running runs: %s", exc)
            try:
                memory_dream_service.fail_stale_running_dreams(db)
            except Exception as exc:
                logger.warning("failed to mark stale running dreams: %s", exc)
        # 补丁 C 启动恢复：进程崩溃/重启时正在发送的交易意图状态不明，
        # 收敛为 UNKNOWN（阻塞同标的同方向的后续提交直到核对），绝不
        # 把未完成操作当作新操作重新执行。
        try:
            from app.services.trade_intent_service import trade_intent_service

            trade_intent_service.reconcile_stale_executing_intents()
        except Exception as exc:
            logger.warning("failed to reconcile stale trade intents: %s", exc)

    _run_with_lock_retry("restore_runtime_state", _restore_runtime_state)
    step = _log_step("restore_runtime_state", step)

    # 日历预热放到后台线程：预热涉及远程日历接口（最坏 4 次重试 ×
    # 30 秒超时），网络黑洞时会阻塞启动关键路径数分钟。运行期的懒加载
    # 与失败负缓存会接管，调度循环对 is_trading_day 的异常同样有容忍。
    today = datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ).date()

    def _warm_up_calendar() -> None:
        try:
            trading_calendar_service.warm_up_months(today)
            logger.info("app startup: calendar warm-up finished (background)")
        except Exception as exc:
            logger.warning("交易日历预热失败，将在运行期重试: %s", exc)

    Thread(
        target=_warm_up_calendar,
        name="aniu-calendar-warmup",
        daemon=True,
    ).start()
    step = _log_step("calendar warm-up dispatched to background", step)

    scheduler_service.start()
    step = _log_step("scheduler_service.start", step)

    logger.info(
        "app startup: all steps finished in %.2fs, serving requests",
        time.monotonic() - started_at,
    )
    try:
        yield
    finally:
        scheduler_service.stop()


def create_app() -> FastAPI:
    _configure_app_logging()
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=app_lifespan)

    if "*" in settings.cors_allow_origins:
        logger.warning(
            "CORS is configured with wildcard '*'. "
            "Set CORS_ALLOW_ORIGINS to specific origins in production."
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)

    app.include_router(aniu_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ── Serve frontend static files when built output exists (Docker) ──
    _serve_frontend(app)

    return app


def _serve_frontend(app: FastAPI) -> None:
    """Mount Vite build output and add SPA catch-all fallback.

    Only activates when ``static/index.html`` exists (i.e. inside the Docker
    image).  In local dev mode the directory doesn't exist, so this is a no-op
    and the Vite dev server handles the frontend as before.
    """
    index_html = _STATIC_DIR / "index.html"
    if not _STATIC_DIR.is_dir() or not index_html.is_file():
        return

    logger.info("Serving frontend from %s", _STATIC_DIR)

    # Serve hashed assets (JS/CSS/images) under /assets
    assets_dir = _STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # SPA catch-all: return index.html for frontend routes only.
    # Exclude /api and /health so that unknown API paths still return proper 404.
    # If the requested path matches a real file under static/, serve it directly
    # (e.g. /aniu.ico, /favicon.ico, /robots.txt).
    _index_content = index_html.read_text(encoding="utf-8")

    @app.get("/{full_path:path}", response_model=None, include_in_schema=False)
    async def spa_fallback(request: Request, full_path: str):
        if full_path.startswith(("api/", "health")):
            return HTMLResponse(content="Not Found", status_code=404)

        if full_path == "favicon.ico":
            fallback_favicon = _STATIC_DIR / "aniu.ico"
            if fallback_favicon.is_file():
                return FileResponse(str(fallback_favicon))

        # Serve real static files (favicon, icons, etc.)
        candidate = _STATIC_DIR / full_path
        if (
            full_path
            and candidate.is_file()
            and _STATIC_DIR in candidate.resolve().parents
        ):
            return FileResponse(str(candidate))

        return HTMLResponse(content=_index_content)


app = create_app()
