from __future__ import annotations

from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import (
    get_persistent_jwt_secret_file,
    get_runtime_data_dir,
    get_settings,
    get_skill_workspace_skills_dir,
)


def _reset_settings_cache() -> None:
    get_settings.cache_clear()


def test_local_backend_runtime_uses_repo_level_runtime_data(monkeypatch, tmp_path) -> None:
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    monkeypatch.chdir(backend_dir)
    monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)

    _reset_settings_cache()
    settings = get_settings()

    assert get_runtime_data_dir(settings) == tmp_path / "data"
    assert get_skill_workspace_skills_dir(settings) == (
        tmp_path / "data" / "skill_workspace" / "skills"
    )

    secret_file = get_persistent_jwt_secret_file(settings)
    assert secret_file == tmp_path / "data" / "jwt_secret.txt"
    first_secret = settings.jwt_secret
    assert first_secret
    assert secret_file.read_text(encoding="utf-8") == first_secret

    _reset_settings_cache()
    assert get_settings().jwt_secret == first_secret
    _reset_settings_cache()


def test_local_backend_runtime_copies_legacy_skill_workspace(monkeypatch, tmp_path) -> None:
    backend_dir = tmp_path / "backend"
    legacy_skill_dir = backend_dir / "data" / "skill_workspace" / "skills" / "legacy-skill"
    legacy_skill_dir.mkdir(parents=True)
    (legacy_skill_dir / "SKILL.md").write_text("# Legacy Skill\n", encoding="utf-8")

    monkeypatch.chdir(backend_dir)
    monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
    monkeypatch.setenv("JWT_SECRET", "stable-secret")

    _reset_settings_cache()
    get_settings()

    migrated_skill = tmp_path / "data" / "skill_workspace" / "skills" / "legacy-skill" / "SKILL.md"
    assert migrated_skill.exists()
    assert migrated_skill.read_text(encoding="utf-8") == "# Legacy Skill\n"
    _reset_settings_cache()
