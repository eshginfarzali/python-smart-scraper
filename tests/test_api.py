"""End-to-end tests: FastAPI app + worker pool + SQLite, with HTTP mocked at the transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from smart_scraper.config import Settings
from smart_scraper.db import JobStatus, create_engine, create_sessionmaker, init_db
from smart_scraper.main import create_app
from smart_scraper.repository import JobRepository
from smart_scraper.scraping.retry import no_jitter
from tests.helpers import FakeClock, fixture_html

SITE = "https://shop.example"
TERMINAL = {"completed", "failed", "cancelled"}

AppFactory = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def make_api(database_url: str, router: respx.Router) -> AppFactory:
    """Start the full app (lifespan included) and yield an HTTP client for it."""

    @asynccontextmanager
    async def factory(
        transport: httpx.AsyncBaseTransport | None = None, **overrides: Any
    ) -> AsyncIterator[httpx.AsyncClient]:
        values: dict[str, Any] = {
            "database_url": database_url,
            "worker_count": 2,
            "min_host_delay_seconds": 0.5,
            "max_retries": 1,
            "backoff_base_seconds": 0.1,
            **overrides,
        }
        settings = Settings(_env_file=None, **values)  # ignore any local .env
        app = create_app(
            settings,
            transport=transport or httpx.MockTransport(router.async_handler),
            clock=FakeClock(),
            jitter=no_jitter,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as api,
        ):
            yield api

    return factory


@pytest.fixture
async def api(make_api: AppFactory) -> AsyncIterator[httpx.AsyncClient]:
    async with make_api() as client:
        yield client


@pytest.fixture(autouse=True)
def site(router: respx.Router) -> None:
    router.get(f"{SITE}/robots.txt").respond(
        200, text="User-agent: *\nDisallow: /private\n", headers={"Content-Type": "text/plain"}
    )
    router.get(f"{SITE}/catalogue").respond(200, html=fixture_html("product_list.html"))
    router.get(f"{SITE}/missing").respond(404)


async def wait_for_job(
    api: httpx.AsyncClient, job_id: str, statuses: set[str] = TERMINAL
) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            response = await api.get(f"/jobs/{job_id}")
            assert response.status_code == 200
            body: dict[str, Any] = response.json()
            if body["status"] in statuses:
                return body
            await asyncio.sleep(0.01)


async def submit(api: httpx.AsyncClient, urls: list[str], fields: dict[str, Any]) -> str:
    response = await api.post("/jobs", json={"urls": urls, "fields": fields})
    assert response.status_code == 202, response.text
    job_id: str = response.json()["id"]
    return job_id


async def test_health(api: httpx.AsyncClient) -> None:
    response = await api.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["workers"] == 2


async def test_job_lifecycle_create_to_completed(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/jobs",
        json={
            "urls": [f"{SITE}/catalogue"],
            "fields": {
                "title": "h1.page-title",
                "names": {"selector": "article.product .name", "multiple": True},
                "links": {"selector": "a.details", "attr": "href", "multiple": True},
                "missing": ".nope",
            },
        },
    )
    assert response.status_code == 202
    created = response.json()
    assert created["status"] == "queued"
    assert created["results"] == []
    assert created["fields"]["title"] == {
        "selector": "h1.page-title",
        "attr": None,
        "multiple": False,
    }

    job = await wait_for_job(api, created["id"])
    assert job["status"] == "completed"
    assert job["error"] is None
    assert job["started_at"] is not None
    assert job["finished_at"] is not None
    [result] = job["results"]
    assert result == {
        "url": f"{SITE}/catalogue",
        "ok": True,
        "final_url": f"{SITE}/catalogue",
        "status_code": 200,
        "data": {
            "title": "Our Teas",
            "names": ["Sencha", "Darjeeling First Flush", "Rooibos"],
            "links": ["/teas/sencha", "/teas/darjeeling"],
            "missing": None,
        },
        "error": None,
    }


async def test_robots_disallowed_url_fails_with_clear_reason(
    api: httpx.AsyncClient, router: respx.Router
) -> None:
    private = router.get(f"{SITE}/private/orders").respond(200, html="<h1>secret</h1>")
    job_id = await submit(api, [f"{SITE}/private/orders"], {"title": "h1"})

    job = await wait_for_job(api, job_id)
    assert job["status"] == "failed"
    assert job["error"].startswith("Blocked by robots.txt:")
    assert f"{SITE}/private/orders is disallowed" in job["error"]
    assert job["results"][0]["ok"] is False
    assert job["results"][0]["data"] is None
    assert private.call_count == 0


async def test_partial_success_completes_with_per_url_errors(api: httpx.AsyncClient) -> None:
    job_id = await submit(api, [f"{SITE}/catalogue", f"{SITE}/missing"], {"title": "h1"})
    job = await wait_for_job(api, job_id)
    assert job["status"] == "completed"
    ok, missing = job["results"]
    assert ok["ok"] is True
    assert missing == {
        "url": f"{SITE}/missing",
        "ok": False,
        "final_url": None,
        "status_code": 404,
        "data": None,
        "error": f"HTTP 404 from {SITE}/missing",
    }


async def test_job_fails_when_every_url_fails(api: httpx.AsyncClient) -> None:
    job_id = await submit(api, [f"{SITE}/missing", f"{SITE}/private/x"], {"title": "h1"})
    job = await wait_for_job(api, job_id)
    assert job["status"] == "failed"
    assert job["error"] == f"All 2 URLs failed; first error: HTTP 404 from {SITE}/missing"


async def test_server_errors_are_retried_before_failing(
    api: httpx.AsyncClient, router: respx.Router
) -> None:
    route = router.get(f"{SITE}/flaky").respond(503)
    job_id = await submit(api, [f"{SITE}/flaky"], {"title": "h1"})
    job = await wait_for_job(api, job_id)
    assert job["status"] == "failed"
    assert job["error"] == f"HTTP 503 from {SITE}/flaky after 2 attempt(s)"
    assert route.call_count == 2  # max_retries=1


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        ({"urls": [], "fields": {"t": "h1"}}, ["body", "urls"]),
        ({"urls": ["ftp://shop.example/x"], "fields": {"t": "h1"}}, ["body", "urls", 0]),
        ({"urls": ["not a url"], "fields": {"t": "h1"}}, ["body", "urls", 0]),
        ({"urls": [f"{SITE}/{i}" for i in range(21)], "fields": {"t": "h1"}}, ["body", "urls"]),
        ({"urls": [f"{SITE}/"], "fields": {}}, ["body", "fields"]),
        ({"urls": [f"{SITE}/"], "fields": {"t": "p["}}, ["body", "fields", "t", "selector"]),
        ({"urls": [f"{SITE}/"], "fields": {"t": ""}}, ["body", "fields", "t", "selector"]),
        ({"urls": [f"{SITE}/"], "fields": {"1bad": "h1"}}, ["body", "fields", "1bad", "[key]"]),
        (
            {"urls": [f"{SITE}/"], "fields": {"t": {"css": "h1"}}},
            ["body", "fields", "t", "selector"],
        ),
        ({"urls": [f"{SITE}/"], "fields": {"t": "h1"}, "extra": 1}, ["body", "extra"]),
        ({"fields": {"t": "h1"}}, ["body", "urls"]),
    ],
)
async def test_validation_errors(
    api: httpx.AsyncClient, payload: dict[str, Any], location: list[str | int]
) -> None:
    response = await api.post("/jobs", json=payload)
    assert response.status_code == 422
    locations = [error["loc"] for error in response.json()["detail"]]
    assert location in locations


async def test_duplicate_urls_are_collapsed(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/jobs", json={"urls": [f"{SITE}/catalogue", f"{SITE}/catalogue"], "fields": {"t": "h1"}}
    )
    assert response.json()["urls"] == [f"{SITE}/catalogue"]


async def test_unknown_job_returns_404(api: httpx.AsyncClient) -> None:
    assert (await api.get("/jobs/does-not-exist")).status_code == 404
    response = await api.post("/jobs/does-not-exist/cancel")
    assert response.status_code == 404
    assert response.json() == {"detail": "Job does-not-exist not found"}


async def test_list_jobs_with_filter_and_pagination(api: httpx.AsyncClient) -> None:
    ok_ids = [await submit(api, [f"{SITE}/catalogue"], {"t": "h1"}) for _ in range(3)]
    failed_id = await submit(api, [f"{SITE}/missing"], {"t": "h1"})
    for job_id in [*ok_ids, failed_id]:
        await wait_for_job(api, job_id)

    everything = (await api.get("/jobs")).json()
    assert everything["total"] == 4
    assert [job["id"] for job in everything["items"]] == [failed_id, *reversed(ok_ids)]

    page = (await api.get("/jobs", params={"status": "completed", "limit": 2, "offset": 1})).json()
    assert page["total"] == 3
    assert page["limit"] == 2
    assert page["offset"] == 1
    assert [job["id"] for job in page["items"]] == list(reversed(ok_ids))[1:]

    failed = (await api.get("/jobs", params={"status": "failed"})).json()
    assert [job["id"] for job in failed["items"]] == [failed_id]

    assert (await api.get("/jobs", params={"status": "bogus"})).status_code == 422
    assert (await api.get("/jobs", params={"limit": 0})).status_code == 422


async def test_cancel_queued_and_running_jobs(make_api: AppFactory) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        started.set()
        await release.wait()
        return httpx.Response(200, html="<h1>late</h1>")

    async with make_api(httpx.MockTransport(handler), worker_count=1) as api:
        running_id = await submit(api, [f"{SITE}/slow"], {"t": "h1"})
        queued_id = await submit(api, [f"{SITE}/slow-too"], {"t": "h1"})
        async with asyncio.timeout(5):
            await started.wait()
        assert (await api.get(f"/jobs/{running_id}")).json()["status"] == "running"
        assert (await api.get(f"/jobs/{queued_id}")).json()["status"] == "queued"

        response = await api.post(f"/jobs/{queued_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

        response = await api.post(f"/jobs/{running_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["error"] == "Cancelled by request"

        release.set()
        # A third job proves the single worker survived the cancellation.
        follow_up = await submit(api, [f"{SITE}/after"], {"t": "h1"})
        assert (await wait_for_job(api, follow_up))["status"] == "completed"

        for job_id in (running_id, queued_id):
            job = (await api.get(f"/jobs/{job_id}")).json()
            assert job["status"] == "cancelled"
            assert job["results"] == []

        response = await api.post(f"/jobs/{running_id}/cancel")
        assert response.status_code == 409
        assert "already cancelled" in response.json()["detail"]


async def test_interrupted_jobs_are_resumed_on_startup(
    make_api: AppFactory, database_url: str
) -> None:
    engine = create_engine(database_url)
    await init_db(engine)
    repository = JobRepository(create_sessionmaker(engine))
    interrupted = await repository.create([f"{SITE}/catalogue"], {"t": {"selector": "h1"}})
    assert await repository.mark_running(interrupted.id)
    waiting = await repository.create([f"{SITE}/catalogue"], {"t": {"selector": "h1"}})
    await engine.dispose()

    async with make_api() as api:
        for job_id in (interrupted.id, waiting.id):
            job = await wait_for_job(api, job_id)
            assert job["status"] == JobStatus.COMPLETED
            assert job["results"][0]["data"] == {"t": "Our Teas"}


async def test_openapi_schema_is_served(api: httpx.AsyncClient) -> None:
    schema = (await api.get("/openapi.json")).json()
    assert {"/health", "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/cancel"} <= set(schema["paths"])
