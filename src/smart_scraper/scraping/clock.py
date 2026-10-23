"""Time abstraction so that rate limiting and backoff can be tested without real waiting."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """Monotonic time source plus an awaitable sleep."""

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock: :func:`time.monotonic` and :func:`asyncio.sleep`."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
