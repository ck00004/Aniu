from pathlib import Path
import sys
from datetime import date, datetime
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.database import init_db
from app.db.models import StrategyRun, StrategySchedule
from app.schemas.aniu import ScheduleUpdate
from app.services.aniu_service import aniu_service
from app.services.jin10_news_service import Jin10NewsService
from app.services.mx_skill_service import mx_skill_service
from app.services.llm_service import LLMService, LLMUpstreamError, llm_service


def test_execute_run_rejects_unknown_schedule_id(monkeypatch, tmp_path) -> None:
    from app.core.config import get_settings
    from app.db import database as database_module
    from app.services.trading_calendar_service import trading_calendar_service

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "guards.db"))
    monkeypatch.setattr(
        trading_calendar_service,
        "warm_up_months",
        lambda current: None,
    )
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    with pytest.raises(RuntimeError, match="指定的定时任务不存在"):
        aniu_service.execute_run(schedule_id=999999)

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_moni_trade_requires_limit_price() -> None:
    with pytest.raises(RuntimeError, match="LIMIT 委托必须提供有效价格"):
        mx_skill_service._handle_moni_trade(
            client=None,
            app_settings=None,
            arguments={
                "action": "BUY",
                "symbol": "600519.SH",
                "quantity": 100,
                "price_type": "LIMIT",
            },
        )


def test_moni_trade_rejects_non_positive_limit_price() -> None:
    with pytest.raises(RuntimeError, match="LIMIT 委托价格必须大于 0"):
        mx_skill_service._handle_moni_trade(
            client=None,
            app_settings=None,
            arguments={
                "action": "BUY",
                "symbol": "600519.SH",
                "quantity": 100,
                "price_type": "LIMIT",
                "price": 0,
            },
        )


def test_manage_self_select_rejects_multiple_targets() -> None:
    with pytest.raises(RuntimeError, match="一次只能添加或删除一只自选股"):
        mx_skill_service._handle_manage_self_select(
            client=None,
            app_settings=None,
            arguments={
                "query": "把贵州茅台和东方财富加入自选股",
            },
        )


def test_moni_trade_rejects_multiple_symbols() -> None:
    with pytest.raises(RuntimeError, match="一次只能交易一只股票"):
        mx_skill_service._handle_moni_trade(
            client=None,
            app_settings=None,
            arguments={
                "action": "BUY",
                "symbol": "600519,300059",
                "quantity": 100,
                "price_type": "MARKET",
            },
        )


def test_moni_cancel_rejects_batch_all_cancel() -> None:
    with pytest.raises(RuntimeError, match="不允许 all 批量撤单"):
        mx_skill_service._handle_moni_cancel(
            client=None,
            app_settings=None,
            arguments={
                "cancel_type": "all",
            },
        )


def test_moni_cancel_requires_single_order_id() -> None:
    with pytest.raises(RuntimeError, match="必须提供 order_id"):
        mx_skill_service._handle_moni_cancel(
            client=None,
            app_settings=None,
            arguments={
                "cancel_type": "order",
            },
        )


def test_resolve_run_type_maps_schedule_names() -> None:
    assert aniu_service._resolve_run_type(None) == "analysis"
    assert aniu_service._resolve_run_type(StrategySchedule(name="盘前分析", run_type="analysis")) == "analysis"
    assert aniu_service._resolve_run_type(StrategySchedule(name="午间复盘", run_type="analysis")) == "analysis"
    assert aniu_service._resolve_run_type(StrategySchedule(name="收盘分析", run_type="analysis")) == "analysis"
    assert aniu_service._resolve_run_type(StrategySchedule(name="夜间分析", run_type="analysis")) == "analysis"
    assert aniu_service._resolve_run_type(StrategySchedule(name="上午运行1号", run_type="trade")) == "trade"
    assert aniu_service._resolve_run_type(StrategySchedule(name="下午运行2号", run_type="trade")) == "trade"


def test_resolve_run_type_falls_back_to_name_when_schedule_type_missing() -> None:
    assert aniu_service._resolve_run_type(StrategySchedule(name="上午运行1号", run_type="")) == "trade"
    assert aniu_service._resolve_run_type(StrategySchedule(name="收盘分析", run_type="")) == "analysis"
    assert aniu_service._resolve_run_type(StrategySchedule(name="夜间分析", run_type="")) == "analysis"


def test_build_persistent_session_user_content_includes_prefetched_context() -> None:
    content = aniu_service._build_persistent_session_user_content(
        settings=None,
        trigger_source="manual",
        schedule_id=None,
        schedule_name=None,
        run_type="analysis",
        task_prompt="请分析今天市场",
        prefetched_context="[Jin10 当天新闻参考]\n1. [09:30:00] 新闻A",
        runtime_context=None,
    )

    assert "本轮任务:" in content
    assert "请分析今天市场" in content
    assert "[Jin10 当天新闻参考]" in content
    assert "新闻A" in content
    assert "mx_get_self_selects" in content
    assert "mx_search_news" in content
    assert "mx_screen_stocks" in content
    assert "mx_manage_self_select" in content


