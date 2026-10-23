"""Worker-pool behaviour that is awkward to reach through the HTTP API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from smart_scraper.db import JobStatus, create_engine, create_sessionmaker, init_db
from smart_scraper.repository import JobRepository
from smart_scraper.schemas import UrlResult
from smart_scraper.scraping.extract import FieldRule
from smart_scraper.worker import JobQueue

RULES = {"t": {"selector": "h1"}}


class ScriptedScraper:
    """Raises for URLs containing 'boom', blocks on 'slow', succeeds otherwise."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def scrape(self, url: str, rules: dict[str, FieldRule]) -> UrlResult:
        if "boom" in url:
            raise RuntimeError("unexpected bug")
        if "slow" in url:
            self.started.set()
            await self.release.wait()
        return UrlResult(url=url, ok=True, status_code=200, data={"t": "hi"})


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[JobRepository]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    await init_db(engine)
    yield JobRepository(create_sessionmaker(engine))
    await engine.dispose()


async def test_unexpected_errors_fail_the_job_but_not_the_worker(
    repository: JobRepository,
) -> None:
    queue = JobQueue(repository, ScriptedScraper(), worker_count=1)
    await queue.start()
    try:
        broken = await repository.create(["https://a.example/boom"], RULES)
        fine = await repository.create(["https://a.example/ok"], RULES)
        queue.submit(broken.id)
        queue.submit(fine.id)
        async with asyncio.timeout(5):
            await queue.join()
    finally:
        await queue.stop()

    broken_job = await repository.get(broken.id)
    assert broken_job is not None
    assert broken_job.status == JobStatus.FAILED
    assert broken_job.error == "Internal error while processing the job"
    fine_job = await repository.get(fine.id)
    assert fine_job is not None
    assert fine_job.status == JobStatus.COMPLETED


async def test_shutdown_leaves_in_flight_jobs_resumable(repository: JobRepository) -> None:
    scraper = ScriptedScraper()
    queue = JobQueue(repository, scraper, worker_count=1)
    await queue.start()
    job = await repository.create(["https://a.example/slow"], RULES)
    queue.submit(job.id)
    async with asyncio.timeout(5):
        await scraper.started.wait()
    await queue.stop()

    stored = await repository.get(job.id)
    assert stored is not None
    assert stored.status == JobStatus.RUNNING  # will be re-queued by the next start()

    second_run = ScriptedScraper()
    second_run.release.set()
    restarted = JobQueue(repository, second_run, worker_count=1)
    await restarted.start()
    try:
        async with asyncio.timeout(5):
            await restarted.join()
    finally:
        await restarted.stop()
    stored = await repository.get(job.id)
    assert stored is not None
    assert stored.status == JobStatus.COMPLETED


async def test_cancelling_unknown_or_finished_jobs_is_a_no_op(repository: JobRepository) -> None:
    queue = JobQueue(repository, ScriptedScraper(), worker_count=1)
    assert await queue.cancel("missing") is False
    job = await repository.create(["https://a.example/ok"], RULES)
    assert await repository.mark_running(job.id)
    assert await repository.finish(job.id, status=JobStatus.COMPLETED, results=[])
    assert await queue.cancel(job.id) is False


async def test_finish_requires_a_terminal_status(repository: JobRepository) -> None:
    with pytest.raises(ValueError, match="not a terminal status"):
        await repository.finish("x", status=JobStatus.RUNNING, results=[])
