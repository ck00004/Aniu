"""阶段 E：数据库维护与生命周期管理。

    # 体检报告（表/索引/载荷规模、复合索引覆盖提示）
    .venv/bin/python -m scripts.db_maintenance report

    # 统计信息（ANALYZE + PRAGMA optimize），让查询计划更准
    .venv/bin/python -m scripts.db_maintenance analyze

    # WAL checkpoint（清空 -wal，不做数据变更）
    .venv/bin/python -m scripts.db_maintenance checkpoint

    # 回收磁盘（先自动备份；只有清空旧字段后才有明显收益）
    .venv/bin/python -m scripts.db_maintenance vacuum --yes

    # 保留策略：冷表载荷 / 工具日志 / 记忆活动
    .venv/bin/python -m scripts.db_maintenance retention --payload-days 180 \
        --tool-log-days 90 --memory-activity-days 365 --apply

安全原则：
* 交易事实（trade_orders / trade_intents / self_select_intents）、运行结论
  (`analysis_summary` / `final_answer`) 与执行动作**永不**进入保留策略；
* 所有删除操作默认 dry-run，必须显式 `--apply`；
* `vacuum` 需要 `--yes`，且自动先备份。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.db.database import init_db, session_scope
from app.db.models import (
    MemoryActivity,
    StrategyRun,
    StrategyRunPayload,
    ToolInvocationLog,
)

logger = logging.getLogger("db_maintenance")


def _db_path():
    path = get_settings().sqlite_db_path
    if not path.is_absolute():
        from pathlib import Path

        path = Path.cwd() / path
    return path


def report() -> None:
    db_path = _db_path()
    logger.info("数据库：%s（%.1f MB）", db_path, db_path.stat().st_size / 1e6)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            logger.info("%s：%.1f MB", sidecar.name, sidecar.stat().st_size / 1e6)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        logger.info("== 各表行数 ==")
        for table in tables:
            try:
                count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:  # pragma: no cover
                logger.warning("%s: %s", table, exc)
                continue
            logger.info("  %-32s %8d", table, count)

        logger.info("== 冷表载荷压缩效果 ==")
        try:
            row = conn.execute(
                "SELECT count(*), sum(original_size), sum(compressed_size) "
                "FROM strategy_run_payloads"
            ).fetchone()
            if row and row[0]:
                original = row[1] or 0
                compressed = row[2] or 0
                ratio = (compressed / original * 100) if original else 0
                logger.info(
                    "  行数=%s 原始=%.1fMB 压缩后=%.1fMB 压缩率=%.1f%%",
                    row[0],
                    original / 1e6,
                    compressed / 1e6,
                    ratio,
                )
        except sqlite3.Error as exc:
            logger.warning("冷表统计失败：%s", exc)

        logger.info("== 索引覆盖提示（单列索引是否为复合索引前缀）==")
        per_table: dict[str, list[tuple[str, list[str]]]] = {}
        for name, table, sql in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type='index' AND sql IS NOT NULL"
        ).fetchall():
            prefix = sql.split("(", 1)[1]
            columns = [
                part.strip().split()[0]
                for part in prefix.rstrip(")").split(",")
                if part.strip()
            ]
            per_table.setdefault(table, []).append((name, columns))
        for table, indexes in sorted(per_table.items()):
            singles = [(name, cols) for name, cols in indexes if len(cols) == 1]
            composites = [(name, cols) for name, cols in indexes if len(cols) > 1]
            for single_name, single_cols in singles:
                for composite_name, composite_cols in composites:
                    if single_cols == composite_cols[:1]:
                        logger.info(
                            "  %s.%s 已被 %s%s 覆盖（可用 EXPLAIN 复核后删除）",
                            table,
                            single_name,
                            composite_name,
                            composite_cols,
                        )
    finally:
        conn.close()


def analyze() -> None:
    init_db()
    conn = sqlite3.connect(str(_db_path()), isolation_level=None)
    try:
        conn.execute("PRAGMA optimize")
        conn.execute("ANALYZE")
        logger.info("ANALYZE + PRAGMA optimize 完成")
    finally:
        conn.close()


def checkpoint() -> None:
    conn = sqlite3.connect(str(_db_path()), isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        passive = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        logger.info("PASSIVE → busy=%s log=%s checkpointed=%s", *passive)
        truncated = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        logger.info("TRUNCATE → busy=%s log=%s checkpointed=%s", *truncated)
    finally:
        conn.close()


def vacuum(assume_yes: bool) -> None:
    if not assume_yes:
        logger.error(
            "VACUUM 会重建整库（4GB 级需要数分钟到数十分钟）。"
            "确认已清空热表旧字段并完成备份后，加 --yes 再执行。"
        )
        return
    from scripts.migrate_db import backup

    backup()
    db_path = _db_path()
    size_before = db_path.stat().st_size / 1e6
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        # WAL 模式下 VACUUM 的重写先落到 -wal，主文件要 checkpoint 之后才会真正截断，
        # 所以前后各做一次 TRUNCATE，否则会出现"VACUUM 跑了但文件没变小"。
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        logger.info("VACUUM 后 checkpoint：busy=%s log=%s checkpointed=%s", *checkpoint)
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        logger.info("页统计：page_count=%s freelist=%s", page_count, freelist)
    finally:
        conn.close()
    logger.info(
        "VACUUM 完成：%.1f MB → %.1f MB",
        size_before,
        db_path.stat().st_size / 1e6,
    )


def retention(
    payload_days: int,
    tool_log_days: int,
    memory_activity_days: int,
    apply: bool,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as db:
        resolved: list[str] = []

        # 1) 冷表原始载荷：按 run 的 started_at 定窗口
        payload_cutoff_run_id = None
        if payload_days > 0:
            cutoff_time = now - timedelta(days=payload_days)
            payload_cutoff_run_id = db.scalar(
                StrategyRun.__table__.select()
                .with_only_columns(StrategyRun.id)
                .where(StrategyRun.started_at >= cutoff_time)
                .order_by(StrategyRun.id.asc())
                .limit(1)
            )
            if payload_cutoff_run_id is None:
                resolved.append(f"冷表载荷：{payload_days} 天内无运行，跳过")
            else:
                stmt_count = (
                    StrategyRunPayload.__table__.select()
                    .with_only_columns(StrategyRunPayload.id)
                    .where(
                        StrategyRunPayload.run_id < int(payload_cutoff_run_id)
                    )
                )
                count = len(db.execute(stmt_count).all())
                resolved.append(
                    f"冷表载荷：run_id < {payload_cutoff_run_id} 共 {count} 行"
                )
                if apply and count:
                    db.execute(
                        StrategyRunPayload.__table__.delete().where(
                            StrategyRunPayload.run_id < int(payload_cutoff_run_id)
                        )
                    )

        # 2) 工具调用日志
        if tool_log_days > 0:
            cutoff = now - timedelta(days=tool_log_days)
            rows = db.execute(
                ToolInvocationLog.__table__.select()
                .with_only_columns(ToolInvocationLog.id)
                .where(ToolInvocationLog.created_at < cutoff)
            ).all()
            resolved.append(f"工具日志：早于 {cutoff.date()} 共 {len(rows)} 行")
            if apply and rows:
                db.execute(
                    ToolInvocationLog.__table__.delete().where(
                        ToolInvocationLog.created_at < cutoff
                    )
                )

        # 3) 记忆活动日志（记忆本体走软删，不动）
        if memory_activity_days > 0:
            cutoff = now - timedelta(days=memory_activity_days)
            rows = db.execute(
                MemoryActivity.__table__.select()
                .with_only_columns(MemoryActivity.id)
                .where(MemoryActivity.created_at < cutoff)
            ).all()
            resolved.append(f"记忆活动：早于 {cutoff.date()} 共 {len(rows)} 行")
            if apply and rows:
                db.execute(
                    MemoryActivity.__table__.delete().where(
                        MemoryActivity.created_at < cutoff
                    )
                )

        for line in resolved:
            logger.info("%s%s", line, "" if apply else "（dry-run，未删除）")
        if not apply:
            logger.info("未加 --apply，未做任何删除")


def main() -> int:
    parser = argparse.ArgumentParser(description="数据库维护（阶段 E）")
    parser.add_argument(
        "command",
        choices=["report", "analyze", "checkpoint", "vacuum", "retention"],
    )
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--payload-days", type=int, default=180)
    parser.add_argument("--tool-log-days", type=int, default=90)
    parser.add_argument("--memory-activity-days", type=int, default=365)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.command == "report":
        report()
    elif args.command == "analyze":
        analyze()
    elif args.command == "checkpoint":
        checkpoint()
    elif args.command == "vacuum":
        vacuum(args.yes)
    else:
        retention(
            args.payload_days,
            args.tool_log_days,
            args.memory_activity_days,
            args.apply,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
