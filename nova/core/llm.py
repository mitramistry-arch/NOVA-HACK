"""LLM client interface and testing doubles."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for language model completion providers."""

    def complete(self, prompt: str, *, json: bool = False) -> str:
        """Generate a response completion for the prompt."""
        ...


class UsageTally:
    """Accumulates token usage that a provider genuinely reported (never estimated)."""

    def __init__(self) -> None:
        """Start with nothing reported."""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reported_calls = 0

    def add(self, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        """Record usage from one provider response when both counts are present."""
        if prompt_tokens is None or completion_tokens is None:
            return
        self.prompt_tokens += max(0, int(prompt_tokens))
        self.completion_tokens += max(0, int(completion_tokens))
        self.reported_calls += 1


def snapshot_usage(llm: Any) -> tuple[int, int, int] | None:
    """Return (prompt, completion, reported_calls) if the client tracks usage, else None."""
    tally = getattr(llm, "usage", None)
    if not isinstance(tally, UsageTally):
        return None
    return (tally.prompt_tokens, tally.completion_tokens, tally.reported_calls)


def usage_delta(
    before: tuple[int, int, int] | None,
    after: tuple[int, int, int] | None,
) -> tuple[int, int] | None:
    """Return tokens used between two snapshots, or None when nothing was reported."""
    if before is None or after is None or after[2] <= before[2]:
        return None
    return (after[0] - before[0], after[1] - before[1])


class FakeLLM:
    """Scripted LLM implementation for offline testing."""

    def __init__(
        self,
        responses: list[str] | dict[str, str] | str | None = None,
        default_response: str = "",
    ) -> None:
        """Initialize FakeLLM with scripted responses."""
        self.default_response = default_response
        self.calls: list[dict[str, Any]] = []
        if isinstance(responses, str):
            self._responses_list = [responses]
            self._responses_dict: dict[str, str] = {}
        elif isinstance(responses, list):
            self._responses_list = list(responses)
            self._responses_dict = {}
        elif isinstance(responses, dict):
            self._responses_list = []
            self._responses_dict = dict(responses)
        else:
            self._responses_list = []
            self._responses_dict = {}

    def complete(self, prompt: str, *, json: bool = False) -> str:
        """Return scripted response matching the prompt or next in list."""
        self.calls.append({"prompt": prompt, "json": json})
        if prompt in self._responses_dict:
            return self._responses_dict[prompt]
        if self._responses_list:
            return self._responses_list.pop(0)
        return self.default_response
