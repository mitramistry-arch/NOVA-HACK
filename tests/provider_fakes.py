"""Deterministic test doubles for provider adapters.

These live under ``tests/`` on purpose: they are never imported by production code and never
shipped in the wheel, so a real provider can never be silently replaced by a fake at runtime.
Each double records what happened so a test can assert on call counts and timing.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

OK_BODY: dict[str, Any] = {
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}


def ok_response(body: dict[str, Any] | None = None) -> httpx.Response:
    """Return a well-formed Chat Completions response."""
    return httpx.Response(200, json=body if body is not None else OK_BODY)


def error_response(status: int, *, retry_after: str | None = None) -> httpx.Response:
    """Return an error response, optionally carrying a Retry-After header."""
    headers = {"retry-after": retry_after} if retry_after is not None else None
    return httpx.Response(status, text="upstream detail that must not leak", headers=headers)


@dataclass
class ScriptedTransport:
    """Mock transport that replays a script of responses, then repeats the last one."""

    script: list[Callable[[httpx.Request], httpx.Response] | httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Fail loudly on an empty script instead of inventing a response."""
        if not self.script:
            raise ValueError("script must contain at least one response")

    @property
    def calls(self) -> int:
        """Return how many HTTP attempts the adapter made."""
        return len(self.requests)

    def transport(self) -> httpx.MockTransport:
        """Return the transport handed to the adapter under test."""

        def handler(request: httpx.Request) -> httpx.Response:
            index = min(len(self.requests), len(self.script) - 1)
            self.requests.append(request)
            step = self.script[index]
            if isinstance(step, httpx.Response):
                return step
            return step(request)

        return httpx.MockTransport(handler)


@dataclass
class RecordingSleep:
    """Records requested sleeps and returns immediately, so tests never actually wait."""

    delays: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        """Record one sleep request."""
        self.delays.append(seconds)


@dataclass
class FakeClock:
    """Monotonic clock that only advances when a sleep is requested."""

    now: float = 0.0
    step_on_sleep: bool = True

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now

    def sleep_with(self, recorder: RecordingSleep) -> Callable[[float], None]:
        """Return a sleep function that records the delay and advances time."""

        def _sleep(seconds: float) -> None:
            recorder.delays.append(seconds)
            if self.step_on_sleep:
                self.now += seconds

        return _sleep
