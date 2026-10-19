"""mx_core 官方规范更新（A620260623NIYC2U）的回归测试。

覆盖：限价单价格放大、status 字段业务错误检测、code=114 错误提示、
mx_query_market 结果格式化（官方 dataTableDTOList 表格结构）。
"""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from skills.mx_core.client import MXClient
from skills.mx_core.execution import (
    MXExecutionService,
    _format_query_market_result,
    preflight_cancel,
    preflight_trade,
)


# ---------------------------------------------------------------------------
# 限价单价格放大（官方 mx-moni 规范：沪市 2 位小数、深市 3 位小数）
# ---------------------------------------------------------------------------


class _StubClient:
    """MXClient 桩：只记录 _post 调用（trade 仅依赖 self._post）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _post(self, endpoint: str, payload: dict) -> dict:
        self.calls.append((endpoint, dict(payload)))
        return {"code": "200", "message": "成功", "data": {}}


def _trade_payload(symbol: str, price: float, price_type: str = "LIMIT") -> dict:
    stub = _StubClient()
    MXClient.trade(
        stub,
        action="BUY",
        symbol=symbol,
        quantity=100,
        price_type=price_type,
        price=price,
    )
    assert len(stub.calls) == 1
    return stub.calls[0][1]


def test_limit_price_sent_as_raw_decimal() -> None:
    """接口接收原始小数价格（生产 2026-10-19 验证：放大为整数被拒
    status=100 "输入价格不合理，超过限价"；上周五原始价格全部成交）。"""
    payload = _trade_payload("600519", 1780.5)
    assert payload["price"] == 1780.5
    assert payload["useMarketPrice"] is False


def test_limit_price_decimal_places_normalized() -> None:
    """按市场规则规范化小数位：沪市（6/9 开头）2 位、深市（0/3 开头）3 位。"""
    # 沪市 6 开头：3 位小数 → 规范化为 2 位
    assert _trade_payload("600519", 1780.555)["price"] == 1780.56
    # 沪市 9 开头：同样 2 位
    assert _trade_payload("920001", 12.346)["price"] == 12.35
    # 深市 0 开头：4 位小数 → 规范化为 3 位
    assert _trade_payload("000001", 10.1234)["price"] == 10.123
    # 深市 3 开头：同样 3 位
    assert _trade_payload("300059", 15.6789)["price"] == 15.679
    # 合规小数位原样透传
    assert _trade_payload("600519", 277.0)["price"] == 277.0


def test_market_order_omits_price() -> None:
    payload = _trade_payload("600519", 1780.5, price_type="MARKET")
    assert "price" not in payload
    assert payload["useMarketPrice"] is True


# ---------------------------------------------------------------------------
# status 字段业务错误检测（官方 mx-data 规范）
# ---------------------------------------------------------------------------


def test_status_zero_does_not_raise() -> None:
    assert (
        MXClient._raise_on_business_error(
            {"status": 0, "data": {"anything": 1}}
        )
        is None
    )


def test_nonzero_status_raises() -> None:
    with pytest.raises(RuntimeError, match="status=500"):
        MXClient._raise_on_business_error(
            {"status": 500, "message": "内部错误"}
        )


def test_string_status_raises() -> None:
    with pytest.raises(RuntimeError, match="status=1"):
        MXClient._raise_on_business_error({"status": "1", "message": "失败"})


def test_missing_status_still_uses_code_path() -> None:
    """无 status 字段时走 code 检测（模拟交易类接口）。"""
    with pytest.raises(RuntimeError, match="code=501"):
        MXClient._raise_on_business_error(
            {"code": "501", "message": "买入委托失败: 当前时间不可交易"}
        )


def test_code_114_hint_matches() -> None:
    """code=114（API密钥不存在/失效）的引导提示可达。"""
    service = MXExecutionService()
    try:
        MXClient._raise_on_business_error(
            {"code": "114", "message": "API密钥不存在"}
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        guidance = service._build_error_guidance(str(exc))
        assert "重新获取" in guidance


# ---------------------------------------------------------------------------
# mx_query_market 结果格式化（官方 dataTableDTOList 结构）
# ---------------------------------------------------------------------------


def _official_style_response(rows: int = 3) -> dict:
    return {
        "status": 0,
        "message": "成功",
        "data": {
            "data": {
                "searchDataResultDTO": {
                    "questionId": "Q123",
                    "entityTagDTOList": [
                        {
                            "fullName": "贵州茅台",
                            "secuCode": "600519",
                            "entityTypeName": "A股",
                        }
                    ],
                    "dataTableDTOList": [
                        {
                            "title": "贵州茅台最新价",
                            "entityName": "贵州茅台 (600519.SH)",
                            "condition": "时间: 2026-10-15",
                            "table": {
                                "headName": ["2026-10-14", "2026-10-15"],
                                "f2": ["1700.00", "1710.50"],
                            },
                            "nameMap": {"f2": "最新价"},
                            "indicatorOrder": ["f2"],
                        }
                    ],
                }
            }
        },
    }


def test_format_query_market_result_tables() -> None:
    formatted = _format_query_market_result(_official_style_response())
    assert formatted["ok"] is True
    assert formatted["question_id"] == "Q123"
    assert formatted["entities"][0]["name"] == "贵州茅台"
    assert formatted["total_rows"] == 2

    table = formatted["tables"][0]
    assert table["columns"] == ["日期", "最新价"]
    assert table["row_count"] == 2
    assert "| 2026-10-15 | 1710.50 |" in table["markdown"]
    assert "**贵州茅台最新价**" in table["markdown"]
    assert formatted["conditions"] == ["[贵州茅台 (600519.SH)] 时间: 2026-10-15"]


def test_format_query_market_result_truncates_long_tables() -> None:
    response = _official_style_response()
    dto = response["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"][0]
    dto["table"]["headName"] = [f"2026-10-{i:02d}" for i in range(1, 31)]
    dto["table"]["f2"] = [str(1700 + i) for i in range(30)]

    formatted = _format_query_market_result(response)
    table = formatted["tables"][0]
    assert table["row_count"] == 30
    assert table["truncated"] is True
    assert "其余 10 行已省略" in table["markdown"]


def test_format_query_market_result_missing_table() -> None:
    formatted = _format_query_market_result(
        {"status": 0, "data": {"data": {"searchDataResultDTO": {}}}}
    )
    assert formatted["ok"] is False
    assert "无 dataTableDTOList" in formatted["error"]


def test_format_query_market_result_invalid_payload() -> None:
    assert _format_query_market_result("not a dict")["ok"] is False
    assert _format_query_market_result(None)["ok"] is False


def test_handle_query_market_returns_formatted_result() -> None:
    """mx_query_market 工具返回格式化结果而非原始 JSON。"""

    class _FakeClient:
        def query_market(self, query: str) -> dict:
            return _official_style_response()

    service = MXExecutionService()
    result = service.execute_tool(
        client=_FakeClient(),
        app_settings=SimpleNamespace(task_prompt=""),
        tool_name="mx_query_market",
        arguments={"query": "贵州茅台最新价"},
    )
    assert result["ok"] is True
    formatted = result["result"]
    assert formatted["table_count"] == 1
    assert formatted["entities"][0]["code"] == "600519"
    # 原始 JSON 的深层结构不应再直接暴露给模型
    assert "searchDataResultDTO" not in str(formatted)


def test_handle_query_market_empty_result_returns_error() -> None:
    class _EmptyClient:
        def query_market(self, query: str) -> dict:
            return {"status": 0, "data": {}}

    service = MXExecutionService()
    result = service.execute_tool(
        client=_EmptyClient(),
        app_settings=SimpleNamespace(task_prompt=""),
        tool_name="mx_query_market",
        arguments={"query": "不存在的数据"},
    )
    assert result["ok"] is False
    assert "无 dataTableDTOList" in result["error"]


# ---------------------------------------------------------------------------
# 生产结构回归（2026-10-19 生产排查确认的真实载荷结构）
# ---------------------------------------------------------------------------


def _production_positions_payload() -> dict:
    """生产 mx_get_positions 真实结构（data.posList，2026-10-19 确认）。"""
    return {
        "code": "200",
        "message": "成功",
        "data": {
            "totalAssets": 904030.323,
            "availBalance": 757903.323,
            "posCount": 4,
            "posList": [
                {
                    "secCode": "300059", "secMkt": 0, "count": 800, "availCount": 800,
                    "costPriceDec": 3, "costPrice": 18298, "priceDec": 2, "price": 1807,
                    "secName": "东方财富",
                },
                {
                    "secCode": "603986", "secMkt": 1, "count": 200, "availCount": 200,
                    "costPriceDec": 3, "costPrice": 286589, "priceDec": 2, "price": 27500,
                    "secName": "兆易创新",
                },
                {
                    "secCode": "600938", "secMkt": 1, "count": 1000, "availCount": 1000,
                    "costPriceDec": 3, "costPrice": 34993, "priceDec": 2, "price": 3528,
                    "secName": "中国海油",
                },
            ],
            "currencyUnit": 1,
        },
        "traceId": None,
    }


def _production_orders_payload() -> dict:
    """生产 mx_get_orders 真实结构（data.orders，2026-10-19 确认）。"""
    return {
        "code": "200",
        "message": "成功",
        "data": {
            "totalNum": 3,
            "orders": [
                {
                    "id": "262884800000052612", "type": 5, "status": 9, "dbStatus": 206,
                    "time": 1792132612, "secCode": "603986", "secMkt": 1, "drt": 2,
                    "priceDec": 0, "price": 279, "count": 200, "tradeCount": 0,
                    "tradePrice": 0, "secName": "兆易创新",
                },
                {
                    "id": "262884700000047253", "type": 5, "status": 8, "dbStatus": 100,
                    "time": 1792127253, "secCode": "688825", "secMkt": 1, "drt": 1,
                    "priceDec": 1, "price": 445, "count": 400, "tradeCount": 0,
                    "tradePrice": 0, "secName": "长鑫科技",
                },
                {
                    "id": "262884800000049805", "type": 5, "status": 4, "dbStatus": 200,
                    "time": 1792129805, "secCode": "600276", "secMkt": 1, "drt": 2,
                    "priceDec": 2, "price": 4635, "count": 300, "tradeCount": 300,
                    "tradePrice": 4636, "secName": "恒瑞医药",
                },
            ],
        },
        "traceId": None,
    }


def test_sell_preflight_with_production_positions_payload() -> None:
    """生产结构（posList）下卖出预检必须找到持仓并放行。

    2026-10-19 生产事故：_payload_rows 漏了 posList 键，导致全部卖出
    委托被误判"未找到该标的持仓"，当日零成交。
    """
    client = SimpleNamespace(
        get_positions=lambda: _production_positions_payload(),
        get_orders=lambda: _production_orders_payload(),
    )
    # 兆易创新 200 股可卖，卖 100 → 放行
    preflight_trade(
        client=client,
        action="SELL",
        symbol="603986",
        quantity=100,
        price_type="LIMIT",
        price=274.5,
    )
    # 中国海油 1000 股可卖，卖 500 → 放行
    preflight_trade(
        client=client,
        action="SELL",
        symbol="600938",
        quantity=500,
        price_type="LIMIT",
        price=36.13,
    )
    # 超可卖 → 拒绝
    with pytest.raises(RuntimeError, match="可卖数量 200"):
        preflight_trade(
            client=client,
            action="SELL",
            symbol="603986",
            quantity=300,
            price_type="MARKET",
            price=None,
        )


def test_sell_preflight_uses_avail_count_from_production_payload() -> None:
    """availCount 存在时直接用 availCount 判可卖（生产字段）。"""
    client = SimpleNamespace(
        get_positions=lambda: {
            "code": "200",
            "data": {
                "posList": [
                    {"secCode": "600276", "count": 900, "availCount": 400},
                ]
            },
        },
        get_orders=lambda: _production_orders_payload(),
    )
    with pytest.raises(RuntimeError, match="可卖数量 400"):
        preflight_trade(
            client=client,
            action="SELL",
            symbol="600276",
            quantity=500,
            price_type="MARKET",
            price=None,
        )


def test_cancel_preflight_with_production_orders_payload() -> None:
    """生产结构（data.orders + drt/status 字段）下撤单预检。"""
    client = SimpleNamespace(
        get_positions=lambda: _production_positions_payload(),
        get_orders=lambda: _production_orders_payload(),
    )
    # 未成交委托（status=8）→ 可撤
    preflight_cancel(
        client=client,
        order_id="262884700000047253",
        stock_code="688825",
    )
    # 已撤单（status=9/dbStatus=206）→ 不可撤
    with pytest.raises(RuntimeError, match="不可撤销"):
        preflight_cancel(
            client=client,
            order_id="262884800000052612",
            stock_code="603986",
        )
    # 已成交（status=4，tradeCount=count）→ 不可撤
    with pytest.raises(RuntimeError, match="不可撤销"):
        preflight_cancel(
            client=client,
            order_id="262884800000049805",
            stock_code="600276",
        )
