"""Usage and cost metadata that never invents numbers.

Token counts are recorded only when the provider reports them. A cost is computed only when the
operator configured prices for that tenant, and it is always labelled an estimate: prices are
operator-supplied metadata, not a provider statement.
"""

from dataclasses import dataclass
from enum import StrEnum


class UsageSource(StrEnum):
    """Where a token count came from."""

    PROVIDER = "provider"
    ESTIMATE = "estimate"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class UsageReport:
    """Token usage and optional cost for one call, with an explicit provenance label."""

    source: UsageSource
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    cost_is_estimate: bool = False

    @property
    def total_tokens(self) -> int | None:
        """Return the combined token count, or None when either side is unknown."""
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens

    @classmethod
    def unavailable(cls) -> "UsageReport":
        """Report that the provider supplied no usable usage block."""
        return cls(source=UsageSource.UNAVAILABLE)

    @classmethod
    def from_provider(
        cls,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        input_cost_per_1k: float | None = None,
        output_cost_per_1k: float | None = None,
    ) -> "UsageReport":
        """Report provider-supplied counts, with a cost only when prices are configured."""
        prompt = max(0, int(prompt_tokens))
        completion = max(0, int(completion_tokens))
        cost = estimate_cost(
            prompt,
            completion,
            input_cost_per_1k=input_cost_per_1k,
            output_cost_per_1k=output_cost_per_1k,
        )
        return cls(
            source=UsageSource.PROVIDER,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_usd=cost,
            cost_is_estimate=cost is not None,
        )


def estimate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    input_cost_per_1k: float | None,
    output_cost_per_1k: float | None,
) -> float | None:
    """Return an estimated cost from operator-configured prices, or None when unset."""
    if input_cost_per_1k is None or output_cost_per_1k is None:
        return None
    prompt = max(0, int(prompt_tokens))
    completion = max(0, int(completion_tokens))
    return round(
        (prompt / 1000.0) * input_cost_per_1k + (completion / 1000.0) * output_cost_per_1k,
        6,
    )


__all__ = ["UsageReport", "UsageSource", "estimate_cost"]