def test_build_persistent_session_user_content_includes_source_overview() -> None:
    content = aniu_service._build_persistent_session_user_content(
        settings=None,
        trigger_source="manual",
        schedule_id=None,
        schedule_name=None,
        run_type="analysis",
        task_prompt="请分析今天市场",
        prefetched_context="[Jin10 新闻诊断]\n摘要A",
        prefetched_context_meta={
            "sources": {
                "jin10": {
                    "source_key": "jin10",
                    "source_label": "Jin10",
                    "analysis_meta": {"summary": "风险偏好回升"},
                },
                "cls": {
                    "source_key": "cls",
                    "source_label": "CLS",
                    "analysis_meta": {"summary": "政策催化增强"},
                },
            },
            "used_sources": ["jin10", "cls"],
        },
        runtime_context=None,
    )

    assert "本轮资讯来源摘要" in content
    assert "Jin10：风险偏好回升" in content
    assert "CLS：政策催化增强" in content


def test_build_persistent_session_user_content_skips_self_select_guidance_for_trade() -> None:
    content = aniu_service._build_persistent_session_user_content(
        settings=None,
        trigger_source="manual",
        schedule_id=None,
        schedule_name=None,
        run_type="trade",
        task_prompt="请根据持仓执行交易",
        prefetched_context="[Jin10 当天新闻参考]\n1. [09:30:00] 新闻A",
        runtime_context=None,
    )

    assert "请根据持仓执行交易" in content
    assert "[Jin10 当天新闻参考]" in content
    assert "mx_manage_self_select" not in content
    assert "mx_moni_trade" in content
    assert "本轮未实际交易" in content


def test_resolve_manual_run_profile_uses_prompt_template_overrides() -> None:
    settings = SimpleNamespace(
        task_prompt="",
        prompt_templates={
            "manual_analysis_task_prompt": "分析模板A",
            "manual_trade_task_prompt": "交易模板B",
        },
    )

    analysis_run_type, analysis_prompt = aniu_service._resolve_manual_run_profile(
        settings=settings,
        manual_run_type=None,
    )
    trade_run_type, trade_prompt = aniu_service._resolve_manual_run_profile(
        settings=settings,
        manual_run_type="trade",
    )

    assert analysis_run_type == "analysis"
    assert analysis_prompt == "分析模板A"
    assert trade_run_type == "trade"
    assert trade_prompt == "交易模板B"


def test_build_run_type_guidance_uses_prompt_template_overrides() -> None:
    settings = SimpleNamespace(
        prompt_templates={
            "analysis_self_select_guidance": "分析约束A",
            "trade_execution_guidance": "交易约束B",
        }
    )

    assert (
        aniu_service._build_run_type_guidance(
            settings=settings,
            run_type="analysis",
        )
        == "分析约束A"
    )
    assert (
        aniu_service._build_run_type_guidance(
            settings=settings,
            run_type="trade",
        )
        == "交易约束B"
    )


def test_build_jin10_prompt_templates_use_overrides() -> None:
    settings = SimpleNamespace(
        prompt_templates={
            "jin10_news_analysis_output_format": "输出格式X",
            "jin10_chunk_analysis_prompt_template": "{header}\n--{chunk_text}--\n{output_format}",
            "jin10_merge_analysis_prompt_template": "{header}\n=={chunk_outputs}==\n{output_format}",
        }
    )

    chunk_prompt = aniu_service._build_jin10_chunk_analysis_prompt(
        settings=settings,
        chunk_text="新闻块",
        chunk_index=1,
        chunk_total=2,
        item_count=6,
        target_day=date(2026, 4, 26),
        current_time=datetime(2026, 4, 26, 9, 30),
    )
    merge_prompt = aniu_service._build_jin10_merge_analysis_prompt(
        settings=settings,
        chunk_outputs=["结论1", "结论2"],
        item_count=6,
        target_day=date(2026, 4, 26),
        current_time=datetime(2026, 4, 26, 9, 30),
    )

    assert "输出格式X" in chunk_prompt
    assert "--新闻块--" in chunk_prompt
    assert "==第 1 批诊断：\n结论1\n\n第 2 批诊断：\n结论2==" in merge_prompt


def test_build_cls_prompt_templates_use_overrides() -> None:
    settings = SimpleNamespace(
        prompt_templates={
            "jin10_news_analysis_output_format": "输出格式Y",
            "jin10_chunk_analysis_prompt_template": "{header}\n--{chunk_text}--\n{output_format}",
            "jin10_merge_analysis_prompt_template": "{header}\n=={chunk_outputs}==\n{output_format}",
        }
    )

    chunk_prompt = aniu_service._build_cls_chunk_analysis_prompt(
        settings=settings,
        chunk_text="电报块",
        chunk_index=1,
        chunk_total=2,
        item_count=8,
        target_day=date(2026, 4, 26),
        current_time=datetime(2026, 4, 26, 9, 30),
    )
    merge_prompt = aniu_service._build_cls_merge_analysis_prompt(
        settings=settings,
        chunk_outputs=["结论A", "结论B"],
        item_count=8,
        target_day=date(2026, 4, 26),
        current_time=datetime(2026, 4, 26, 9, 30),
    )

    assert "CLS 新闻原文" in chunk_prompt
    assert "输出格式Y" in chunk_prompt
    assert "--电报块--" in chunk_prompt
    assert "CLS 新闻分批诊断结果" in merge_prompt
    assert "==第 1 批诊断：\n结论A\n\n第 2 批诊断：\n结论B==" in merge_prompt


