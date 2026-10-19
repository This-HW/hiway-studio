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
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    redis_url: str
    projects_root: Path
    api_port: int = 8000
    sse_backend: str = "redis"  # "redis" | "memory" (memory is for tests / demos)
    log_level: str = "INFO"
    comfyui_url: str = ""  # reserved for a later phase; empty means "not configured"
    cors_origins: str = ""  # comma-separated override; empty = defaults.CORS_ALLOWED_ORIGINS

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip():
            return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return list(defaults.CORS_ALLOWED_ORIGINS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
