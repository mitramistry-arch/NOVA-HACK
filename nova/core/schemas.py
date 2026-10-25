"""Core domain schemas and enumerations for Nova."""

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    Field,
    StringConstraints,
    model_validator,
)

MAX_ID_LEN = 128
MAX_SUBJECT_LEN = 200
MAX_MESSAGE_TEXT_LEN = 20_000
MAX_VALUE_LEN = 4_000
MAX_QUOTE_LEN = 4_000
MAX_REASON_LEN = 2_000
MAX_CLAIM_LEN = 4_000


def _blank_check(value: str) -> str:
    """Reject whitespace-only strings."""
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _to_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC; naive values are rejected upstream."""
    return value.astimezone(UTC)


def _finite(value: float) -> float:
    """Reject NaN and infinities."""
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ID_LEN),
]
Subject = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUBJECT_LEN),
]
NonBlankText = Annotated[str, AfterValidator(_blank_check)]
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False), AfterValidator(_finite)]


class MemoryType(StrEnum):
    """Enumeration of recognized memory types."""

    FACT = "fact"
    DECISION = "decision"
    CORRECTION = "correction"
    PREFERENCE = "preference"
    COMMITMENT = "commitment"


class MemoryStatus(StrEnum):
    """Lifecycle status of a stored memory."""

    CURRENT = "current"
    SUPERSEDED = "superseded"


class MessageRole(StrEnum):
    """Allowed conversation roles."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class VerdictLabel(StrEnum):
    """Verification label for a claim against evidence."""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    OUTDATED = "outdated"
    UNSUPPORTED = "unsupported"


class DecisionAction(StrEnum):
    """Deterministic action chosen based on claim verdicts."""

    ANSWER = "answer"
    FLAG_CONFLICT = "flag_conflict"
    CORRECT = "correct"
    QUALIFY = "qualify"
    ABSTAIN = "abstain"


class Memory(BaseModel):
    """Stored atomic memory with provenance and supersession state.

    Timestamps must carry a UTC offset and are normalized to UTC; naive timestamps are rejected.
    """

    id: Identifier
    type: MemoryType
    subject: Subject
    value: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_VALUE_LEN)
    ]
    reason: Annotated[str, StringConstraints(max_length=MAX_REASON_LEN)] | None = None
    source_msg_id: Identifier
    source_quote: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUOTE_LEN)
    ]
    session_id: Identifier
    timestamp: UtcDatetime
    status: MemoryStatus = MemoryStatus.CURRENT
    supersedes_id: Identifier | None = None
    confidence: Confidence = 1.0

    @model_validator(mode="after")
    def _no_self_supersession(self) -> "Memory":
        if self.supersedes_id is not None and self.supersedes_id == self.id:
            raise ValueError("a memory cannot supersede itself")
        return self


class Message(BaseModel):
    """Conversation turn within a session."""

    id: Identifier
    session_id: Identifier
    role: MessageRole
    text: Annotated[
        str, StringConstraints(max_length=MAX_MESSAGE_TEXT_LEN), AfterValidator(_blank_check)
    ]
    timestamp: UtcDatetime


class Verdict(BaseModel):
    """Evaluation verdict for an individual atomic claim."""

    claim: str
    label: VerdictLabel
    memory_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class Decision(BaseModel):
    """Final decision determined from claim verdicts."""

    action: DecisionAction
    verdicts: list[Verdict] = Field(default_factory=list)
    final_answer: str | None = None


class IngestReport(BaseModel):
    """Summary counts returned after memory ingestion."""

    added: int = 0
    superseded: int = 0
    dropped: int = 0


class EmbeddingRecord(BaseModel):
    """A stored embedding vector with the model identity that produced it."""

    vector: bytes
    model: str | None = None
    dim: int = Field(ge=1)