def test_select_primary_prefetched_context_meta_prefers_jin10_then_cls() -> None:
    meta = {
        "sources": {
            "cls": {"source_key": "cls", "analysis_text": "cls"},
            "jin10": {"source_key": "jin10", "analysis_text": "jin10"},
        }
    }

    selected = aniu_service._select_primary_prefetched_context_meta(meta)

    assert selected == {"source_key": "jin10", "analysis_text": "jin10"}

    cls_only = {"sources": {"cls": {"source_key": "cls", "analysis_text": "cls"}}}
    assert aniu_service._select_primary_prefetched_context_meta(cls_only) == {
        "source_key": "cls",
        "analysis_text": "cls",
    }


def test_build_analysis_summary_and_final_answer_include_source_labels() -> None:
    source_meta = {
        "sources": {
            "jin10": {
                "source_key": "jin10",
                "source_label": "Jin10",
                "analysis_meta": {"summary": "风险偏好回升"},
            },
            "cls": {
                "source_key": "cls",
                "source_label": "CLS",
                "analysis_meta": {"summary": "政策催化增强"},
            },
        },
        "used_sources": ["jin10", "cls"],
    }

    summary = aniu_service._build_analysis_summary(
        "市场主线转强，关注券商与科技。",
        source_meta,
    )
    decorated = aniu_service._decorate_final_answer_with_sources(
        "市场主线转强，关注券商与科技。",
        source_meta,
    )

    assert summary is not None
    assert summary.startswith("资讯源：Jin10、CLS；")
    assert decorated is not None
    assert decorated.startswith("本轮资讯来源\n- Jin10：风险偏好回升\n- CLS：政策催化增强")
    assert decorated.endswith("市场主线转强，关注券商与科技。")


def test_build_run_trade_details_includes_source_labels() -> None:
    run = StrategyRun(
        executed_actions=[
            {
                "action": "BUY",
                "symbol": "600519.SH",
                "quantity": 100,
                "status": "submitted",
            }
        ],
        skill_payloads={
            "prefetched_context_sources": {
                "sources": {
                    "jin10": {"source_key": "jin10", "source_label": "Jin10"},
                    "cls": {"source_key": "cls", "source_label": "CLS"},
                },
                "used_sources": ["jin10", "cls"],
            }
        },
        decision_payload={"tool_calls": []},
    )
    run.trade_orders = []

    details = aniu_service._build_run_trade_details(run)

    assert len(details) == 1
    assert details[0]["source_labels"] == ["Jin10", "CLS"]
    assert "资讯依据：Jin10 / CLS" in details[0]["summary"]


def test_llm_augment_system_prompt_uses_chat_prompt_override() -> None:
    prompt = llm_service._augment_system_prompt(
        "基础系统提示",
        run_type="chat",
        prompt_templates={
            "chat_confirmation_append_prompt": "聊天确认规则XYZ",
        },
    )

    assert "基础系统提示" in prompt
    assert "聊天确认规则XYZ" in prompt


def test_build_persistent_session_user_content_includes_beijing_time_and_call_index() -> None:
    content = aniu_service._build_persistent_session_user_content(
        settings=None,
        trigger_source="schedule",
        schedule_id=1,
        schedule_name="盘前分析",
        run_type="analysis",
        task_prompt="请分析今天市场",
        prefetched_context=None,
        runtime_context={
            "current_time": datetime(2026, 4, 13, 8, 45),
            "is_trading_day": True,
            "call_index": 3,
            "analysis_call_index": 2,
        },
    )

    assert "北京时间：2026年4月13日 08:45:00" in content
    assert "今日类型: 交易日" in content
    assert "今日第 3 次调用" in content
    assert "今日第 2 次分析调用" in content


def test_validate_schedule_payloads_rejects_more_than_two_non_trading_tasks() -> None:
    with pytest.raises(RuntimeError, match="非交易日最多只能配置两条分析定时任务"):
        aniu_service._validate_schedule_payloads(
            [
                ScheduleUpdate(
                    name="非交易日分析1号",
                    run_type="analysis",
                    market_day_type="non_trading_day",
                    cron_expression="0 9 * * *",
                    task_prompt="a",
                    timeout_seconds=1800,
                    enabled=True,
                ),
                ScheduleUpdate(
                    name="非交易日分析2号",
                    run_type="analysis",
                    market_day_type="non_trading_day",
                    cron_expression="0 14 * * *",
                    task_prompt="b",
                    timeout_seconds=1800,
                    enabled=True,
                ),
                ScheduleUpdate(
                    name="非交易日分析3号",
                    run_type="analysis",
                    market_day_type="non_trading_day",
                    cron_expression="0 20 * * *",
                    task_prompt="c",
                    timeout_seconds=1800,
                    enabled=True,
                ),
            ]
        )


def test_schedule_update_defaults_task_prompt_by_run_type() -> None:
    analysis_payload = ScheduleUpdate(name="盘前分析", run_type="analysis")
    trade_payload = ScheduleUpdate(name="上午运行1号", run_type="trade")

    assert "自选股" in analysis_payload.task_prompt
    assert "本轮未实际交易" in trade_payload.task_prompt


def test_resolve_manual_run_profile_uses_run_type_specific_defaults() -> None:
    analysis_settings = SimpleNamespace(task_prompt="")

    analysis_run_type, analysis_prompt = aniu_service._resolve_manual_run_profile(
        settings=analysis_settings,
        manual_run_type=None,
    )
    trade_run_type, trade_prompt = aniu_service._resolve_manual_run_profile(
        settings=analysis_settings,
        manual_run_type="trade",
    )

    assert analysis_run_type == "analysis"
    assert "自选股" in analysis_prompt
    assert "模拟交易" not in analysis_prompt
    assert trade_run_type == "trade"
    assert "本轮未实际交易" in trade_prompt


