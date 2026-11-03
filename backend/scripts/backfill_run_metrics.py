"""回填 strategy_runs 的统计快照列（input/output/total_tokens、
api_call_count、executed_trade_count）。

背景
----
运行列表 / 运行概览 / 每日报告改成了"只读窄列"，不再为了算指标去解析
`llm_request_payload` / `skill_payloads` 等大 JSON。历史行这些列是 NULL，
列表会显示 0 / "--"，需要一次性回填。

用法
----
    cd backend
    .venv/bin/python -m scripts.backfill_run_metrics --batch-size 20 --sleep 0.2

    # 只统计规模、不写库
    .venv/bin/python -m scripts.backfill_run_metrics --dry-run

注意
----
* 这是**唯一**会读取历史大字段的路径，一定要在低负载窗口跑；
* 分批提交，每批只读 batch_size 条，避免长时间读事务把 WAL checkpoint 卡住；
* 只处理五列全为 NULL 的行，可重复执行、可中断续跑；
* 不会修改任何业务字段（状态、结论、交易记录都不动）。
"""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import or_, select

from app.db.database import init_db, session_scope
from app.db.models import StrategyRun
from app.services.aniu_service import aniu_service

logger = logging.getLogger("backfill_run_metrics")


def _pending_condition():
    return or_(
        StrategyRun.api_call_count.is_(None),
        StrategyRun.executed_trade_count.is_(None),
        StrategyRun.input_tokens.is_(None),
        StrategyRun.output_tokens.is_(None),
        StrategyRun.total_tokens.is_(None),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="回填运行统计快照列")
    parser.add_argument("--batch-size", type=int, default=20, help="每批读取的 run 数量")
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.2,
        help="每批之间的休眠秒数（给 WAL checkpoint 与业务写入让路）",
    )
    parser.add_argument("--max-batches", type=int, default=0, help="0 表示不限制")
    parser.add_argument("--dry-run", action="store_true", help="只统计待回填行数")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    init_db()

    with session_scope() as db:
        pending = db.scalar(
            select(StrategyRun.id).where(_pending_condition()).limit(1)
        )
    if pending is None:
        logger.info("没有待回填的行")
        return 0

    total = 0
    batches = 0
    while True:
        with session_scope() as db:
            rows = list(
                db.scalars(
                    select(StrategyRun)
                    .where(_pending_condition())
                    .order_by(StrategyRun.id.asc())
                    .limit(max(1, args.batch_size))
                ).all()
            )
            if not rows:
                break
            if args.dry_run:
                logger.info(
                    "dry-run: 下一批待回填 id=%s..%s（共 %d 条）",
                    rows[0].id,
                    rows[-1].id,
                    len(rows),
                )
                total += len(rows)
                break
            for run in rows:
                aniu_service._hydrate_run_summary_metrics(run)
                db.add(run)
            total += len(rows)
            batches += 1
            logger.info(
                "回填 %d 条（累计 %d，id 到 %s）", len(rows), total, rows[-1].id
            )
        if args.max_batches and batches >= args.max_batches:
            logger.info("达到 --max-batches=%s，提前结束", args.max_batches)
            break
        time.sleep(max(0.0, args.sleep))

    logger.info("完成：回填 %d 条", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
