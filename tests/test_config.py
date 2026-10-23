from __future__ import annotations

import pytest
from pydantic import ValidationError

from smart_scraper.config import DEFAULT_USER_AGENT, Settings


def test_defaults_are_polite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.user_agent == DEFAULT_USER_AGENT
    assert settings.user_agent.startswith("SmartScraperBot/")
    assert settings.min_host_delay_seconds >= 1.0
    assert settings.max_retries == 3
    assert settings.database_url.startswith("sqlite+aiosqlite://")


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/scraper")
    monkeypatch.setenv("SCRAPER_WORKER_COUNT", "5")
    monkeypatch.setenv("SCRAPER_MIN_HOST_DELAY_SECONDS", "2.5")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql+asyncpg://u:p@db/scraper"
    assert settings.worker_count == 5
    assert settings.min_host_delay_seconds == 2.5


def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPER_WORKER_COUNT", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
