"""Temporal policy per memory type: which types take part in supersession chains."""

from dataclasses import dataclass

from nova.core.schemas import MemoryType


@dataclass(frozen=True)
class TypePolicy:
    """How a memory type behaves when a newer memory with the same subject arrives.

    ``chained`` types form per-subject chains: a newer memory supersedes the older one.
    Non-chained types are append-only: entries for one subject coexist, are never
    superseded automatically and never supersede anything. Conflicts between them are
    surfaced at verification time instead of being silently overwritten.
    """

    chained: bool
    rationale: str


SUPERSESSION_POLICY: dict[MemoryType, TypePolicy] = {
    MemoryType.FACT: TypePolicy(False, "facts are an append-only ledger; conflicts are flagged"),
    MemoryType.COMMITMENT: TypePolicy(
        False, "commitments are append-only; revoking one requires an explicit operator action"
    ),
    MemoryType.DECISION: TypePolicy(True, "a later decision on the same subject replaces it"),
    MemoryType.CORRECTION: TypePolicy(True, "a later correction replaces the earlier statement"),
    MemoryType.PREFERENCE: TypePolicy(True, "a later preference replaces the earlier one"),
}

CHAINED_TYPES: frozenset[MemoryType] = frozenset(
    t for t, p in SUPERSESSION_POLICY.items() if p.chained
)


def is_chained(memory_type: MemoryType) -> bool:
    """Return True when the type participates in supersession chains."""
    return SUPERSESSION_POLICY[memory_type].chained
