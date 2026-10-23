"""Persistence for scrape jobs. All state transitions are conditional updates."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import CursorResult, Result, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smart_scraper.db import JobRecord, JobStatus, utcnow


class JobRepository:
    """CRUD + guarded state transitions for :class:`JobRecord`.

    Transitions use ``UPDATE ... WHERE status IN (...)`` so that a worker finishing a job
    can never overwrite a cancellation (and vice versa), regardless of timing.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def ping(self) -> None:
        async with self._sessionmaker() as session:
            await session.execute(text("SELECT 1"))

    async def create(self, urls: list[str], fields: dict[str, Any]) -> JobRecord:
        record = JobRecord(
            id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            urls=urls,
            fields=fields,
            results=[],
            created_at=utcnow(),
        )
        async with self._sessionmaker() as session, session.begin():
            session.add(record)
        return record

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._sessionmaker() as session:
            return await session.get(JobRecord, job_id)

    async def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[JobRecord], int]:
        query = select(JobRecord)
        count_query = select(func.count()).select_from(JobRecord)
        if status is not None:
            query = query.where(JobRecord.status == status)
            count_query = count_query.where(JobRecord.status == status)
        query = (
            query.order_by(JobRecord.created_at.desc(), JobRecord.id).limit(limit).offset(offset)
        )
        async with self._sessionmaker() as session:
            records = list((await session.scalars(query)).all())
            total = (await session.execute(count_query)).scalar_one()
        return records, total

    async def mark_running(self, job_id: str) -> bool:
        return await self._transition(
            job_id, {JobStatus.QUEUED}, status=JobStatus.RUNNING, started_at=utcnow()
        )

    async def finish(
        self,
        job_id: str,
        *,
        status: JobStatus,
        results: list[dict[str, Any]],
        error: str | None = None,
    ) -> bool:
        if not status.is_terminal:
            raise ValueError(f"{status} is not a terminal status")
        return await self._transition(
            job_id,
            {JobStatus.RUNNING},
            status=status,
            results=results,
            error=error,
            finished_at=utcnow(),
        )

    async def cancel(self, job_id: str) -> bool:
        return await self._transition(
            job_id,
            {JobStatus.QUEUED, JobStatus.RUNNING},
            status=JobStatus.CANCELLED,
            error="Cancelled by request",
            finished_at=utcnow(),
        )

    async def requeue_interrupted(self) -> int:
        """Move jobs left ``running`` by a previous process back to ``queued``."""
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                update(JobRecord)
                .where(JobRecord.status == JobStatus.RUNNING)
                .values(status=JobStatus.QUEUED, started_at=None)
            )
        return _rowcount(result)

    async def queued_ids(self) -> list[str]:
        async with self._sessionmaker() as session:
            rows = await session.scalars(
                select(JobRecord.id)
                .where(JobRecord.status == JobStatus.QUEUED)
                .order_by(JobRecord.created_at)
            )
            return list(rows.all())

    async def _transition(
        self, job_id: str, from_statuses: Iterable[JobStatus], **values: Any
    ) -> bool:
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id, JobRecord.status.in_(list(from_statuses)))
                .values(**values)
            )
        return _rowcount(result) > 0


def _rowcount(result: Result[Any]) -> int:
    # UPDATE statements always produce a CursorResult, which carries the affected row count.
    return int(cast(CursorResult[Any], result).rowcount)
