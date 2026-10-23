"""Scraping errors. Each message is safe to show to API clients."""

from __future__ import annotations


class ScrapeError(Exception):
    """Base class for expected, per-URL scraping failures."""


class InvalidURLError(ScrapeError):
    """The URL cannot be fetched (unsupported scheme, missing host, ...)."""


class RobotsDisallowedError(ScrapeError):
    """robots.txt does not allow our user-agent to fetch the URL."""

    def __init__(self, url: str, user_agent: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(
            f"Blocked by robots.txt: {url} is disallowed for {user_agent!r} ({reason})"
        )


class PageTooLargeError(ScrapeError):
    """The response body exceeds the configured maximum size."""

    def __init__(self, url: str, limit: int) -> None:
        super().__init__(f"Response from {url} exceeds the {limit}-byte page size limit")


class TooManyRedirectsError(ScrapeError):
    """The redirect chain is longer than allowed."""


class FetchFailedError(ScrapeError):
    """The request failed permanently (non-retryable status, exhausted retries, ...)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)
