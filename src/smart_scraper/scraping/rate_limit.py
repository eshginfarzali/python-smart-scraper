"""Per-host politeness: a minimum delay between requests and a concurrency cap."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from smart_scraper.scraping.clock import Clock


@dataclass
class _HostState:
    semaphore: asyncio.Semaphore
    # Earliest monotonic time at which the next request to this host may start.
    next_start: float = field(default=-math.inf)


class HostRateLimiter:
    """Spaces out requests to the same host and caps how many are in flight.

    Start times are *reserved* atomically (there is no ``await`` between reading and
    writing ``next_start``), so concurrent callers queue up one ``interval`` apart
    instead of all waking at once. Different hosts never wait for each other.
    """

    def __init__(self, *, min_delay: float, max_concurrency: int, clock: Clock) -> None:
        if min_delay < 0:
            raise ValueError("min_delay must be >= 0")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._min_delay = min_delay
        self._max_concurrency = max_concurrency
        self._clock = clock
        self._hosts: dict[str, _HostState] = {}

    def _state(self, host: str) -> _HostState:
        state = self._hosts.get(host)
        if state is None:
            state = _HostState(semaphore=asyncio.Semaphore(self._max_concurrency))
            self._hosts[host] = state
        return state

    def reserve(self, host: str, interval: float | None = None) -> float:
        """Reserve the next start slot for ``host`` and return how long to wait for it."""
        gap = max(self._min_delay, interval or 0.0)
        state = self._state(host)
        now = self._clock.monotonic()
        start = max(now, state.next_start)
        state.next_start = start + gap
        return start - now

    def penalize(self, host: str, seconds: float) -> None:
        """Push back every future request to ``host`` by at least ``seconds`` from now.

        Used for ``Retry-After`` and retry backoff so that *all* requests to an overloaded
        host slow down, not just the one that got the 429/5xx.
        """
        if seconds <= 0:
            return
        state = self._state(host)
        state.next_start = max(state.next_start, self._clock.monotonic() + seconds)

    @asynccontextmanager
    async def slot(self, host: str, interval: float | None = None) -> AsyncIterator[None]:
        """Wait for permission to send one request to ``host``; hold it for the block."""
        state = self._state(host)
        async with state.semaphore:
            wait = self.reserve(host, interval)
            if wait > 0:
                await self._clock.sleep(wait)
            yield
