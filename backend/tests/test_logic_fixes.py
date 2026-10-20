"""针对 2026-10 逻辑审计修复的回归测试。

覆盖：A1-A5（交易执行链路）、B1/B2（时区）、C1-C4（上下文）、
D1/D2（启动单点）、E1-E3（统计/契约/事件流）及同区域 P2 修复。
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.rate_limit import _limiter
from app.db import database as database_module
from app.db.database import init_db, session_scope
from app.db.models import ChatMessageRecord, ChatSession, StrategyRun
from app.main import create_app
from app.schemas.aniu import AppSettingsUpdate, ChatStreamRequest
from app.services.aniu_service import SHANGHAI_TZ, aniu_service, now_utc
from app.services.aniu_service import _assume_utc
from app.services.chat_session_service import chat_session_service
from app.services.cls_news_service import ClsNewsService
from app.services.event_bus import EventBus, _MAX_SUBSCRIBER_BACKLOG
from app.services.scheduler_service import scheduler_service
from app.services.trading_calendar_service import trading_calendar_service
from app.skills import loader as skill_loader
from skills.mx_core.client import MXClient


def create_test_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(
        trading_calendar_service, "warm_up_months", lambda current: None
    )
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()
    aniu_service._account_overview_cache = None
    aniu_service._account_overview_cache_expires_at = None
    app = create_app()
    return TestClient(app)


def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/aniu/login", json={"password": "release-pass"})
    payload = response.json()
    return {"Authorization": f"Bearer {payload['token']}"}


def reset_db_state() -> None:
    database_module._engine = None
    database_module._session_local = None
    _limiter._buckets.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# A1：交易一致性 autofill 的 run_type 门控
# ---------------------------------------------------------------------------


def _trade_enforce_settings(run_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        run_type=run_type,
        prompt_templates={},
        llm_base_url="https://example.com/v1",
        llm_api_key="key",
        llm_model="model",
    )


def test_enforce_trade_consistency_skipped_for_analysis_runs() -> None:
    """分析任务中声称的交易不应触发 autofill（否则会自动下达真实委托）。"""
    decision = {
        "final_answer": "操作建议：买入贵州茅台(600519) 1000股",
        "tool_calls": [],
    }
    merged, _, _, _, actions = aniu_service._enforce_trade_consistency(
        settings=_trade_enforce_settings("analysis"),
        client=None,
        decision=dict(decision),
        llm_request={},
        llm_response={},
        runtime_trace={},
    )
    assert merged == decision
    assert actions == []
    assert not merged.get("consistency_autofill_applied")


def test_enforce_trade_consistency_runs_for_trade_runs(monkeypatch) -> None:
    """交易任务中声称未执行的交易仍应触发 autofill 流程（进入分析分支）。"""
    captured: dict[str, object] = {}

    def fake_autofill(**kwargs):
        captured["called"] = True
        analysis = {
            "schema": "consistency_operation_v1",
            "check_name": "trade",
            "operations": [],
            "materialized_operations": [],
            "materialized_tool_calls": [],
            "autofill_applied": False,
        }
        return analysis, []

    monkeypatch.setattr(
        aniu_service, "_build_trade_consistency_autofill_tool_calls", fake_autofill
    )
    decision = {
        "final_answer": "执行情况：买入贵州茅台(600519) 1000股",
        "tool_calls": [],
    }
    merged, _, _, _, _ = aniu_service._enforce_trade_consistency(
        settings=_trade_enforce_settings("trade"),
        client=None,
        decision=dict(decision),
        llm_request={},
        llm_response={},
        runtime_trace={},
    )
    assert captured.get("called") is True
    assert merged["final_answer"] != decision["final_answer"]


# ---------------------------------------------------------------------------
# A2：autofill 名称级查重（与 gap 判定同粒度）
# ---------------------------------------------------------------------------


def test_trade_autofill_skips_claims_covered_by_existing_actions() -> None:
    """模型已执行 BUY 600519（500 股）时，声称的 BUY 600519（1000 股）
    不应再生成补单操作（数量不一致是表述问题，不是执行缺口）。"""
    tool_calls = [
        {
            "name": "mx_moni_trade",
            "id": "call-1",
            "arguments": {"action": "BUY", "symbol": "600519", "quantity": 500},
            "result": {
                "ok": True,
                "planned_action": {
                    "action": "BUY",
                    "symbol": "600519",
                    "quantity": 500,
                    "price_type": "MARKET",
                    "price": None,
                },
            },
        }
    ]
    final_answer = (
        "执行情况：买入贵州茅台(600519) 1000股\n"
        "执行情况：卖出五粮液(000858) 200股"
    )
    analysis = aniu_service._analyze_trade_consistency(
        final_answer=final_answer,
        tool_calls=tool_calls,
        run_type="trade",
        gap_detected=True,
    )
    operations = analysis["operations"]
    buy_ops = [op for op in operations if op["arguments"]["action"] == "BUY"]
    sell_ops = [op for op in operations if op["arguments"]["action"] == "SELL"]
    assert buy_ops == []
    assert len(sell_ops) == 1
    assert sell_ops[0]["arguments"]["symbol"] == "000858"


# ---------------------------------------------------------------------------
# A4：自选股声称提取的位置感知与名称长度过滤
# ---------------------------------------------------------------------------


def test_stock_mentions_ignore_inline_reason_text() -> None:
    mentions = aniu_service._extract_stock_mentions(
        "贵州茅台(600519)：业绩超预期，估值偏低"
    )
    assert "贵州茅台" in mentions
    assert "600519" in mentions
    assert "业绩超预期" not in mentions
    assert "估值偏低" not in mentions


def test_stock_mentions_reject_long_plain_phrases() -> None:
    mentions = aniu_service._extract_stock_mentions("本轮新增了2只自选股")
    assert "只自选股" not in mentions
    assert "本轮新增了" not in mentions


def test_stock_mentions_accept_normal_name_length() -> None:
    mentions = aniu_service._extract_stock_mentions("新增自选股：贵州茅台、五粮液")
    assert "贵州茅台" in mentions
    assert "五粮液" in mentions


def test_claimed_self_select_changes_use_name_segment_only() -> None:
    changes = aniu_service._extract_claimed_self_select_changes(
        "新增自选股：\n- 贵州茅台(600519)：业绩超预期，估值偏低\n"
    )
    targets = {change["target"] for change in changes}
    assert "贵州茅台" in targets or "600519" in targets
    assert "业绩超预期" not in targets
    assert "估值偏低" not in targets


# ---------------------------------------------------------------------------
# A5：MX 客户端响应体业务错误检测
# ---------------------------------------------------------------------------


def test_mx_client_raises_on_numeric_error_code() -> None:
    with pytest.raises(RuntimeError, match="code=113"):
        MXClient._raise_on_business_error(
            {"code": 113, "message": "今日调用次数已达上限"}
        )


def test_mx_client_raises_on_known_body_marker() -> None:
    with pytest.raises(RuntimeError, match="未绑定模拟组合账户"):
        MXClient._raise_on_business_error(
            {"message": "当前账户未绑定模拟组合账户"}
        )


def test_mx_client_raises_on_plain_text_error_body() -> None:
    with pytest.raises(RuntimeError, match="API密钥不存在"):
        MXClient._raise_on_business_error("API密钥不存在或已失效")


def test_mx_client_accepts_success_payloads() -> None:
    assert MXClient._raise_on_business_error({"data": {"rows": []}}) is None
    assert MXClient._raise_on_business_error({"code": 200, "data": {}}) is None
    assert MXClient._raise_on_business_error({"code": 0}) is None
    assert MXClient._raise_on_business_error([1, 2, 3]) is None


def test_mx_client_error_message_matches_error_hints() -> None:
    """抛出的异常消息需包含响应体文本，ERROR_HINTS 引导才能命中。"""
    from skills.mx_core.execution import MXExecutionService

    service = MXExecutionService()
    try:
        MXClient._raise_on_business_error(
            {"code": 113, "message": "今日调用次数已达上限"}
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        guidance = service._build_error_guidance(str(exc))
        assert "调用次数" in guidance


# ---------------------------------------------------------------------------
# B1：run 日期过滤的上海日界换算
# ---------------------------------------------------------------------------


def test_list_runs_date_filter_uses_shanghai_day_boundary(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "runs.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    # 上海 2026-10-15 00:30 = UTC 2026-10-14 16:30（存储为 naive UTC）
    early_run = StrategyRun(
        status="completed",
        run_type="analysis",
        started_at=datetime(2026, 10, 14, 16, 30),
        finished_at=datetime(2026, 10, 14, 16, 31),
    )
    # 上海 2026-10-15 10:00 = UTC 2026-10-15 02:00
    mid_run = StrategyRun(
        status="completed",
        run_type="analysis",
        started_at=datetime(2026, 10, 15, 2, 0),
        finished_at=datetime(2026, 10, 15, 2, 5),
    )
    with session_scope() as db:
        db.add_all([early_run, mid_run])

    with session_scope() as db:
        runs = aniu_service.list_runs(db, run_date=date(2026, 10, 15))
        assert {run.id for run in runs} == {early_run.id, mid_run.id}
        runs_prev = aniu_service.list_runs(db, run_date=date(2026, 10, 14))
        assert all(run.id not in {early_run.id, mid_run.id} for run in runs_prev)

    reset_db_state()


# ---------------------------------------------------------------------------
# B2：CLS 快讯时间渲染固定上海时区
# ---------------------------------------------------------------------------


def test_cls_normalize_item_renders_shanghai_time() -> None:
    # 2026-10-15 09:30:00 上海 = epoch 1792785000（无论服务器时区）
    epoch = int(
        datetime(2026, 10, 15, 9, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    service = ClsNewsService()
    item = service._normalize_item(
        {"ctime": epoch, "title": "测试", "content": "内容"}
    )
    assert item is not None
    assert item["time"] == "09:30:00"


# ---------------------------------------------------------------------------
# C2/C3：聊天历史裁剪与附件降级
# ---------------------------------------------------------------------------


def test_chat_history_records_capped_to_recent_window(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            session = ChatSession(title="Long", kind="user")
            db.add(session)
            db.flush()
            for index in range(50):
                db.add(
                    ChatMessageRecord(
                        session_id=session.id,
                        role="user" if index % 2 == 0 else "assistant",
                        content=f"消息 {index}",
                    )
                )
            db.commit()

            from app.services.chat_session_service import MAX_HISTORY_RECORDS

            history_records = (
                db.execute(
                    __import__("sqlalchemy").select(ChatMessageRecord)
                    .where(ChatMessageRecord.session_id == session.id)
                    .order_by(ChatMessageRecord.id.desc())
                    .limit(MAX_HISTORY_RECORDS)
                )
                .scalars()
                .all()
            )
            history_records.reverse()
            messages = chat_session_service._build_history_messages(history_records)
            assert len(messages) == MAX_HISTORY_RECORDS
            contents = [str(m.get("content")) for m in messages]
            assert any("消息 49" in content for content in contents)
            assert not any("消息 0" in content for content in contents)

    reset_db_state()


def test_history_attachments_degraded_except_most_recent(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            session = ChatSession(title="Attachments", kind="user")
            db.add(session)
            db.flush()
            old_record = ChatMessageRecord(
                session_id=session.id,
                role="user",
                content="旧消息",
                attachments=[
                    {"filename": "old.png", "mime_type": "image/png", "storage_path": "/nonexistent/old.png"}
                ],
            )
            db.add(old_record)
            db.flush()
            recent_record = ChatMessageRecord(
                session_id=session.id,
                role="user",
                content="新消息",
                attachments=[
                    {"filename": "new.png", "mime_type": "image/png", "storage_path": "/nonexistent/new.png"}
                ],
            )
            db.add(recent_record)
            db.flush()

            messages = chat_session_service._build_history_messages(
                [old_record, recent_record]
            )
            assert len(messages) == 2
            # 旧消息的附件降级为占位符
            assert messages[0]["content"] == (
                "旧消息\n[历史附件 old.png 内容已省略]"
            )
            # 最近一条带附件的消息保留完整附件内容（文件缺失提示）
            recent_content = messages[1]["content"]
            assert isinstance(recent_content, list)
            recent_texts = [
                part["text"] for part in recent_content if part.get("type") == "text"
            ]
            assert any("新消息" in text for text in recent_texts)
            assert any("文件缺失" in text for text in recent_texts)

    reset_db_state()


# ---------------------------------------------------------------------------
# C4：流式内容聚合语义（committed 与增量缓冲分离）
# ---------------------------------------------------------------------------


def _run_stream_events(monkeypatch, tmp_path, events) -> dict[str, object]:
    """用受控事件序列驱动 stream_chat，返回落库的助手消息。"""
    captured: dict[str, object] = {}

    def fake_chat(*, messages, emit=None, **kwargs):
        captured["messages"] = messages
        for event_type, data in events:
            if emit:
                emit(event_type, **data)
        return "final"

    import app.services.chat_session_service as chat_module

    monkeypatch.setattr(
        chat_module.llm_service, "chat_with_details", fake_chat
    )

    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.llm_base_url = "https://example.com/v1"
            settings.llm_api_key = "key"
            settings.llm_model = "model"
            session = chat_session_service.create_session(db, title="C4")
            session_id = session.id

        list(
            chat_session_service.stream_chat(
                ChatStreamRequest(session_id=session_id, content="问题")
            )
        )

        with session_scope() as db:
            records = (
                db.query(ChatMessageRecord)
                .filter(ChatMessageRecord.session_id == session_id)
                .order_by(ChatMessageRecord.id.asc())
                .all()
            )
            captured["assistant_content"] = (
                records[-1].content if len(records) > 1 else None
            )
    reset_db_state()
    return captured


def test_stream_failure_persists_separated_content(monkeypatch, tmp_path) -> None:
    """中间说明文本 + 半截最终回答 + 失败提示，必须有清晰分隔而非无分隔拼接。"""
    captured = _run_stream_events(
        monkeypatch,
        tmp_path,
        [
            ("llm_message", {"iteration": 1, "content": "我先说明拟执行操作"}),
            ("final_started", {}),
            ("final_delta", {"delta": "真正的"}),
            ("final_delta", {"delta": "部分回答"}),
            ("failed", {"message": "上游超时"}),
        ],
    )
    content = str(captured["assistant_content"])
    assert "我先说明拟执行操作" in content
    assert "真正的部分回答" in content
    assert "我先说明拟执行操作\n\n真正的部分回答" in content
    assert "执行失败：上游超时" in content


def test_stream_retry_replay_does_not_duplicate_content(monkeypatch, tmp_path) -> None:
    """流式重试重播：final_started 重置增量缓冲，内容不重复累积。"""
    captured = _run_stream_events(
        monkeypatch,
        tmp_path,
        [
            ("final_started", {}),
            ("final_delta", {"delta": "第一次尝试的"}),
            ("final_started", {}),
            ("final_delta", {"delta": "重试后的"}),
            ("final_delta", {"delta": "完整回答"}),
            ("completed", {"message": "重试后的完整回答"}),
        ],
    )
    assert captured["assistant_content"] == "重试后的完整回答"


# ---------------------------------------------------------------------------
# D1：技能 loader 故障隔离
# ---------------------------------------------------------------------------


def _write_skill(base: Path, name: str, skill_md: str, handler: str | None = None) -> None:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if handler is not None:
        (skill_dir / "handler.py").write_text(handler, encoding="utf-8")


_GOOD_SKILL_MD = """---
name: good-skill
description: 正常技能
metadata:
  aniu:
    handler_module: null
