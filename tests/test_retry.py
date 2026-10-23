from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smart_scraper.scraping.retry import (
    backoff_delay,
    equal_jitter,
    is_retryable_status,
    no_jitter,
    parse_retry_after,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        (" 3 ", 3.0),
        ("0", 0.0),
        ("Thu, 01 Jan 2026 12:00:30 GMT", 30.0),
        ("Thu, 01 Jan 2026 11:00:00 GMT", 0.0),  # in the past
        (None, None),
        ("", None),
        ("soon", None),
        ("-5", None),
        ("1.5", None),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header, now=NOW) == expected


def test_parse_retry_after_defaults_to_current_time() -> None:
    assert parse_retry_after("Thu, 01 Jan 2004 00:00:00 GMT") == 0.0


def test_backoff_grows_exponentially_and_is_capped() -> None:
    delays = [backoff_delay(n, base=0.5, cap=3.0, jitter=no_jitter) for n in range(5)]
    assert delays == [0.5, 1.0, 2.0, 3.0, 3.0]


def test_backoff_rejects_negative_attempt() -> None:
    with pytest.raises(ValueError, match="attempt"):
        backoff_delay(-1, base=1, cap=1)


def test_equal_jitter_stays_within_bounds() -> None:
    for _ in range(200):
        assert 2.0 <= equal_jitter(4.0) <= 4.0


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (502, True), (503, True), (504, True), (404, False), (501, False)],
)
def test_retryable_statuses(status: int, retryable: bool) -> None:
    assert is_retryable_status(status) is retryable
