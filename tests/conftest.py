from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from smart_scraper.scraping.fetcher import PoliteFetcher
from smart_scraper.scraping.rate_limit import HostRateLimiter
from smart_scraper.scraping.retry import no_jitter
from smart_scraper.scraping.robots import RobotsCache
from tests.helpers import USER_AGENT, FakeClock, FetcherFactory, make_config


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def router() -> respx.Router:
    return respx.Router(assert_all_called=False)


@pytest.fixture
async def client(router: respx.Router) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler)) as c:
        yield c


@pytest.fixture
def make_fetcher(client: httpx.AsyncClient, clock: FakeClock) -> FetcherFactory:
    """Build a fetcher wired to the mocked HTTP client and the fake clock."""

    def factory(
        *, min_delay: float = 0.0, max_concurrency: int = 2, **config: Any
    ) -> PoliteFetcher:
        limiter = HostRateLimiter(min_delay=min_delay, max_concurrency=max_concurrency, clock=clock)
        robots = RobotsCache(
            client=client,
            limiter=limiter,
            clock=clock,
            user_agent=USER_AGENT,
            ttl=3600,
            error_ttl=60,
        )
        return PoliteFetcher(
            client=client,
            robots=robots,
            limiter=limiter,
            config=make_config(**config),
            jitter=no_jitter,
        )

    return factory
