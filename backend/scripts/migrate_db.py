"""阶段 D：数据库结构迁移与状态检查（SQLite）。

子命令：

    # 备份（务必在 apply 前先跑；用 SQLite Backup API，WAL 也一致）
    .venv/bin/python -m scripts.migrate_db backup

    # 查看当前结构/索引/外键/版本
    .venv/bin/python -m scripts.migrate_db status

    # 应用迁移：补列 + 补索引（幂等，等价于 init_db 的结构部分）
    .venv/bin/python -m scripts.migrate_db apply

    # 给交易意图/自选股意图补外键（SQLite 需要重建表，默认先备份）
    .venv/bin/python -m scripts.migrate_db rebuild-intent-fks --yes

为什么需要重建表：SQLite 不支持 `ALTER TABLE ... ADD CONSTRAINT`，
只能"建新表 → 拷数据 → 换名 → 重建索引"。本脚本会：

1. 先把原 DDL 读出来，只在最后补上 FOREIGN KEY 子句（其余定义原样保留）；
2. 拷数据、校验行数、重建原索引；
3. 结束前跑 `PRAGMA foreign_key_check`，有违规立即回滚报错。

交易意图外键用 ON DELETE SET NULL：删除运行记录时意图本身（标的/方向/
数量/远端单号/状态）必须保留，只解除与运行/动作的关联；应用层
`delete_run` 另加显式确认，避免静默断链。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.db.database import init_db

logger = logging.getLogger("migrate_db")

SCHEMA_VERSION = "2026-11-03-stage-d"

# 需要补外键的表：表名 → [(列, 目标表, 目标列, ON DELETE)]
INTENT_FOREIGN_KEYS: dict[str, list[tuple[str, str, str, str]]] = {
    "trade_intents": [
        ("run_id", "strategy_runs", "id", "SET NULL"),
        ("action_id", "strategy_run_actions", "id", "SET NULL"),
    ],
    "self_select_intents": [
        ("run_id", "strategy_runs", "id", "SET NULL"),
    ],
}


def _db_path() -> Path:
    settings = get_settings()
    path = settings.sqlite_db_path
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def backup() -> Path:
    db_path = _db_path()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(target))
        try:
            # backup API 会包含 WAL 中已提交的内容，不能直接 cp 主文件
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    logger.info("备份完成：%s（%.1f MB）", target, target.stat().st_size / 1e6)
    return target


def status() -> None:
    db_path = _db_path()
    logger.info("数据库：%s", db_path)
    if not db_path.exists():
        logger.error("数据库不存在")
        return
    logger.info("大小：%.1f MB", db_path.stat().st_size / 1e6)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for pragma in ("journal_mode", "page_count", "page_size", "freelist_count"):
            logger.info("PRAGMA %s = %s", pragma, conn.execute(f"PRAGMA {pragma}").fetchone())
        rows = conn.execute(
            "SELECT name, (SELECT count(*) FROM pragma_table_info(name)) "
            "FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        logger.info("表（%d）：%s", len(rows), [row[0] for row in rows])
        for table in ("trade_intents", "self_select_intents", "strategy_run_payloads"):
            if not any(row[0] == table for row in rows):
                logger.info("%s：表不存在", table)
                continue
            fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            logger.info("%s 外键：%s", table, fks or "无")
        logger.info(
            "schema_meta：%s",
            conn.execute(
                "SELECT key, value FROM schema_meta"
            ).fetchall()
            if conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='schema_meta'"
            ).fetchone()[0]
            else "无",
        )
    finally:
        conn.close()


def apply_structure() -> None:
    """补列 + 补索引（幂等；与启动时 init_db 的结构部分一致）。"""
    init_db()
    logger.info("结构迁移完成（列 + 索引）")


def _fk_clause(column: str, ref_table: str, ref_column: str, on_delete: str) -> str:
    return (
        f"FOREIGN KEY({column}) REFERENCES {ref_table}({ref_column}) "
        f"ON DELETE {on_delete}"
    )


def rebuild_table_with_fks(
    conn: sqlite3.Connection,
    table: str,
    foreign_keys: list[tuple[str, str, str, str]],
) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None or not row[0]:
        raise RuntimeError(f"表 {table} 不存在或 DDL 不可读")
    original_sql: str = row[0]

    existing_fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    if existing_fks:
        logger.info("%s 已有外键 %s，跳过重建", table, existing_fks)
        return

    # 在 DDL 末尾的 ')' 之前插入外键子句
    stripped = original_sql.rstrip()
    assert stripped.endswith(")"), f"{table} 的 DDL 结构异常"
    body, tail = stripped[:-1], stripped[-1]
    clauses = ",\n  ".join(_fk_clause(*fk) for fk in foreign_keys)
    new_sql = f"{body},\n  {clauses}\n{tail}"

    temp_table = f"{table}__new"
    index_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    before_count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    logger.info("重建 %s（%d 行，%d 个索引）", table, before_count, len(index_rows))
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
        conn.execute(new_sql.replace(f"TABLE {table}", f"TABLE {temp_table}", 1))
        columns = [
            info[1] for info in conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        column_list = ", ".join(columns)
        conn.execute(
            f"INSERT INTO {temp_table} ({column_list}) SELECT {column_list} FROM {table}"
        )
        after_count = conn.execute(f"SELECT count(*) FROM {temp_table}").fetchone()[0]
        if after_count != before_count:
            raise RuntimeError(
                f"{table} 行数不一致：before={before_count} after={after_count}"
            )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")
        for _name, index_sql in index_rows:
            conn.execute(index_sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"重建后外键校验失败：{violations}")
    logger.info("%s 重建完成，外键校验通过", table)


def rebuild_intent_fks(skip_backup: bool) -> None:
    if not skip_backup:
        backup()
    db_path = _db_path()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        for table, fks in INTENT_FOREIGN_KEYS.items():
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                logger.info("%s 不存在，跳过", table)
                continue
            rebuild_table_with_fks(conn, table, fks)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        logger.info("schema_version = %s", SCHEMA_VERSION)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="数据库结构迁移与检查（阶段 D）")
    parser.add_argument(
        "command",
        choices=["status", "backup", "apply", "rebuild-intent-fks"],
    )
    parser.add_argument("--yes", action="store_true", help="rebuild 前不自动备份")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.command == "status":
        status()
    elif args.command == "backup":
        backup()
    elif args.command == "apply":
        apply_structure()
    else:
        rebuild_intent_fks(skip_backup=args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
