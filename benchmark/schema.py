"""Benchmark schema definitions for Context Anchor evaluation."""

from enum import StrEnum

from pydantic import BaseModel, Field

from nova.core.schemas import DecisionAction, Message

BASELINE_WINDOW = 20


class Scenario(StrEnum):
    """Evaluation scenario type."""

    BURIED_FACT = "buried_fact"
    DECISION_REVERSED = "decision_reversed"
    UNANSWERABLE = "unanswerable"
    CONTRADICTING_DRAFT = "contradicting_draft"
    PARAPHRASED = "paraphrased"
    CROSS_SESSION = "cross_session"
    VALID_ANSWER = "valid_answer"


class Split(StrEnum):
    """Dataset partition designation."""

    DEV = "dev"
    TEST = "test"


class Expected(BaseModel):
    """Expected evaluation outcome."""

    action: DecisionAction | str
    key_fact: str | None = None
    source_msg_id: str | None = None


class Case(BaseModel):
    """Benchmark test case covering one evaluation scenario."""

    id: str
    scenario: Scenario
    split: Split
    sessions: list[list[Message]] = Field(default_factory=list)
    context_dropped: bool
    question: str
    draft_answer: str | None = None
    include_history: bool = False
    domain: str | None = None
    expected: Expected


def compute_context_dropped(
    sessions: list[list[Message]],
    source_msg_id: str | None,
    window: int = BASELINE_WINDOW,
) -> bool:
    """True iff the source message lies outside the baseline's last-`window` messages."""
    if source_msg_id is None:
        return False
    flat_ids = [m.id for session in sessions for m in session]
    return source_msg_id not in flat_ids[-window:]