---
正常技能正文
"""


def test_scan_dir_skips_gbk_encoded_skill(tmp_path) -> None:
    base = tmp_path / "skills"
    base.mkdir()
    _write_skill(base, "good", _GOOD_SKILL_MD)
    broken = base / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_bytes("---\nname: 坏\n---\n正文".encode("gbk"))
    packages = skill_loader._scan_dir(base, "workspace")
    assert [p.id for p in packages] == ["good"]


def test_scan_dir_skips_skill_with_non_dict_metadata(tmp_path) -> None:
    """frontmatter 的 metadata 值为字符串时旧代码会抛 AttributeError 使整个
    注册表重载失败；现在应作为无 handler 的提示型技能正常装载。"""
    base = tmp_path / "skills"
    base.mkdir()
    _write_skill(base, "good", _GOOD_SKILL_MD)
    _write_skill(base, "badmeta", "---\nname: bad\nmetadata: 字符串值\n---\n正文")
    packages = skill_loader._scan_dir(base, "workspace")
    ids = [p.id for p in packages]
    assert "good" in ids
    assert "badmeta" in ids
    bad = next(p for p in packages if p.id == "badmeta")
    assert bad.skill is None


def test_scan_dir_skips_skill_with_raising_handler(tmp_path) -> None:
    """handler 的 Skill 子类 __init__ 抛异常时只跳过该技能，不影响其他技能。"""
    base = tmp_path / "skills"
    base.mkdir()
    _write_skill(base, "good", _GOOD_SKILL_MD)
    _write_skill(
        base,
        "badhandler",
        "---\nname: bad\n---\n正文",
        handler=(
            "from app.skills.base import BaseSkill\n\n"
            "class Skill(BaseSkill):\n"
            "    name = 'bad'\n"
            "    description = 'bad'\n"
            "    def __init__(self):\n"
            "        raise ValueError('handler init boom')\n"
        ),
    )
    packages = skill_loader._scan_dir(base, "workspace")
    assert [p.id for p in packages] == ["good"]


# ---------------------------------------------------------------------------
# D2：交易日历失败负缓存
# ---------------------------------------------------------------------------


def test_calendar_negative_cache_prevents_repeated_fetch(monkeypatch) -> None:
    service = trading_calendar_service.__class__()
    monkeypatch.setattr(service, "_data_path", Path("/nonexistent/calendar.json"))
    fetch_calls = {"count": 0}

    def failing_fetch(month_key):
        fetch_calls["count"] += 1
        raise RuntimeError("remote unavailable")

    monkeypatch.setattr(service, "_fetch_month", failing_fetch)
    with pytest.raises(RuntimeError):
        service.ensure_months(["2027-01"])
    assert fetch_calls["count"] == 1
    # 负缓存生效：第二次调用快速失败，不再发起远程请求
    with pytest.raises(RuntimeError, match="负缓存"):
        service.ensure_months(["2027-01"])
    assert fetch_calls["count"] == 1


# ---------------------------------------------------------------------------
# E2：settings 部分更新不重置未提交字段
# ---------------------------------------------------------------------------


def test_update_settings_preserves_untouched_automation_fields(
    monkeypatch, tmp_path
) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            settings = aniu_service.get_or_create_settings(db)
            settings.automation_session_id = 42
            settings.automation_recent_message_limit = 100
            settings.automation_idle_summary_hours = 24
            db.commit()

        # 前端只提交它已知的字段（不含 automation_*）
        payload = AppSettingsUpdate(
            llm_model="new-model",
            system_prompt="新系统提示",
        )
        with session_scope() as db:
            updated = aniu_service.update_settings(db, payload)
            assert updated.automation_session_id == 42
            assert updated.automation_recent_message_limit == 100
            assert updated.automation_idle_summary_hours == 24
            assert updated.llm_model == "new-model"

    reset_db_state()


# ---------------------------------------------------------------------------
# E1：runtime overview 按时间窗口统计
# ---------------------------------------------------------------------------


def test_runtime_overview_counts_runs_beyond_100_records(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "overview.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    now = now_utc().replace(tzinfo=None)
    with session_scope() as db:
        for index in range(120):
            started = now - timedelta(minutes=5 * (119 - index))
            db.add(
                StrategyRun(
                    status="completed",
                    run_type="analysis",
                    started_at=started,
                    finished_at=started + timedelta(minutes=1),
                )
            )
        db.commit()

    with session_scope() as db:
        overview = aniu_service.get_runtime_overview(db)
        # 旧实现只取最近 100 条，120 条全部在 7 天窗口内时会被截断
        assert overview["recent_7_days"]["analysis_count"] == 120

    reset_db_state()


# ---------------------------------------------------------------------------
# E3：事件流终止保障
# ---------------------------------------------------------------------------


def test_event_bus_never_drops_terminal_events() -> None:
    bus = EventBus()
    sub, _snapshot = bus.subscribe(999)
    # 把订阅者队列填到积压上限
    for index in range(_MAX_SUBSCRIBER_BACKLOG):
        sub.put_nowait({"type": "noise", "i": index})
    # 终端事件必须绕过积压丢弃
    bus.publish(999, "completed", {"message": "done"})
    received = [sub.get_nowait() for _ in range(_MAX_SUBSCRIBER_BACKLOG + 1)]
    assert received[-1]["type"] == "completed"


def test_run_events_endpoint_returns_404_for_unknown_run(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = auth_headers(client)
        response = client.get("/api/aniu/runs/999999/events", headers=headers)
        assert response.status_code == 404
        assert "运行记录不存在" in response.json()["detail"]
    reset_db_state()


def test_fail_stale_running_runs_marks_them_failed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "stale.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    with session_scope() as db:
        stale = StrategyRun(status="running", run_type="analysis")
        finished = StrategyRun(status="completed", run_type="analysis")
        db.add_all([stale, finished])
        db.commit()
        stale_id, finished_id = stale.id, finished.id

    with session_scope() as db:
        marked = aniu_service.fail_stale_running_runs(db)
        assert marked == 1
        assert db.get(StrategyRun, stale_id).status == "failed"
        assert "服务重启" in db.get(StrategyRun, stale_id).error_message
        assert db.get(StrategyRun, finished_id).status == "completed"

    reset_db_state()


# ---------------------------------------------------------------------------
# P2-21/P2-22：一致性提取的条件词与撤单判定
# ---------------------------------------------------------------------------


def test_conditional_marker_does_not_skip_ruyou_list_lines() -> None:
    changes = aniu_service._extract_claimed_trade_changes(
        "执行情况如下：买入贵州茅台(600519) 1000股"
    )
    assert any(change["action"] == "BUY" for change in changes)


def test_conditional_marker_still_skips_real_conditionals() -> None:
    changes = aniu_service._extract_claimed_trade_changes(
        "若突破前高则买入贵州茅台(600519) 1000股"
    )
    assert changes == []


def test_executed_cancel_with_order_id_only_is_not_a_gap() -> None:
    executed = [
        {
            "action": "CANCEL",
            "cancel_type": "order",
            "order_id": "12345",
            "stock_code": None,
            "reason": "",
        }
    ]
    final_answer = "已撤销委托 12345"
    assert aniu_service._has_trade_consistency_gap(final_answer, executed) is False


def test_actual_trade_changes_reads_stock_code_field() -> None:
    changes = aniu_service._extract_actual_trade_changes(
        [
            {
                "action": "CANCEL",
                "cancel_type": "order",
                "order_id": "A1",
                "stock_code": "600519",
            }
        ]
    )
    pairs = [(c["action"], c["target"]) for c in changes]
    assert ("CANCEL", "600519") in pairs


# ---------------------------------------------------------------------------
# 重复委托加固（实际运行中同账户重复下单、敞口被动放大）
# ---------------------------------------------------------------------------


def _trade_tool_call(
    call_id: str,
    *,
    action: str,
    symbol: str,
    quantity: int,
    price_type: str = "MARKET",
    price: float | None = None,
) -> dict:
    return {
        "id": call_id,
        "name": "mx_moni_trade",
        "arguments": {
            "action": action,
            "symbol": symbol,
            "quantity": quantity,
            "price_type": price_type,
            "price": price,
        },
        "result": {
            "ok": True,
            "summary": f"已生成{action}执行计划：{symbol}",
            "planned_action": {
                "action": action,
                "symbol": symbol,
                "quantity": quantity,
                "price_type": price_type,
                "price": price,
            },
        },
    }


def test_duplicate_planned_drafts_in_same_run_are_deduped() -> None:
    """主循环草稿 + followup 草稿（或模型重复发起）对同一标的生成完全
    相同的计划时，只保留一条——否则会双重下单、敞口被动放大。"""
    tool_calls = [
        _trade_tool_call("call-main", action="BUY", symbol="600519", quantity=1000),
        _trade_tool_call("call-followup", action="BUY", symbol="600519", quantity=1000),
    ]
    drafts = aniu_service._extract_planned_action_drafts(tool_calls)
    assert len(drafts) == 1
    assert drafts[0]["tool_call_id"] == "call-main"

    planned = aniu_service._extract_planned_actions(tool_calls)
    assert len(planned) == 1


def test_legitimate_scaling_orders_are_not_deduped() -> None:
    """数量/价格不同的同标的订单（合法加仓/分批）不去重。"""
    tool_calls = [
        _trade_tool_call("call-1", action="BUY", symbol="600519", quantity=500),
        _trade_tool_call("call-2", action="BUY", symbol="600519", quantity=1000),
        _trade_tool_call(
            "call-3", action="SELL", symbol="600519", quantity=500, price_type="LIMIT", price=1800.0
        ),
    ]
    drafts = aniu_service._extract_planned_action_drafts(tool_calls)
    assert len(drafts) == 3


def test_execute_plan_skips_completed_actions(monkeypatch, tmp_path) -> None:
    """对同一 run 重复调用 execute_plan 不得重放已完成的 BUY/SELL。"""
    from app.services.execution_runner_service import execution_runner_service
    from app.services.mx_skill_service import mx_skill_service
    from app.db.models import StrategyRunAction

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "replay.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    executed_symbols: list[str] = []

    def fake_execute_tool(*, client, app_settings, tool_name, arguments):
        executed_symbols.append(str(arguments.get("symbol")))
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": "已提交",
            "result": {"status": "ok"},
            "executed_action": {
                "symbol": str(arguments.get("symbol")),
                "action": str(arguments.get("action")),
                "quantity": int(arguments.get("quantity") or 0),
                "price_type": str(arguments.get("price_type") or "MARKET"),
                "price": arguments.get("price"),
            },
        }

    monkeypatch.setattr(mx_skill_service, "execute_tool", fake_execute_tool)

    with session_scope() as db:
        run = StrategyRun(status="running", run_type="trade")
        db.add(run)
        db.flush()
        completed_action = StrategyRunAction(
            run_id=run.id,
            sequence_no=1,
            phase="executed",
            tool_name="mx_moni_trade",
            action_type="BUY",
            status="completed",
            arguments_payload={"action": "BUY", "symbol": "600519", "quantity": 1000},
        )
        pending_action = StrategyRunAction(
            run_id=run.id,
            sequence_no=2,
            phase="planned",
            tool_name="mx_moni_trade",
            action_type="SELL",
            status="planned",
            arguments_payload={"action": "SELL", "symbol": "000858", "quantity": 200},
        )
        db.add_all([completed_action, pending_action])
        db.commit()
        run_id = run.id

    summary = execution_runner_service.execute_plan(
        run_id=run_id,
        client=None,
        app_settings=SimpleNamespace(run_type="trade"),
    )
    # 只有 planned 状态的 SELL 被执行，completed 的 BUY 不重放
    assert executed_symbols == ["000858"]
    assert summary.action_status_counts.get("completed") == 2

    # 二次调用 execute_plan：全部动作已终态，不再执行任何真实交易
    executed_symbols.clear()
    summary_again = execution_runner_service.execute_plan(
        run_id=run_id,
        client=None,
        app_settings=SimpleNamespace(run_type="trade"),
    )
    assert executed_symbols == []

    reset_db_state()


def test_fail_stale_running_runs_advances_affected_schedule(
    monkeypatch, tmp_path
) -> None:
    """重启后僵尸 run 标记失败的同时，受影响调度的 next_run_at 必须推进：
    被杀的 run 可能已执行部分真实交易，调度若仍指向过去会立即重跑
    同一槽位导致重复下单。"""
    from app.db.models import StrategySchedule

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "stale2.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    with session_scope() as db:
        schedule = StrategySchedule(
            name="交易任务",
            run_type="trade",
            cron_expression="*/30 * * * *",
            task_prompt="prompt",
            enabled=True,
        )
        db.add(schedule)
        db.flush()
        # next_run_at 停留在过去（run 被杀时未推进）
        schedule.next_run_at = now_utc() - timedelta(minutes=10)
        db.add(
            StrategyRun(
                status="running",
                run_type="trade",
                schedule_id=schedule.id,
            )
        )
        db.commit()
        schedule_id = schedule.id

    with session_scope() as db:
        marked = aniu_service.fail_stale_running_runs(db)
        assert marked == 1
        updated = db.get(StrategySchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at is not None
        # 推进到未来（下一个 cron 槽位），不再是过去时间
        assert _assume_utc(updated.next_run_at) > now_utc() - timedelta(seconds=5)

    reset_db_state()


def test_safe_compute_next_run_at_falls_back_when_calendar_fails() -> None:
    """日历等依赖失败时退到 +30 分钟，绝不让已完成的交易运行被翻转成
    failed（否则调度重试会重跑并重复下单）。"""
    from app.db.models import StrategySchedule

    schedule = StrategySchedule(
        name="任务",
        run_type="trade",
        cron_expression="*/30 * * * *",
        task_prompt="prompt",
        enabled=True,
    )

    def failing_compute(*args, **kwargs):
        raise RuntimeError("calendar unavailable")

    original = aniu_service._compute_next_run_at
    aniu_service._compute_next_run_at = failing_compute
    try:
        next_run_at = aniu_service._safe_compute_next_run_at(schedule)
    finally:
        aniu_service._compute_next_run_at = original

    assert next_run_at is not None
    assert _assume_utc(next_run_at) > now_utc() + timedelta(minutes=29)


# ---------------------------------------------------------------------------
# 生产事故回归（2026-10-20 11:15）：撤单声明触发 IndexError: no such group
# ---------------------------------------------------------------------------


def test_claimed_cancel_line_does_not_crash_extraction() -> None:
    """CANCEL 模式分组数少于 BUY/SELL，取组必须安全。

    生产事故：模型最终答复出现可匹配的撤单句（如行首"撤销委托"），
    match.group(3) 对只有 2 个分组的 CANCEL 匹配抛 IndexError，
    导致整次运行在一致性校验阶段失败、最终答复丢失。
    """
    changes = aniu_service._extract_claimed_trade_changes(
        "撤销委托\n已执行：撤单东方财富(600030)\n买入贵州茅台(600519) 1000股"
    )

    actions = {(change["action"], change["target"]) for change in changes}
    assert ("CANCEL", "委托") in actions
    assert ("CANCEL", "东方财富") in actions
    assert ("CANCEL", "600030") in actions
    assert ("BUY", "贵州茅台") in actions
    assert ("BUY", "600519") in actions


def test_claimed_bare_cancel_word_does_not_crash_extraction() -> None:
    """仅有撤单动作词（无目标）的行也不能崩溃。"""
    changes = aniu_service._extract_claimed_trade_changes("撤单")

    assert changes == [{"action": "CANCEL", "target": "委托", "raw_query": "撤单"}]


def test_has_trade_consistency_gap_survives_cancel_claim() -> None:
    """事故调用链：_has_trade_consistency_gap → _extract_claimed_trade_changes。"""
    # 声称撤单但无实际撤单 → 有缺口（True），且不抛异常
    assert aniu_service._has_trade_consistency_gap("撤销委托", []) is True

    # 存在实际撤单 → 视为一致（False）
    executed = [
        {
            "action": "CANCEL",
            "cancel_type": "order",
            "order_id": "261114200000051761",
            "stock_code": None,
            "reason": "",
        }
    ]
    assert aniu_service._has_trade_consistency_gap("撤销委托", executed) is False
