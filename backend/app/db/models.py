from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.constants import DEFAULT_SYSTEM_PROMPT
from app.core.prompt_templates import encode_prompt_template_overrides, merge_prompt_templates


class Base(DeclarativeBase):
    pass


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_name: Mapped[str] = mapped_column(String(32), default="openai-compatible")
    mx_api_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    jin10_api_base_url: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    cls_api_base_url: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    llm_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    llm_api_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    llm_model: Mapped[str] = mapped_column(String(128), default="gpt-4o-mini")
    disabled_skill_ids_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
    )
    system_prompt: Mapped[str] = mapped_column(
        Text,
        default=DEFAULT_SYSTEM_PROMPT,
    )
    prompt_templates_json: Mapped[str] = mapped_column(
        Text,
        default="{}",
    )
    analyst_prompt: Mapped[str] = mapped_column(
        Text,
        default=(
            "请结合市场数据、资讯、候选股票、持仓和资金情况做判断。"
            "当信号不明确时返回HOLD。"
        ),
    )
    market_query: Mapped[str] = mapped_column(
        String(255), default="上证指数今天走势和市场概况"
    )
    news_query: Mapped[str] = mapped_column(String(255), default="今天A股市场热点新闻")
    screener_query: Mapped[str] = mapped_column(
        String(255), default="A股今天值得关注的强势股"
    )
    max_actions: Mapped[int] = mapped_column(Integer, default=2)
    trade_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    automation_session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    automation_context_window_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=131072
    )
    automation_target_prompt_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=111411
    )
    automation_recent_message_limit: Mapped[int] = mapped_column(
        Integer, default=24
    )
    automation_enable_auto_compaction: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    automation_idle_summary_hours: Mapped[int] = mapped_column(Integer, default=12)
    automation_dream_time: Mapped[str] = mapped_column(String(8), default="00:30")
    automation_context_source: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default="default"
    )
    automation_context_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    @property
    def prompt_templates(self) -> dict[str, str]:
        return merge_prompt_templates(self.prompt_templates_json)

    @prompt_templates.setter
    def prompt_templates(self, value: Any) -> None:
        self.prompt_templates_json = encode_prompt_template_overrides(value)


class StrategySchedule(Base):
    __tablename__ = "strategy_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="默认调度任务")
    run_type: Mapped[str] = mapped_column(String(32), default="analysis")
    market_day_type: Mapped[str] = mapped_column(String(32), default="trading_day")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    cron_expression: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger_source: Mapped[str] = mapped_column(String(32), default="manual")
    run_type: Mapped[str] = mapped_column(String(32), default="analysis")
    market_day_type: Mapped[str] = mapped_column(String(32), default="trading_day")
    schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    schedule_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chat_session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    prompt_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_summary_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_tokens_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    analysis_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_request_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    llm_response_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    skill_payloads: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    decision_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    executed_actions: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    trade_orders: Mapped[list["TradeOrder"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    actions: Mapped[list["StrategyRunAction"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="StrategyRunAction.sequence_no, StrategyRunAction.id",
    )


class StrategyRunAction(Base):
    __tablename__ = "strategy_run_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id", ondelete="CASCADE"), index=True)
    sequence_no: Mapped[int] = mapped_column(Integer, default=1)
    phase: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    tool_name: Mapped[str] = mapped_column(String(64), index=True)
    action_type: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    status: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arguments_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    planned_action_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    executed_action_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped[StrategyRun] = relationship(back_populates="actions")
    results: Mapped[list["StrategyRunActionResult"]] = relationship(
        back_populates="action",
        cascade="all, delete-orphan",
        order_by="StrategyRunActionResult.attempt_no, StrategyRunActionResult.id",
    )


class StrategyRunActionResult(Base):
    __tablename__ = "strategy_run_action_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    action_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_run_actions.id", ondelete="CASCADE"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    action: Mapped[StrategyRunAction] = relationship(back_populates="results")


class TradeOrder(Base):
    __tablename__ = "trade_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(Integer)
    price_type: Mapped[str] = mapped_column(String(16), default="MARKET")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="submitted")
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    run: Mapped[StrategyRun] = relationship(back_populates="trade_orders")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120), default="新对话")
    kind: Mapped[str] = mapped_column(String(32), default="user", index=True)
    slug: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    archived_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_compacted_message_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    last_compacted_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )

    messages: Mapped[list["ChatMessageRecord"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessageRecord.id",
    )


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    message_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meta_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True
    )
    attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class ChatAttachment(Base):
    __tablename__ = "chat_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ToolInvocationLog(Base):
    """上游工具调用日志（移植自原版 Aniu stock_api_call_logs 的思路）。

    记录每次妙想接口调用的端点、参数摘要、状态、耗时与响应规模，
    让上游调用可观测、可排障。
    """

    __tablename__ = "tool_invocation_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="mx")
    operation: Mapped[str] = mapped_column(String(128))
    request_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="success")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class MemoryItem(Base):
    """长期交易记忆（移植自原版 Aniu memory_items）。

    乐观锁版本控制：update/delete 必须携带 expected_version；
    软删除保留历史。
    """

    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )


class MemoryActivity(Base):
    """记忆读写活动审计（移植自原版 Aniu memory_activities）。"""

    __tablename__ = "memory_activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(16))
    keywords: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class MemoryDream(Base):
    """记忆梦境任务（移植自原版 Aniu memory_dreams）。

    每个整理日期一条（幂等）；状态机 pending → running → completed/failed。
    """

    __tablename__ = "memory_dreams"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TradeIntent(Base):
    """交易意图（补丁 C：跨轮/跨任务防重复交易的持久化挡板）。

    signal_id 由后端按（交易日, 标的, 方向）派生——同一天同一标的同一
    方向视为同一个交易信号，15 分钟任务反复触发同一信号不会产生新键。
    tranche_id 区分同一信号下的合法分批：前一档已成交且冷却期已过才允
    许建立新档，每日每标的每方向有分批上限。
    """

    __tablename__ = "trade_intents"
    __table_args__ = (
        UniqueConstraint(
            "signal_date",
            "symbol",
            "side",
            "tranche_id",
            name="uq_trade_intent_signal_tranche",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_date: Mapped[str] = mapped_column(String(10), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8), index=True)
    tranche_id: Mapped[int] = mapped_column(Integer, default=1)
    intent_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    # PLANNED / EXECUTING / SUBMITTED / UNKNOWN / FILLED / PARTIALLY_FILLED
    # / CANCELED / FAILED / CANCEL_UNKNOWN
    status: Mapped[str] = mapped_column(String(20), default="planned", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # UNKNOWN 可见性核验：远端委托列表暂不可见时不能立即判定"未提交"，
    # 需要连续多次独立核验且超过等待期才允许收敛为 failed。
    unknown_checks: Mapped[int] = mapped_column(Integer, default=0)
    first_unknown_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
