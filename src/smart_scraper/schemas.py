"""Public API models (request/response bodies)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl, field_validator

from smart_scraper.db import JobRecord, JobStatus
from smart_scraper.scraping.extract import ExtractedValue, FieldName, FieldRule

MAX_URLS_PER_JOB = 20
MAX_FIELDS_PER_JOB = 50


def _expand_shorthand(value: Any) -> Any:
    return {"selector": value} if isinstance(value, str) else value


RuleInput = Annotated[FieldRule, BeforeValidator(_expand_shorthand)]


class JobCreate(BaseModel):
    """A scrape job: which pages to fetch and which fields to extract from each."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "urls": ["https://example.com/"],
                    "fields": {
                        "title": "h1",
                        "links": {"selector": "a", "attr": "href", "multiple": True},
                    },
                }
            ]
        },
    )

    urls: Annotated[list[HttpUrl], Field(min_length=1, max_length=MAX_URLS_PER_JOB)]
    fields: Annotated[
        dict[FieldName, RuleInput],
        Field(
            min_length=1,
            max_length=MAX_FIELDS_PER_JOB,
            description='Field name -> rule. A plain string is shorthand for {"selector": ...}.',
        ),
    ]

    @field_validator("urls")
    @classmethod
    def _dedupe_urls(cls, urls: list[HttpUrl]) -> list[HttpUrl]:
        return list(dict.fromkeys(urls))


class UrlResult(BaseModel):
    """Outcome for one URL of a job."""

    url: str
    ok: bool
    final_url: str | None = None
    status_code: int | None = None
    data: dict[str, ExtractedValue] | None = None
    error: str | None = None


class JobOut(BaseModel):
    id: str
    status: JobStatus
    urls: list[str]
    fields: dict[str, FieldRule]
    results: list[UrlResult]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_record(cls, record: JobRecord) -> JobOut:
        return cls(
            id=record.id,
            status=JobStatus(record.status),
            urls=list(record.urls),
            fields={name: FieldRule.model_validate(rule) for name, rule in record.fields.items()},
            results=[UrlResult.model_validate(result) for result in record.results or []],
            error=record.error,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )


class JobList(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


class Health(BaseModel):
    status: str
    version: str
    database: str
    queue_size: int
    workers: int


class ErrorBody(BaseModel):
    detail: str
