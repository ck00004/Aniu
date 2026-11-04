"""自选股清单读取 / NOOP 判定的数据一致性回归测试。

背景（2026-11-04 线上排查）：`mx_get_self_selects` 旧实现把 70KB+ 原始载荷
丢给模型，其中人类可读的 `data.partialResults` 只有前 10 行（实际 89 只）。
写操作挡板用 `dataList` 判定"已在列表"（NOOP_VERIFIED），模型却只看到 10 行、
回答"未见该标的"——同一份数据出现两种相反结论，用户看到的就是这个矛盾。

本文件锁两件事：
1. 工具返回给模型的是**完整且紧凑**的清单（全部 89 行，几 KB）；
2. NOOP 判定带"命中证据行"，让模型/日志能说明为什么判定已达目标状态。
"""

from pathlib import Path
import json
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

import pytest

from skills.mx_core import execution as mx


@pytest.fixture(autouse=True)
def _temp_db(monkeypatch, tmp_path):
    """manage_self_select 会读写 self_select_intents，需要真实 SQLite。"""
    from app.core.config import get_settings
    from app.db import database as database_module
    from app.db.database import init_db

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "ss.db"))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    init_db()
    yield
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def _production_payload(total: int = 89, include: tuple[str, str] = ("002281", "光迅科技")) -> dict:
    """还原生产真实结构：data.allResults.result.dataList（含 IN_OPTIONAL 等列）。"""
    names = ["阳光电源", "三一重工", "巨化股份", "芒果超媒", "宁德时代"]
    rows = []
    for index in range(total - 1):
        rows.append(
            {
                "SECURITY_CODE": f"60{index:04d}"[-6:].zfill(6),
                "SECURITY_SHORT_NAME": names[index] if index < len(names) else f"标的{index}",
                "MARKET_SHORT_NAME": "SH",
                "IN_OPTIONAL": True,
                "NEWEST_PRICE": 10.0 + index,
            }
        )
    rows.append(
        {
            "SECURITY_CODE": include[0],
            "SECURITY_SHORT_NAME": include[1],
            "MARKET_SHORT_NAME": "SZ",
            "IN_OPTIONAL": True,
            "NEWEST_PRICE": 60.0,
        }
    )
    return {
        "status": 0,
        "code": 0,
        "data": {
            "protocolType": "SELECT_STOCK",
            "title": "我的自选",
            "securityCount": total,
            "partialResults": "|序号|代码|名称|\n" + "\n".join(
                f"|{i + 1}|{r['SECURITY_CODE']}|{r['SECURITY_SHORT_NAME']}|"
                for i, r in enumerate(rows[:10])  # 生产里只给前 10 行
            ),
            "allResults": {"result": {"dataList": rows, "total": total}},
        },
    }


class _StubClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.write_calls: list[str] = []

    def get_self_selects(self) -> dict:
        return self._payload

    def manage_self_select(self, query: str) -> dict:
        self.write_calls.append(query)
        return {"status": 0, "code": 0, "data": "ok"}


def test_get_self_selects_returns_complete_compact_list() -> None:
    payload = _production_payload()
    result = mx.mx_execution_service.execute_tool(
        client=_StubClient(payload),
        app_settings=None,
        tool_name="mx_get_self_selects",
        arguments={},
    )

    assert result["ok"] is True
    data = result["result"]
    assert data["total"] == 89
    assert len(data["items"]) == 89, "必须返回完整清单，而不是前 10 行"
    assert "002281" in data["codes"]
    assert any(item["name"] == "光迅科技" for item in data["items"])
    # 不再把原始 70KB 载荷塞给模型
    assert "allResults" not in data
    assert "partialResults" not in json.dumps(data, ensure_ascii=False)
    assert len(json.dumps(data, ensure_ascii=False)) < 12000


def test_noop_decision_reports_matched_evidence_row() -> None:
    """已在列表时，NOOP 必须给出命中行，而不是只有一句"已在自选"。"""
    payload = _production_payload()
    client = _StubClient(payload)

    result = mx.mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "把光迅科技加入自选股"},
    )

    assert result["ok"] is True
    assert result["result"]["noop"] is True
    assert result["result"]["matched"] == {
        "code": "002281",
        "name": "光迅科技",
        "market": "SZ",
    }
    assert result["result"]["list_total"] == 89
    assert "002281" in result["summary"]
    # 已经在列表里 → 不得发送写请求
    assert client.write_calls == []


def test_noop_uses_exact_name_match_only() -> None:
    """名称子串不得误判（"光迅" 不能命中 "光迅科技"）。"""
    payload = _production_payload(include=("002281", "光迅科技"))
    client = _StubClient(payload)

    result = mx.mx_execution_service.execute_tool(
        client=client,
        app_settings=None,
        tool_name="mx_manage_self_select",
        arguments={"query": "把光迅加入自选股"},
    )

    # 子串未命中 → 必须走真实写请求（不是 NOOP）
    assert (result.get("result") or {}).get("noop") is not True
    assert client.write_calls == ["把光迅加入自选股"]


def test_unparseable_payload_falls_back_to_raw() -> None:
    """结构不可识别时保持旧行为（返回原始载荷），不因为改动丢信息。"""
    payload = {"status": 0, "data": {"unexpected": "shape"}}
    result = mx.mx_execution_service.execute_tool(
        client=_StubClient(payload),
        app_settings=None,
        tool_name="mx_get_self_selects",
        arguments={},
    )

    assert result["ok"] is True
    assert result["result"] == payload
    assert "结构不可解析" in result["summary"]
