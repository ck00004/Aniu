"""交易任务手动时间机制的后端兼容性测试。

前端“定时设置-交易任务”支持手动调整运行时间（每时段最多 8 个，
时间限定交易时段窗口）。手动时间最终仍生成标准的
「上午运行N号/下午运行N号」调度（cron 由时间推导，run_type=trade）。
本文件验证后端全链路对这种形态的兼容性：
- 校验层不限制交易日交易任务数量（最多 8+8=16 个）；
- 任意合法 M H * * 1-5 cron 均可解析并计算 next_run_at；
- replace_schedules 全量替换/按 id 复用不重复创建；
- 历史 run_type 回填的名称前缀匹配覆盖 5号-8号。
"""

from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import StrategySchedule
from app.schemas.aniu import ScheduleUpdate
from app.services.aniu_service import aniu_service


def _use_temp_db(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None


def _reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def _manual_session_payloads(
    morning_times: list[tuple[int, int]],
    afternoon_times: list[tuple[int, int]],
    *,
    enabled: bool = True,
    existing_ids: dict[str, int | None] | None = None,
) -> list[ScheduleUpdate]:
    """按前端 buildPayload 手动模式的产物构造请求体。"""
    payloads: list[ScheduleUpdate] = []
    existing_ids = existing_ids or {}

    for index, (hour, minute) in enumerate(morning_times, start=1):
        name = f"上午运行{index}号"
        payloads.append(
            ScheduleUpdate(
                id=existing_ids.get(name),
                name=name,
                run_type="trade",
                market_day_type="trading_day",
                cron_expression=f"{minute} {hour} * * 1-5",
                task_prompt="你正在执行盘中交易操作，你的唯一目标是追求收益最大化。",
                timeout_seconds=1800,
                enabled=enabled,
            )
        )
    for index, (hour, minute) in enumerate(afternoon_times, start=1):
        name = f"下午运行{index}号"
        payloads.append(
            ScheduleUpdate(
                id=existing_ids.get(name),
                name=name,
                run_type="trade",
                market_day_type="trading_day",
                cron_expression=f"{minute} {hour} * * 1-5",
                task_prompt="你正在执行盘中交易操作，你的唯一目标是追求收益最大化。",
                timeout_seconds=1800,
                enabled=enabled,
            )
        )
    return payloads


def test_replace_schedules_accepts_sixteen_manual_trade_tasks(
    monkeypatch, tmp_path
) -> None:
    """每时段 8 个手动时间（合计 16 个交易任务）可被后端完整接收。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    morning_times = [
        (9, 30), (9, 38), (9, 52), (10, 4), (10, 26), (10, 41), (10, 55), (11, 12),
    ]
    afternoon_times = [
        (13, 3), (13, 19), (13, 34), (13, 48), (14, 2), (14, 23), (14, 39), (14, 51),
    ]

    try:
        with session_scope() as db:
            schedules = aniu_service.replace_schedules(
                db, _manual_session_payloads(morning_times, afternoon_times)
            )

            assert len(schedules) == 16
            by_name = {item.name: item for item in schedules}
            for index, (hour, minute) in enumerate(morning_times, start=1):
                item = by_name[f"上午运行{index}号"]
                assert item.run_type == "trade"
                assert item.market_day_type == "trading_day"
                assert item.cron_expression == f"{minute} {hour} * * 1-5"
                assert item.next_run_at is not None
            for index, (hour, minute) in enumerate(afternoon_times, start=1):
                item = by_name[f"下午运行{index}号"]
                assert item.run_type == "trade"
                assert item.cron_expression == f"{minute} {hour} * * 1-5"
                assert item.next_run_at is not None
    finally:
        _reset_db_state()


def test_replace_schedules_reuses_ids_on_resave_without_duplicates(
    monkeypatch, tmp_path
) -> None:
    """再次保存（前端携带已有 id）应更新原任务而不是重复创建。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        with session_scope() as db:
            created = aniu_service.replace_schedules(
                db, _manual_session_payloads([(9, 30), (10, 0)], [(13, 30), (14, 30)])
            )
            existing_ids = {item.name: item.id for item in created}

        with session_scope() as db:
            resaved = aniu_service.replace_schedules(
                db,
                _manual_session_payloads(
                    [(9, 31), (10, 1), (10, 31)],
                    [(13, 31), (14, 31)],
                    existing_ids=existing_ids,
                ),
            )

            assert len(resaved) == 5
            assert len({item.id for item in resaved}) == 5
            by_name = {item.name: item for item in resaved}
            assert by_name["上午运行1号"].cron_expression == "31 9 * * 1-5"
            assert by_name["上午运行3号"].cron_expression == "31 10 * * 1-5"
            # 复用 id 的任务保持原 id
            assert by_name["上午运行1号"].id == existing_ids["上午运行1号"]
            assert by_name["下午运行1号"].id == existing_ids["下午运行1号"]
    finally:
        _reset_db_state()


def test_compute_next_run_at_handles_arbitrary_manual_minutes() -> None:
    """手动时间可产生任意分钟值（非整点/半点），cron 解析必须支持。"""
    shanghai = ZoneInfo("Asia/Shanghai")
    # 周一 09:00（交易时段开始前）
    start = datetime(2026, 10, 19, 9, 0, tzinfo=shanghai)

    result = aniu_service._compute_next_run_at("7 9 * * 1-5", from_time=start)

    assert result is not None
    in_shanghai = result.astimezone(shanghai)
    assert in_shanghai.date().isoformat() == "2026-10-19"
    assert (in_shanghai.hour, in_shanghai.minute) == (9, 7)


def test_compute_next_run_at_handles_afternoon_manual_time() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 10, 19, 12, 0, tzinfo=shanghai)

    result = aniu_service._compute_next_run_at("43 14 * * 1-5", from_time=start)

    assert result is not None
    in_shanghai = result.astimezone(shanghai)
    assert in_shanghai.date().isoformat() == "2026-10-19"
    assert (in_shanghai.hour, in_shanghai.minute) == (14, 43)


def test_validate_schedule_payloads_allows_many_trading_day_trade_tasks() -> None:
    """交易日交易任务数量不受限制（限制仅针对非交易日分析任务）。"""
    payloads = _manual_session_payloads(
        [(9, 30 + i) for i in range(8)],
        [(13, 0 + i) for i in range(8)],
    )

    # 不抛异常即通过
    aniu_service._validate_schedule_payloads(payloads)


def test_backfill_schedule_run_types_prefix_matches_beyond_four(
    monkeypatch, tmp_path
) -> None:
    """历史 run_type 回填的 LIKE 前缀匹配覆盖 上午运行5号-8号。"""
    _use_temp_db(monkeypatch, tmp_path)
    init_db()

    try:
        with session_scope() as db:
            for index in range(1, 9):
                db.add(
                    StrategySchedule(
                        name=f"上午运行{index}号",
                        run_type=None,
                        market_day_type="trading_day",
                        cron_expression=f"0 {9 + index} * * 1-5",
                        task_prompt="legacy",
                        timeout_seconds=1800,
                        enabled=False,
                    )
                )

        database_module._backfill_schedule_run_types(database_module._engine)

        with session_scope() as db:
            schedules = (
                db.query(StrategySchedule)
                .filter(StrategySchedule.name.like("上午运行%"))
                .all()
            )
            assert len(schedules) == 8
            for item in schedules:
                assert item.run_type == "trade", item.name
    finally:
        _reset_db_state()
