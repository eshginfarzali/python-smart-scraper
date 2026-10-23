"""In-process job queue and worker pool.

Jobs are persisted first and then their IDs are pushed onto an :class:`asyncio.Queue`.
``worker_count`` long-lived tasks pull IDs and process one job each at a time; the URLs of a
job are fetched concurrently and politeness is enforced per host by the fetcher.

Durability: the database is the source of truth. On start-up, jobs that were ``running`` when
the previous process stopped are moved back to ``queued`` and every ``queued`` job is
re-enqueued. This design assumes a *single* API process; see the README roadmap for a
multi-process broker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from smart_scraper.db import JobStatus
from smart_scraper.repository import JobRepository
from smart_scraper.schemas import UrlResult
from smart_scraper.scraping.errors import FetchFailedError, ScrapeError
from smart_scraper.scraping.extract import FieldRule, extract
from smart_scraper.scraping.fetcher import PoliteFetcher

logger = logging.getLogger(__name__)


class Scraper:
    """Fetch one URL politely and apply the extraction rules to it."""

    def __init__(self, fetcher: PoliteFetcher) -> None:
        self._fetcher = fetcher

    async def scrape(self, url: str, rules: dict[str, FieldRule]) -> UrlResult:
        try:
            page = await self._fetcher.fetch(url)
            # Parsing is CPU-bound; keep the event loop responsive for other jobs.
            data = await asyncio.to_thread(extract, page.text, rules)
        except ScrapeError as exc:
            status = exc.status_code if isinstance(exc, FetchFailedError) else None
            return UrlResult(url=url, ok=False, status_code=status, error=str(exc))
        return UrlResult(
            url=url,
            ok=True,
            final_url=page.final_url,
            status_code=page.status_code,
            data=data,
        )


class UrlScraper(Protocol):
    async def scrape(self, url: str, rules: dict[str, FieldRule]) -> UrlResult: ...


def summarise_failure(results: list[UrlResult]) -> str:
    errors = [r.error or "unknown error" for r in results if not r.ok]
    if len(errors) == 1:
        return errors[0]
    return f"All {len(errors)} URLs failed; first error: {errors[0]}"


class JobQueue:
    def __init__(
        self, repository: JobRepository, scraper: UrlScraper, *, worker_count: int
    ) -> None:
        self._repository = repository
        self._scraper = scraper
        self._worker_count = worker_count
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}

    @property
    def size(self) -> int:
        """Number of job IDs waiting to be picked up."""
        return self._queue.qsize()

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    async def start(self) -> None:
        recovered = await self._repository.requeue_interrupted()
        if recovered:
            logger.info("Re-queued %d interrupted job(s)", recovered)
        for job_id in await self._repository.queued_ids():
            self._queue.put_nowait(job_id)
        self._workers = [
            asyncio.create_task(self._work(), name=f"scrape-worker-{index}")
            for index in range(self._worker_count)
        ]

    async def stop(self) -> None:
        """Stop workers. Jobs still running stay ``running`` and are resumed on next start."""
        in_flight = list(self._running.values())
        for task in [*self._workers, *in_flight]:
            task.cancel()
        await asyncio.gather(*self._workers, *in_flight, return_exceptions=True)
        self._workers = []

    def submit(self, job_id: str) -> None:
        self._queue.put_nowait(job_id)

    async def join(self) -> None:
        """Wait until every submitted job has been processed (useful in tests and scripts)."""
        await self._queue.join()

    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns ``False`` if it was already finished."""
        cancelled = await self._repository.cancel(job_id)
        task = self._running.get(job_id)
        if cancelled and task is not None:
            task.cancel()
        return cancelled

    async def _work(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                task = asyncio.create_task(self._process(job_id), name=f"scrape-job-{job_id}")
                self._running[job_id] = task
                # ``wait`` (unlike ``await task``) does not raise if the job task is cancelled,
                # so cancelling a job never kills the worker that ran it.
                await asyncio.wait({task})
                if not task.cancelled() and (exc := task.exception()) is not None:
                    logger.error("Job %s crashed", job_id, exc_info=exc)
            finally:
                self._running.pop(job_id, None)
                self._queue.task_done()

    async def _process(self, job_id: str) -> None:
        if not await self._repository.mark_running(job_id):
            return  # cancelled while queued, or unknown
        job = await self._repository.get(job_id)
        if job is None:  # pragma: no cover - deleted underneath us
            return

        try:
            rules = {name: FieldRule.model_validate(rule) for name, rule in job.fields.items()}
            results = list(
                await asyncio.gather(*(self._scraper.scrape(url, rules) for url in job.urls))
            )
        except Exception:
            logger.exception("Unexpected error while processing job %s", job_id)
            await self._repository.finish(
                job_id,
                status=JobStatus.FAILED,
                results=[],
                error="Internal error while processing the job",
            )
            return

        succeeded = any(result.ok for result in results)
        await self._repository.finish(
            job_id,
            status=JobStatus.COMPLETED if succeeded else JobStatus.FAILED,
            results=[result.model_dump(mode="json") for result in results],
            error=None if succeeded else summarise_failure(results),
        )
        logger.info(
            "Job %s finished: %d/%d URL(s) succeeded",
            job_id,
            sum(result.ok for result in results),
            len(results),
        )
