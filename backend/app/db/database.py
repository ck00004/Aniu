from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import Base

_engine = None
_session_local = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{settings.sqlite_db_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
    return _engine


def get_session_local():
    global _session_local
    if _session_local is None:
        _session_local = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    return _session_local


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _ensure_app_settings_columns(engine)
    _ensure_chat_session_columns(engine)
    _ensure_chat_message_columns(engine)
    _ensure_strategy_schedule_columns(engine)
    _ensure_strategy_run_columns(engine)
    _ensure_strategy_run_action_columns(engine)
    _ensure_strategy_run_action_result_columns(engine)
    _ensure_chat_session_indexes(engine)
    _ensure_chat_message_indexes(engine)
    _ensure_strategy_run_indexes(engine)
    _ensure_strategy_run_action_indexes(engine)
    _ensure_strategy_run_action_result_indexes(engine)
    _backfill_schedule_run_types(engine)
    _backfill_strategy_run_types(engine)


def _ensure_app_settings_columns(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "app_settings" not in table_names:
        return

    columns = {column["name"] for column in inspector.get_columns("app_settings")}
    statements: list[str] = []
    if "mx_api_key" not in columns:
        statements.append("ALTER TABLE app_settings ADD COLUMN mx_api_key VARCHAR(255)")
    if "jin10_api_base_url" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN jin10_api_base_url VARCHAR(255)"
        )
    if "cls_api_base_url" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN cls_api_base_url VARCHAR(255)"
        )
    if "disabled_skill_ids_json" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN disabled_skill_ids_json TEXT DEFAULT '[]'"
        )
    if "prompt_templates_json" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN prompt_templates_json TEXT DEFAULT '{}'"
        )
    if "automation_session_id" not in columns:
        statements.append("ALTER TABLE app_settings ADD COLUMN automation_session_id INTEGER")
    if "automation_context_window_tokens" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN automation_context_window_tokens INTEGER DEFAULT 131072"
        )
    if "automation_target_prompt_tokens" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN automation_target_prompt_tokens INTEGER DEFAULT 111411"
        )
    if "automation_recent_message_limit" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN automation_recent_message_limit INTEGER DEFAULT 24"
        )
    if "automation_enable_auto_compaction" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN automation_enable_auto_compaction BOOLEAN DEFAULT 1"
        )
    if "automation_idle_summary_hours" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN automation_idle_summary_hours INTEGER DEFAULT 12"
        )
    if "automation_context_source" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN automation_context_source VARCHAR(32) DEFAULT 'default'"
        )
    if "automation_context_detected_at" not in columns:
        statements.append(
            "ALTER TABLE app_settings ADD COLUMN automation_context_detected_at DATETIME"
        )

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text(
                "UPDATE app_settings SET automation_context_window_tokens = CASE "
                "WHEN automation_context_window_tokens IS NULL OR automation_context_window_tokens = 65536 THEN 131072 "
                "ELSE automation_context_window_tokens END, "
                "automation_target_prompt_tokens = CASE "
                "WHEN automation_target_prompt_tokens IS NULL OR automation_target_prompt_tokens = 24000 THEN 111411 "
                "ELSE automation_target_prompt_tokens END, "
                "prompt_templates_json = COALESCE(prompt_templates_json, '{}'), "
                "automation_recent_message_limit = COALESCE(automation_recent_message_limit, 24), "
                "automation_enable_auto_compaction = COALESCE(automation_enable_auto_compaction, 1), "
                "automation_idle_summary_hours = COALESCE(automation_idle_summary_hours, 12), "
                "automation_context_source = COALESCE(NULLIF(trim(automation_context_source), ''), 'default')"
            )
        )