def test_list_schedules_upgrades_legacy_task_prompts(monkeypatch, tmp_path) -> None:
    from app.core.config import get_settings
    from app.db import database as database_module
    from app.db.database import session_scope
    from app.services.trading_calendar_service import trading_calendar_service

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "guards-schedules.db"))
    monkeypatch.setattr(
        trading_calendar_service,
        "warm_up_months",
        lambda current: None,
    )
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()

    with session_scope() as db:
        db.add_all(
            [
                StrategySchedule(
                    name="盘前分析",
                    run_type="analysis",
                    cron_expression="45 8 * * 1-5",
                    task_prompt="请根据当前市场和持仓情况生成交易决策。",
                    timeout_seconds=1800,
                    enabled=False,
                ),
                StrategySchedule(
                    name="上午运行1号",
                    run_type="trade",
                    cron_expression="30 9 * * 1-5",
                    task_prompt="你正在执行盘中交易操作，你的唯一目标是追求收益最大化。",
                    timeout_seconds=1800,
                    enabled=False,
                ),
            ]
        )

    with session_scope() as db:
        schedules = aniu_service.list_schedules(db)

    analysis_schedule = next(item for item in schedules if item.run_type == "analysis")
    trade_schedule = next(item for item in schedules if item.run_type == "trade")

    assert "自选股" in str(analysis_schedule.task_prompt)
    assert "本轮未实际交易" in str(trade_schedule.task_prompt)

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_safe_prompt_budget_tracks_85_percent_of_max_context() -> None:
    settings = SimpleNamespace(automation_context_window_tokens=131072)

    assert aniu_service._safe_prompt_budget(settings) == int(131072 * 0.85)


def test_extract_claimed_self_select_changes_detects_added_stock_from_final_answer() -> None:
    final_answer = """
七、自选股维护结论
1. 新增自选股
- 光迅科技（002281）

十、本轮自选维护汇总
新增自选股：光迅科技（002281）
"""

    changes = aniu_service._extract_claimed_self_select_changes(final_answer)

    assert {item["action"] for item in changes} == {"add"}
    assert any(item["target"] == "光迅科技" for item in changes)
    assert any(item["target"] == "002281" for item in changes)


def test_extract_claimed_self_select_changes_treats_new_candidates_as_additions() -> None:
    final_answer = """
五、条件选股筛选后的新增候选结论
其中，我认为今天最值得重点跟踪的新增候选只有3个：
1. 亨通光电
2. 三环集团
3. 中船防务
"""

    changes = aniu_service._extract_claimed_self_select_changes(final_answer)

    assert any(item["action"] == "add" and item["target"] == "亨通光电" for item in changes)
    assert any(item["action"] == "add" and item["target"] == "三环集团" for item in changes)
    assert any(item["action"] == "add" and item["target"] == "中船防务" for item in changes)


def test_extract_claimed_self_select_changes_ignores_reason_lines_in_non_trading_day_summary() -> None:
    final_answer = """
六、本轮自选股调整
1. 本轮新增
1. 杰瑞股份
- 理由：周末油气地缘风险未退，油服是A股里相对更有基本面承接的映射方向
- 后续观察信号：国际油价是否继续走强，A股油服是否放量跟随，而不是只高开脉冲

2. 本轮移除
1. 工业富联
- 理由：旧算力中军短线明显弱于半导体/国产芯片新主线，风险收益比恶化
- 后续观察信号：若未来重新出现放量止跌、强于板块的修复结构，再考虑重新纳入

七、继续保留观察的自选股
1. 中芯国际
- 跟踪理由：半导体制造中军，决定主线强弱
"""

    changes = aniu_service._extract_claimed_self_select_changes(final_answer)

    assert changes == [
        {"action": "add", "target": "杰瑞股份", "raw_query": "杰瑞股份"},
        {"action": "remove", "target": "工业富联", "raw_query": "工业富联"},
    ]


def test_finalize_self_select_consistency_appends_warning_when_claim_has_no_action() -> None:
    final_answer = "新增自选股：光迅科技（002281）"

    result = aniu_service._finalize_self_select_consistency(final_answer, [])

    assert "系统一致性提示" in result
    assert "mx_manage_self_select" in result


def test_finalize_self_select_consistency_keeps_answer_when_action_exists() -> None:
    final_answer = "新增自选股：光迅科技（002281）"
    executed_actions = [
        {
            "action": "MANAGE_SELF_SELECT",
            "query": "将光迅科技（002281）加入自选股",
        }
    ]

    result = aniu_service._finalize_self_select_consistency(final_answer, executed_actions)

    assert result == final_answer


def test_finalize_self_select_consistency_appends_execution_correction_when_answer_denies_actions() -> None:
    final_answer = "本轮没有实际新增任何自选股。\n本轮没有实际移除任何自选股。"
    executed_actions = [
        {
            "action": "MANAGE_SELF_SELECT",
            "query": "把杰瑞股份加入自选股",
        },
        {
            "action": "MANAGE_SELF_SELECT",
            "query": "把工业富联从自选股删除",
        },
    ]

    result = aniu_service._finalize_self_select_consistency(final_answer, executed_actions)

    assert "[系统一致性修正]" in result
    assert "实际新增自选股：杰瑞股份" in result
    assert "实际移除自选股：工业富联" in result


