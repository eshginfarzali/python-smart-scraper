# python-smart-scraper

**Submit a scrape job over HTTP, get structured JSON back, and stay polite to the sites you scrape.**

[![CI](https://github.com/eshginfarzali/python-smart-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/eshginfarzali/python-smart-scraper/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

A FastAPI service with an async job queue. You post one or more URLs plus simple
extraction rules (CSS selector → field name); a background worker pool fetches the pages
with an `httpx` client that enforces robots.txt, per-host rate limits, timeouts, size caps
and retry etiquette, then stores the extracted data in SQLite or Postgres.

## Features

- **Job API** – create, inspect, list (filter + pagination) and cancel scrape jobs.
- **Declarative extraction** – `{"title": "h1"}` or
  `{"links": {"selector": "a", "attr": "href", "multiple": true}}`; selectors are validated
  when the job is submitted, so a typo is a `422`, not a failed job.
- **Async worker pool** – an in-process `asyncio` queue with a configurable number of workers;
  the URLs of one job are fetched concurrently, politeness is enforced per host.
- **Persistent and restart-safe** – jobs live in the database; after a restart, interrupted
  jobs are re-queued automatically.
- **Ethical by construction** – robots.txt, rate limits, backoff and size limits are enforced
  in the fetch path itself, not left to the caller (see [Ethics](#ethics)).
- **Typed and tested** – mypy `--strict`, Ruff, Black, 120 pytest tests with no real network
  access, ~98% branch coverage.
- **Container-ready** – multi-stage, non-root Docker image with a health check; Compose file
  with an optional Postgres profile.

## Ethics

Every request the service makes goes through `PoliteFetcher`
([`src/smart_scraper/scraping/fetcher.py`](src/smart_scraper/scraping/fetcher.py)). Concretely:

| Concern | What the code does | Default |
| --- | --- | --- |
| Identification | Sends an honest `User-Agent` with a link to this project; robots.txt groups are matched against it. | `SmartScraperBot/0.1.0 (+https://github.com/eshginfarzali/python-smart-scraper)` |
| robots.txt | Fetched once per origin (`scheme://host:port`) and evaluated with [Protego](https://github.com/scrapy/protego) (RFC 9309: `Allow`/`Disallow`, longest match, `*` and `$` wildcards). Disallowed URLs are never requested. | cached 1 h |
| robots.txt edge cases | `404`/`410`/other 4xx → no restrictions. `401`/`403` → treated as *disallow all*. `429`, `5xx`, timeouts and network errors → *disallow all* until re-checked. Only the first 512 KiB of the file are parsed. | errors cached 5 min |
| Redirects | Followed manually (max 5); **each hop** is checked against the target origin's robots.txt and rate limit. | 5 |
| Rate limiting | Minimum gap between request starts to the same host; concurrent callers are queued one gap apart. Different hosts do not block each other. The robots.txt request counts too. | 1 s |
| Crawl-delay | A robots.txt `Crawl-delay` for our agent raises the per-host gap (capped). | cap 30 s |
| Concurrency | At most N in-flight requests per host. | 2 |
| Timeouts | Connect timeout plus a total time budget per attempt that also covers slow-drip downloads. | 5 s / 15 s |
| Page size | `Content-Length` above the cap is rejected up front; bodies without it are streamed and aborted as soon as they exceed the cap. | 2 MiB |
| Retries | `429`, `500`, `502`, `503`, `504`, timeouts and connection errors are retried with exponential backoff (`base * 2^n`, capped, "equal jitter"). Other 4xx are never retried. | 3 retries, 1 s base, 60 s cap |
| Retry-After | Honoured on retryable responses (delta-seconds or HTTP-date). The delay is applied to the **whole host**, so parallel requests back off too. A `Retry-After` above the limit fails the URL instead of hammering earlier. | limit 120 s |
| Scope | Only absolute `http`/`https` URLs; at most 20 URLs per job. | |

What it deliberately does **not** do: render JavaScript, rotate proxies or user agents, solve
CAPTCHAs, or bypass logins/paywalls.

All of the above is covered by unit tests that run against mocked HTTP and a fake clock
(`tests/test_robots.py`, `tests/test_rate_limit.py`, `tests/test_retry.py`,
`tests/test_fetcher.py`).

## Tech stack

| Area | Choice |
| --- | --- |
| Language | Python 3.12+ (tested on 3.12 and 3.13) |
| Web framework | FastAPI + Uvicorn |
| HTTP client | httpx (async, streaming) |
| HTML parsing | selectolax (Lexbor backend, CSS selectors) |
| robots.txt | Protego |
| Models / config | Pydantic v2, pydantic-settings |
| Persistence | SQLAlchemy 2 async — SQLite via aiosqlite (default), Postgres via asyncpg (optional) |
| Quality | Ruff (lint), Black (format), mypy (strict) |
| Tests | pytest, pytest-asyncio, respx / `httpx.MockTransport`, pytest-cov |
| Delivery | Docker (multi-stage, non-root), Docker Compose, GitHub Actions |

## Architecture

```mermaid
flowchart LR
    client([API client]) -->|POST /jobs| api[FastAPI routes]
    api -->|insert job, status=queued| db[(SQLite / Postgres)]
    api -->|job id| queue[[asyncio.Queue]]
    queue --> workers[Worker pool<br/>N asyncio tasks]
    workers -->|queued → running → completed / failed| db
    workers --> scraper[Scraper]
    scraper --> fetcher[PoliteFetcher]
    fetcher --> robots[RobotsCache<br/>per origin, TTL]
    fetcher --> limiter[HostRateLimiter<br/>min delay · Crawl-delay · concurrency]
    robots --> limiter
    fetcher -->|retries + backoff · size cap · timeout| web((Websites))
    scraper --> extract[selectolax extractor<br/>runs in a thread]
    client -->|GET /jobs/id · POST /jobs/id/cancel| api
```

- **Job states:** `queued → running → completed | failed`, and `queued | running → cancelled`.
  A job is `completed` if at least one URL succeeded (failed URLs carry their own `error`),
  and `failed` if every URL failed (the job `error` explains why).
- **Guarded transitions:** every state change is a conditional `UPDATE … WHERE status IN (…)`,
  so a worker finishing a job can never overwrite a cancellation.
- **Cancellation:** a queued job is skipped when dequeued; a running job's task is cancelled,
  which aborts its in-flight HTTP requests.
- **Durability:** the database is the source of truth. On shutdown, running jobs stay
  `running`; on start-up they are moved back to `queued` and every queued job is re-enqueued.
- **Scope of the queue:** the queue lives inside the API process, which keeps deployment to a
  single container. Run **one** API process per database; scaling out needs an external
  broker (see [Roadmap](#roadmap)).
- **Schema management:** tables are created on start-up with `metadata.create_all`
  (idempotent, never destructive). There are no migrations yet.

Code map:

```
src/smart_scraper/
├── main.py            # app factory + lifespan wiring
├── config.py          # pydantic-settings
├── api/routes.py      # HTTP endpoints
├── schemas.py         # request/response models
├── worker.py          # JobQueue (worker pool) + Scraper
├── repository.py      # job persistence, guarded state transitions
├── db.py              # SQLAlchemy models, engine, init
└── scraping/
    ├── fetcher.py     # PoliteFetcher: robots → rate limit → request → retry
    ├── robots.py      # RobotsCache
    ├── rate_limit.py  # HostRateLimiter
    ├── retry.py       # backoff + Retry-After parsing
    ├── extract.py     # CSS-selector extraction
    ├── clock.py       # injectable clock (fake in tests)
    └── errors.py
```

## Getting started

### Local (Python 3.12+)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # add ",postgres" for the asyncpg driver
cp .env.example .env             # optional: every setting has a sensible default
uvicorn --factory smart_scraper.main:create_app --port 8400
```

Open <http://localhost:8400/docs> for the interactive OpenAPI UI. With the default
`DATABASE_URL` the SQLite file is created at `./data/scraper.db`.

### Docker Compose

```bash
docker compose up --build -d     # API on http://localhost:8400, SQLite in a named volume
curl http://localhost:8400/health
docker compose down              # add -v to delete the data volume
```

The host port defaults to `8400`; change it with `HOST_PORT=9000 docker compose up`.

With Postgres instead of SQLite:

```bash
DATABASE_URL=postgresql+asyncpg://scraper:scraper@postgres:5432/scraper \
  docker compose --profile postgres up --build -d
docker compose --profile postgres down
```

The bundled Postgres password defaults to `scraper` for local use; set `POSTGRES_PASSWORD`
(and the matching `DATABASE_URL`) for anything shared.

### Configuration

All settings are environment variables (or `.env` entries) — see
[`.env.example`](.env.example) for the full list with defaults. `DATABASE_URL` is read as-is;
everything else uses the `SCRAPER_` prefix, e.g. `SCRAPER_WORKER_COUNT=4`,
`SCRAPER_MIN_HOST_DELAY_SECONDS=2`.

## API examples

Submit a job (returns `202 Accepted` and the queued job):

```bash
curl -s -X POST http://localhost:8400/jobs \
  -H 'Content-Type: application/json' \
  -d '{
        "urls": ["https://example.com/"],
        "fields": {
          "title": "title",
          "summary": "p",
          "links": {"selector": "a", "attr": "href", "multiple": true}
        }
      }'
```

```json
{
  "id": "f4c3a25c-c30c-4aee-ba4f-0668505ba359",
  "status": "queued",
  "urls": ["https://example.com/"],
  "fields": {
    "title": {"selector": "title", "attr": null, "multiple": false},
    "summary": {"selector": "p", "attr": null, "multiple": false},
    "links": {"selector": "a", "attr": "href", "multiple": true}
  },
  "results": [],
  "error": null,
  "created_at": "2026-10-23T21:43:34.250997Z",
  "started_at": null,
  "finished_at": null
}
```

Poll for the result:

```bash
curl -s http://localhost:8400/jobs/f4c3a25c-c30c-4aee-ba4f-0668505ba359
```

```json
{
  "id": "f4c3a25c-c30c-4aee-ba4f-0668505ba359",
  "status": "completed",
  "results": [
    {
      "url": "https://example.com/",
      "ok": true,
      "final_url": "https://example.com/",
      "status_code": 200,
      "data": {
        "title": "Example Domain",
        "summary": "This domain is for use in documentation examples without needing permission. …",
        "links": []
      },
      "error": null
    }
  ],
  "error": null,
  "...": "..."
}
```

A URL blocked by robots.txt fails with an explicit reason:

```json
{
  "status": "failed",
  "error": "Blocked by robots.txt: https://shop.example/private/orders is disallowed for 'SmartScraperBot/0.1.0 (+https://github.com/eshginfarzali/python-smart-scraper)' (robots.txt rules)",
  "...": "..."
}
```

List, filter and paginate (newest first):

```bash
curl -s 'http://localhost:8400/jobs?status=completed&limit=10&offset=0'
```

Cancel a queued or running job (`409` if it already finished, `404` if unknown):

```bash
curl -s -X POST http://localhost:8400/jobs/f4c3a25c-c30c-4aee-ba4f-0668505ba359/cancel
```

Health check (`503` if the database is unreachable):

```bash
curl -s http://localhost:8400/health
# {"status":"ok","version":"0.1.0","database":"ok","queue_size":0,"workers":2}
```

Validation errors return `422` with the offending location, e.g. an invalid selector
(`{"t": "p["}`), a non-HTTP URL, more than 20 URLs, an empty `fields` object or a field name
that is not a valid identifier.

## Scripts / commands

| Task | Command |
| --- | --- |
| Install (dev) | `pip install -e ".[dev]"` |
| Run the API | `uvicorn --factory smart_scraper.main:create_app --port 8400` |
| Lint | `ruff check .` |
| Format | `black .` (CI runs `black --check .`) |
| Type-check | `mypy` |
| Tests + coverage | `pytest --cov --cov-report=term-missing --cov-report=xml` |
| Docker image | `docker build -t python-smart-scraper .` |
| Compose stack | `docker compose up --build -d` / `docker compose down` |

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs exactly these lint, format,
type and test commands on Python 3.12, then builds the Docker image and probes `/health` in a
running container.

## Testing

```bash
pytest --cov --cov-report=term-missing
```

The suite never touches the network: HTTP is mocked at the transport layer with
[respx](https://lundberg.github.io/respx/) and `httpx.MockTransport`, and timing is
controlled by a fake clock so rate-limit and backoff tests are deterministic and instant.

| File | Covers |
| --- | --- |
| `tests/test_robots.py` | allow/deny per user-agent group, wildcards, caching + TTL expiry, 4xx/5xx/network handling, redirects, size cap |
| `tests/test_rate_limit.py` | min delay, partial waits, per-host independence, queued reservations, Crawl-delay interval, penalties, concurrency cap |
| `tests/test_retry.py` | `Retry-After` parsing (seconds, HTTP-date, invalid), backoff growth/cap, jitter bounds |
| `tests/test_fetcher.py` | robots enforcement, delays, Crawl-delay cap, 5xx backoff, 429 + Retry-After, give-up paths, timeouts, size limits, redirects |
| `tests/test_extract.py` | text/attribute/multiple extraction from fixture HTML, missing values, invalid selectors |
| `tests/test_api.py` | job lifecycle through the API (create → completed), robots-disallowed → failed, partial success, retries, validation errors, list/filter/pagination, cancellation, restart recovery |
| `tests/test_worker.py` | worker survives unexpected errors, shutdown/resume semantics |
| `tests/test_config.py` | defaults and environment overrides |

## Roadmap

- Optional JavaScript rendering with Playwright for client-side-rendered pages.
- Alembic migrations instead of `create_all`.
- External broker (e.g. Redis with arq) so several API/worker processes can share one queue.
- SSRF protection: refuse URLs that resolve to private, loopback or link-local addresses.
  **Until then, do not expose the service to untrusted users.**
- Authentication / API keys and per-client quotas.
- Optional resolution of relative `href`/`src` values to absolute URLs.
- Webhook callbacks when a job finishes; Prometheus metrics.

## License

[MIT](LICENSE) © 2026 Eshgin Farzaliyev
