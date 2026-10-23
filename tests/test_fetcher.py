from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from smart_scraper.scraping.errors import (
    FetchFailedError,
    InvalidURLError,
    PageTooLargeError,
    RobotsDisallowedError,
    TooManyRedirectsError,
)
from smart_scraper.scraping.fetcher import PoliteFetcher
from smart_scraper.scraping.rate_limit import HostRateLimiter
from smart_scraper.scraping.retry import no_jitter
from smart_scraper.scraping.robots import RobotsCache
from tests.helpers import USER_AGENT, FakeClock, FetcherFactory, make_config

SITE = "https://shop.example"
PAGE = f"{SITE}/teas"


@pytest.fixture(autouse=True)
def allow_all_robots(router: respx.Router) -> None:
    router.get(f"{SITE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /private\n")


async def test_successful_fetch(router: respx.Router, make_fetcher: FetcherFactory) -> None:
    route = router.get(PAGE).respond(
        200, html="<h1>Teas</h1>", headers={"Content-Type": "text/html; charset=utf-8"}
    )
    result = await make_fetcher().fetch(PAGE)

    assert result.status_code == 200
    assert result.text == "<h1>Teas</h1>"
    assert result.final_url == PAGE
    assert result.content_type == "text/html; charset=utf-8"
    request = route.calls.last.request
    assert request.headers["user-agent"] == USER_AGENT
    assert "text/html" in request.headers["accept"]