def test_enforce_self_select_consistency_corrects_followup_text_when_actions_exist(
    monkeypatch,
) -> None:
    original_has_gap = aniu_service._has_self_select_consistency_gap
    call_count = {"value": 0}

    def fake_has_gap(final_answer, executed_actions):
        call_count["value"] += 1
        if call_count["value"] == 1:
            return True
        return original_has_gap(final_answer, executed_actions)

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del messages, kwargs
        return (
            {
                "final_answer": "本轮没有实际新增任何自选股。\n本轮没有实际移除任何自选股。",
                "tool_calls": [],
            },
            {"messages": []},
            {"responses": [], "final_message": {"content": "followup"}},
            {"messages": [{"role": "assistant", "content": "followup"}]},
        )

    monkeypatch.setattr(aniu_service, "_has_self_select_consistency_gap", fake_has_gap)
    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)

    decision = {
        "final_answer": "新增自选股：杰瑞股份\n移除自选股：工业富联",
        "tool_calls": [
            {
                "name": "mx_manage_self_select",
                "result": {
                    "ok": True,
                    "executed_action": {
                        "action": "MANAGE_SELF_SELECT",
                        "query": "把杰瑞股份加入自选股",
                    },
                },
            },
            {
                "name": "mx_manage_self_select",
                "result": {
                    "ok": True,
                    "executed_action": {
                        "action": "MANAGE_SELF_SELECT",
                        "query": "把工业富联从自选股删除",
                    },
                },
            },
        ],
    }
    settings = SimpleNamespace(run_type="analysis", prompt_templates={})

    merged_decision, _, _, _, executed_actions = aniu_service._enforce_self_select_consistency(
        settings=settings,
        client=None,
        decision=decision,
        llm_request={},
        llm_response={},
        runtime_trace={"messages": [{"role": "assistant", "content": decision["final_answer"]}]},
    )

    assert len(executed_actions) == 2
    assert "[系统一致性修正]" in str(merged_decision.get("final_answer") or "")
    assert "实际新增自选股：杰瑞股份" in str(merged_decision.get("final_answer") or "")
    assert "实际移除自选股：工业富联" in str(merged_decision.get("final_answer") or "")


def test_merge_consistency_followup_final_answer_preserves_original_analysis() -> None:
    original = "一、行情判断\n今天市场偏强。\n\n七、自选股维护结论\n新增自选股：光迅科技（002281）"
    revised = "一、行情判断\n今天市场偏强。\n\n七、自选股维护结论\n本轮没有实际新增或移除任何自选股。"

    result = aniu_service._merge_consistency_followup_final_answer(original, revised)

    assert "今天市场偏强" in result
    assert "[一致性检查修正说明]" in result
    assert "本轮没有实际新增或移除任何自选股" in result


def test_merge_consistency_followup_final_answer_keeps_revised_when_original_is_contained() -> None:
    original = "今天市场偏强。"
    revised = "今天市场偏强。\n\n补充结论：本轮没有实际新增或移除任何自选股。"

    result = aniu_service._merge_consistency_followup_final_answer(original, revised)

    assert result == revised


def test_finalize_trade_consistency_appends_warning_when_claim_has_no_trade_action() -> None:
    final_answer = "执行：卖出立讯精密 5000股"

    result = aniu_service._finalize_trade_consistency(final_answer, [])

    assert "系统一致性提示" in result
    assert "mx_moni_trade" in result


def test_finalize_trade_consistency_keeps_answer_when_trade_action_exists() -> None:
    final_answer = "执行：卖出立讯精密 5000股"
    executed_actions = [
        {
            "action": "SELL",
            "symbol": "002475",
            "name": "立讯精密",
            "quantity": 5000,
        }
    ]

    result = aniu_service._finalize_trade_consistency(final_answer, executed_actions)

    assert result == final_answer


def test_finalize_trade_consistency_appends_execution_correction_when_answer_denies_trade() -> None:
    final_answer = "本轮未实际交易。"
    executed_actions = [
        {
            "action": "SELL",
            "symbol": "002475",
            "name": "立讯精密",
            "quantity": 5000,
        }
    ]

    result = aniu_service._finalize_trade_consistency(final_answer, executed_actions)

    assert "[系统一致性修正]" in result
    assert "实际卖出：立讯精密(002475) 5000股" in result


