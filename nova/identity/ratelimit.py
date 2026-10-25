"""In-process sliding-window rate limiting for login and mutation endpoints.

Per-process only: run behind an edge limiter for a multi-worker or multi-host deployment.
"""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

from nova.identity.errors import RateLimited

MAX_TRACKED_KEYS = 20_000


@dataclass
class _Window:
    """Recent hit timestamps for one key."""

    hits: deque[float]


class SlidingWindowLimiter:
    """Allow at most ``limit`` hits per key in a rolling window."""

    def __init__(
        self,
        limit: int,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Configure the limit; ``clock`` is injectable for tests."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = defaultdict(lambda: _Window(deque()))

    def hit(self, key: str) -> None:
        """Record an attempt, raising RateLimited when the window is exhausted."""
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            if len(self._windows) > MAX_TRACKED_KEYS:
                self._prune(cutoff)
            window = self._windows[key]
            while window.hits and window.hits[0] <= cutoff:
                window.hits.popleft()
            if len(window.hits) >= self.limit:
                retry_after = max(1, int(window.hits[0] + self.window_seconds - now) + 1)
                raise RateLimited(retry_after_seconds=retry_after)
            window.hits.append(now)

    def _prune(self, cutoff: float) -> None:
        """Drop windows with no hits inside the current window (bounded memory)."""
        stale = [
            key
            for key, window in self._windows.items()
            if not window.hits or window.hits[-1] <= cutoff
        ]
        for key in stale:
            del self._windows[key]

    def reset(self) -> None:
        """Clear all counters (used by tests)."""
        with self._lock:
            self._windows.clear()


__all__ = ["SlidingWindowLimiter"]
