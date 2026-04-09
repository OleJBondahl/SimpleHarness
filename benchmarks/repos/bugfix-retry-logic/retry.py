"""Simple HTTP retry logic with configurable backoff."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Response:
    """Minimal HTTP response representation."""

    status_code: int
    body: str


@dataclass(frozen=True)
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    backoff_factor: float = 0.1
    retry_on_status_codes: tuple[int, ...] = field(default_factory=lambda: (500, 502, 503, 504))


class RetriesExhausted(Exception):
    """Raised when all retry attempts have been exhausted."""

    def __init__(self, last_response: Response, attempts: int) -> None:
        self.last_response = last_response
        self.attempts = attempts
        super().__init__(
            f"All {attempts} attempts exhausted. Last status: {last_response.status_code}"
        )


def _compute_backoff(attempt: int, backoff_factor: float) -> float:
    """Return the sleep duration for a given attempt number."""
    return backoff_factor * (2**attempt)


def retry_request(fn: Callable[[], Response], config: RetryConfig) -> Response:
    """Execute *fn* with retries according to *config*.

    Makes one initial attempt plus up to ``config.max_retries`` retries
    for responses whose status code is in ``config.retry_on_status_codes``.

    Parameters
    ----------
    fn:
        A callable that returns a :class:`Response`.
    config:
        Retry configuration controlling attempts, backoff, and retryable codes.

    Returns
    -------
    Response
        The first successful response, or the last response if retries are
        exhausted.

    Raises
    ------
    RetriesExhausted
        When all attempts (initial + retries) fail with retryable status codes.
    """
    response = fn()

    if response.status_code not in config.retry_on_status_codes:
        return response

    # BUG: off-by-one -- uses (max_retries - 1) instead of max_retries
    for attempt in range(config.max_retries - 1):
        delay = _compute_backoff(attempt, config.backoff_factor)
        time.sleep(delay)

        response = fn()

        if response.status_code not in config.retry_on_status_codes:
            return response

    raise RetriesExhausted(last_response=response, attempts=config.max_retries)
