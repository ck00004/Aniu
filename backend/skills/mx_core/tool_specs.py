from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_QUERY_TEMPLATES: dict[str, list[str]] = {
    "mx_query_market": [
        "上证指数今日走势和成交额",
        "半导体板块今日涨跌和主力资金流向",
        "贵州茅台近三年净利润和营业收入",
    ],
    "mx_search_news": [
        "今日A股市场热点新闻",
        "人工智能板块近期新闻",
        "美联储加息对A股影响分析",
    ],
    "mx_screen_stocks": [
        "今日涨幅大于2%的A股",
        "净利润增长率大于30%的股票",
        "新能源板块市盈率小于30的股票",
    ],
    "mx_manage_self_select": [
        "把贵州茅台加入自选股",
        "把东方财富从自选中删除",
    ],
}

COMMON_TOOL_NAMES: set[str] = {
    "mx_query_market",
    "mx_search_news",
    "mx_screen_stocks",
    "mx_get_positions",
    "mx_get_balance",
    "mx_get_orders",
    "mx_get_self_selects",
    "mx_manage_self_select",
}

TRADE_TOOL_NAMES: set[str] = {
    "mx_moni_trade",
    "mx_moni_cancel",
}

TOOL_PROFILES: dict[str, set[str]] = {
    "analysis": set(COMMON_TOOL_NAMES),
    "trade": {*COMMON_TOOL_NAMES, *TRADE_TOOL_NAMES},
    "chat": {*COMMON_TOOL_NAMES, *TRADE_TOOL_NAMES},
}


