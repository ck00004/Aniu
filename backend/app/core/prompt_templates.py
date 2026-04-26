from __future__ import annotations

import json
from typing import Any


MANUAL_ANALYSIS_TASK_PROMPT = (
    "请结合当前市场环境、持仓、自选股与最新资讯完成本轮分析。"
    "重点说明主题主线、催化验证、风险点、自选股维护结论，以及后续观察信号。"
)

MANUAL_TRADE_TASK_PROMPT = (
    "请根据当前市场、持仓、资金、委托与最新行情生成交易决策。"
    "如决定买入、卖出或撤单，必须调用交易工具完成实际执行后再写入最终结论；"
    "如本轮不交易，必须明确说明本轮未实际交易及原因。"
)

DEFAULT_PROMPT_TEMPLATES: dict[str, str] = {
    "manual_analysis_task_prompt": MANUAL_ANALYSIS_TASK_PROMPT,
    "manual_trade_task_prompt": MANUAL_TRADE_TASK_PROMPT,
    "analysis_self_select_guidance": """[分析执行要求]
本轮属于分析任务，必须执行选股与自选维护流程：
1. 优先结合上方 Jin10 当天新闻参考，提炼当日主题、催化、风险与资金偏好；若未提供 Jin10 参考，也要基于当日资讯继续完成分析。
2. 必须调用 mx_get_self_selects 查看当前自选股列表，先盘点已有关注标的。
3. 必须调用 mx_search_news 检索与当日主题、候选行业、候选个股相关的最新资讯、公告、研报或政策信息，核验催化是否成立。
4. 必须调用 mx_screen_stocks 按主题、业绩、资金、趋势或估值等条件筛选候选股票，不得只凭主观记忆直接给出结论。
5. 对确认值得后续持续跟踪的股票，调用 mx_manage_self_select 加入自选股；单次调用只能新增一只股票，如需新增多只必须依次多次调用。
6. 对现有自选股中逻辑证伪、催化结束、关注价值下降、流动性明显不足或风险收益比恶化的股票，调用 mx_manage_self_select 从自选股移除；单次调用只能移除一只股票，如需移除多只必须依次多次调用。
7. 如果没有找到合适的新标的，可以不新增；如果现有自选股仍应继续观察，可以不移除，但必须明确说明检查结论。
8. 最终输出必须单独说明：新增自选股、移除自选股、继续保留观察的自选股，以及每只股票的跟踪理由与后续观察信号。""",
    "trade_execution_guidance": """[交易执行要求]
本轮属于交易任务，最终结论必须与真实交易执行完全一致：
1. 必须先调用持仓、资产、委托或行情相关工具，再做买卖决策，不得跳过核验直接下结论。
2. 如果你决定买入、卖出或撤单，必须调用 mx_moni_trade 或 mx_moni_cancel 执行实际操作，然后才能在最终结论中写“买入”“卖出”“撤单”“已执行”。其中买入或卖出时，单次工具调用只能处理一只股票；撤单时单次工具调用只能按单号撤销一笔委托，不允许 all 批量撤单；如需处理多笔交易或撤单，必须依次多次调用工具。
3. 如果你没有实际调用交易工具，就不能在最终结论中声称已经下单、已经卖出、已经买入或已经撤单。
4. 如果本轮只形成观察结论而不执行交易，必须明确写出“本轮未实际交易”。
5. 最终输出必须单独说明：本轮实际交易动作、未执行原因、保留仓位与下一步观察条件。""",
    "self_select_consistency_followup_prompt": """你刚才的最终结论里提到了自选股新增或移除，但当前工具执行记录没有对应的 mx_manage_self_select 操作。

请立即进行一致性修正，且只能二选一：
1. 如果你确认本轮确实应该新增或移除自选股，请立刻调用 mx_manage_self_select 完成实际操作，然后再给出最终结论。
2. 如果你不打算实际执行自选股变更，请重写最终结论，明确说明本轮没有实际新增或移除任何自选股，不能再声称“已新增”“已移除”，也不能把未加入自选的股票表述为“新增候选”“新增重点跟踪对象”或其他等价新增表述。

注意：最终自然语言结论必须与真实工具执行结果完全一致。""",
    "trade_consistency_followup_prompt": """你刚才的最终结论里提到了明确的买入、卖出或撤单动作，但当前工具执行记录没有对应的交易执行。

请立即进行一致性修正，且只能二选一：
1. 如果你确认本轮确实应该执行交易，请立刻调用 mx_moni_trade 或 mx_moni_cancel 完成实际操作，然后再给出最终结论。
2. 如果你不打算实际执行交易，请重写最终结论，明确说明本轮未实际交易，不能再声称“已买入”“已卖出”“执行买入”“执行卖出”“执行撤单”。

注意：最终自然语言结论必须与真实工具执行结果完全一致。""",
    "jin10_news_analysis_system_prompt": """你是负责盘前/盘中情报研判的专业市场策略分析师。
你的任务是只基于给定的 {source_name} 新闻原文，提炼对 A股市场和金融市场最重要的影响。
严格要求：
1. 只能依据输入新闻做归纳，不得编造未出现的信息。
2. 优先关注宏观政策、监管、产业政策、汇率利率、大宗商品、海外市场联动、券商银行、科技制造、地产链、消费、军工与风险偏好变化。
3. 输出必须服务于后续分析任务和交易任务，强调“可能影响哪些板块/资产、为何影响、还需验证什么”。
4. 若新闻噪音较多，要主动去噪，只保留真正可能影响 A股或金融市场定价的信号。""",
    "jin10_news_analysis_output_format": """请严格使用以下结构输出：
市场总览：用 3-5 句概括今日新闻流对 A股与金融市场的主导影响。
A股重点方向：列出 3-5 个最值得关注的方向或板块，并写明驱动原因。
金融市场联动：说明对利率、汇率、商品、港股、美股或风险偏好的联动影响；若不明显请明确写“不明显”。
交易观察：给出 3-5 条可供后续分析/交易任务直接使用的观察点，强调需要结合盘口、量价、公告或持仓验证。
风险提示：指出最值得警惕的 2-3 个不确定性或反转风险。""",
    "jin10_chunk_analysis_prompt_template": "{header}\n\n{chunk_text}\n\n请只基于这些新闻原文，提炼这批新闻对 A股市场和金融市场的影响。\n{output_format}",
    "jin10_merge_analysis_prompt_template": "{header}\n\n{chunk_outputs}\n\n请去重、消除矛盾，保留真正对 A股和金融市场有定价意义的信息。\n{output_format}",
    "empty_final_answer_followup_prompt": "你已经完成了工具调用。请基于上面的工具结果直接输出最终结论，必须给出可展示的中文结论，不要返回空字符串，也不要只返回工具调用。",
    "chat_confirmation_append_prompt": "聊天专用安全规则：当操作涉及交易执行、下单、撤单、自选股增删、写入、删除、覆盖、批量修改或其他会改变数据、文件、配置、状态的破坏性操作时，你必须先明确说明拟执行操作、影响范围和潜在风险，并在得到用户明确确认后才能调用工具或执行操作；若未获得明确确认，只能提供方案、预览或建议，不得直接执行。",
}