def test_enforce_trade_consistency_corrects_followup_text_when_actions_exist(
    monkeypatch,
) -> None:
    original_has_gap = aniu_service._has_trade_consistency_gap
    call_count = {"value": 0}

    def fake_has_gap(final_answer, executed_actions):
        call_count["value"] += 1
        if call_count["value"] == 1:
            return True
        return original_has_gap(final_answer, executed_actions)

    def fake_run_agent_with_messages(*, messages, **kwargs):
        del messages, kwargs
        return (
            {
                "final_answer": "本轮未实际交易。",
                "tool_calls": [],
            },
            {"messages": []},
            {"responses": [], "final_message": {"content": "followup"}},
            {"messages": [{"role": "assistant", "content": "followup"}]},
        )

    monkeypatch.setattr(aniu_service, "_has_trade_consistency_gap", fake_has_gap)
    monkeypatch.setattr(llm_service, "run_agent_with_messages", fake_run_agent_with_messages)

    decision = {
        "final_answer": "执行：卖出立讯精密 5000股",
        "tool_calls": [
            {
                "name": "mx_moni_trade",
                "result": {
                    "ok": True,
                    "executed_action": {
                        "action": "SELL",
                        "symbol": "002475",
                        "name": "立讯精密",
                        "quantity": 5000,
                        "price_type": "MARKET",
                    },
                },
            }
        ],
    }
    settings = SimpleNamespace(run_type="trade", prompt_templates={})

    merged_decision, _, _, _, executed_actions = aniu_service._enforce_trade_consistency(
        settings=settings,
        client=None,
        decision=decision,
        llm_request={},
        llm_response={},
        runtime_trace={"messages": [{"role": "assistant", "content": decision["final_answer"]}]},
    )

    assert len(executed_actions) == 1
    assert "[系统一致性修正]" in str(merged_decision.get("final_answer") or "")
    assert "实际卖出：立讯精密(002475) 5000股" in str(merged_decision.get("final_answer") or "")


def test_jin10_news_service_fetches_raw_news_items(monkeypatch) -> None:
    service = Jin10NewsService()

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "items": [
                    {
                        "id": "1",
                        "time": "09:31:00",
                        "title": "央行公开市场操作",
                        "content": "今日净投放资金，市场流动性边际改善。",
                        "important": True,
                        "createdAt": 1776821460000,
                    }
                ],
                "total": 1,
                "hasMore": False,
            }

    captured: dict[str, object] = {}

    def fake_get(url: str, *, params: dict[str, str], timeout: float):
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("app.services.jin10_news_service.httpx.get", fake_get)

    items, meta = service.fetch_news_items(
        base_url="http://127.0.0.1:3000",
        target_day=date(2026, 4, 22),
        current_time=datetime(2026, 4, 22, 14, 30),
        limit=10,
        timeout_seconds=3,
    )

    assert captured["url"] == "http://127.0.0.1:3000/api/news"
    assert captured["params"] == {
        "date": "2026-04-22",
        "startTime": "00:00:00",
        "endTime": "14:30:00",
        "limit": "10",
    }
    assert captured["timeout"] == 3
    assert items == [
        {
            "time": "09:31:00",
            "title": "央行公开市场操作",
            "content": "今日净投放资金，市场流动性边际改善。",
        }
    ]
    assert meta is not None
    assert meta["ok"] is True


def test_jin10_news_service_fetches_all_pages(monkeypatch) -> None:
    service = Jin10NewsService()

    calls: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    def fake_get(url: str, *, params: dict[str, str], timeout: float):
        calls.append({"url": url, "params": dict(params), "timeout": timeout})
        before = params.get("before")
        if before is None:
            return FakeResponse(
                {
                    "items": [
                        {
                            "id": "1",
                            "time": "09:31:00",
                            "title": "新闻A",
                            "content": "内容A",
                            "important": True,
                            "createdAt": 1776821460000,
                        },
                        {
                            "id": "2",
                            "time": "09:25:00",
                            "title": "新闻B",
                            "content": "内容B",
                            "important": True,
                            "createdAt": 1776821100000,
                        },
                    ],
                    "total": 3,
                    "hasMore": True,
                }
            )
        return FakeResponse(
            {
                "items": [
                    {
                        "id": "3",
                        "time": "09:10:00",
                        "title": "新闻C",
                        "content": "内容C",
                        "important": True,
                        "createdAt": 1776820200000,
                    }
                ],
                "total": 3,
                "hasMore": False,
            }
        )

    monkeypatch.setattr("app.services.jin10_news_service.httpx.get", fake_get)

    items, meta = service.fetch_news_items(
        base_url="http://127.0.0.1:3000",
        target_day=date(2026, 4, 22),
        current_time=datetime(2026, 4, 22, 14, 30),
        limit=2,
        timeout_seconds=3,
    )

    assert len(calls) == 2
    assert calls[0]["params"]["limit"] == "2"
    assert "before" not in calls[0]["params"]
    assert calls[1]["params"]["before"] == "1776821100000"
    assert [item["title"] for item in items] == ["新闻A", "新闻B", "新闻C"]
    assert meta is not None
    assert meta["ok"] is True
    assert meta["item_count"] == 3
    assert meta["total"] == 3
    assert meta["has_more"] is False


def test_analyze_jin10_news_builds_diagnosis(monkeypatch) -> None:
    captured_messages: list[list[dict[str, str]]] = []

    def fake_generate_text(**kwargs):
        captured_messages.append(kwargs["messages"])
        return (
            "市场总览：政策偏暖，风险偏好修复。\nA股重点方向：券商；金融市场联动：汇率压力缓和。\n交易观察：关注成交量验证。\n风险提示：海外扰动仍在。",
            {"messages": kwargs["messages"]},
            {"choices": [{"message": {"content": "ok"}}]},
        )

    monkeypatch.setattr(llm_service, "generate_text", fake_generate_text)

    text, meta = aniu_service._analyze_jin10_news(
        settings=SimpleNamespace(
            llm_model="demo-model",
            llm_base_url="https://example.com/v1",
            llm_api_key="llm-key",
            timeout_seconds=60,
        ),
        items=[
            {
                "time": "09:30:00",
                "title": "央行公开市场操作",
                "content": "流动性边际改善。",
            },
            {
                "time": "10:00:00",
                "title": "科技政策推进",
                "content": "自主可控方向继续强化。",
            },
        ],
        target_day=date(2026, 4, 22),
        current_time=datetime(2026, 4, 22, 14, 30),
        emit=None,
    )

    assert text is not None
    assert "市场总览" in text
    assert meta is not None
    assert meta["ok"] is True
    assert meta["status"] == "ok"
    assert meta["item_count"] == 2
    assert meta["chunk_count"] == 1
    assert len(captured_messages) == 1
    assert "Jin10 新闻原文" in str(captured_messages[0][0]["content"])


