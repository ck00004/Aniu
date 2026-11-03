"""回填 strategy_runs 的统计快照列（input/output/total_tokens、
api_call_count、executed_trade_count）。

背景
----
运行列表 / 运行概览 / 每日报告改成了"只读窄列"，不再为了算指标去解析
`llm_request_payload` / `skill_payloads` 等大 JSON。历史行这些列是 NULL，
列表会显示 0 / "--"，需要一次性回填。

数据来源（重要）
----------------
原始载荷可能在两处：
1. 热表旧字段（还没跑 `clear-legacy`）；
2. 冷表 `strategy_run_payloads`（已迁移 + 已清空旧字段）。

本脚本对每一行：旧字段有值就直接用；旧字段为空则**先从冷表解压恢复**，
恢复不到才跳过（不写 0）。因此"先清空旧字段、后回填指标"不会再生成假 0。

用法
----
    cd backend
    # 正常回填（默认 5 条/批、批间 1 秒，适配 8GB 内存的小机器）
    .venv/bin/python -m scripts.backfill_run_metrics

    # 只统计规模、不写库
    .venv/bin/python -m scripts.backfill_run_metrics --dry-run

    # 已经有假 0（历史上顺序跑错过）时，强制按载荷重算全部行
    .venv/bin/python -m scripts.backfill_run_metrics --recompute

注意
----
* 这是**唯一**会读取历史大字段的路径，务必在低负载窗口跑；
* 分批提交，批间休眠，避免长时间读事务把 WAL checkpoint 卡住；
* 只处理五列中存在 NULL 的行（`--recompute` 除外），可重复执行、可中断续跑；
* 不修改任何业务字段（状态、结论、交易记录都不动）。
"""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import or_, select
from sqlalchemy.orm.attributes import set_committed_value

from app.db.database import init_db, session_scope
from app.db.models import StrategyRun
from app.services.aniu_service import aniu_service
from app.services.run_payload_service import (
    FIELD_TO_PAYLOAD_TYPE,
    PAYLOAD_TYPE_TO_FIELD,
    run_payload_service,
)

logger = logging.getLogger("backfill_run_metrics")

_LEGACY_FIELDS = tuple(FIELD_TO_PAYLOAD_TYPE.keys())
_METRIC_FIELDS = (
    "api_call_count",
    "executed_trade_count",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)


def _pending_condition():
    return or_(*[getattr(StrategyRun, field).is_(None) for field in _METRIC_FIELDS])


def _hydrate_payloads_from_cold_table(db, run: StrategyRun) -> str | None:
    """按字段补齐载荷：已有的用热表值，缺失的从冷表补。

    两个约束（第 17 轮审计 DB-02 / DB-03）：
    * 冷表补全必须用 `set_committed_value` —— 普通 setattr 会把对象标脏，
      提交时把几 MB 原始 JSON 又 UPDATE 回 `strategy_runs`，等于撤销
      clear-legacy；
    * 不能"只要有一个旧字段存在就认为全部来自热表"，要逐字段合并。

    返回值：`"legacy"` / `"cold"` / `"mixed"` 表示来源，None 表示完全没有载荷。
    """
    missing_fields = [
        field for field in _LEGACY_FIELDS if getattr(run, field, None) is None
    ]
    has_legacy = len(missing_fields) < len(_LEGACY_FIELDS)
    if not missing_fields:
        return "legacy"
    try:
        loaded = run_payload_service.load_run_payloads(
            db,
            int(run.id),
            payload_types=[
                FIELD_TO_PAYLOAD_TYPE[field] for field in missing_fields
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取冷表失败 run_id=%s: %s", run.id, exc)
        return "legacy" if has_legacy else None
    restored = 0
    for payload_type, value in loaded.items():
        field = PAYLOAD_TYPE_TO_FIELD.get(payload_type)
        if field is None or getattr(run, field, None) is not None:
            continue
        # 只填会话内视图，绝不写回热表
        set_committed_value(run, field, value)
        restored += 1
    if restored == 0:
        return "legacy" if has_legacy else None
    return "mixed" if has_legacy else "cold"


def main() -> int:
    parser = argparse.ArgumentParser(description="回填运行统计快照列")
    parser.add_argument(
        "--batch-size", type=int, default=5, help="每批读取的 run 数量（默认 5）"
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="每批之间的休眠秒数（默认 1.0，给小机器 I/O 让路）",
    )
    parser.add_argument("--max-batches", type=int, default=0, help="0 表示不限制")
    parser.add_argument("--dry-run", action="store_true", help="只统计待回填行数")
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="不按 NULL 过滤，按载荷重算全部行（用于修正历史假 0）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    init_db()

    total = 0
    skipped_no_payload = 0
    from_cold = 0
    batches = 0
    last_id = 0

    while True:
        with session_scope() as db:
            stmt = select(StrategyRun).where(StrategyRun.id > last_id)
            if not args.recompute:
                stmt = stmt.where(_pending_condition())
            rows = list(
                db.scalars(
                    stmt.order_by(StrategyRun.id.asc()).limit(max(1, args.batch_size))
                ).all()
            )
            if not rows:
                break
            last_id = int(rows[-1].id)

            if args.dry_run:
                logger.info(
                    "dry-run: 下一批待处理 id=%s..%s（共 %d 条）",
                    rows[0].id,
                    rows[-1].id,
                    len(rows),
                )
                total += len(rows)
                break

            touched = 0
            for run in rows:
                source = _hydrate_payloads_from_cold_table(db, run)
                if source is None:
                    # 既没有旧字段也没有冷表载荷：可能是启动即失败的运行。
                    # 这种情况下不写 0（避免伪造统计），保持 NULL 并在报告里可见。
                    skipped_no_payload += 1
                    logger.debug("skip run_id=%s: no payload available", run.id)
                    continue
                if source in {"cold", "mixed"}:
                    from_cold += 1
                aniu_service._hydrate_run_summary_metrics(run)
                db.add(run)
                touched += 1
            total += touched
            batches += 1
            logger.info(
                "回填 %d 条（累计 %d，跳过无载荷 %d，id 到 %s）",
                touched,
                total,
                skipped_no_payload,
                rows[-1].id,
            )
        if args.max_batches and batches >= args.max_batches:
            logger.info("达到 --max-batches=%s，提前结束", args.max_batches)
            break
        time.sleep(max(0.0, args.sleep))

    logger.info(
        "完成：回填 %d 条（其中从冷表恢复 %d 条），跳过无载荷 %d 条",
        total,
        from_cold,
        skipped_no_payload,
    )
    if skipped_no_payload:
        logger.warning(
            "有 %d 条运行既无旧字段也无冷表载荷，指标保持 NULL；"
            "如确认这些运行本就没有载荷（启动即失败），属正常情况。",
            skipped_no_payload,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