async def test_body_is_decoded_with_declared_charset(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    router.get(PAGE).respond(
        200,
        content="Çay və qəhvə".encode("iso-8859-9", errors="replace"),
        headers={"Content-Type": "text/html; charset=iso-8859-9"},
    )
    result = await make_fetcher().fetch(PAGE)
    assert result.text.startswith("Çay")


async def test_unknown_charset_falls_back_to_utf8(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    router.get(PAGE).respond(
        200, content="héllo".encode(), headers={"Content-Type": "text/html; charset=bogus"}
    )
    assert (await make_fetcher().fetch(PAGE)).text == "héllo"


async def test_robots_disallowed_url_is_never_requested(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    page = router.get(f"{SITE}/private/orders").respond(200)
    with pytest.raises(RobotsDisallowedError, match=r"Blocked by robots\.txt"):
        await make_fetcher().fetch(f"{SITE}/private/orders")
    assert page.call_count == 0


async def test_unsupported_scheme_is_rejected(make_fetcher: FetcherFactory) -> None:
    with pytest.raises(InvalidURLError):
        await make_fetcher().fetch("file:///etc/passwd")


async def test_min_delay_is_applied_between_requests_to_the_same_host(
    router: respx.Router, make_fetcher: FetcherFactory, clock: FakeClock
) -> None:
    router.get(PAGE).respond(200, html="ok")
    fetcher = make_fetcher(min_delay=2.0)
    await fetcher.fetch(PAGE)  # robots.txt at t=0, page waits 2s
    await fetcher.fetch(PAGE)  # waits another 2s
    assert clock.sleeps == [2.0, 2.0]


async def test_robots_crawl_delay_is_honoured_and_capped(
    router: respx.Router, make_fetcher: FetcherFactory, clock: FakeClock
) -> None:
    router.get("https://slow.example/robots.txt").respond(
        200, text="User-agent: *\nCrawl-delay: 5\n"
    )
    router.get("https://slower.example/robots.txt").respond(
        200, text="User-agent: *\nCrawl-delay: 600\n"
    )
    router.get(url__regex=r"https://slow(er)?\.example/page").respond(200, html="ok")
    fetcher = make_fetcher(min_delay=1.0, max_crawl_delay=10.0)

    await fetcher.fetch("https://slow.example/page")
    await fetcher.fetch("https://slow.example/page")
    # robots.txt -> page: min delay (1s); page -> page: Crawl-delay (5s).
    assert clock.sleeps == [1.0, 5.0]

    clock.sleeps.clear()
    await fetcher.fetch("https://slower.example/page")
    await fetcher.fetch("https://slower.example/page")
    assert clock.sleeps == [1.0, 10.0]  # 600s capped at max_crawl_delay


async def test_retries_5xx_with_exponential_backoff(
    router: respx.Router, make_fetcher: FetcherFactory, clock: FakeClock
) -> None:
    route = router.get(PAGE)
    route.side_effect = [httpx.Response(503), httpx.Response(502), httpx.Response(200, html="ok")]
    result = await make_fetcher(backoff_base=0.5).fetch(PAGE)
    assert result.text == "ok"
    assert route.call_count == 3
    assert clock.sleeps == [0.5, 1.0]


async def test_429_honours_retry_after(
    router: respx.Router, make_fetcher: FetcherFactory, clock: FakeClock
) -> None:
    route = router.get(PAGE)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, html="ok"),
    ]
    await make_fetcher(backoff_base=0.5).fetch(PAGE)
    assert clock.sleeps == [7.0]


async def test_retry_after_beyond_limit_fails_fast(
    router: respx.Router, make_fetcher: FetcherFactory, clock: FakeClock
) -> None:
    route = router.get(PAGE).respond(429, headers={"Retry-After": "3600"})
    with pytest.raises(
        FetchFailedError, match="Retry-After of 3600s exceeds the 60s limit"
    ) as info:
        await make_fetcher().fetch(PAGE)
    assert info.value.status_code == 429
    assert route.call_count == 1
    assert clock.sleeps == []


async def test_gives_up_after_max_retries(
    router: respx.Router, make_fetcher: FetcherFactory, clock: FakeClock
) -> None:
    route = router.get(PAGE).respond(500)
    with pytest.raises(FetchFailedError, match=r"HTTP 500 .* after 3 attempt\(s\)") as info:
        await make_fetcher(max_retries=2).fetch(PAGE)
    assert info.value.status_code == 500
    assert route.call_count == 3
    assert clock.sleeps == [1.0, 2.0]


async def test_client_errors_are_not_retried(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    route = router.get(PAGE).respond(404)
    with pytest.raises(FetchFailedError, match="HTTP 404") as info:
        await make_fetcher().fetch(PAGE)
    assert info.value.status_code == 404
    assert route.call_count == 1


async def test_transport_timeouts_are_retried_then_reported(
    router: respx.Router, make_fetcher: FetcherFactory, clock: FakeClock
) -> None:
    route = router.get(PAGE)
    route.side_effect = httpx.ReadTimeout("read timed out")
    with pytest.raises(FetchFailedError, match=r"timed out after 2 attempt\(s\)"):
        await make_fetcher(max_retries=1).fetch(PAGE)
    assert route.call_count == 2
    assert clock.sleeps == [1.0]


async def test_connection_errors_are_retried(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    route = router.get(PAGE)
    route.side_effect = [httpx.ConnectError("refused"), httpx.Response(200, html="ok")]
    assert (await make_fetcher().fetch(PAGE)).text == "ok"


async def test_total_timeout_bounds_slow_downloads(clock: FakeClock) -> None:
    async def trickle() -> AsyncIterator[bytes]:
        yield b"<html>"
        await asyncio.sleep(5)
        yield b"</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=trickle())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        limiter = HostRateLimiter(min_delay=0, max_concurrency=1, clock=clock)
        robots = RobotsCache(
            client=client, limiter=limiter, clock=clock, user_agent=USER_AGENT, ttl=60, error_ttl=60
        )
        fetcher = PoliteFetcher(
            client=client,
            robots=robots,
            limiter=limiter,
            config=make_config(total_timeout=0.05, max_retries=0),
            jitter=no_jitter,
        )
        with pytest.raises(FetchFailedError, match="timed out"):
            await fetcher.fetch(PAGE)


async def test_declared_content_length_over_limit_is_rejected(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    router.get(PAGE).respond(200, content=b"x" * 2048)
    with pytest.raises(PageTooLargeError, match="1024-byte page size limit"):
        await make_fetcher(max_page_bytes=1024).fetch(PAGE)


async def test_streamed_body_over_limit_is_aborted(clock: FakeClock) -> None:
    chunks_sent = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal chunks_sent
        for _ in range(1000):
            chunks_sent += 1
            yield b"x" * 512

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=endless())  # chunked: no Content-Length

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        limiter = HostRateLimiter(min_delay=0, max_concurrency=1, clock=clock)
        robots = RobotsCache(
            client=client, limiter=limiter, clock=clock, user_agent=USER_AGENT, ttl=60, error_ttl=60
        )
        fetcher = PoliteFetcher(
            client=client,
            robots=robots,
            limiter=limiter,
            config=make_config(max_page_bytes=2048),
        )
        with pytest.raises(PageTooLargeError):
            await fetcher.fetch(PAGE)
    assert chunks_sent < 10  # stopped reading early instead of downloading everything


async def test_redirects_are_followed_and_reported(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    router.get(PAGE).respond(301, headers={"Location": "/teas/"})
    router.get(f"{PAGE}/").respond(200, html="final")
    result = await make_fetcher().fetch(PAGE)
    assert result.url == PAGE
    assert result.final_url == f"{PAGE}/"
    assert result.text == "final"


async def test_redirect_target_is_checked_against_its_own_robots(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    router.get(PAGE).respond(302, headers={"Location": "https://other.example/x"})
    router.get("https://other.example/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    target = router.get("https://other.example/x").respond(200)
    with pytest.raises(RobotsDisallowedError, match=r"other\.example"):
        await make_fetcher().fetch(PAGE)
    assert target.call_count == 0


async def test_redirect_loops_are_bounded(
    router: respx.Router, make_fetcher: FetcherFactory
) -> None:
    router.get(f"{SITE}/a").respond(302, headers={"Location": "/b"})
    router.get(f"{SITE}/b").respond(302, headers={"Location": "/a"})
    with pytest.raises(TooManyRedirectsError, match="More than 3 redirects"):
        await make_fetcher(max_redirects=3).fetch(f"{SITE}/a")
