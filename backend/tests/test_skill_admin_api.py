from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import zipfile

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core import rate_limit as rate_limit_module
from app.db import database as database_module
from app.main import create_app
from app.services.scheduler_service import scheduler_service
from app.services.skill_admin_service import skill_admin_service
from app.services.trading_calendar_service import trading_calendar_service
from app.skills import skill_registry


def create_test_client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
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
    app = create_app()
    return TestClient(app)


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/aniu/login", json={"password": "release-pass"})
    payload = response.json()
    return {"Authorization": f"Bearer {payload['token']}"}


def _build_skill_zip(skill_markdown: str, meta: dict[str, object] | None = None) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zf:
        zf.writestr("SKILL.md", skill_markdown)
        if meta is not None:
            import json

            zf.writestr("_meta.json", json.dumps(meta, ensure_ascii=False))
    return buffer.getvalue()


def test_skills_endpoint_lists_builtin_skills(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.get("/api/aniu/skills", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert any(item["id"] == "builtin_utils" for item in payload)
    assert any(item["id"] == "mx_core" for item in payload)
    builtin_utils = next(item for item in payload if item["id"] == "builtin_utils")
    assert builtin_utils["name"] == "builtin_utils"
    assert builtin_utils["source"] == "builtin"
    assert "location" not in builtin_utils
    assert "support_files" not in builtin_utils
    assert "tool_names" not in builtin_utils
    assert "compatibility_level" not in builtin_utils

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_disable_and_enable_skill_persist_state(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)

        disable_response = client.post(
            "/api/aniu/skills/chat_context/disable",
            headers=headers,
        )
        assert disable_response.status_code == 200
        assert disable_response.json()["enabled"] is False

        tool_names = {
            spec["function"]["name"] for spec in skill_registry.build_tools(run_type="chat")
        }
        assert "chat_get_account_summary" not in tool_names

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.get("/api/aniu/skills", headers=headers)
        assert response.status_code == 200
        chat_context = next(item for item in response.json() if item["id"] == "chat_context")
        assert chat_context["enabled"] is False

        enable_response = client.post(
            "/api/aniu/skills/chat_context/enable",
            headers=headers,
        )
        assert enable_response.status_code == 200
        assert enable_response.json()["enabled"] is True

        tool_names = {
            spec["function"]["name"] for spec in skill_registry.build_tools(run_type="chat")
        }
        assert "chat_get_account_summary" in tool_names

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_system_runtime_skill_cannot_be_disabled(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        disable_response = client.post(
            "/api/aniu/skills/builtin_utils/disable",
            headers=headers,
        )
        assert disable_response.status_code == 400
        assert "cannot be disabled" in disable_response.json()["detail"]

        tool_names = {
            spec["function"]["name"] for spec in skill_registry.build_tools(run_type="chat")
        }
        assert "read_file" in tool_names
        assert "exec" in tool_names

        response = client.get("/api/aniu/skills", headers=headers)
        assert response.status_code == 200
        builtin_utils = next(item for item in response.json() if item["id"] == "builtin_utils")
        assert builtin_utils["enabled"] is True

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_import_clawhub_skill_creates_workspace_skill_disabled_by_default(
    monkeypatch, tmp_path
) -> None:
    skill_markdown = """---
name: Fancy Skill Name
description: Imported from ClawHub
metadata: {"openclaw":{"requires":{"bins":["missing-cli"]},"install":[{"kind":"node","package":"clawhub"}]}}
---

# Fancy Skill

This skill should only appear in the prompt supplement after it is enabled.
"""
    archive_bytes = _build_skill_zip(
        skill_markdown,
        meta={
            "slug": "fancy-skill",
            "version": "1.2.3",
            "publishedAt": 1771933320437,
        },
    )
    monkeypatch.setattr(
        skill_admin_service,
        "_download_clawhub_archive",
        lambda slug_or_url: ("fancy-skill", archive_bytes),
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)

        response = client.post(
            "/api/aniu/skills/import-clawhub",
            headers=headers,
            json={"slug_or_url": "https://clawhub.ai/skills/fancy-skill"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == "fancy-skill"
        assert payload["name"] == "Fancy Skill Name"
        assert Path(payload["location"]).name == "fancy-skill"
        assert Path(payload["location"]).parent.name == "skills"
        assert payload["source"] == "workspace"
        assert payload["enabled"] is False
        assert payload["compatibility_level"] == "needs_attention"

        target_skill = (
            tmp_path / "skill_workspace" / "skills" / "fancy-skill" / "SKILL.md"
        )
        assert target_skill.exists()

        supplement = skill_registry.build_prompt_supplement(run_type="analysis")
        assert "Fancy Skill" not in supplement

        enable_response = client.post(
            "/api/aniu/skills/fancy-skill/enable",
            headers=headers,
        )
        assert enable_response.status_code == 200
        assert enable_response.json()["enabled"] is True

        supplement = skill_registry.build_prompt_supplement(run_type="analysis")
        assert "Fancy Skill Name" in supplement
        assert "SKILL.md" in supplement
        assert "没有 Python handler 的技能也属于已支持技能" not in supplement

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_always_skill_is_injected_as_full_body_when_enabled(monkeypatch, tmp_path) -> None:
    archive_bytes = _build_skill_zip(
        """---
name: Always Skill
description: Always-on workspace skill
always: true
---

# Always Skill

This body should be injected directly.
"""
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/api/aniu/skills/import-zip",
            headers=headers,
            files={
                "file": ("always-skill.zip", archive_bytes, "application/zip"),
            },
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is False

        supplement = skill_registry.build_prompt_supplement(run_type="analysis")
        assert "This body should be injected directly." not in supplement

        enable_response = client.post(
            "/api/aniu/skills/always-skill/enable",
            headers=headers,
        )
        assert enable_response.status_code == 200

        supplement = skill_registry.build_prompt_supplement(run_type="analysis")
        assert "常驻技能：Always Skill" in supplement
        assert "This body should be injected directly." in supplement

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_import_zip_skill_uses_filename_when_meta_slug_missing(monkeypatch, tmp_path) -> None:
    archive_bytes = _build_skill_zip(
        """---
name: Uploaded Skill
description: Imported from a local archive
---

# Uploaded Skill

This skill comes from a local zip file.
"""
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/api/aniu/skills/import-zip",
            headers=headers,
            files={
                "file": ("uploaded-skill.zip", archive_bytes, "application/zip"),
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == "uploaded-skill"
        assert payload["name"] == "Uploaded Skill"
        assert payload["source"] == "workspace"
        assert payload["enabled"] is False

        target_skill = (
            tmp_path / "skill_workspace" / "skills" / "uploaded-skill" / "SKILL.md"
        )
        assert target_skill.exists()

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_skill_order_stays_stable_after_disabling(monkeypatch, tmp_path) -> None:
    alpha_archive = _build_skill_zip(
        """---
name: Alpha Skill
description: First imported workspace skill
---

# Alpha Skill

Workspace skill alpha.
"""
    )
    beta_archive = _build_skill_zip(
        """---
name: Beta Skill
description: Second imported workspace skill
---

# Beta Skill

Workspace skill beta.
"""
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)

        alpha_import = client.post(
            "/api/aniu/skills/import-zip",
            headers=headers,
            files={
                "file": ("alpha-skill.zip", alpha_archive, "application/zip"),
            },
        )
        assert alpha_import.status_code == 200

        beta_import = client.post(
            "/api/aniu/skills/import-zip",
            headers=headers,
            files={
                "file": ("beta-skill.zip", beta_archive, "application/zip"),
            },
        )
        assert beta_import.status_code == 200

        alpha_enable = client.post(
            "/api/aniu/skills/alpha-skill/enable",
            headers=headers,
        )
        assert alpha_enable.status_code == 200

        beta_enable = client.post(
            "/api/aniu/skills/beta-skill/enable",
            headers=headers,
        )
        assert beta_enable.status_code == 200

        alpha_disable = client.post(
            "/api/aniu/skills/alpha-skill/disable",
            headers=headers,
        )
        assert alpha_disable.status_code == 200
        assert alpha_disable.json()["enabled"] is False

        skills_response = client.get("/api/aniu/skills", headers=headers)
        assert skills_response.status_code == 200

        workspace_ids = [
            item["id"] for item in skills_response.json() if item["source"] == "workspace"
        ]
        assert workspace_ids[:2] == ["alpha-skill", "beta-skill"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_delete_workspace_skill_removes_directory_and_allows_reimport(
    monkeypatch, tmp_path
) -> None:
    archive_bytes = _build_skill_zip(
        """---
name: Uploaded Skill
description: Imported from a local archive
---

# Uploaded Skill

This skill comes from a local zip file.
"""
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        import_response = client.post(
            "/api/aniu/skills/import-zip",
            headers=headers,
            files={
                "file": ("uploaded-skill.zip", archive_bytes, "application/zip"),
            },
        )
        assert import_response.status_code == 200

        target_skill = (
            tmp_path / "skill_workspace" / "skills" / "uploaded-skill" / "SKILL.md"
        )
        assert target_skill.exists()

        delete_response = client.delete(
            "/api/aniu/skills/uploaded-skill",
            headers=headers,
        )
        assert delete_response.status_code == 204
        assert not target_skill.exists()

        skills_response = client.get("/api/aniu/skills", headers=headers)
        assert skills_response.status_code == 200
        assert all(item["id"] != "uploaded-skill" for item in skills_response.json())

        reimport_response = client.post(
            "/api/aniu/skills/import-zip",
            headers=headers,
            files={
                "file": ("uploaded-skill.zip", archive_bytes, "application/zip"),
            },
        )
        assert reimport_response.status_code == 200
        assert reimport_response.json()["id"] == "uploaded-skill"

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_delete_builtin_skill_is_rejected(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.delete(
            "/api/aniu/skills/builtin_utils",
            headers=headers,
        )

    assert response.status_code == 400
    assert "Built-in skills cannot be deleted." in response.json()["detail"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_import_skillhub_skill_downloads_archive_and_installs(monkeypatch, tmp_path) -> None:
    archive_bytes = _build_skill_zip(
        """---
name: NewsNow V2
description: Downloaded from SkillHub
---

# NewsNow V2

SkillHub downloaded skill.
""",
        meta={
            "version": "1.1.0",
        },
    )
    monkeypatch.setattr(
        skill_admin_service,
        "_download_skillhub_archive",
        lambda slug_or_url: ("newsnow-v2", archive_bytes),
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/api/aniu/skills/import-skillhub",
            headers=headers,
            json={"slug_or_url": "https://skillhub.cn/skills/newsnow-v2"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == "newsnow-v2"
        assert payload["name"] == "NewsNow V2"
        assert Path(payload["location"]).name == "newsnow-v2"
        assert Path(payload["location"]).parent.name == "skills"
        assert payload["source"] == "workspace"
        assert payload["enabled"] is False
        assert payload["clawhub_slug"] == "newsnow-v2"
        assert payload["clawhub_version"] == "1.1.0"
        assert payload["clawhub_url"] == "https://skillhub.cn/skills/newsnow-v2"

        target_skill = (
            tmp_path / "skill_workspace" / "skills" / "newsnow-v2" / "SKILL.md"
        )
        assert target_skill.exists()

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_import_zip_rejects_archive_that_expands_past_safety_limit(
    monkeypatch, tmp_path
) -> None:
    original_limit = skill_admin_service.MAX_SKILL_ARCHIVE_EXTRACT_BYTES
    skill_admin_service.MAX_SKILL_ARCHIVE_EXTRACT_BYTES = 32
    archive_bytes = _build_skill_zip("# " + ("A" * 256))

    try:
        with create_test_client(monkeypatch, tmp_path) as client:
            headers = _auth_headers(client)
            response = client.post(
                "/api/aniu/skills/import-zip",
                headers=headers,
                files={
                    "file": ("oversized-skill.zip", archive_bytes, "application/zip"),
                },
            )
    finally:
        skill_admin_service.MAX_SKILL_ARCHIVE_EXTRACT_BYTES = original_limit

    assert response.status_code == 400
    assert "解压后体积过大" in response.json()["detail"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_import_zip_reports_builtin_skill_conflict_with_readable_message(
    monkeypatch, tmp_path
) -> None:
    archive_bytes = _build_skill_zip(
        """---
name: Builtin Clash
description: should fail
---

# Builtin Clash
""",
        meta={"slug": "builtin_utils"},
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/api/aniu/skills/import-zip",
            headers=headers,
            files={
                "file": ("builtin-utils.zip", archive_bytes, "application/zip"),
            },
        )

    assert response.status_code == 400
    assert "导入的技能标识与内置技能冲突" in response.json()["detail"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()
