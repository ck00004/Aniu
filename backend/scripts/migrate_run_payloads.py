"""阶段 C 数据迁移：把 strategy_runs 的大字段搬进 strategy_run_payloads（gzip）。

子命令（可组合，默认全部顺序执行）：

    # 1) 回填（压缩写冷表），分批可中断
    .venv/bin/python -m scripts.migrate_run_payloads backfill --batch-size 20 --sleep 0.2

    # 2) 校验（重算 sha256 / 大小 / JSON 一致性）
    .venv/bin/python -m scripts.migrate_run_payloads verify

    # 3) 清空热表旧字段（只清"已回填且校验一致"的行）
    .venv/bin/python -m scripts.migrate_run_payloads clear-legacy --batch-size 20

    # 4) 按保留窗口清理冷表载荷（run_id < 某个边界）
    .venv/bin/python -m scripts.migrate_run_payloads purge --keep-days 180

安全约束：
* 只读历史大字段，不改状态/结论/交易记录；
* 每批一个事务，批间 sleep，避免长读事务卡住 WAL checkpoint；
* `clear-legacy` 逐行比对（解压结果 vs 热表值），只有完全一致才清空；
* 全部步骤幂等，可重复执行、可中断续跑。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from app.db.database import init_db, session_scope
from app.db.models import StrategyRun, StrategyRunPayload
from app.services.run_payload_service import (
    ALL_PAYLOAD_TYPES,
    FIELD_TO_PAYLOAD_TYPE,
    compress_json,
    decompress_json,
)

logger = logging.getLogger("migrate_run_payloads")

_LEGACY_FIELDS = tuple(FIELD_TO_PAYLOAD_TYPE.keys())


def _legacy_not_null_condition():
    return or_(*[getattr(StrategyRun, field).is_not(None) for field in _LEGACY_FIELDS])


def _payload_row_count(db, run_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(StrategyRunPayload.id)).where(
                StrategyRunPayload.run_id == run_id
            )
        )
        or 0
    )


def backfill(batch_size: int, sleep_seconds: float, max_batches: int) -> int:
    total = 0
    batches = 0
    last_id = 0
    while True:
        with session_scope() as db:
            rows = list(
                db.scalars(
                    select(StrategyRun)
                    .where(
                        StrategyRun.id > last_id,
                        _legacy_not_null_condition(),
                    )
                    .order_by(StrategyRun.id.asc())
                    .limit(max(1, batch_size))
                ).all()
            )
            if not rows:
                break
            last_id = int(rows[-1].id)
            pending = []
            for run in rows:
                stored = _payload_row_count(db, int(run.id))
                values = [
                    getattr(run, field)
                    for field in _LEGACY_FIELDS
                    if getattr(run, field) is not None
                ]
                if stored < len(values):
                    pending.append(run)
            if not pending:
                continue
            for run in pending:
                for field, payload_type in FIELD_TO_PAYLOAD_TYPE.items():
                    value = getattr(run, field, None)
                    if value is None:
                        continue
                    existing = db.scalar(
                        select(StrategyRunPayload).where(
                            StrategyRunPayload.run_id == run.id,
                            StrategyRunPayload.payload_type == payload_type,
                        )
                    )
                    if existing is not None:
                        continue
                    blob, digest, original, compressed = compress_json(value)
                    db.add(
                        StrategyRunPayload(
                            run_id=run.id,
                            payload_type=payload_type,
                            payload_data=blob,
                            compression="gzip",
                            original_size=original,
                            compressed_size=compressed,
                            sha256=digest,
                        )
                    )
            total += len(pending)
            batches += 1
            logger.info(
                "回填 %d 条（累计 %d，id 到 %s）",
                len(pending),
                total,
                pending[-1].id,
            )
        if max_batches and batches >= max_batches:
            logger.info("达到 --max-batches=%s，提前结束", max_batches)
            break
        time.sleep(max(0.0, sleep_seconds))
    logger.info("回填完成：%d 条", total)
    return total


def verify(batch_size: int, sleep_seconds: float) -> int:
    mismatched = 0
    checked = 0
    last_id = 0
    while True:
        with session_scope() as db:
            rows = list(
                db.scalars(
                    select(StrategyRunPayload)
                    .where(StrategyRunPayload.id > last_id)
                    .order_by(StrategyRunPayload.id.asc())
                    .limit(max(1, batch_size))
                ).all()
            )
            if not rows:
                break
            for row in rows:
                last_id = int(row.id)
                checked += 1
                raw = decompress_json(row.payload_data, compression=row.compression)
                if raw is None:
                    mismatched += 1
                    logger.error(
                        "payload 解压失败: run_id=%s type=%s",
                        row.run_id,
                        row.payload_type,
                    )
                    continue
                recomputed = json.dumps(
                    raw, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                if hashlib.sha256(recomputed).hexdigest() != row.sha256:
                    mismatched += 1
                    logger.error(
                        "payload sha256 不一致: run_id=%s type=%s",
                        row.run_id,
                        row.payload_type,
                    )
                if len(recomputed) != int(row.original_size or 0):
                    mismatched += 1
                    logger.error(
                        "payload 原始大小不一致: run_id=%s type=%s",
                        row.run_id,
                        row.payload_type,
                    )
        time.sleep(max(0.0, sleep_seconds))
    logger.info("校验完成：检查 %d 行，异常 %d 行", checked, mismatched)
    return mismatched


def clear_legacy(batch_size: int, sleep_seconds: float, max_batches: int) -> int:
    cleared = 0
    batches = 0
    last_id = 0
    while True:
        with session_scope() as db:
            rows = list(
                db.scalars(
                    select(StrategyRun)
                    .where(
                        StrategyRun.id > last_id,
                        _legacy_not_null_condition(),
                    )
                    .order_by(StrategyRun.id.asc())
                    .limit(max(1, batch_size))
                ).all()
            )
            if not rows:
                break
            last_id = int(rows[-1].id)
            touched = 0
            for run in rows:
                payload_rows = {
                    row.payload_type: row
                    for row in db.scalars(
                        select(StrategyRunPayload).where(
                            StrategyRunPayload.run_id == run.id
                        )
                    ).all()
                }
                for field, payload_type in FIELD_TO_PAYLOAD_TYPE.items():
                    value = getattr(run, field, None)
                    if value is None:
                        continue
                    row = payload_rows.get(payload_type)
                    if row is None:
                        # 未回填：保留热表字段，绝不清空
                        continue
                    restored = decompress_json(
                        row.payload_data, compression=row.compression
                    )
                    if restored != value:
                        logger.warning(
                            "跳过清空（内容不一致）: run_id=%s type=%s",
                            run.id,
                            payload_type,
                        )
                        continue
                    setattr(run, field, None)
                    touched += 1
                if touched:
                    db.add(run)
            cleared += touched
            batches += 1
            logger.info(
                "本批清空 %d 个旧字段（累计 %d，id 到 %s）",
                touched,
                cleared,
                rows[-1].id,
            )
        if max_batches and batches >= max_batches:
            break
        time.sleep(max(0.0, sleep_seconds))
    logger.info("clear-legacy 完成：清空 %d 个字段", cleared)
    return cleared


def purge(keep_days: int) -> int:
    cutoff_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=max(0, keep_days)
    )
    with session_scope() as db:
        cutoff_run_id = db.scalar(
            select(func.min(StrategyRun.id)).where(
                StrategyRun.started_at >= cutoff_time
            )
        )
        if cutoff_run_id is None:
            logger.info("保留窗口内没有运行记录，跳过 purge")
            return 0
        result = db.execute(
            StrategyRunPayload.__table__.delete().where(
                StrategyRunPayload.run_id < int(cutoff_run_id)
            )
        )
    count = int(result.rowcount or 0)
    logger.info("purge 完成：删除 %d 条冷表载荷（保留 %d 天）", count, keep_days)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="运行载荷冷表迁移（阶段 C）")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["backfill", "verify", "clear-legacy", "purge", "all"],
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--keep-days", type=int, default=180)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    init_db()

    if args.command in {"backfill", "all"}:
        backfill(args.batch_size, args.sleep, args.max_batches)
    if args.command in {"verify", "all"}:
        if verify(args.batch_size, args.sleep) > 0:
            logger.error(
                "校验发现异常，已停止；请先排查再执行 clear-legacy"
            )
            return 1
    if args.command in {"clear-legacy", "all"}:
        clear_legacy(args.batch_size, args.sleep, args.max_batches)
    if args.command == "purge":
        purge(args.keep_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
