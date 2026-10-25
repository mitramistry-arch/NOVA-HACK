"""Bounded retries with exponential backoff and jitter for provider calls.

Only transient failures are retried. Authentication, configuration, malformed-response, and budget
errors are raised immediately: retrying them would waste the operator's quota without changing the
outcome. The total time spent retrying is bounded so one slow call cannot hold a request open.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from nova.providers.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

RETRYABLE_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
MAX_RETRY_AFTER_S = 30.0
logger = logging.getLogger("nova.providers.retry")

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry, how long to wait, and when to stop."""

    max_attempts: int = 3
    base_delay_s: float = 0.25
    max_delay_s: float = 2.0
    total_timeout_s: float = 30.0
    jitter_ratio: float = 0.25
    _rng: random.Random = field(default_factory=random.Random, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Reject policies that could stall a request or hot-loop."""
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("retry delays must not be negative")
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s must be at least base_delay_s")
        if self.total_timeout_s <= 0:
            raise ValueError("total_timeout_s must be positive")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be between 0 and 1")

    @property
    def retries(self) -> int:
        """Return the number of retries (attempts minus the first try)."""
        return max(0, self.max_attempts - 1)

    def is_retryable(self, exc: BaseException) -> bool:
        """Return True only for transient provider failures."""
        return isinstance(exc, RETRYABLE_ERRORS)

    def delay_for(self, attempt: int, *, retry_after_s: float | None = None) -> float:
        """Return the delay before the next attempt (``attempt`` is 1-based)."""
        if retry_after_s is not None and retry_after_s >= 0:
            return min(float(retry_after_s), MAX_RETRY_AFTER_S)
        raw = self.base_delay_s * (2 ** max(0, attempt - 1))
        capped = min(raw, self.max_delay_s)
        if self.jitter_ratio == 0 or capped == 0:
            return capped
        spread = capped * self.jitter_ratio
        return max(0.0, capped - spread + self._rng.random() * spread * 2)

    def exhausted(self, *, elapsed_s: float, next_delay_s: float) -> bool:
        """Return True when sleeping again would exceed the total retry budget."""
        return elapsed_s + next_delay_s > self.total_timeout_s


def retry_after_from_error(exc: ProviderError) -> float | None:
    """Return a provider-supplied retry delay when the adapter captured one."""
    value = getattr(exc, "retry_after_s", None)
    return float(value) if isinstance(value, int | float) else None


def call_with_retries(
    call: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Run ``call``, retrying transient failures within the policy's attempt and time budget."""
    started = monotonic()
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return call()
        except ProviderError as exc:
            if not policy.is_retryable(exc) or attempt == policy.max_attempts:
                raise
            delay = policy.delay_for(attempt, retry_after_s=retry_after_from_error(exc))
            if policy.exhausted(elapsed_s=monotonic() - started, next_delay_s=delay):
                raise
            logger.warning(
                "retrying provider call after transient failure",
                extra={
                    "attempt": attempt,
                    "max_attempts": policy.max_attempts,
                    "delay_s": round(delay, 3),
                    "error_code": getattr(exc, "code", "unknown"),
                },
            )
            sleep(delay)
    raise AssertionError("retry loop exited without returning or raising")  # pragma: no cover


__all__ = [
    "MAX_RETRY_AFTER_S",
    "RETRYABLE_ERRORS",
    "RetryPolicy",
    "call_with_retries",
    "retry_after_from_error",
]
