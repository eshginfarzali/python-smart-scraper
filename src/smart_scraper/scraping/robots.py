"""robots.txt fetching, caching and evaluation (RFC 9309 semantics via Protego)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from protego import Protego

from smart_scraper.scraping.clock import Clock
from smart_scraper.scraping.errors import InvalidURLError, RobotsDisallowedError
from smart_scraper.scraping.rate_limit import HostRateLimiter

logger = logging.getLogger(__name__)

# RFC 9309 asks crawlers to parse at least 500 KiB of a robots.txt file.
ROBOTS_MAX_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """The outcome of fetching one origin's robots.txt."""

    rules: Protego | None
    allow_all: bool
    reason: str

    def can_fetch(self, url: str, user_agent: str) -> bool:
        if self.rules is None:
            return self.allow_all
        return self.rules.can_fetch(url, user_agent)

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.rules is None:
            return None
        return self.rules.crawl_delay(user_agent)


@dataclass(slots=True)
class _CacheEntry:
    policy: RobotsPolicy
    expires_at: float


def origin_of(url: str) -> str:
    """Return ``scheme://host[:port]`` for an absolute http(s) URL."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise InvalidURLError(f"Unsupported URL (only absolute http/https URLs): {url}")
    return f"{parts.scheme}://{parts.netloc.lower()}"


class RobotsCache:
    """Fetches robots.txt once per origin and caches the parsed policy.

    Status handling:

    * ``2xx`` - rules are parsed and enforced.
    * ``401``/``403`` - treated as *disallow everything* (conservative).
    * other ``4xx`` (e.g. 404) - no robots.txt, everything is allowed.
    * ``429``/``5xx``/network errors - *disallow everything* for ``error_ttl`` seconds,
      as RFC 9309 recommends for an unreachable robots.txt.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        limiter: HostRateLimiter,
        clock: Clock,
        user_agent: str,
        ttl: float,
        error_ttl: float,
    ) -> None:
        self._client = client
        self._limiter = limiter
        self._clock = clock
        self._user_agent = user_agent
        self._ttl = ttl
        self._error_ttl = error_ttl
        self._entries: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def policy_for(self, url: str) -> RobotsPolicy:
        origin = origin_of(url)
        entry = self._entries.get(origin)
        if entry is not None and entry.expires_at > self._clock.monotonic():
            return entry.policy

        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            # Another task may have refreshed the entry while we waited for the lock.
            entry = self._entries.get(origin)
            if entry is not None and entry.expires_at > self._clock.monotonic():
                return entry.policy
            policy, ttl = await self._fetch(origin)
            self._entries[origin] = _CacheEntry(policy, self._clock.monotonic() + ttl)
            return policy

    async def check(self, url: str) -> RobotsPolicy:
        """Return the policy for ``url``'s origin, raising if ``url`` is disallowed."""
        policy = await self.policy_for(url)
        if not policy.can_fetch(url, self._user_agent):
            raise RobotsDisallowedError(url, self._user_agent, policy.reason)
        return policy

    async def _fetch(self, origin: str) -> tuple[RobotsPolicy, float]:
        robots_url = f"{origin}/robots.txt"
        host = urlsplit(origin).netloc
        try:
            async with (
                self._limiter.slot(host),
                self._client.stream(
                    "GET",
                    robots_url,
                    headers={"User-Agent": self._user_agent},
                    follow_redirects=True,
                ) as response,
            ):
                status = response.status_code
                if 200 <= status < 300:
                    body = await _read_capped(response, ROBOTS_MAX_BYTES)
                    text = body.decode(response.encoding or "utf-8", errors="replace")
                    return (
                        RobotsPolicy(
                            Protego.parse(text), allow_all=False, reason="robots.txt rules"
                        ),
                        self._ttl,
                    )
        except httpx.HTTPError as exc:
            logger.warning("robots.txt fetch failed for %s: %r", origin, exc)
            reason = f"robots.txt unreachable: {type(exc).__name__}"
            return RobotsPolicy(None, allow_all=False, reason=reason), self._error_ttl

        if status in {401, 403}:
            reason = f"robots.txt access denied (HTTP {status})"
            return RobotsPolicy(None, allow_all=False, reason=reason), self._ttl
        if 400 <= status < 500 and status != 429:
            reason = f"no robots.txt (HTTP {status})"
            return RobotsPolicy(None, allow_all=True, reason=reason), self._ttl
        reason = f"robots.txt unreachable (HTTP {status})"
        return RobotsPolicy(None, allow_all=False, reason=reason), self._error_ttl


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read at most ``limit`` bytes of a streamed body (the rest is ignored)."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= limit:
            break
    return b"".join(chunks)[:limit]
