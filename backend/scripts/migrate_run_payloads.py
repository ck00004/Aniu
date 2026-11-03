"""阶段 C 数据迁移：把 strategy_runs 的大字段搬进 strategy_run_payloads（gzip）。

子命令（可组合，默认全部顺序执行）：

    # 1) 回填（压缩写冷表），分批可中断（默认 5 条/批、批间 1 秒）
    .venv/bin/python -m scripts.migrate_run_payloads backfill

    # 2) 校验（重算 sha256 / 大小 / JSON 一致性）
    .venv/bin/python -m scripts.migrate_run_payloads verify

    # 3) 清空热表旧字段（只清"已回填且校验一致"的行）
    .venv/bin/python -m scripts.migrate_run_payloads clear-legacy

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
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, text

from app.db.database import init_db, session_scope
from app.db.models import StrategyRun, StrategyRunPayload
from app.services.run_payload_service import (
    ALL_PAYLOAD_TYPES,
    FIELD_TO_PAYLOAD_TYPE,
    compress_json,
    decompress_json,
)

logger = logging.getLogger("migrate_run_payloads")

# 迁移/维护脚本不走服务启动路径：跳过 strategy_runs 复合索引，
# 否则每次 init_db() 都会在 4.7GB 表上全表扫描建索引（实测 20~30 分钟）。
# 瘦身后用 `scripts.migrate_db indexes` 单独创建。
os.environ.setdefault("ANIU_SKIP_HEAVY_INDEXES", "1")

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
                if len(row.payload_data or b"") != int(row.compressed_size or 0):
                    mismatched += 1
                    logger.error(
                        "payload compressed_size 与实际长度不一致: run_id=%s type=%s",
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
    mismatched += verify_legacy_coverage()
    return mismatched


def verify_legacy_coverage() -> int:
    """检查"旧字段非空但没有对应冷表行"的缺口。

    第 17 轮审计 DB-04：旧实现按批 `select(StrategyRun)` 加载整行 ORM 对象，
    每批 200 条 × 6MB ≈ 1.2GB 原始 JSON 进内存，在 8GB 机器上纯属浪费。
    这里改成纯 SQL：只判断"字段非空 + 冷表无对应行"，不反序列化 JSON。
    """
    gaps = 0
    per_field: dict[str, int] = {}
    with session_scope() as db:
        for field, payload_type in FIELD_TO_PAYLOAD_TYPE.items():
            # CAST(...) != 'null' 兜住历史遗留的 JSON 文本 null
            count = int(
                db.scalar(
                    text(
                        f"SELECT COUNT(*) FROM strategy_runs AS r "
                        f"WHERE r.{field} IS NOT NULL "
                        f"AND CAST(r.{field} AS TEXT) != 'null' "
                        f"AND NOT EXISTS ("
                        f"  SELECT 1 FROM strategy_run_payloads AS p "
                        f"  WHERE p.run_id = r.id AND p.payload_type = :ptype"
                        f")"
                    ),
                    {"ptype": payload_type},
                )
                or 0
            )
            per_field[field] = count
            gaps += count
    logger.info(
        "旧字段覆盖度（SQL 检查）：缺少冷表记录 %d 处 %s",
        gaps,
        {k: v for k, v in per_field.items() if v},
    )
    if gaps:
        logger.error("存在未回填的旧字段，禁止执行 clear-legacy")
    return gaps


def inspect() -> None:
    """迁移进度体检：剩余旧字段、冷表行数、压缩效果、覆盖缺口。"""
    with session_scope() as db:
        total_runs = int(db.scalar(select(func.count(StrategyRun.id))) or 0)
        remaining = {
            field: int(
                db.scalar(
                    select(func.count(StrategyRun.id)).where(
                        getattr(StrategyRun, field).is_not(None)
                    )
                )
                or 0
            )
            for field in _LEGACY_FIELDS
        }
        payload_rows = int(
            db.scalar(select(func.count(StrategyRunPayload.id))) or 0
        )
        payload_runs = int(
            db.scalar(
                select(func.count(func.distinct(StrategyRunPayload.run_id)))
            )
            or 0
        )
        original = int(
            db.scalar(select(func.sum(StrategyRunPayload.original_size))) or 0
        )
        compressed = int(
            db.scalar(select(func.sum(StrategyRunPayload.compressed_size))) or 0
        )
    logger.info("运行记录总数：%d", total_runs)
    logger.info("仍带旧字段的运行数：%s", remaining)
    logger.info("冷表：%d 行 / %d 个运行", payload_rows, payload_runs)
    if original:
        logger.info(
            "压缩效果：原始 %.1fMB → 压缩后 %.1fMB（%.1f%%）",
            original / 1e6,
            compressed / 1e6,
            100 * compressed / original,
        )
    with session_scope() as db:
        storage_rows = db.execute(
            text(
                "SELECT COALESCE(payload_storage,'(未标记)'), COUNT(*) "
                "FROM strategy_runs GROUP BY 1 ORDER BY 2 DESC"
            )
        ).all()
        metrics_rows = db.execute(
            text(
                "SELECT COALESCE(metrics_status,'(未标记)'), COUNT(*) "
                "FROM strategy_runs GROUP BY 1 ORDER BY 2 DESC"
            )
        ).all()
    logger.info("payload_storage 分布：%s", [tuple(r) for r in storage_rows])
    logger.info("metrics_status 分布：%s", [tuple(r) for r in metrics_rows])
    if payload_runs < total_runs:
        logger.info(
            "尚未有冷表载荷的运行数：%d（可能从未产生载荷，或还没回填）",
            total_runs - payload_runs,
        )


def nullify_json_null_literals() -> int:
    """把历史遗留的 JSON 文本 "null" 收敛成 SQL NULL。

    JSON 列默认 none_as_null=False，早期代码写 None 时会落成 4 字节的
    `null` 文本：ORM 读出来是 None，但 `IS NOT NULL` 为真，会让
    "旧字段是否清空"的判断一直显示未清空。这里一次性收敛。
    """
    total = 0
    fields = (*_LEGACY_FIELDS, "executed_actions")
    with session_scope() as db:
        for field in fields:
            result = db.execute(
                text(
                    f"UPDATE strategy_runs SET {field} = NULL "
                    f"WHERE CAST({field} AS TEXT) = 'null'"
                )
            )
            count = int(result.rowcount or 0)
            if count:
                logger.info("清理 %s 的 JSON null 文本：%d 行", field, count)
            total += count
    if total:
        logger.info("JSON null 文本收敛完成：共 %d 处", total)
    return total


def clear_legacy(batch_size: int, sleep_seconds: float, max_batches: int) -> int:
    nullify_json_null_literals()
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
        choices=["inspect", "backfill", "verify", "clear-legacy", "purge", "all"],
    )
    parser.add_argument(
        "--batch-size", type=int, default=5, help="每批读取的 run 数量（默认 5）"
    )
    parser.add_argument(
        "--sleep", type=float, default=1.0, help="每批之间休眠秒数（默认 1.0）"
    )
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--keep-days", type=int, default=180)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    init_db()

    if args.command == "inspect":
        inspect()
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
