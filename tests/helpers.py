"""Shared test utilities."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any, Protocol

from smart_scraper.scraping.fetcher import FetchConfig, PoliteFetcher

FIXTURES = Path(__file__).parent / "fixtures"
USER_AGENT = "SmartScraperBot/0.1.0 (+https://github.com/eshginfarzali/python-smart-scraper)"

BASE_CONFIG = FetchConfig(
    user_agent=USER_AGENT,
    total_timeout=5.0,
    max_page_bytes=64 * 1024,
    max_redirects=3,
    max_retries=3,
    backoff_base=1.0,
    backoff_max=30.0,
    max_retry_after=60.0,
    max_crawl_delay=10.0,
)


class FakeClock:
    """Deterministic clock: ``sleep`` advances time instantly and records the duration."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(**overrides: Any) -> FetchConfig:
    return dataclasses.replace(BASE_CONFIG, **overrides)


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FetcherFactory(Protocol):
    def __call__(
        self, *, min_delay: float = ..., max_concurrency: int = ..., **config: Any
    ) -> PoliteFetcher: ...
