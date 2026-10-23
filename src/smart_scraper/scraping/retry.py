"""Retry policy helpers: which responses to retry, exponential backoff, ``Retry-After``."""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

Jitter = Callable[[float], float]


def equal_jitter(delay: float) -> float:
    """Return a value in ``[delay / 2, delay]`` (AWS "equal jitter")."""
    half = delay / 2
    return half + random.uniform(0, half)  # noqa: S311 - not used for security


def no_jitter(delay: float) -> float:
    return delay


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS


def backoff_delay(attempt: int, *, base: float, cap: float, jitter: Jitter = equal_jitter) -> float:
    """Delay before retry number ``attempt`` (0-based): ``min(cap, base * 2**attempt)`` + jitter."""
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    return jitter(min(cap, base * (2**attempt)))


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into seconds.

    Returns ``None`` when the header is missing or malformed. Dates in the past yield ``0``.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (when - current).total_seconds())
