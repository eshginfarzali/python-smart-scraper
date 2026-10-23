from __future__ import annotations

import asyncio
import itertools

import pytest

from smart_scraper.scraping.rate_limit import HostRateLimiter
from tests.helpers import FakeClock


def make_limiter(
    clock: FakeClock, min_delay: float = 1.0, max_concurrency: int = 2
) -> HostRateLimiter:
    return HostRateLimiter(min_delay=min_delay, max_concurrency=max_concurrency, clock=clock)


async def test_first_request_to_a_host_does_not_wait(clock: FakeClock) -> None:
    limiter = make_limiter(clock)
    async with limiter.slot("example.com"):
        pass
    assert clock.sleeps == []


async def test_consecutive_requests_are_spaced_by_min_delay(clock: FakeClock) -> None:
    limiter = make_limiter(clock, min_delay=1.5)
    starts: list[float] = []
    for _ in range(3):
        async with limiter.slot("example.com"):
            starts.append(clock.monotonic())
    assert clock.sleeps == [1.5, 1.5]
    assert [b - a for a, b in itertools.pairwise(starts)] == [1.5, 1.5]


async def test_no_wait_when_enough_time_has_passed(clock: FakeClock) -> None:
    limiter = make_limiter(clock, min_delay=1.0)
    async with limiter.slot("example.com"):
        pass
    clock.advance(5)
    async with limiter.slot("example.com"):
        pass
    assert clock.sleeps == []


async def test_partial_wait_when_some_time_has_passed(clock: FakeClock) -> None:
    limiter = make_limiter(clock, min_delay=2.0)
    async with limiter.slot("example.com"):
        pass
    clock.advance(0.5)
    async with limiter.slot("example.com"):
        pass
    assert clock.sleeps == [1.5]


async def test_hosts_are_limited_independently(clock: FakeClock) -> None:
    limiter = make_limiter(clock, min_delay=10.0)
    for host in ("a.example", "b.example", "c.example"):
        async with limiter.slot(host):
            pass
    assert clock.sleeps == []


def test_concurrent_reservations_are_queued_one_interval_apart(clock: FakeClock) -> None:
    limiter = make_limiter(clock, min_delay=1.0)
    waits = [limiter.reserve("example.com") for _ in range(4)]
    assert waits == [0.0, 1.0, 2.0, 3.0]


def test_interval_larger_than_min_delay_wins(clock: FakeClock) -> None:
    limiter = make_limiter(clock, min_delay=1.0)
    assert limiter.reserve("example.com", interval=5.0) == 0.0
    assert limiter.reserve("example.com", interval=5.0) == 5.0
    # A smaller interval never undercuts the configured minimum.
    assert limiter.reserve("example.com", interval=0.1) == 10.0
    assert limiter.reserve("example.com") == 11.0


async def test_penalize_delays_the_next_request(clock: FakeClock) -> None:
    limiter = make_limiter(clock, min_delay=0.0)
    async with limiter.slot("example.com"):
        pass
    limiter.penalize("example.com", 7.0)
    limiter.penalize("example.com", 0)  # no-op
    async with limiter.slot("example.com"):
        pass
    assert clock.sleeps == [7.0]


async def test_concurrency_is_capped_per_host(clock: FakeClock) -> None:
    limiter = make_limiter(clock, min_delay=0.0, max_concurrency=2)
    release = asyncio.Event()
    active = 0
    peak = 0

    async def request() -> None:
        nonlocal active, peak
        async with limiter.slot("example.com"):
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1

    tasks = [asyncio.create_task(request()) for _ in range(5)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert active == 2
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


@pytest.mark.parametrize(("min_delay", "max_concurrency"), [(-1.0, 1), (0.0, 0)])
def test_rejects_invalid_configuration(
    clock: FakeClock, min_delay: float, max_concurrency: int
) -> None:
    with pytest.raises(ValueError, match="must be"):
        HostRateLimiter(min_delay=min_delay, max_concurrency=max_concurrency, clock=clock)
