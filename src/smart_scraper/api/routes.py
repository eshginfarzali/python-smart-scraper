"""HTTP routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from smart_scraper import __version__
from smart_scraper.db import JobStatus
from smart_scraper.repository import JobRepository
from smart_scraper.schemas import ErrorBody, Health, JobCreate, JobList, JobOut
from smart_scraper.worker import JobQueue

router = APIRouter()


def get_repository(request: Request) -> JobRepository:
    repository: JobRepository = request.app.state.repository
    return repository


def get_queue(request: Request) -> JobQueue:
    queue: JobQueue = request.app.state.queue
    return queue


RepositoryDep = Annotated[JobRepository, Depends(get_repository)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorBody, "description": "Job not found"}
}


async def _get_job_or_404(repository: JobRepository, job_id: str) -> JobOut:
    record = await repository.get(job_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    return JobOut.from_record(record)


@router.get("/health", tags=["meta"])
async def health(repository: RepositoryDep, queue: QueueDep, response: Response) -> Health:
    """Liveness/readiness: checks the database connection and reports queue stats."""
    try:
        await repository.ping()
        database = "ok"
    except Exception:
        database = "unavailable"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Health(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        database=database,
        queue_size=queue.size,
        workers=queue.worker_count,
    )


@router.post(
    "/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
    summary="Submit a scrape job",
)
async def create_job(payload: JobCreate, repository: RepositoryDep, queue: QueueDep) -> JobOut:
    """Persist the job and enqueue it. Poll ``GET /jobs/{id}`` for the result."""
    record = await repository.create(
        urls=[str(url) for url in payload.urls],
        fields={name: rule.model_dump(mode="json") for name, rule in payload.fields.items()},
    )
    queue.submit(record.id)
    return JobOut.from_record(record)


@router.get("/jobs", tags=["jobs"], summary="List jobs (newest first)")
async def list_jobs(
    repository: RepositoryDep,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobList:
    records, total = await repository.list_jobs(status=status_filter, limit=limit, offset=offset)
    return JobList(
        items=[JobOut.from_record(record) for record in records],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", tags=["jobs"], responses=NOT_FOUND, summary="Get job status")
async def get_job(job_id: str, repository: RepositoryDep) -> JobOut:
    return await _get_job_or_404(repository, job_id)


@router.post(
    "/jobs/{job_id}/cancel",
    tags=["jobs"],
    summary="Cancel a queued or running job",
    responses={
        **NOT_FOUND,
        status.HTTP_409_CONFLICT: {"model": ErrorBody, "description": "Job already finished"},
    },
)
async def cancel_job(job_id: str, repository: RepositoryDep, queue: QueueDep) -> JobOut:
    if not await queue.cancel(job_id):
        job = await _get_job_or_404(repository, job_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is already {job.status.value} and cannot be cancelled",
        )
    return await _get_job_or_404(repository, job_id)
