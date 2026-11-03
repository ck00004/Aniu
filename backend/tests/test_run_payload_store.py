"""阶段 C：运行载荷冷表（strategy_run_payloads）回归测试。

覆盖：压缩往返、双写幂等、冷表补全热表、批量读取、清空旧字段后详情与
每日报告仍可用、保留窗口清理。
"""

from datetime import date, datetime, timedelta
from pathlib import Path
import sys

import pytest
from sqlalchemy import func, select, text

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


def test_metrics_backfill_can_restore_from_cold_table(db_env) -> None:
    """清空旧字段之后再回填指标，必须能从冷表恢复，而不是写假 0。"""
    from scripts.backfill_run_metrics import _hydrate_payloads_from_cold_table

    run_id = _make_run()
    with session_scope() as db:
        run_payload_service.store_run_payloads(db, db.get(StrategyRun, run_id))
    # 先提交"旧字段已清空"的状态（真实迁移里 clear-legacy 是独立事务）
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run.llm_request_payload = None
        run.llm_response_payload = None
        run.skill_payloads = None
        run.decision_payload = None

    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        source = _hydrate_payloads_from_cold_table(db, run)
        assert source == "cold"
        assert run.llm_response_payload is not None
        assert run.skill_payloads is not None

        aniu_service._hydrate_run_summary_metrics(run)
        assert run.api_call_count == 1
        assert run.input_tokens == 5
        assert run.output_tokens == 6
        assert run.total_tokens == 11

    # DB-02：冷表恢复必须只写会话视图，提交后热表仍然是 NULL
    assert _hot_remaining() == (0, 0), "指标回填把载荷写回了热表"


def test_metrics_backfill_skips_runs_without_payload(db_env) -> None:
    from scripts.backfill_run_metrics import _hydrate_payloads_from_cold_table

    run_id = _make_run(status="failed")
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run.llm_request_payload = None
        run.llm_response_payload = None
        run.skill_payloads = None
        run.decision_payload = None
        assert _hydrate_payloads_from_cold_table(db, run) is None


def test_get_run_does_not_write_payloads_back_to_hot_table(db_env) -> None:
    """冷表补全必须是"只读"的：提交后热表旧字段仍为空。

    历史缺陷：hydrate 用 setattr 会把对象标脏，skill 路由的 session_scope
    一提交就把几 MB 载荷写回 strategy_runs，等于撤销 clear-legacy。
    """
    run_id = _make_run()
    with session_scope() as db:
        run_payload_service.store_run_payloads(db, db.get(StrategyRun, run_id))
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run.llm_request_payload = None
        run.llm_response_payload = None
        run.skill_payloads = None
        run.decision_payload = None
        db.add(run)

    # 会提交的调用路径（等价 skill handler 里的 session_scope）
    with session_scope() as db:
        hydrated = aniu_service.get_run(db, run_id)
        assert hydrated.skill_payloads is not None

    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        assert run.llm_request_payload is None
        assert run.llm_response_payload is None
        assert run.skill_payloads is None
        assert run.decision_payload is None


def test_cleared_legacy_columns_are_sql_null(db_env) -> None:
    """clear-legacy 必须写 SQL NULL，而不是 JSON 文本 "null"。

    历史缺陷：JSON 列默认 none_as_null=False，写 None 会落 4 字节的 "null"，
    导致 `IS NOT NULL` 仍为真，迁移校验/旧字段统计全部失真。
    """
    from sqlalchemy import text

    run_id = _make_run()
    with session_scope() as db:
        run_payload_service.store_run_payloads(db, db.get(StrategyRun, run_id))
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run.llm_request_payload = None
        run.llm_response_payload = None
        run.skill_payloads = None
        run.decision_payload = None

    with session_scope() as db:
        remaining = db.execute(
            text(
                "SELECT SUM(CASE WHEN llm_request_payload IS NOT NULL THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN skill_payloads IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM strategy_runs"
            )
        ).one()
    assert tuple(remaining) == (0, 0)


def _make_bare_run(**kwargs) -> int:
    """创建一条"载荷还在内存里"的运行（模拟收尾前的状态）。"""
    payload = {
        "status": "running",
        "run_type": "analysis",
        "executed_actions": [{"action": "BUY", "symbol": "600000"}],
        "started_at": datetime(2026, 11, 3, 1, 0),
    }
    payload.update(kwargs)
    with session_scope() as db:
        run = StrategyRun(**payload)
        db.add(run)
        db.flush()
        return int(run.id)


