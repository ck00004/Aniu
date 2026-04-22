from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core import rate_limit as rate_limit_module
from app.db import database as database_module
from app.db.database import session_scope
from app.db.models import StrategyRun
from app.main import create_app
from app.services.scheduler_service import scheduler_service
from app.services.trading_calendar_service import trading_calendar_service


def create_test_client(monkeypatch, tmp_path) -> TestClient:
    from app.services.aniu_service import aniu_service

    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "preview.db"))
    monkeypatch.setattr(
        trading_calendar_service,
        "warm_up_months",
        lambda current: None,
    )
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    rate_limit_module._limiter.reset()
    aniu_service._account_overview_cache = None
    aniu_service._account_overview_cache_expires_at = None
    app = create_app()
    return TestClient(app)


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/aniu/login",
        json={"password": "release-pass"},
    )
    payload = response.json()
    return {"Authorization": f"Bearer {payload['token']}"}


def test_get_run_raw_tool_preview_returns_full_text(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        with session_scope() as db:
            run = StrategyRun(
                trigger_source="schedule",
                run_type="analysis",
                status="completed",
                skill_payloads={
                    "tool_calls": [
                        {
                            "name": "mx_search_news",
                            "result": {
                                "ok": True,
                                "summary": "返回大量资讯正文",
                                "result": {"text": "A" * 6500},
                            },
                        }
                    ]
                },
            )
            db.add(run)
            db.flush()
            run_id = run.id

        detail_response = client.get(f"/api/aniu/runs/{run_id}", headers=headers)
        assert detail_response.status_code == 200
        detail_payload = detail_response.json()
        assert detail_payload["raw_tool_previews"][0]["truncated"] is True
        assert detail_payload["raw_tool_previews"][0]["preview"].endswith("<已截断>")

        response = client.get(
            f"/api/aniu/runs/{run_id}/raw-tool-previews/0",
            headers=headers,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["preview_index"] == 0
    assert payload["truncated"] is False
    assert len(payload["full_preview"]) > 6000
    assert "<已截断>" not in payload["full_preview"]
    assert payload["preview"] == payload["full_preview"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_get_run_raw_tool_preview_returns_404_for_missing_index(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        with session_scope() as db:
            run = StrategyRun(
                trigger_source="schedule",
                run_type="analysis",
                status="completed",
                skill_payloads={
                    "tool_calls": [
                        {
                            "name": "mx_get_balance",
                            "result": {
                                "ok": True,
                                "summary": "读取资产",
                                "result": {"asset": 1},
                            },
                        }
                    ]
                },
            )
            db.add(run)
            db.flush()
            run_id = run.id

        response = client.get(
            f"/api/aniu/runs/{run_id}/raw-tool-previews/3",
            headers=headers,
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "原始工具预览不存在。"

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()
