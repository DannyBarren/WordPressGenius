"""Application configuration helpers for local and production deployments."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv


AppEnvironment = Literal["dev", "staging", "prod"]


def _load_dotenv_files() -> None:
    """Load base and environment-specific dotenv files without overriding env vars."""

    load_dotenv()
    app_env = os.getenv("APP_ENV", "dev").strip().lower()
    env_file = Path(f".env.{app_env}")
    if env_file.exists():
        load_dotenv(env_file, override=False)


_load_dotenv_files()


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _as_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _as_optional_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _secret_or_env(name: str, default: str = "") -> str:
    """Read a setting from ENV, ENV_FILE, or Docker secrets.

    Docker secrets can be mounted as `/run/secrets/<lowercase_name>` or pointed
    to explicitly with `<NAME>_FILE`.
    """

    file_value = os.getenv(f"{name}_FILE")
    secret_paths = []
    if file_value:
        secret_paths.append(Path(file_value))
    secret_paths.append(Path("/run/secrets") / name.lower())
    secret_paths.append(Path("/run/secrets") / name)
    for path in secret_paths:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return os.getenv(name, default)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _path_env(name: str, default: str) -> Path:
    return Path(_env(name, default))


@dataclass(frozen=True)
class AppConfig:
    """Runtime settings loaded from environment variables and optional secrets."""

    app_env: AppEnvironment = field(
        default_factory=lambda: _env("APP_ENV", "dev").strip().lower()  # type: ignore[assignment]
    )
    app_name: str = field(default_factory=lambda: _env("APP_NAME", "WordPressGenius"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_file: Path = field(default_factory=lambda: _path_env("LOG_FILE", "logs/wordpressgenius.log"))
    json_logs: bool = field(
        default_factory=lambda: _as_bool(
            _env("JSON_LOGS", "true" if _env("APP_ENV", "dev") == "prod" else "false"),
            False,
        )
    )
    enable_file_logging: bool = field(
        default_factory=lambda: _as_bool(_env("ENABLE_FILE_LOGGING"), True)
    )
    wordpress_site_url: str = field(default_factory=lambda: _env("WORDPRESS_SITE_URL", ""))
    wordpress_username: str = field(default_factory=lambda: _env("WORDPRESS_USERNAME", ""))
    wordpress_application_password: str = field(
        default_factory=lambda: _secret_or_env("WORDPRESS_APPLICATION_PASSWORD", "")
    )
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "openai"))
    openai_api_key: str = field(default_factory=lambda: _secret_or_env("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini"))
    anthropic_api_key: str = field(default_factory=lambda: _secret_or_env("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(
        default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
    )
    groq_api_key: str = field(default_factory=lambda: _secret_or_env("GROQ_API_KEY", ""))
    groq_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "llama-3.3-70b-versatile"))
    gemini_api_key: str = field(default_factory=lambda: _secret_or_env("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-1.5-flash"))
    llm_settings_path: Path = field(
        default_factory=lambda: _path_env("LLM_SETTINGS_PATH", "data/llm_settings.enc")
    )
    llm_agentic_enabled: bool = field(
        default_factory=lambda: _as_bool(_env("LLM_AGENTIC_ENABLED"), True)
    )
    # Cost-aware model routing: a cheaper "fast" model powers planning, research,
    # execution checks, and reviewing; a stronger "premium" model (or the
    # UI-selected model) powers content creation. Blank = per-provider defaults.
    llm_fast_model: str = field(default_factory=lambda: _env("LLM_FAST_MODEL", ""))
    llm_premium_model: str = field(default_factory=lambda: _env("LLM_PREMIUM_MODEL", ""))
    # Optional temperature overrides by agent type (blank = sensible per-agent defaults).
    llm_temperature_precise: float | None = field(
        default_factory=lambda: _as_optional_float(_env("LLM_TEMPERATURE_PRECISE"))
    )
    llm_temperature_creative: float | None = field(
        default_factory=lambda: _as_optional_float(_env("LLM_TEMPERATURE_CREATIVE"))
    )
    # Live web research for the Researcher agent (DuckDuckGo). Only runs in
    # agentic mode and only when the model requests it for external facts.
    web_search_enabled: bool = field(
        default_factory=lambda: _as_bool(_env("WEB_SEARCH_ENABLED"), True)
    )
    web_search_max_results: int = field(
        default_factory=lambda: _as_int(_env("WEB_SEARCH_MAX_RESULTS"), 6)
    )
    require_confirmation_for_major_changes: bool = field(
        default_factory=lambda: _as_bool(_env("REQUIRE_CONFIRMATION_FOR_MAJOR_CHANGES"), True)
    )
    backup_dir: Path = field(default_factory=lambda: _path_env("BACKUP_DIR", "data/backups"))
    backup_keep_last: int = field(default_factory=lambda: _as_int(_env("BACKUP_KEEP_LAST"), 25))
    activity_log_path: Path = field(
        default_factory=lambda: _path_env("ACTIVITY_LOG_PATH", "data/activity_log.jsonl")
    )
    memory_path: Path = field(default_factory=lambda: _path_env("MEMORY_PATH", "data/site_memory.json"))
    upload_dir: Path = field(default_factory=lambda: _path_env("UPLOAD_DIR", "data/uploads"))
    max_requests_per_minute: int = field(
        default_factory=lambda: _as_int(_env("MAX_REQUESTS_PER_MINUTE"), 10)
    )
    max_prompt_length: int = field(default_factory=lambda: _as_int(_env("MAX_PROMPT_LENGTH"), 4000))
    wordpress_max_retries: int = field(default_factory=lambda: _as_int(_env("WORDPRESS_MAX_RETRIES"), 2))
    wordpress_retry_backoff_seconds: float = field(
        default_factory=lambda: _as_float(_env("WORDPRESS_RETRY_BACKOFF_SECONDS"), 0.5)
    )
    auth_enabled: bool = field(default_factory=lambda: _as_bool(_env("AUTH_ENABLED"), True))
    auth_users_path: Path = field(default_factory=lambda: _path_env("AUTH_USERS_PATH", "config/users.yml"))
    credential_vault_path: Path = field(
        default_factory=lambda: _path_env("CREDENTIAL_VAULT_PATH", "data/credentials.enc")
    )
    sites_vault_path: Path = field(
        default_factory=lambda: _path_env("SITES_VAULT_PATH", "data/sites.enc")
    )
    credential_key: str = field(default_factory=lambda: _secret_or_env("CREDENTIAL_ENCRYPTION_KEY", ""))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", ""))
    sentry_dsn: str = field(default_factory=lambda: _secret_or_env("SENTRY_DSN", ""))
    sentry_traces_sample_rate: float = field(
        default_factory=lambda: _as_float(_env("SENTRY_TRACES_SAMPLE_RATE"), 0.0)
    )
    usage_analytics_enabled: bool = field(
        default_factory=lambda: _as_bool(_env("USAGE_ANALYTICS_ENABLED"), False)
    )
    usage_analytics_path: Path = field(
        default_factory=lambda: _path_env("USAGE_ANALYTICS_PATH", "data/usage_analytics.jsonl")
    )
    wp_cache_ttl_seconds: int = field(
        default_factory=lambda: _as_int(_env("WP_CACHE_TTL_SECONDS"), 60)
    )
    health_host: str = field(default_factory=lambda: _env("HEALTH_HOST", "0.0.0.0"))
    health_port: int = field(default_factory=lambda: _as_int(_env("HEALTH_PORT"), 8081))

    @property
    def is_production(self) -> bool:
        """Return True when running with `APP_ENV=prod`."""

        return self.app_env == "prod"

    @property
    def data_paths(self) -> tuple[Path, ...]:
        """Directories/files that should be persisted by deployments."""

        return (
            self.backup_dir,
            self.activity_log_path.parent,
            self.memory_path.parent,
            self.upload_dir,
            self.usage_analytics_path.parent,
        )


def get_config() -> AppConfig:
    """Return application settings.

    The dataclass uses default factories so tests and Streamlit reruns see fresh
    values from environment variables and Docker secret files.
    """

    return AppConfig()