def _ensure_chat_session_columns(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "chat_sessions" not in table_names:
        return

    required_columns = {
        "kind": "ALTER TABLE chat_sessions ADD COLUMN kind VARCHAR(32) DEFAULT 'user'",
        "slug": "ALTER TABLE chat_sessions ADD COLUMN slug VARCHAR(120)",
        "archived_summary": "ALTER TABLE chat_sessions ADD COLUMN archived_summary TEXT",
        "summary_updated_at": "ALTER TABLE chat_sessions ADD COLUMN summary_updated_at DATETIME",
        "last_compacted_message_id": "ALTER TABLE chat_sessions ADD COLUMN last_compacted_message_id INTEGER",
        "last_compacted_run_id": "ALTER TABLE chat_sessions ADD COLUMN last_compacted_run_id INTEGER",
        "summary_revision": "ALTER TABLE chat_sessions ADD COLUMN summary_revision INTEGER DEFAULT 0",
    }

    with engine.begin() as connection:
        for column_name, statement in required_columns.items():
            current_columns = {
                column["name"]
                for column in inspect(connection).get_columns("chat_sessions")
            }
            if column_name in current_columns:
                continue
            connection.execute(text(statement))
        connection.execute(
            text(
                "UPDATE chat_sessions SET kind = COALESCE(NULLIF(trim(kind), ''), 'user'), "
                "summary_revision = COALESCE(summary_revision, 0)"
            )
        )


def _ensure_chat_message_columns(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "chat_messages" not in table_names:
        return

    required_columns = {
        "source": "ALTER TABLE chat_messages ADD COLUMN source VARCHAR(32)",
        "run_id": "ALTER TABLE chat_messages ADD COLUMN run_id INTEGER",
        "message_kind": "ALTER TABLE chat_messages ADD COLUMN message_kind VARCHAR(32)",
        "meta_payload": "ALTER TABLE chat_messages ADD COLUMN meta_payload JSON",
    }

    with engine.begin() as connection:
        for column_name, statement in required_columns.items():
            current_columns = {
                column["name"]
                for column in inspect(connection).get_columns("chat_messages")
            }
            if column_name in current_columns:
                continue
            connection.execute(text(statement))


def _ensure_strategy_schedule_columns(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "strategy_schedules" not in table_names:
        return

    required_columns = {
        "run_type": "ALTER TABLE strategy_schedules ADD COLUMN run_type VARCHAR(32) DEFAULT 'analysis'",
        "market_day_type": "ALTER TABLE strategy_schedules ADD COLUMN market_day_type VARCHAR(32) DEFAULT 'trading_day'",
        "cron_expression": "ALTER TABLE strategy_schedules ADD COLUMN cron_expression VARCHAR(64)",
        "task_prompt": "ALTER TABLE strategy_schedules ADD COLUMN task_prompt TEXT",
        "timeout_seconds": "ALTER TABLE strategy_schedules ADD COLUMN timeout_seconds INTEGER DEFAULT 1800",
        "retry_count": "ALTER TABLE strategy_schedules ADD COLUMN retry_count INTEGER DEFAULT 0",
        "retry_after_at": "ALTER TABLE strategy_schedules ADD COLUMN retry_after_at DATETIME",
    }

    with engine.begin() as connection:
        for column_name, statement in required_columns.items():
            current_columns = {
                column["name"]
                for column in inspect(connection).get_columns("strategy_schedules")
            }
            if column_name in current_columns:
                continue
            connection.execute(text(statement))


def _ensure_strategy_run_action_columns(engine) -> None:
    inspector = inspect(engine)
    if "strategy_run_actions" not in set(inspector.get_table_names()):
        return

    required_columns = {
        "sequence_no": "ALTER TABLE strategy_run_actions ADD COLUMN sequence_no INTEGER DEFAULT 1",
        "phase": "ALTER TABLE strategy_run_actions ADD COLUMN phase VARCHAR(32) DEFAULT 'planned'",
        "tool_name": "ALTER TABLE strategy_run_actions ADD COLUMN tool_name VARCHAR(64)",
        "action_type": "ALTER TABLE strategy_run_actions ADD COLUMN action_type VARCHAR(32) DEFAULT 'UNKNOWN'",
        "status": "ALTER TABLE strategy_run_actions ADD COLUMN status VARCHAR(32) DEFAULT 'planned'",
        "tool_call_id": "ALTER TABLE strategy_run_actions ADD COLUMN tool_call_id VARCHAR(128)",
        "arguments_payload": "ALTER TABLE strategy_run_actions ADD COLUMN arguments_payload JSON",
        "planned_action_payload": "ALTER TABLE strategy_run_actions ADD COLUMN planned_action_payload JSON",
        "executed_action_payload": "ALTER TABLE strategy_run_actions ADD COLUMN executed_action_payload JSON",
        "result_summary": "ALTER TABLE strategy_run_actions ADD COLUMN result_summary TEXT",
        "error_message": "ALTER TABLE strategy_run_actions ADD COLUMN error_message TEXT",
        "updated_at": "ALTER TABLE strategy_run_actions ADD COLUMN updated_at DATETIME",
        "executed_at": "ALTER TABLE strategy_run_actions ADD COLUMN executed_at DATETIME",
    }

    with engine.begin() as connection:
        for column_name, statement in required_columns.items():
            current_columns = {
                column["name"]
                for column in inspect(connection).get_columns("strategy_run_actions")
            }
            if column_name in current_columns:
                continue
            connection.execute(text(statement))


def _ensure_strategy_run_action_result_columns(engine) -> None:
    inspector = inspect(engine)
    if "strategy_run_action_results" not in set(inspector.get_table_names()):
        return

    required_columns = {
        "attempt_no": "ALTER TABLE strategy_run_action_results ADD COLUMN attempt_no INTEGER DEFAULT 1",
        "status": "ALTER TABLE strategy_run_action_results ADD COLUMN status VARCHAR(32) DEFAULT 'planned'",
        "response_payload": "ALTER TABLE strategy_run_action_results ADD COLUMN response_payload JSON",
        "error_message": "ALTER TABLE strategy_run_action_results ADD COLUMN error_message TEXT",
        "finished_at": "ALTER TABLE strategy_run_action_results ADD COLUMN finished_at DATETIME",
    }

    with engine.begin() as connection:
        for column_name, statement in required_columns.items():
            current_columns = {
                column["name"]
                for column in inspect(connection).get_columns("strategy_run_action_results")
            }
            if column_name in current_columns:
                continue
            connection.execute(text(statement))


def _ensure_strategy_run_action_indexes(engine) -> None:
    inspector = inspect(engine)
    if "strategy_run_actions" not in set(inspector.get_table_names()):
        return

    existing_indexes = {
        index["name"] for index in inspector.get_indexes("strategy_run_actions")
    }
    statements: list[str] = []
    if "ix_strategy_run_actions_run_id_sequence_no" not in existing_indexes:
        statements.append(
            "CREATE INDEX ix_strategy_run_actions_run_id_sequence_no ON strategy_run_actions (run_id, sequence_no)"
        )
    if "ix_strategy_run_actions_run_id_status" not in existing_indexes:
        statements.append(
            "CREATE INDEX ix_strategy_run_actions_run_id_status ON strategy_run_actions (run_id, status)"
        )

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _ensure_strategy_run_action_result_indexes(engine) -> None:
    inspector = inspect(engine)
    if "strategy_run_action_results" not in set(inspector.get_table_names()):
        return

    existing_indexes = {
        index["name"] for index in inspector.get_indexes("strategy_run_action_results")
    }
    statements: list[str] = []
    if "ix_strategy_run_action_results_action_id_attempt_no" not in existing_indexes:
        statements.append(
            "CREATE INDEX ix_strategy_run_action_results_action_id_attempt_no ON strategy_run_action_results (action_id, attempt_no)"
        )

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _backfill_schedule_run_types(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "strategy_schedules" not in table_names:
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE strategy_schedules SET market_day_type = 'trading_day' "
                "WHERE market_day_type IS NULL OR trim(market_day_type) = ''"
            )
        )
        connection.execute(
            text(
                "UPDATE strategy_schedules SET run_type = 'trade' "
                "WHERE name LIKE '上午运行%' OR name LIKE '下午运行%'"
            )
        )
        connection.execute(
            text(
                "UPDATE strategy_schedules SET run_type = 'analysis' "
                "WHERE run_type IS NULL OR trim(run_type) = '' OR name IN ('盘前分析', '午间复盘', '收盘分析', '夜间分析', '默认任务')"
            )
        )


def _backfill_strategy_run_types(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "strategy_runs" not in table_names:
        return

    db_path = Path(get_settings().sqlite_db_path)
    if not db_path.exists():
        return

    import json
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        runs = connection.execute(
            "SELECT id, run_type, market_day_type, schedule_name, schedule_id, executed_actions, skill_payloads, decision_payload FROM strategy_runs"
        ).fetchall()
        schedule_day_types = {
            int(row[0]): (str(row[1] or "").strip() or "trading_day")
            for row in connection.execute(
                "SELECT id, market_day_type FROM strategy_schedules"
            ).fetchall()
        }
        trade_order_counts = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT run_id, COUNT(*) FROM trade_orders GROUP BY run_id"
            ).fetchall()
        }

        for row in runs:
            schedule_name = str(row["schedule_name"] or "").strip()
            stored_run_type = str(row["run_type"] or "").strip()
            stored_market_day_type = str(row["market_day_type"] or "").strip()
            inferred = "analysis"
            inferred_market_day_type = "trading_day"

            if schedule_name.startswith("上午运行") or schedule_name.startswith("下午运行"):
                inferred = "trade"
            elif schedule_name in {"盘前分析", "午间复盘", "收盘分析", "夜间分析"}:
                inferred = "analysis"
            elif trade_order_counts.get(int(row["id"]), 0) > 0:
                inferred = "trade"
            else:
                executed_actions = []
                if row["executed_actions"]:
                    try:
                        parsed_actions = json.loads(row["executed_actions"])
                        if isinstance(parsed_actions, list):
                            executed_actions = [item for item in parsed_actions if isinstance(item, dict)]
                    except Exception:
                        executed_actions = []

                if any(str(item.get("action") or "").upper() in {"BUY", "SELL", "CANCEL"} for item in executed_actions):
                    inferred = "trade"
                else:
                    tool_calls: list[dict[str, object]] = []
                    for payload_key in ("skill_payloads", "decision_payload"):
                        raw_payload = row[payload_key]
                        if not raw_payload:
                            continue
                        try:
                            parsed_payload = json.loads(raw_payload)
                        except Exception:
                            continue
                        if not isinstance(parsed_payload, dict):
                            continue
                        payload_tool_calls = parsed_payload.get("tool_calls")
                        if isinstance(payload_tool_calls, list):
                            tool_calls = [item for item in payload_tool_calls if isinstance(item, dict)]
                            if tool_calls:
                                break

                    if any(str(item.get("name") or "") in {"mx_moni_trade", "mx_moni_cancel"} for item in tool_calls):
                        inferred = "trade"
                    elif stored_run_type in {"analysis", "trade"}:
                        inferred = stored_run_type

            schedule_id = row["schedule_id"]
            if schedule_id is not None:
                inferred_market_day_type = schedule_day_types.get(
                    int(schedule_id),
                    "trading_day",
                )
            elif stored_market_day_type in {"trading_day", "non_trading_day"}:
                inferred_market_day_type = stored_market_day_type

            connection.execute(
                "UPDATE strategy_runs SET run_type = ?, market_day_type = ? WHERE id = ?",
                (inferred, inferred_market_day_type, int(row["id"])),
            )

        connection.commit()
    finally:
        connection.close()


def _ensure_strategy_run_columns(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "strategy_runs" not in table_names:
        return

    required_columns = {
        "final_answer": "ALTER TABLE strategy_runs ADD COLUMN final_answer TEXT",
        "run_type": "ALTER TABLE strategy_runs ADD COLUMN run_type VARCHAR(32) DEFAULT 'analysis'",
        "market_day_type": "ALTER TABLE strategy_runs ADD COLUMN market_day_type VARCHAR(32) DEFAULT 'trading_day'",
        "schedule_name": "ALTER TABLE strategy_runs ADD COLUMN schedule_name VARCHAR(64)",
        "schedule_id": "ALTER TABLE strategy_runs ADD COLUMN schedule_id INTEGER",
        "chat_session_id": "ALTER TABLE strategy_runs ADD COLUMN chat_session_id INTEGER",
        "prompt_message_id": "ALTER TABLE strategy_runs ADD COLUMN prompt_message_id INTEGER",
        "response_message_id": "ALTER TABLE strategy_runs ADD COLUMN response_message_id INTEGER",
        "context_summary_version": "ALTER TABLE strategy_runs ADD COLUMN context_summary_version INTEGER",
        "context_tokens_estimate": "ALTER TABLE strategy_runs ADD COLUMN context_tokens_estimate INTEGER",
    }

    with engine.begin() as connection:
        for column_name, statement in required_columns.items():
            current_columns = {
                column["name"]
                for column in inspect(connection).get_columns("strategy_runs")
            }
            if column_name in current_columns:
                continue
            connection.execute(text(statement))


def _ensure_strategy_run_indexes(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "strategy_runs" not in table_names:
        return

    index_names = {
        index["name"]
        for index in inspector.get_indexes("strategy_runs")
        if index.get("name")
    }

    statements: list[str] = []
    if "ix_strategy_runs_started_at" not in index_names:
        statements.append(
            "CREATE INDEX ix_strategy_runs_started_at ON strategy_runs (started_at)"
        )
    if "ix_strategy_runs_chat_session_id" not in index_names:
        statements.append(
            "CREATE INDEX ix_strategy_runs_chat_session_id ON strategy_runs (chat_session_id)"
        )
    if "ix_strategy_runs_schedule_id" not in index_names:
        statements.append(
            "CREATE INDEX ix_strategy_runs_schedule_id ON strategy_runs (schedule_id)"
        )

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _ensure_chat_session_indexes(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "chat_sessions" not in table_names:
        return

    index_names = {
        index["name"]
        for index in inspector.get_indexes("chat_sessions")
        if index.get("name")
    }

    statements: list[str] = []
    if "ix_chat_sessions_kind" not in index_names:
        statements.append("CREATE INDEX ix_chat_sessions_kind ON chat_sessions (kind)")
    if "ix_chat_sessions_slug" not in index_names:
        statements.append("CREATE INDEX ix_chat_sessions_slug ON chat_sessions (slug)")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _ensure_chat_message_indexes(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "chat_messages" not in table_names:
        return

    index_names = {
        index["name"]
        for index in inspector.get_indexes("chat_messages")
        if index.get("name")
    }

    statements: list[str] = []
    if "ix_chat_messages_run_id" not in index_names:
        statements.append("CREATE INDEX ix_chat_messages_run_id ON chat_messages (run_id)")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def get_db() -> Generator[Session, None, None]:
    session = get_session_local()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = get_session_local()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
