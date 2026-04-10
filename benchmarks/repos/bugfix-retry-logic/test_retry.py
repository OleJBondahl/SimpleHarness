"""Tests for retry_request."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from retry import Response, RetriesExhaustedError, RetryConfig, retry_request


def _make_fn(*status_codes: int) -> MagicMock:
    """Create a callable that returns Responses with the given status codes in order."""
    fn = MagicMock()
    fn.side_effect = [Response(status_code=sc, body="") for sc in status_codes]
    return fn


# ---------- Tests that pass WITH the bug ----------


def test_successful_request_returns_immediately():
    """A 200 on the first call should return without any retries."""
    fn = _make_fn(200)
    config = RetryConfig(max_retries=3, backoff_factor=0)

    result = retry_request(fn, config)

    assert result.status_code == 200
    assert fn.call_count == 1


def test_non_retryable_status_returns_without_retry():
    """A 400 (not in retry_on_status_codes) should return immediately."""
    fn = _make_fn(400)
    config = RetryConfig(max_retries=3, backoff_factor=0)

    result = retry_request(fn, config)

    assert result.status_code == 400
    assert fn.call_count == 1


def test_retries_on_retryable_status_then_succeeds():
    """Should retry on 503 and return once a 200 arrives."""
    fn = _make_fn(503, 200)
    config = RetryConfig(max_retries=3, backoff_factor=0)

    result = retry_request(fn, config)

    assert result.status_code == 200
    assert fn.call_count == 2


# ---------- Test that FAILS with the bug ----------


def test_exact_retry_count():
    """With max_retries=3, there should be 1 initial + 3 retries = 4 total calls.

    The function should exhaust all retries and raise RetriesExhaustedError.
    """
    fn = _make_fn(503, 503, 503, 503)
    config = RetryConfig(max_retries=3, backoff_factor=0)

    with pytest.raises(RetriesExhaustedError):
        retry_request(fn, config)

    assert fn.call_count == 4, (
        f"Expected 4 total calls (1 initial + 3 retries), got {fn.call_count}"
    )
