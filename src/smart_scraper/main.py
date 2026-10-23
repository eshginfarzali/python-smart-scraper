"""Application factory.

Run with ``uvicorn --factory smart_scraper.main:create_app``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from smart_scraper import __version__
from smart_scraper.api.routes import router
from smart_scraper.config import Settings, get_settings
from smart_scraper.db import create_engine, create_sessionmaker, init_db
from smart_scraper.repository import JobRepository
from smart_scraper.scraping.clock import Clock, SystemClock
from smart_scraper.scraping.fetcher import FetchConfig, PoliteFetcher
from smart_scraper.scraping.rate_limit import HostRateLimiter
from smart_scraper.scraping.retry import Jitter, equal_jitter
from smart_scraper.scraping.robots import RobotsCache
from smart_scraper.worker import JobQueue, Scraper


def fetch_config(settings: Settings) -> FetchConfig:
    return FetchConfig(
        user_agent=settings.user_agent,
        total_timeout=settings.request_timeout_seconds,
        max_page_bytes=settings.max_page_bytes,
        max_redirects=settings.max_redirects,
        max_retries=settings.max_retries,
        backoff_base=settings.backoff_base_seconds,
        backoff_max=settings.backoff_max_seconds,
        max_retry_after=settings.max_retry_after_seconds,
        max_crawl_delay=settings.max_crawl_delay_seconds,
    )


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
    jitter: Jitter = equal_jitter,
) -> FastAPI:
    """Build the FastAPI app.

    ``transport``, ``clock`` and ``jitter`` exist so tests can run the full stack without
    network access or real waiting.
    """
    settings = settings or get_settings()
    clock = clock or SystemClock()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        await init_db(engine)
        repository = JobRepository(create_sessionmaker(engine))

        client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                settings.request_timeout_seconds, connect=settings.connect_timeout_seconds
            ),
            headers={"User-Agent": settings.user_agent},
            follow_redirects=False,
        )
        limiter = HostRateLimiter(
            min_delay=settings.min_host_delay_seconds,
            max_concurrency=settings.max_host_concurrency,
            clock=clock,
        )
        robots = RobotsCache(
            client=client,
            limiter=limiter,
            clock=clock,
            user_agent=settings.user_agent,
            ttl=settings.robots_cache_ttl_seconds,
            error_ttl=settings.robots_error_ttl_seconds,
        )
        fetcher = PoliteFetcher(
            client=client,
            robots=robots,
            limiter=limiter,
            config=fetch_config(settings),
            jitter=jitter,
        )
        queue = JobQueue(repository, Scraper(fetcher), worker_count=settings.worker_count)

        app.state.settings = settings
        app.state.repository = repository
        app.state.queue = queue
        await queue.start()
        try:
            yield
        finally:
            await queue.stop()
            await client.aclose()
            await engine.dispose()

    app = FastAPI(
        title="python-smart-scraper",
        version=__version__,
        description="A polite, async web-scraping service with a persistent job queue.",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app