def _coerce_prompt_template_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items() if item is not None}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return {
                str(key): str(item)
                for key, item in parsed.items()
                if item is not None
            }
    return {}


def merge_prompt_templates(value: Any) -> dict[str, str]:
    overrides = _coerce_prompt_template_map(value)
    result: dict[str, str] = {}
    for key, default_value in DEFAULT_PROMPT_TEMPLATES.items():
        override = str(overrides.get(key) or "").strip()
        result[key] = override or default_value
    return result


def normalize_prompt_template_overrides(value: Any) -> dict[str, str]:
    merged = _coerce_prompt_template_map(value)
    overrides: dict[str, str] = {}
    for key, default_value in DEFAULT_PROMPT_TEMPLATES.items():
        text = str(merged.get(key) or "").strip()
        if text and text != default_value:
            overrides[key] = text
    return overrides


def encode_prompt_template_overrides(value: Any) -> str:
    return json.dumps(
        normalize_prompt_template_overrides(value),
        ensure_ascii=False,
        sort_keys=True,
    )


def resolve_prompt_template(value: Any, key: str) -> str:
    return merge_prompt_templates(value).get(key, "")


class _SafeTemplateDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_prompt_template(value: Any, key: str, **context: Any) -> str:
    template = resolve_prompt_template(value, key)
    return template.format_map(_SafeTemplateDict(context)).strip()