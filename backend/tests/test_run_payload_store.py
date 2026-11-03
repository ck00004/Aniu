"""阶段 C：运行载荷冷表（strategy_run_payloads）回归测试。

覆盖：压缩往返、双写幂等、冷表补全热表、批量读取、清空旧字段后详情与
每日报告仍可用、保留窗口清理。
"""

from datetime import date, datetime, timedelta
from pathlib import Path
import sys

import pytest
from sqlalchemy import func, select

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import StrategyRun, StrategyRunPayload
from app.services import daily_report_service
from app.services.aniu_service import aniu_service
from app.services.run_payload_service import (
    compress_json,
    decompress_json,
    run_payload_service,
)


@pytest.fixture()
def db_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "payload.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()
    yield
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def _make_run(**kwargs) -> int:
    payload = {
        "status": "completed",
        "run_type": "analysis",
        "llm_request_payload": {"messages": [{"role": "user", "content": "x" * 2000}]},
        "llm_response_payload": {"usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}},
        "skill_payloads": {
            "tool_calls": [
                {"name": "mx_get_balance", "result": {"ok": True, "result": {"total_assets": 1}}}
            ]
        },
        "decision_payload": {"final_answer": "结论"},
        "executed_actions": [{"action": "BUY", "symbol": "600000"}],
        "final_answer": "结论",
        "started_at": datetime(2026, 11, 3, 1, 0),
        "finished_at": datetime(2026, 11, 3, 1, 5),
    }
    payload.update(kwargs)
    with session_scope() as db:
        run = StrategyRun(**payload)
        db.add(run)
        db.flush()
        return int(run.id)


def test_compress_roundtrip_is_lossless_and_smaller() -> None:
    payload = {"messages": [{"content": "重复文本" * 500}]}
    blob, digest, original, compressed = compress_json(payload)

    assert len(digest) == 64
    assert original > compressed, "gzip 压缩应显著小于原始 JSON"
    assert decompress_json(blob) == payload


def test_store_run_payloads_is_idempotent(db_env) -> None:
    run_id = _make_run()
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        assert run_payload_service.store_run_payloads(db, run) == 4
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        # 再次写入应更新而不是新增
        assert run_payload_service.store_run_payloads(db, run) == 4
    with session_scope() as db:
        assert db.scalar(select(func.count(StrategyRunPayload.id))) == 4
        types = {
            row.payload_type
            for row in db.scalars(select(StrategyRunPayload)).all()
        }
    assert types == {"llm_request", "llm_response", "skill", "decision"}


def test_hydrate_run_restores_cleared_columns(db_env) -> None:
    run_id = _make_run()
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run_payload_service.store_run_payloads(db, run)

    # 模拟 clear-legacy 之后的形态
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run.llm_request_payload = None
        run.llm_response_payload = None
        run.skill_payloads = None
        run.decision_payload = None
        db.add(run)

    with session_scope() as db:
        hydrated = aniu_service.get_run(db, run_id)
        assert hydrated is not None
        assert hydrated.llm_request_payload is not None
        assert hydrated.skill_payloads is not None
        assert hydrated.decision_payload is not None
        # 详情派生字段（工具调用/API 明细）仍能工作
        assert hydrated.api_details is not None


def test_load_payloads_for_runs_batches(db_env) -> None:
    first = _make_run()
    second = _make_run()
    with session_scope() as db:
        for run_id in (first, second):
            run_payload_service.store_run_payloads(db, db.get(StrategyRun, run_id))
    with session_scope() as db:
        grouped = run_payload_service.load_payloads_for_runs(
            db, [first, second], payload_types=("skill",)
        )
    assert set(grouped) == {first, second}
    assert grouped[first]["skill"]["tool_calls"][0]["name"] == "mx_get_balance"


def test_daily_report_uses_cold_payloads_after_legacy_cleared(db_env) -> None:
    run_id = _make_run()
    with session_scope() as db:
        run_payload_service.store_run_payloads(db, db.get(StrategyRun, run_id))
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run.skill_payloads = None
        db.add(run)

    with session_scope() as db:
        report = daily_report_service.DailyReportService().build_daily_report(
            db, target_date=date(2026, 11, 3)
        )
    assert report["run_count"] == 1
    assert report["runs"][0]["evidence"], "清空旧字段后证据预算应从冷表取到内容"


def test_purge_removes_old_payloads_only(db_env) -> None:
    old_id = _make_run(started_at=datetime(2026, 1, 1), finished_at=datetime(2026, 1, 1, 0, 5))
    new_id = _make_run(started_at=datetime(2026, 11, 2), finished_at=datetime(2026, 11, 2, 0, 5))
    with session_scope() as db:
        for run_id in (old_id, new_id):
            run_payload_service.store_run_payloads(db, db.get(StrategyRun, run_id))

    with session_scope() as db:
        removed = run_payload_service.purge_payloads_older_than(db, cutoff_run_id=new_id)
    assert removed == 4
    with session_scope() as db:
        remaining = {int(row.run_id) for row in db.scalars(select(StrategyRunPayload)).all()}
    assert remaining == {new_id}
    # 运行记录本身不受影响（交易事实与结论保留）
    with session_scope() as db:
        assert db.get(StrategyRun, old_id) is not None


def test_delete_run_requires_force_when_intents_reference_it(db_env) -> None:
    """交易意图是审计记录：删除运行前必须显式确认，不能静默断链。"""
    from app.db.models import TradeIntent

    run_id = _make_run()
    with session_scope() as db:
        db.add(
            TradeIntent(
                signal_date="2026-11-03",
                symbol="600000",
                side="BUY",
                tranche_id=1,
                intent_key="2026-11-03:600000:BUY:1",
                run_id=run_id,
                quantity=100,
                status="submitted",
                unknown_checks=0,
            )
        )

    with session_scope() as db:
        with pytest.raises(RuntimeError) as excinfo:
            aniu_service.delete_run(db, run_id)
        assert "交易意图" in str(excinfo.value)

    # force=True：删除运行，但意图记录保留，只解除关联
    with session_scope() as db:
        aniu_service.delete_run(db, run_id, force=True)
    with session_scope() as db:
        intent = db.scalars(select(TradeIntent)).one()
        assert intent.run_id is None
        assert db.get(StrategyRun, run_id) is None