def test_infer_run_type_recovers_trade_runs_from_schedule_name() -> None:
    run = SimpleNamespace(
        schedule_name="上午运行1号",
        trade_orders=[],
        executed_actions=None,
        skill_payloads=None,
        decision_payload=None,
        run_type="analysis",
    )

    assert aniu_service._infer_run_type(run) == "trade"


def test_infer_run_type_recovers_trade_runs_from_actions() -> None:
    run = SimpleNamespace(
        schedule_name=None,
        trade_orders=[],
        executed_actions=[{"action": "BUY", "symbol": "300059"}],
        skill_payloads=None,
        decision_payload=None,
        run_type="analysis",
    )

    assert aniu_service._infer_run_type(run) == "trade"


def test_build_tools_excludes_trade_mutations_for_analysis_runs() -> None:
    tools = mx_skill_service.build_tools(run_type="analysis")
    names = {tool["function"]["name"] for tool in tools}

    assert "mx_moni_trade" not in names
    assert "mx_moni_cancel" not in names
    assert "mx_query_market" in names
    assert "mx_get_positions" in names


def test_build_tools_includes_trade_mutations_for_trade_runs() -> None:
    tools = mx_skill_service.build_tools(run_type="trade")
    names = {tool["function"]["name"] for tool in tools}

    assert "mx_moni_trade" in names
    assert "mx_moni_cancel" in names


def test_build_initial_request_payload_uses_run_type_tool_profile() -> None:
    app_settings = SimpleNamespace(
        llm_model="demo-model",
        system_prompt="system",
        task_prompt="task",
        run_type="analysis",
    )

    payload = llm_service.build_initial_request_payload(app_settings)
    names = {tool["function"]["name"] for tool in payload["tools"]}

    assert "mx_moni_trade" not in names
    assert "mx_query_market" in names


def test_consume_llm_stream_uses_fresh_http_client_per_request(monkeypatch) -> None:
    service = LLMService()
    created_timeouts: list[int] = []
    client_ids: list[int] = []

    class FakeResponse:
        is_error = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            return iter(())

        def read(self) -> bytes:
            return b""

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method, url, headers=None, json=None):
            del method, url, headers, json
            client_ids.append(id(self))
            return FakeResponse()

    def fake_create_http_client(timeout_seconds: int):
        created_timeouts.append(timeout_seconds)
        return FakeClient()

    monkeypatch.setattr(service, "_create_http_client", fake_create_http_client)
    monkeypatch.setattr(
        service,
        "_parse_llm_stream_response",
        lambda *, lines, emit, cancel_event=None: {
            "choices": [{"message": {"content": "ok"}}]
        },
    )

    payload = {"messages": [], "model": "demo"}
    service._consume_llm_stream(
        base_url="https://example.com/v1",
        api_key="token",
        payload=payload,
        timeout_seconds=5,
    )
    service._consume_llm_stream(
        base_url="https://example.com/v1",
        api_key="token",
        payload=payload,
        timeout_seconds=7,
    )

    assert created_timeouts == [5, 7]
    assert len(client_ids) == 2
    assert client_ids[0] != client_ids[1]


def test_consume_llm_stream_reads_json_error_body_from_stream(monkeypatch) -> None:
    service = LLMService()

    class FakeErrorResponse:
        status_code = 400
        is_error = True
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return (
                b'{"error":{"message":"stream_options.include_usage is not supported"}}'
            )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method, url, headers=None, json=None):
            del method, url, headers, json
            return FakeErrorResponse()

    monkeypatch.setattr(service, "_create_http_client", lambda timeout_seconds: FakeClient())

    with pytest.raises(
        RuntimeError,
        match=r"大模型请求参数错误 \(400\): stream_options.include_usage is not supported",
    ) as exc_info:
        service._consume_llm_stream(
            base_url="https://example.com/v1",
            api_key="token",
            payload={"messages": [], "model": "demo"},
            timeout_seconds=5,
        )

    assert "Attempted to access streaming response content" not in str(exc_info.value)


def test_consume_llm_stream_reads_text_error_body_from_stream(monkeypatch) -> None:
    service = LLMService()

    class FakeErrorResponse:
        status_code = 500
        is_error = True
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return b"upstream internal error"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method, url, headers=None, json=None):
            del method, url, headers, json
            return FakeErrorResponse()

    monkeypatch.setattr(service, "_create_http_client", lambda timeout_seconds: FakeClient())

    with pytest.raises(
        RuntimeError,
        match=r"大模型接口返回错误 \(500\): upstream internal error",
    ):
        service._consume_llm_stream(
            base_url="https://example.com/v1",
            api_key="token",
            payload={"messages": [], "model": "demo"},
            timeout_seconds=5,
        )


