"""Application configuration loaded from environment variables (and an optional ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from smart_scraper import __version__

DEFAULT_USER_AGENT = (
    f"SmartScraperBot/{__version__} (+https://github.com/eshginfarzali/python-smart-scraper)"
)


class Settings(BaseSettings):
    """Runtime settings.

    Every field can be overridden with an environment variable prefixed with ``SCRAPER_``
    (e.g. ``SCRAPER_WORKER_COUNT=4``). ``DATABASE_URL`` is also accepted without the prefix
    because that is the de-facto convention for database connection strings.
    """

    model_config = SettingsConfigDict(
        env_prefix="SCRAPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- service -------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/scraper.db",
        validation_alias=AliasChoices("DATABASE_URL", "SCRAPER_DATABASE_URL"),
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host/db",
    )
    worker_count: int = Field(default=2, ge=1, le=64, description="Concurrent jobs.")

    # --- politeness ----------------------------------------------------------------------
    user_agent: str = Field(default=DEFAULT_USER_AGENT, min_length=1)
    min_host_delay_seconds: float = Field(
        default=1.0, ge=0, description="Minimum gap between two requests to the same host."
    )
    max_host_concurrency: int = Field(
        default=2, ge=1, le=32, description="Max in-flight requests per host."
    )
    max_crawl_delay_seconds: float = Field(
        default=30.0, ge=0, description="Upper bound applied to a robots.txt Crawl-delay."
    )
    robots_cache_ttl_seconds: float = Field(default=3600.0, ge=0)
    robots_error_ttl_seconds: float = Field(
        default=300.0, ge=0, description="How long an unreachable robots.txt is cached."
    )

    # --- HTTP ----------------------------------------------------------------------------
    request_timeout_seconds: float = Field(default=15.0, gt=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    max_page_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_redirects: int = Field(default=5, ge=0, le=20)
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_base_seconds: float = Field(default=1.0, ge=0)
    backoff_max_seconds: float = Field(default=60.0, ge=0)
    max_retry_after_seconds: float = Field(
        default=120.0,
        ge=0,
        description="A Retry-After longer than this fails the URL instead of waiting.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide settings (cached)."""
    return Settings()
