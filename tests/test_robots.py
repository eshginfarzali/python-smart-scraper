from __future__ import annotations

import httpx
import pytest
import respx

from smart_scraper.scraping.errors import InvalidURLError, RobotsDisallowedError
from smart_scraper.scraping.rate_limit import HostRateLimiter
from smart_scraper.scraping.robots import ROBOTS_MAX_BYTES, RobotsCache, origin_of
from tests.helpers import USER_AGENT, FakeClock

ROBOTS = """\
User-agent: *
Disallow: /admin

User-agent: SmartScraperBot
Disallow: /private
Disallow: /*.pdf$
Allow: /private/press
Crawl-delay: 4
"""


@pytest.fixture
def robots(client: httpx.AsyncClient, clock: FakeClock) -> RobotsCache:
    limiter = HostRateLimiter(min_delay=0, max_concurrency=1, clock=clock)
    return RobotsCache(
        client=client, limiter=limiter, clock=clock, user_agent=USER_AGENT, ttl=100, error_ttl=10
    )


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://shop.example/products/1", True),
        ("https://shop.example/private/orders", False),
        ("https://shop.example/private/press/2026", True),  # longest match wins
        ("https://shop.example/files/report.pdf", False),  # wildcard + end anchor
        ("https://shop.example/files/report.pdf?v=2", True),
        # Our bot has its own group, so the "*" group's /admin rule does not apply to it.
        ("https://shop.example/admin", True),
    ],
)
async def test_rules_are_evaluated_for_our_user_agent(
    router: respx.Router, robots: RobotsCache, url: str, allowed: bool
) -> None:
    router.get("https://shop.example/robots.txt").respond(200, text=ROBOTS)
    policy = await robots.policy_for(url)
    assert policy.can_fetch(url, USER_AGENT) is allowed


async def test_check_raises_with_a_clear_reason(router: respx.Router, robots: RobotsCache) -> None:
    router.get("https://shop.example/robots.txt").respond(200, text=ROBOTS)
    with pytest.raises(RobotsDisallowedError, match=r"Blocked by robots\.txt.*/private/x"):
        await robots.check("https://shop.example/private/x")


async def test_crawl_delay_is_exposed(router: respx.Router, robots: RobotsCache) -> None:
    router.get("https://shop.example/robots.txt").respond(200, text=ROBOTS)
    policy = await robots.check("https://shop.example/")
    assert policy.crawl_delay(USER_AGENT) == 4.0


async def test_robots_is_fetched_once_per_origin_and_cached(
    router: respx.Router, robots: RobotsCache, clock: FakeClock
) -> None:
    route = router.get("https://shop.example/robots.txt").respond(200, text=ROBOTS)
    await robots.check("https://shop.example/a")
    await robots.check("https://shop.example/b")
    await robots.check("https://SHOP.example/c")
    assert route.call_count == 1

    clock.advance(101)  # past the TTL
    await robots.check("https://shop.example/d")
    assert route.call_count == 2


async def test_robots_request_uses_our_user_agent(
    router: respx.Router, robots: RobotsCache
) -> None:
    route = router.get("https://shop.example/robots.txt").respond(200, text=ROBOTS)
    await robots.check("https://shop.example/")
    assert route.calls.last.request.headers["user-agent"] == USER_AGENT


async def test_origins_are_cached_separately(router: respx.Router, robots: RobotsCache) -> None:
    router.get("https://a.example/robots.txt").respond(200, text="User-agent: *\nDisallow: /")
    router.get("https://b.example/robots.txt").respond(404)
    assert not (await robots.policy_for("https://a.example/x")).can_fetch(
        "https://a.example/x", USER_AGENT
    )
    assert (await robots.policy_for("https://b.example/x")).can_fetch(
        "https://b.example/x", USER_AGENT
    )


@pytest.mark.parametrize(
    ("status", "allowed", "reason"),
    [
        (404, True, "no robots.txt (HTTP 404)"),
        (410, True, "no robots.txt (HTTP 410)"),
        (401, False, "robots.txt access denied (HTTP 401)"),
        (403, False, "robots.txt access denied (HTTP 403)"),
        (429, False, "robots.txt unreachable (HTTP 429)"),
        (500, False, "robots.txt unreachable (HTTP 500)"),
        (503, False, "robots.txt unreachable (HTTP 503)"),
    ],
)
async def test_status_code_handling(
    router: respx.Router, robots: RobotsCache, status: int, allowed: bool, reason: str
) -> None:
    router.get("https://shop.example/robots.txt").respond(status)
    policy = await robots.policy_for("https://shop.example/page")
    assert policy.can_fetch("https://shop.example/page", USER_AGENT) is allowed
    assert policy.reason == reason
    assert policy.crawl_delay(USER_AGENT) is None


async def test_network_error_disallows_and_is_retried_after_error_ttl(
    router: respx.Router, robots: RobotsCache, clock: FakeClock
) -> None:
    route = router.get("https://shop.example/robots.txt")
    route.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(RobotsDisallowedError, match=r"robots\.txt unreachable: ConnectError"):
        await robots.check("https://shop.example/page")

    clock.advance(5)  # still within error_ttl=10: served from cache
    with pytest.raises(RobotsDisallowedError):
        await robots.check("https://shop.example/page")
    assert route.call_count == 1

    clock.advance(6)
    route.side_effect = None
    route.return_value = httpx.Response(404)
    await robots.check("https://shop.example/page")
    assert route.call_count == 2


async def test_redirected_robots_is_followed(router: respx.Router, robots: RobotsCache) -> None:
    router.get("http://shop.example/robots.txt").respond(
        301, headers={"Location": "https://shop.example/robots.txt"}
    )
    router.get("https://shop.example/robots.txt").respond(200, text="User-agent: *\nDisallow: /")
    with pytest.raises(RobotsDisallowedError):
        await robots.check("http://shop.example/page")


async def test_oversized_robots_is_truncated(router: respx.Router, robots: RobotsCache) -> None:
    padding = "# " + "x" * ROBOTS_MAX_BYTES + "\n"
    router.get("https://shop.example/robots.txt").respond(
        200, text=f"User-agent: *\nDisallow: /early\n{padding}Disallow: /late\n"
    )
    policy = await robots.policy_for("https://shop.example/")
    assert not policy.can_fetch("https://shop.example/early", USER_AGENT)
    assert policy.can_fetch("https://shop.example/late", USER_AGENT)


@pytest.mark.parametrize("url", ["ftp://shop.example/file", "/relative/path", "https://"])
def test_origin_of_rejects_unsupported_urls(url: str) -> None:
    with pytest.raises(InvalidURLError):
        origin_of(url)


def test_origin_of_normalises_host() -> None:
    assert origin_of("https://Shop.Example:8443/a?b=c") == "https://shop.example:8443"
