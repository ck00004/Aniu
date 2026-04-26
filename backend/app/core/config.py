from __future__ import annotations

import secrets
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_SKILL_WORKSPACE_DIRNAME = "skill_workspace"
_JWT_SECRET_FILENAME = "jwt_secret.txt"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        enable_decoding=False,
        extra="ignore",
    )

    app_name: str = "Aniu"
    api_prefix: str = "/api/aniu"
    sqlite_db_path: Path = Field(
        default=Path("./data/aniu.sqlite3"), alias="SQLITE_DB_PATH"
    )

    mx_apikey: str | None = Field(default=None, alias="MX_APIKEY")
    mx_api_url: str = Field(
        default="https://mkapi2.dfcfs.com/finskillshub", alias="MX_API_URL"
    )

    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    jin10_api_base_url: str | None = Field(
        default="http://127.0.0.1:3000",
        alias="JIN10_API_BASE_URL",
    )
    cls_api_base_url: str | None = Field(
        default="http://127.0.0.1:3000",
        alias="CLS_API_BASE_URL",
    )
    jin10_api_timeout_seconds: int = Field(
        default=5,
        alias="JIN10_API_TIMEOUT_SECONDS",
    )
    jin10_news_limit: int = Field(
        default=30,
        alias="JIN10_NEWS_LIMIT",
    )
    cls_api_timeout_seconds: int = Field(
        default=5,
        alias="CLS_API_TIMEOUT_SECONDS",
    )
    cls_news_limit: int = Field(
        default=200,
        alias="CLS_NEWS_LIMIT",
    )

    account_overview_cache_ttl_seconds: int = Field(
        default=30, alias="ACCOUNT_OVERVIEW_CACHE_TTL_SECONDS"
    )

    scheduler_poll_seconds: int = Field(default=15, alias="SCHEDULER_POLL_SECONDS")
    app_login_password: str | None = Field(default=None, alias="APP_LOGIN_PASSWORD")
    jwt_secret: str | None = Field(default=None, alias="JWT_SECRET")
    jwt_expire_hours: int = Field(default=24, alias="JWT_EXPIRE_HOURS")
    trust_x_forwarded_for: bool = Field(
        default=False,
        alias="TRUST_X_FORWARDED_FOR",
    )
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["*"], alias="CORS_ALLOW_ORIGINS"
    )

    @field_validator(
        "mx_apikey",
        "openai_base_url",
        "openai_api_key",
        "jin10_api_base_url",
        "cls_api_base_url",
        "app_login_password",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, value: object) -> str | None:
        """Normalize empty / whitespace-only env vars to None."""
        if isinstance(value, str) and not value.strip():
            return None
        return value  # type: ignore[return-value]

    @field_validator("jwt_secret", mode="before")
    @classmethod
    def normalize_jwt_secret(cls, value: object) -> str | None:
        if not value or (isinstance(value, str) and not value.strip()):
            return None
        return str(value).strip()

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return ["*"]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.sqlite_db_path.is_absolute():
        settings.sqlite_db_path = Path.cwd() / settings.sqlite_db_path
    settings.sqlite_db_path = settings.sqlite_db_path.resolve()

    configured_db_path = settings.sqlite_db_path
    default_db_path = Path.cwd() / "data" / "aniu.sqlite3"
    legacy_db_path = Path.cwd() / "data" / "aniu.db"
    using_default_relative_path = configured_db_path == default_db_path
    # Backward-compatible fallback for older deployments that persisted the
    # SQLite file as ./data/aniu.db before the default name was unified.
    if using_default_relative_path and not configured_db_path.exists() and legacy_db_path.exists():
        settings.sqlite_db_path = legacy_db_path.resolve()

    _merge_legacy_skill_workspace(settings)
    if not settings.jwt_secret:
        settings.jwt_secret = _load_or_create_jwt_secret(
            get_persistent_jwt_secret_file(settings)
        )
    return settings


def get_runtime_data_dir(settings: Settings | None = None) -> Path:
    current = settings or get_settings()
    cwd = Path.cwd().resolve()
    if cwd.name == "backend" and cwd in current.sqlite_db_path.parents:
        return (cwd.parent / "data").resolve()
    return current.sqlite_db_path.parent.resolve()


def get_skill_workspace_root(settings: Settings | None = None) -> Path:
    return get_runtime_data_dir(settings) / _SKILL_WORKSPACE_DIRNAME


def get_skill_workspace_skills_dir(settings: Settings | None = None) -> Path:
    return get_skill_workspace_root(settings) / "skills"


def get_persistent_jwt_secret_file(settings: Settings | None = None) -> Path:
    return get_runtime_data_dir(settings) / _JWT_SECRET_FILENAME


def _legacy_skill_workspace_root(settings: Settings) -> Path:
    return settings.sqlite_db_path.parent / _SKILL_WORKSPACE_DIRNAME


def _merge_legacy_skill_workspace(settings: Settings) -> None:
    legacy_root = _legacy_skill_workspace_root(settings)
    target_root = get_skill_workspace_root(settings)
    if not legacy_root.exists():
        return
    if legacy_root.resolve() == target_root.resolve():
        return

    target_root.mkdir(parents=True, exist_ok=True)
    for child in legacy_root.iterdir():
        destination = target_root / child.name
        if destination.exists():
            continue
        if child.is_dir():
            shutil.copytree(child, destination)
        else:
            shutil.copy2(child, destination)


def _load_or_create_jwt_secret(secret_file: Path) -> str:
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    if secret_file.is_file():
        existing = secret_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    secret = secrets.token_urlsafe(32)
    secret_file.write_text(secret, encoding="utf-8")
    return secret