def test_parse_llm_stream_response_raises_for_error_chunk() -> None:
    service = LLMService()

    lines = iter(
        [
            'data: {"error":{"message":"quota exceeded"}}',
            "",
        ]
    )

    with pytest.raises(RuntimeError, match="大模型流式响应错误: quota exceeded"):
        service._parse_llm_stream_response(lines=lines, emit=lambda *_a, **_kw: None)


def test_call_llm_stream_retries_without_include_usage_on_400(monkeypatch) -> None:
    service = LLMService()
    seen_payloads: list[dict[str, object]] = []

    def fake_consume_llm_stream(*, payload, **kwargs):
        del kwargs
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            raise LLMUpstreamError(
                "大模型请求参数错误 (400): unsupported stream_options",
                status_code=400,
            )
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(service, "_consume_llm_stream", fake_consume_llm_stream)

    result = service._call_llm_stream(
        base_url="https://example.com/v1",
        api_key="token",
        payload={"messages": [], "model": "demo"},
        timeout_seconds=5,
    )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert len(seen_payloads) == 2
    assert seen_payloads[0]["stream"] is True
    assert seen_payloads[0]["stream_options"] == {"include_usage": True}
    assert seen_payloads[1]["stream"] is True
    assert "stream_options" not in seen_payloads[1]


def test_call_llm_stream_retries_on_retryable_upstream_error(monkeypatch) -> None:
    service = LLMService()
    seen_payloads: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    def fake_consume_llm_stream(*, payload, **kwargs):
        del kwargs
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            raise LLMUpstreamError(
                "大模型接口返回错误 (502): Upstream request failed",
                status_code=502,
            )
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(service, "_consume_llm_stream", fake_consume_llm_stream)
    monkeypatch.setattr("app.services.llm_service.time.sleep", sleep_calls.append)

    result = service._call_llm_stream(
        base_url="https://example.com/v1",
        api_key="token",
        payload={"messages": [], "model": "demo"},
        timeout_seconds=5,
    )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert len(seen_payloads) == 2
    assert sleep_calls == [1.0]


def test_agent_loop_retries_when_final_answer_is_empty_after_tools() -> None:
    service = LLMService()
    seen_payloads: list[dict[str, object]] = []

    responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "tool-1",
                                "function": {
                                    "name": "demo_tool",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                    },
                    "finish_reason": "stop",
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": "最终结论：继续跟踪新能源主线。",
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    ]

    def fake_call_llm_stream(*, payload, **kwargs):
        del kwargs
        seen_payloads.append(payload)
        return responses[len(seen_payloads) - 1]

    service._call_llm_stream = fake_call_llm_stream  # type: ignore[method-assign]

    result = service._agent_loop(
        model="demo-model",
        base_url="https://example.com/v1",
        api_key="token",
        initial_messages=[{"role": "user", "content": "请分析市场"}],
        run_type="analysis",
        timeout_seconds=1800,
        tool_executor=lambda tool_name, arguments: {
            "ok": True,
            "tool_name": tool_name,
            "summary": "工具执行成功",
            "result": {"value": 1, "arguments": arguments},
        },
        emit=lambda *_a, **_kw: None,
    )

    assert result["final_answer"] == "最终结论：继续跟踪新能源主线。"
    assert len(result["tool_history"]) == 1
    assert len(seen_payloads) == 3
    last_messages = seen_payloads[-1]["messages"]
    assert any(
        message.get("role") == "user"
        and "请基于上面的工具结果直接输出最终结论" in str(message.get("content") or "")
        for message in last_messages
    )


def test_execute_tool_adds_guidance_for_api_key_error() -> None:
    def boom(*, client, app_settings, arguments):
        del client, app_settings, arguments
        raise RuntimeError("401 Unauthorized / API密钥不存在")

    original_handler = mx_skill_service._handlers["mx_get_balance"]
    mx_skill_service._handlers["mx_get_balance"] = boom
    try:
        result = mx_skill_service.execute_tool(
            client=None,
            app_settings=None,
            tool_name="mx_get_balance",
            arguments={},
        )
    finally:
        mx_skill_service._handlers["mx_get_balance"] = original_handler

    assert result["ok"] is False
    assert "请检查 MX_APIKEY" in result["error"]


def test_screen_tool_returns_raw_result_without_normalized() -> None:
    class StubClient:
        def screen_stocks(self, query):
            del query
            return {
                "data": {
                    "data": {
                        "allResults": {
                            "result": {
                                "total": 1,
                                "columns": [
                                    {"key": "SECURITY_CODE", "title": "代码"},
                                    {"key": "SECURITY_SHORT_NAME", "title": "名称"},
                                    {"key": "NEWEST_PRICE", "title": "最新价"},
                                ],
                                "dataList": [
                                    {
                                        "SECURITY_CODE": "300059",
                                        "SECURITY_SHORT_NAME": "东方财富",
                                        "NEWEST_PRICE": "20.01",
                                    }
                                ],
                            }
                        }
                    }
                }
            }

    result = mx_skill_service._handle_screen_stocks(
        client=StubClient(),
        app_settings=SimpleNamespace(task_prompt=""),
        arguments={"query": "低估值股票"},
    )

    assert result["ok"] is True
    assert "normalized" not in result
    rows = result["result"]["data"]["data"]["allResults"]["result"]["dataList"]
    assert rows[0]["SECURITY_CODE"] == "300059"