def _assign_payloads_in_memory(run) -> None:
    run.llm_request_payload = {"messages": [{"role": "user", "content": "x" * 1000}]}
    run.llm_response_payload = {"usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}}
    run.skill_payloads = {"tool_calls": [{"name": "mx_get_balance", "result": {"ok": True}}]}
    run.decision_payload = {"final_answer": "结论"}


def _hot_remaining() -> tuple[int, int]:
    from sqlalchemy import text

    with session_scope() as db:
        row = db.execute(
            text(
                "SELECT SUM(CASE WHEN llm_request_payload IS NOT NULL THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN skill_payloads IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM strategy_runs"
            )
        ).one()
    return int(row[0] or 0), int(row[1] or 0)


def _simulate_run_finalize(
    run_id: int,
    *,
    payloads: dict,
    fail_cold_store: bool = False,
    monkeypatch=None,
) -> None:
    """复刻 aniu_service._run_body 的收尾顺序（独立事务写冷表 → 再写运行状态）。"""
    from types import SimpleNamespace

    if fail_cold_store:
        # 用真实 SQLite 触发器让 INSERT 失败（不是 mock），验证回滚与兜底
        with session_scope() as db:
            db.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS fail_payload_insert "
                    "BEFORE INSERT ON strategy_run_payloads "
                    "BEGIN SELECT RAISE(ABORT, 'forced payload insert failure'); END;"
                )
            )
            db.commit()

    metrics_source = SimpleNamespace(
        llm_request_payload=payloads.get("llm_request"),
        llm_response_payload=payloads.get("llm_response"),
        skill_payloads=payloads.get("skill"),
        decision_payload=payloads.get("decision"),
        executed_actions=[{"action": "BUY", "symbol": "600000"}],
    )
    aniu_service._hydrate_run_summary_metrics(metrics_source)
    cold_stored = run_payload_service.store_run_payloads_isolated(
        run_id=run_id, payloads=payloads
    )

    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        aniu_service._apply_run_payload_storage(
            run,
            cold_stored=cold_stored,
            llm_request=payloads.get("llm_request"),
            llm_response=payloads.get("llm_response"),
            skill_payloads=payloads.get("skill"),
            decision=payloads.get("decision"),
        )
        run.api_call_count = metrics_source.api_call_count
        run.total_tokens = metrics_source.total_tokens
        run.executed_trade_count = metrics_source.executed_trade_count
        run.status = "completed"

    if fail_cold_store:
        with session_scope() as db:
            db.execute(text("DROP TRIGGER IF EXISTS fail_payload_insert"))
            db.commit()


_FAULT_PAYLOADS = {
    "llm_request": {"messages": [{"role": "user", "content": "x" * 500}]},
    "llm_response": {"usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}},
    "skill": {"tool_calls": [{"name": "mx_get_balance", "result": {"ok": True}}]},
    "decision": {"final_answer": "结论"},
}


def test_finalize_writes_payloads_to_cold_table_only(db_env) -> None:
    """DB-01：冷表落盘成功后，热表四个大字段保持 SQL NULL。"""
    run_id = _make_bare_run()
    _simulate_run_finalize(run_id, payloads=_FAULT_PAYLOADS)

    assert _hot_remaining() == (0, 0), "热表仍在持久化原始载荷"
    with session_scope() as db:
        assert db.scalar(select(func.count(StrategyRunPayload.id))) == 4
        run = db.get(StrategyRun, run_id)
        assert run.payload_storage == "cold"
        assert run.metrics_status == "complete"
        assert run.api_call_count == 1
        assert run.total_tokens == 11
        assert run.status == "completed"
    with session_scope() as db:
        hydrated = aniu_service.get_run(db, run_id)
        assert hydrated.skill_payloads is not None


def test_finalize_falls_back_to_hot_table_on_real_insert_failure(db_env) -> None:
    """DB-07：真实 INSERT 失败（触发器）时，冷表零残留 + 热表兜底提交。"""
    run_id = _make_bare_run()
    _simulate_run_finalize(run_id, payloads=_FAULT_PAYLOADS, fail_cold_store=True)

    with session_scope() as db:
        # 冷表插入被回滚，不能留下半份数据
        assert db.scalar(select(func.count(StrategyRunPayload.id))) == 0
        run = db.get(StrategyRun, run_id)
        assert run.payload_storage == "hot_fallback"
        assert run.llm_request_payload is not None
        assert run.skill_payloads is not None
        # 运行本身仍然正常完成，交易事实不丢
        assert run.status == "completed"
        assert run.api_call_count == 1
        assert run.total_tokens == 11

    # 触发器移除后可重新迁移到冷表
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        stored = run_payload_service.store_run_payloads(db, run)
        run.payload_storage = "cold"
        run.llm_request_payload = None
        run.llm_response_payload = None
        run.skill_payloads = None
        run.decision_payload = None
        db.add(run)
    assert stored == 4
    assert _hot_remaining() == (0, 0)


def test_cold_payload_survives_when_run_update_commit_fails(db_env) -> None:
    """DB-07 核心收益：冷表已独立提交后，运行状态提交失败也不会丢载荷。"""
    run_id = _make_bare_run()
    payloads = dict(_FAULT_PAYLOADS)

    # 1) 冷表独立事务：成功提交
    assert (
        run_payload_service.store_run_payloads_isolated(
            run_id=run_id, payloads=payloads
        )
        is True
    )

    # 2) 让运行收尾的 UPDATE 必然失败
    with session_scope() as db:
        db.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS fail_run_update "
                "BEFORE UPDATE ON strategy_runs "
                "BEGIN SELECT RAISE(ABORT, 'forced run update failure'); END;"
            )
        )
        db.commit()

    failed = False
    try:
        with session_scope() as db:
            run = db.get(StrategyRun, run_id)
            run.status = "completed"
            run.finished_at = datetime(2026, 11, 3, 2, 0)
            db.add(run)
    except Exception:
        failed = True

    with session_scope() as db:
        db.execute(text("DROP TRIGGER IF EXISTS fail_run_update"))
        db.commit()

    assert failed, "故障注入未生效，测试无意义"
    with session_scope() as db:
        # 冷表载荷仍在（独立事务已提交）
        assert db.scalar(select(func.count(StrategyRunPayload.id))) == 4
        # 运行状态回滚为原值，但载荷可以从冷表恢复
        run = db.get(StrategyRun, run_id)
        assert run.status == "running"
        assert run_payload_service.load_run_payloads(db, run_id).get("skill")


