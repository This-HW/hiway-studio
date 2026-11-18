"""Environment-based configuration (3-layer config, layer 2).

Layer 1 (public defaults) lives in app/core/defaults.py.
Layer 2 (private overrides) is loaded here from the environment / .env file.
Layer 3 (user settings) lives in the DB (Phase 1+).

Rule (ARCHITECTURE §14): secrets are read from .env only. DATABASE_URL,
REDIS_URL and PROJECTS_ROOT have no code defaults on purpose — the app fails
fast with a clear message when they are missing instead of silently using a
hardcoded value.
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core import defaults


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Empty optional values (e.g. `API_PORT=`) fall back to defaults; the
        # required fields below still fail fast because they have none.
        env_ignore_empty=True,
    )

    database_url: str
    redis_url: str
    projects_root: Path
    api_port: int = 8000
    sse_backend: str = "redis"  # "redis" | "memory" (memory is for tests / demos)
    log_level: str = "INFO"
    comfyui_url: str = ""  # reserved for a later phase; empty means "not configured"
    cors_origins: str = ""  # comma-separated override; empty = defaults.CORS_ALLOWED_ORIGINS
    # Periodic job reconcile in the API lifespan (ADR-0013). None = automatic:
    # defaults.RECONCILE_ENABLED, but off with the in-memory SSE backend.
    reconcile_enabled: bool | None = None

    @property
    def reconcile_active(self) -> bool:
        if self.reconcile_enabled is not None:
            return self.reconcile_enabled
        return defaults.RECONCILE_ENABLED and self.sse_backend != "memory"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip():
            return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return list(defaults.CORS_ALLOWED_ORIGINS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
