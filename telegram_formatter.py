"""Automatic retry with exponential backoff for network-facing calls.

Applied to adapter methods that hit an upstream HTTP source. Retries a
configurable number of times on network-level failures only (timeouts,
connection errors, transient HTTP errors) -- it never retries on data
validation errors, since retrying won't fix malformed data.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataUnavailableError,
)

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

# Network-level exceptions worth retrying. Kept broad but bounded: socket
# errors, timeouts, and urllib's own error hierarchy. Deliberately does NOT
# include ValueError/KeyError/domain exceptions -- those indicate bad data,
# not a transient network hiccup, and retrying would just waste time.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    OSError,        # covers socket.error, ConnectionError, TimeoutError, etc.
    TimeoutError,
)


def retry_on_network_failure(
    max_attempts: int = 3,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 8.0,
    retryable_exceptions: tuple[type[Exception], ...] = RETRYABLE_EXCEPTIONS,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator: retry a function on network failure with exponential
    backoff + jitter. Raises MarketDataUnavailableError after the final
    attempt fails, wrapping the last underlying exception.
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_exception: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exception = exc
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
                    delay += random.uniform(0, base_delay_seconds)  # jitter
                    logger.warning(
                        "%s attempt %d/%d failed (%s); retrying in %.2fs",
                        func.__qualname__,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            raise MarketDataUnavailableError(
                f"{func.__qualname__} failed after {max_attempts} attempts: {last_exception}"
            ) from last_exception

        return wrapper

    return decorator