# ---------------------------------------------------------------------------
# DB-08：收尾提交失败 ≠ 业务失败
# ---------------------------------------------------------------------------


def test_finalization_guard_parks_run_and_skips_business_failure(db_env) -> None:
    """冷表已提交后收尾事务失败：park 成 finalization_pending，不标 failed。"""
    from app.services.aniu_service import RunFinalizationUncommitted

    run_id = _make_bare_run(status="running")
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run.executed_actions = [{"action": "BUY", "symbol": "600000", "quantity": 100}]

    # 冷表独立事务先成功
    assert run_payload_service.store_run_payloads_isolated(
        run_id=run_id, payloads=_FAULT_PAYLOADS
    )

    # 收尾事务被强制失败
    with session_scope() as db:
        db.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS fail_run_update2 "
                "BEFORE UPDATE ON strategy_runs WHEN NEW.status = 'completed' "
                "BEGIN SELECT RAISE(ABORT, 'forced finalize failure'); END;"
            )
        )
        db.commit()
    try:
        with pytest.raises(RunFinalizationUncommitted):
            with aniu_service._finalization_guard(run_id), session_scope() as db:
                run = db.get(StrategyRun, run_id)
                run.status = "completed"
                db.add(run)
    finally:
        with session_scope() as db:
            db.execute(text("DROP TRIGGER IF EXISTS fail_run_update2"))
            db.commit()

    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        # 不是 failed —— 这是"收尾失败"，不是"交易失败"
        assert run.status == "finalization_pending"
        assert "收尾提交失败" in str(run.error_message)
        # 冷表载荷仍然存在
        assert db.scalar(select(func.count(StrategyRunPayload.id))) == 4


def test_recovery_converges_pending_run_with_metrics_and_trades(db_env) -> None:
    """启动恢复：从冷表还原指标、补齐交易事实、收敛状态。"""
    run_id = _make_bare_run(status="running")
    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        run.executed_actions = [
            {"action": "BUY", "symbol": "600000", "quantity": 100, "status": "submitted"}
        ]

    assert run_payload_service.store_run_payloads_isolated(
        run_id=run_id, payloads=_FAULT_PAYLOADS
    )
    aniu_service._persist_archive_markers(
        run_id=run_id,
        payload_storage="cold",
        metrics_status="complete",
        metrics={"api_call_count": None, "executed_trade_count": None,
                 "input_tokens": None, "output_tokens": None, "total_tokens": None},
        executed_actions=[{"action": "BUY", "symbol": "600000", "quantity": 100}],
    )
    aniu_service._park_finalization_pending(run_id, RuntimeError("boom"))

    from app.db.models import TradeOrder

    with session_scope() as db:
        recovered = aniu_service.recover_finalization_pending_runs(db)
    assert recovered == 1

    with session_scope() as db:
        run = db.get(StrategyRun, run_id)
        assert run.status == "completed"
        assert run.payload_storage == "cold"
        # 指标从冷表重算，而不是按空热表算成 0
        assert run.api_call_count == 1
        assert run.total_tokens == 11
        assert run.finished_at is not None
        orders = db.scalars(
            select(TradeOrder).where(TradeOrder.run_id == run_id)
        ).all()
        assert len(orders) == 1 and orders[0].symbol == "600000"
    # 恢复过程不得把载荷写回热表
    assert _hot_remaining() == (0, 0)


def test_cold_store_rejects_missing_digest(db_env) -> None:
    """DB-10：冷表写入后若摘要缺失/损坏，必须判定失败（走热表兜底）。"""
    run_id = _make_bare_run()
    payloads = {"llm_request": {"m": ["x"]}}

    # 让写入的 sha256 变成空串（模拟摘要缺失）
    import app.services.run_payload_service as rps

    original_sha = rps._sha256
    try:
        rps._sha256 = lambda raw: ""
        ok = run_payload_service.store_run_payloads_isolated(
            run_id=run_id, payloads=payloads
        )
    finally:
        rps._sha256 = original_sha

    assert ok is False
    with session_scope() as db:
        assert db.scalar(select(func.count(StrategyRunPayload.id))) == 0
