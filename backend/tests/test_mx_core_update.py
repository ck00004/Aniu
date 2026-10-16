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


def test_limit_price_scaled_for_shanghai_stocks() -> None:
    """沪市（6 开头）2 位小数：1780.5 → 178050。"""
    payload = _trade_payload("600519", 1780.5)
    assert payload["price"] == 178050
    assert payload["useMarketPrice"] is False


def test_limit_price_scaled_for_shanghai_9_prefix() -> None:
    """9 开头（沪市科创板/CDR）同样 2 位小数。"""
    payload = _trade_payload("920001", 12.34)
    assert payload["price"] == 1234


def test_limit_price_scaled_for_shenzhen_stocks() -> None:
    """深市（0/3 开头）3 位小数：10.123 → 10123。"""
    payload = _trade_payload("000001", 10.123)
    assert payload["price"] == 10123

    payload_3 = _trade_payload("300059", 15.678)
    assert payload_3["price"] == 15678


def test_market_order_omits_price() -> None:
    payload = _trade_payload("600519", 1780.5, price_type="MARKET")
    assert "price" not in payload
    assert payload["useMarketPrice"] is True


def test_limit_price_rounding() -> None:
    """超出小数位的输入四舍五入到放大整数。"""
    payload = _trade_payload("600519", 1780.555)
    # 2 位小数放大：1780.555 * 100 = 178055.5 → round → 178056
    assert payload["price"] == 178056


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
