"""HTTP fetching with robots.txt, per-host rate limiting, size caps, timeouts and retries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from smart_scraper.scraping.errors import (
    FetchFailedError,
    PageTooLargeError,
    TooManyRedirectsError,
)
from smart_scraper.scraping.rate_limit import HostRateLimiter
from smart_scraper.scraping.retry import (
    Jitter,
    backoff_delay,
    equal_jitter,
    is_retryable_status,
    parse_retry_after,
)
from smart_scraper.scraping.robots import RobotsCache, origin_of

logger = logging.getLogger(__name__)

ACCEPT_HEADER = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"


@dataclass(frozen=True, slots=True)
class FetchConfig:
    user_agent: str
    total_timeout: float
    max_page_bytes: int
    max_redirects: int
    max_retries: int
    backoff_base: float
    backoff_max: float
    max_retry_after: float
    max_crawl_delay: float


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str | None
    text: str


@dataclass(frozen=True, slots=True)
class _Redirect:
    location: str


@dataclass(frozen=True, slots=True)
class _Retryable:
    status_code: int
    retry_after: float | None


class PoliteFetcher:
    """Fetches one URL at a time while enforcing the scraper's etiquette.

    For every request (including each redirect hop) the fetcher:

    1. checks robots.txt for the target origin (cached per origin),
    2. waits for a per-host slot (minimum delay, robots ``Crawl-delay``, concurrency cap),
    3. sends the request with a total time budget and rejects bodies over the size cap,
    4. retries 429/5xx/transport errors with exponential backoff, honouring ``Retry-After``.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        robots: RobotsCache,
        limiter: HostRateLimiter,
        config: FetchConfig,
        jitter: Jitter = equal_jitter,
    ) -> None:
        self._client = client
        self._robots = robots
        self._limiter = limiter
        self._config = config
        self._jitter = jitter

    async def fetch(self, url: str) -> FetchResult:
        current = url
        for _ in range(self._config.max_redirects + 1):
            outcome = await self._fetch_hop(current)
            if isinstance(outcome, _Redirect):
                current = outcome.location
                continue
            return FetchResult(
                url=url,
                final_url=outcome.final_url,
                status_code=outcome.status_code,
                content_type=outcome.content_type,
                text=outcome.text,
            )
        raise TooManyRedirectsError(
            f"More than {self._config.max_redirects} redirects while fetching {url}"
        )

    async def _fetch_hop(self, url: str) -> FetchResult | _Redirect:
        origin_of(url)  # validates scheme/host
        policy = await self._robots.check(url)
        crawl_delay = policy.crawl_delay(self._config.user_agent)
        interval = min(crawl_delay, self._config.max_crawl_delay) if crawl_delay else None
        host = urlsplit(url).netloc.lower()

        attempt = 0
        while True:
            try:
                async with self._limiter.slot(host, interval):
                    async with asyncio.timeout(self._config.total_timeout):
                        outcome = await self._request(url)
            except (httpx.TransportError, TimeoutError) as exc:
                reason = "timed out" if _is_timeout(exc) else f"failed ({type(exc).__name__})"
                if attempt >= self._config.max_retries:
                    raise FetchFailedError(
                        f"Request to {url} {reason} after {attempt + 1} attempt(s)"
                    ) from exc
                wait = self._backoff(attempt)
                logger.info("Request to %s %s; retrying in %.2fs", url, reason, wait)
                self._limiter.penalize(host, wait)
                attempt += 1
                continue

            if not isinstance(outcome, _Retryable):
                return outcome

            status = outcome.status_code
            if attempt >= self._config.max_retries:
                raise FetchFailedError(
                    f"HTTP {status} from {url} after {attempt + 1} attempt(s)", status
                )
            if outcome.retry_after is not None:
                if outcome.retry_after > self._config.max_retry_after:
                    raise FetchFailedError(
                        f"HTTP {status} from {url}: Retry-After of {outcome.retry_after:.0f}s "
                        f"exceeds the {self._config.max_retry_after:.0f}s limit",
                        status,
                    )
                wait = outcome.retry_after
            else:
                wait = self._backoff(attempt)
            logger.info("HTTP %s from %s; retrying in %.2fs", status, url, wait)
            # Penalise the whole host so concurrent requests to it back off as well.
            self._limiter.penalize(host, wait)
            attempt += 1

    def _backoff(self, attempt: int) -> float:
        return backoff_delay(
            attempt,
            base=self._config.backoff_base,
            cap=self._config.backoff_max,
            jitter=self._jitter,
        )

    async def _request(self, url: str) -> FetchResult | _Redirect | _Retryable:
        headers = {"User-Agent": self._config.user_agent, "Accept": ACCEPT_HEADER}
        async with self._client.stream(
            "GET", url, headers=headers, follow_redirects=False
        ) as response:
            status = response.status_code
            if response.is_redirect:
                return _Redirect(urljoin(url, response.headers["location"]))
            if is_retryable_status(status):
                return _Retryable(status, parse_retry_after(response.headers.get("retry-after")))
            if status >= 400:
                raise FetchFailedError(f"HTTP {status} from {url}", status)

            limit = self._config.max_page_bytes
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > limit:
                raise PageTooLargeError(url, limit)

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise PageTooLargeError(url, limit)
                chunks.append(chunk)

            text = _decode(b"".join(chunks), response.charset_encoding)
            return FetchResult(
                url=url,
                final_url=str(response.url),
                status_code=status,
                content_type=response.headers.get("content-type"),
                text=text,
            )


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:  # unknown charset label in Content-Type
        return body.decode("utf-8", errors="replace")


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError | httpx.TimeoutException)