def empty_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def query_parameters(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": description,
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class MXToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    category: str
    mutation: bool = False

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


TOOL_SPECS: list[MXToolSpec] = [
    MXToolSpec(
        name="mx_query_market",
        description=(
            "基于东方财富权威数据库及最新行情底层数据查询结构化金融数据，"
            "适合需要权威、及时金融数据的任务，避免模型基于过时知识作答。"
            "支持三类能力：\n"
            "1. 行情类：股票、行业、板块、指数、基金、债券的实时行情、主力资金流向、估值等；\n"
            "2. 财务类：上市公司与非上市公司的基本信息、财务指标、高管信息、主营业务、股东结构、融资情况等；\n"
            "3. 关系与经营类：股票、非上市公司、股东及高管之间的关联关系，以及企业经营相关数据。\n"
            "query 示例：'东方财富最新价'、'贵州茅台近三年净利润 营业收入'、"
            "'宁德时代主力资金流向'、'沪深300指数最新点位 涨跌幅'、'比亚迪十大股东'。\n"
            "注意：避免查询过大时间范围的高频或长周期日频数据，例如某只股票多年每日价格，"
            "否则返回内容会过大。"
        ),
        parameters=query_parameters("查询语句，例如上证指数今天走势和市场概况。"),
        category="data",
    ),
    MXToolSpec(
        name="mx_search_news",
        description=(
            "基于东方财富妙想搜索能力和金融场景信源智能筛选，查询时效性金融资讯。"
            "适用于新闻、公告、研报、政策、交易规则、具体事件、影响分析以及需要检索外部数据的非常识信息，"
            "可避免引用非权威或过时信息。"
            "query 示例：'贵州茅台最新研报'、'人工智能板块近期新闻'、"
            "'美联储加息对A股影响分析'、'科创板交易涨跌幅限制'、'今日大盘异动原因分析'。"
        ),
        parameters=query_parameters("资讯查询语句。"),
        category="search",
    ),
    MXToolSpec(
        name="mx_screen_stocks",
        description=(
            "基于东方财富官方选股接口，按自然语言解析选股条件并筛选股票。"
            "支持行情指标、财务指标、行业/板块范围、指数成分股范围，以及股票/上市公司/板块推荐等任务，"
            "避免大模型在选股时使用过时信息。"
            "query 示例：'今日涨幅大于2%的A股'、'净利润增长率大于30%的股票'、"
            "'新能源板块市盈率小于30的股票'、'沪深300成分股中分红率最高的10只股票'。"
        ),
        parameters=query_parameters("选股查询语句。"),
        category="xuangu",
    ),
    MXToolSpec(
        name="mx_get_positions",
        description=(
            "查询当前A股模拟组合持仓。返回持仓股票代码、名称、数量、可用数量、成本价、现价、市值、盈亏等信息。"
            "适合盘点当前持仓结构、单票盈亏、可卖数量和仓位分布。"
            "仅适用于已绑定的模拟组合账户，不涉及真实资金交易。"
        ),
        parameters=empty_parameters(),
        category="moni",
    ),
    MXToolSpec(
        name="mx_get_balance",
        description=(
            "查询当前A股模拟组合资金。返回可用资金、总资产、持仓市值等核心资金信息。"
            "适合判断可开仓资金、组合仓位和账户总规模。"
            "仅适用于已绑定的模拟组合账户，不涉及真实资金交易。"
        ),
        parameters=empty_parameters(),
        category="moni",
    ),
    MXToolSpec(
        name="mx_get_orders",
        description=(
            "查询当前A股模拟组合委托记录。返回委托方向、委托状态、委托价格、委托数量、成交数量、成交价格等。"
            "适合确认未成交/已成交/已撤单委托、查看历史委托，或在撤单前获取委托编号。"
            "仅适用于已绑定的模拟组合账户，不涉及真实资金交易。"
        ),
        parameters=empty_parameters(),
        category="moni",
    ),
    MXToolSpec(
        name="mx_get_self_selects",
        description=(
            "查询当前账户的自选股列表。"
            "适合获取长期关注或待跟踪标的清单，并结合行情、资讯、选股工具继续分析。"
        ),
        parameters=empty_parameters(),
        category="zixuan",
    ),
    MXToolSpec(
        name="mx_manage_self_select",
        description=(
            "通过自然语言添加或删除自选股。"
            "适合把待跟踪标的加入自选，或从自选中移除。"
            "单次调用只能处理一只股票；如果需要增删多只股票，必须分多次调用。"
            "query 示例：'把贵州茅台加入自选股'、'把东方财富从自选中删除'。"
        ),
        parameters=query_parameters(
            "自然语言管理指令。单次只能写一只股票，例如把贵州茅台添加到我的自选股列表。"
        ),
        category="zixuan",
        mutation=True,
    ),
    MXToolSpec(
        name="mx_moni_trade",
        description=(
            "执行A股模拟交易买入或卖出，仅用于模拟组合练习和策略验证，不涉及真实资金。"
            "支持市价和限价委托；股票代码必须是6位A股代码，数量必须是100的整数倍。"
            "单次调用只能交易一只股票；若需处理多只股票，必须分多次调用。"
            "适合模拟建仓、减仓、调仓。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["BUY", "SELL"],
                    "description": "交易方向。",
                },
                "symbol": {
                    "type": "string",
                    "description": "单只股票代码，例如 600519、300059；禁止一次传入多只股票。",
                },
                "name": {
                    "type": "string",
                    "description": "股票名称，可选；已知时一并传入，便于运行记录展示。",
                },
                "quantity": {
                    "type": "integer",
                    "description": "委托数量，必须为100的整数倍，例如100、200、300。",
                },
                "price_type": {
                    "type": "string",
                    "enum": ["MARKET", "LIMIT"],
                    "description": "委托方式：MARKET 为市价，LIMIT 为限价。",
                },
                "price": {
                    "type": ["number", "null"],
                    "description": "限价委托价格；市价时可为空。沪市价格通常不超过2位小数，深市通常不超过3位小数。",
                },
                "reason": {
                    "type": "string",
                    "description": "执行这笔交易的原因，会保存在运行记录中。",
                },
            },
            "required": ["action", "symbol", "quantity", "price_type"],
            "additionalProperties": False,
        },
        category="moni",
        mutation=True,
    ),
    MXToolSpec(
        name="mx_moni_cancel",
        description=(
            "撤销A股模拟交易委托，仅用于模拟组合，不涉及真实资金。"
            "单次调用只能按委托编号撤销一笔委托，不允许 all 批量撤单。"
            "撤单前，通常应先调用 mx_get_orders 确认 order_id 和当前状态。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "cancel_type": {
                    "type": "string",
                    "enum": ["order"],
                    "description": "固定为 order，表示按委托编号单笔撤单。",
                },
                "order_id": {
                    "type": ["string", "null"],
                    "description": "必填，应来自 mx_get_orders 返回的单笔委托编号。",
                },
                "stock_code": {
                    "type": ["string", "null"],
                    "description": "可选，按委托编号撤单时可补充股票代码。",
                },
                "reason": {
                    "type": "string",
                    "description": "撤单原因，用于记录。",
                },
            },
            "required": ["cancel_type", "order_id"],
            "additionalProperties": False,
        },
        category="moni",
        mutation=True,
    ),
]


def build_tools(run_type: str | None = None) -> list[dict[str, Any]]:
    normalized_run_type = str(run_type or "").strip()
    allowed = TOOL_PROFILES.get(normalized_run_type, None) if normalized_run_type else None
    tool_specs = TOOL_SPECS
    if allowed is not None:
        tool_specs = [spec for spec in TOOL_SPECS if spec.name in allowed]
    return [tool_spec.to_openai_tool() for tool_spec in tool_specs]
