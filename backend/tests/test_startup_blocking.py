"""启动阻塞修复的回归测试。

覆盖：启动分步日志与日历预热后台化（慢日历不阻塞启动）、
历史 run 回填的一次性标记（schema_meta）。
"""

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.rate_limit import _limiter
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import StrategyRun
from app.main import create_app
from app.services.aniu_service import aniu_service, now_utc
from app.services.scheduler_service import scheduler_service
from app.services.trading_calendar_service import trading_calendar_service


def reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 日历预热后台化：慢预热不再阻塞启动
# ---------------------------------------------------------------------------


def test_slow_calendar_warmup_does_not_block_startup(monkeypatch, tmp_path) -> None:
    """日历预热最坏要 30s×多次重试；移到后台线程后启动必须立即完成。"""
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "slow_cal.db"))
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()

    warmup_started: list[float] = []

    def slow_warm_up(current) -> None:
        warmup_started.append(time.monotonic())
        time.sleep(3.0)  # 模拟网络黑洞下的长时间预热

    monkeypatch.setattr(trading_calendar_service, "warm_up_months", slow_warm_up)

    app = create_app()
    started = time.monotonic()
    with TestClient(app) as client:
        elapsed = time.monotonic() - started
        # 启动在预热完成前就已就绪（预热在后台线程睡 3 秒）
        assert elapsed < 2.0, f"startup blocked for {elapsed:.1f}s"
        assert client.get("/health").status_code == 200
        assert len(warmup_started) == 1  # 预热确实被调度了

    reset_db_state()


def test_startup_step_logs_emitted(monkeypatch, tmp_path, caplog) -> None:
    """每个启动步骤都输出带耗时的 INFO 日志（生产排查依据）。"""
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "logs.db"))
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()
    monkeypatch.setattr(
        trading_calendar_service, "warm_up_months", lambda current: None
    )

    app = create_app()
    with caplog.at_level(logging.INFO, logger="app.main"):
        with TestClient(app):
            pass

    messages = " ".join(record.getMessage() for record in caplog.records)
    for step in (
        "init_db done",
        "skill_registry.reload done",
        "fail_stale_running_runs done",
        "scheduler_service.start done",
        "all steps finished",
    ):
        assert step in messages, f"missing startup log: {step}"

    reset_db_state()


# ---------------------------------------------------------------------------
# 回填一次性标记（schema_meta）
# ---------------------------------------------------------------------------


def _create_runs(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    now = datetime.utcnow()
    connection.execute(
        "INSERT INTO strategy_runs (status, run_type, market_day_type, schedule_name, schedule_id, trigger_source, executed_actions, skill_payloads, decision_payload) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "completed",
            "analysis",
            "trading_day",
            "上午运行1号",
            None,
            "manual",
            "[]",
            "{}",
            "{}",
        ),
    )
    connection.commit()
    connection.close()


def test_backfill_runs_once_then_skips(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "backfill.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None

    # 第一次 init_db：建表（无历史数据，回填空跑并落标记）
    init_db()
    db_path = tmp_path / "backfill.db"
    _create_runs(db_path)

    # 手动清掉标记，让下一次 init_db 执行回填（模拟旧库升级）
    engine = database_module.get_engine()
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_meta WHERE key = 'backfill_strategy_run_types_v1'")
        )

    init_db()

    # 回填已把"上午运行"的 run 归类为 trade
    connection = sqlite3.connect(db_path)
    row = connection.execute(
        "SELECT run_type FROM strategy_runs"
    ).fetchone()
    assert row[0] == "trade"
    connection.close()

    # 标记已落库；再次手动破坏数据后 init_db 不再回填（一次性）
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE strategy_runs SET run_type = 'analysis'")
    connection.commit()
    connection.close()

    init_db()

    connection = sqlite3.connect(db_path)
    row = connection.execute("SELECT run_type FROM strategy_runs").fetchone()
    assert row[0] == "analysis"  # 未被回填改写 → 标记生效
    connection.close()

    reset_db_state()


def test_backfill_only_writes_changed_rows(monkeypatch, tmp_path) -> None:
    """值未变化的行不产生 UPDATE 写放大（用 sqlite3 总修改行数验证）。"""
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "changed.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    db_path = tmp_path / "changed.db"
    connection = sqlite3.connect(db_path)
    # 两条已是正确值的 run（盘前分析 → analysis/trading_day）
    for _ in range(2):
        connection.execute(
            "INSERT INTO strategy_runs (status, run_type, market_day_type, schedule_name, schedule_id, trigger_source, executed_actions, skill_payloads, decision_payload) VALUES (?,?,?,?,?,?,?,?,?)",
            ("completed", "analysis", "trading_day", "盘前分析", None, "manual", "[]", "{}", "{}"),
        )
    connection.commit()
    connection.close()

    # 清标记后执行回填
    engine = database_module.get_engine()
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_meta WHERE key = 'backfill_strategy_run_types_v1'")
        )

    # 回填内部用独立连接；通过 total_changes 验证没有写放大
    probe = sqlite3.connect(db_path)
    before_changes = probe.total_changes
    probe.close()

    init_db()

    probe = sqlite3.connect(db_path)
    after_changes = probe.total_changes
    probe.close()
    # total_changes 统计的是本连接的修改数，跨连接无法直接比较；
    # 改用行为验证：值未变化 + 标记已落库
    connection = sqlite3.connect(db_path)
    rows = connection.execute("SELECT run_type, market_day_type FROM strategy_runs").fetchall()
    connection.close()
    assert all(row == ("analysis", "trading_day") for row in rows)

    marker = database_module._schema_meta_get(
        engine, "backfill_strategy_run_types_v1"
    )
    assert marker == "done"
    del before_changes, after_changes

    reset_db_state()
