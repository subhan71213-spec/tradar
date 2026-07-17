from __future__ import annotations

import pytest

from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataUnavailableError,
)
from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure


def test_succeeds_on_first_try_without_retrying():
    calls = {"count": 0}

    @retry_on_network_failure(max_attempts=3, base_delay_seconds=0.001)
    def flaky() -> str:
        calls["count"] += 1
        return "ok"

    assert flaky() == "ok"
    assert calls["count"] == 1


def test_retries_then_succeeds():
    calls = {"count": 0}

    @retry_on_network_failure(max_attempts=3, base_delay_seconds=0.001)
    def flaky() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise OSError("transient network blip")
        return "recovered"

    assert flaky() == "recovered"
    assert calls["count"] == 3


def test_exhausts_retries_and_raises_market_data_unavailable():
    calls = {"count": 0}

    @retry_on_network_failure(max_attempts=2, base_delay_seconds=0.001)
    def always_fails() -> None:
        calls["count"] += 1
        raise OSError("network is down")

    with pytest.raises(MarketDataUnavailableError):
        always_fails()
    assert calls["count"] == 2


def test_non_retryable_exception_propagates_immediately():
    calls = {"count": 0}

    @retry_on_network_failure(max_attempts=3, base_delay_seconds=0.001)
    def bad_data() -> None:
        calls["count"] += 1
        raise ValueError("this is a data problem, not a network problem")

    with pytest.raises(ValueError):
        bad_data()
    assert calls["count"] == 1  # never retried
