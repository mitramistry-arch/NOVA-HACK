"""System protocol and evaluation result schema."""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from benchmark.schema import Case
from nova.core.schemas import DecisionAction


class Result(BaseModel):
    """Outcome of evaluating a benchmark case on a system."""

    answer: str
    action: DecisionAction | str
    retrieved_ids: list[str] = Field(default_factory=list)
    latency_ms: float = 0.0
    prompt_tokens: int | None = 0
    completion_tokens: int | None = 0
    tokens_estimated: bool = True
    error: str | None = None


@runtime_checkable
class System(Protocol):
    """Protocol for benchmark evaluation systems."""

    def run(self, case: Case) -> Result:
        """Run evaluation on a single benchmark case."""
        ...
