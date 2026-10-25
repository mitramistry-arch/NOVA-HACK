"""Code-side validation of verdict evidence and memory supersession chains."""

import re
from collections import Counter

from nova.core.schemas import Memory, MemoryStatus, MemoryType, Verdict, VerdictLabel

_SUPERSEDABLE_TYPES = {
    MemoryType.DECISION,
    MemoryType.CORRECTION,
    MemoryType.PREFERENCE,
}


def _compatible(old: Memory, new: Memory) -> bool:
    """Return whether two memories can participate in the same supersession chain."""
    return (
        old.subject == new.subject
        and old.type in _SUPERSEDABLE_TYPES
        and new.type in _SUPERSEDABLE_TYPES
    )


def _claim_mentions_value(claim: str, value: str) -> bool:
    """Require a direct normalized value mention before treating a prior value as verified."""
    normalized_claim = " ".join(claim.casefold().split())
    normalized_value = " ".join(value.casefold().split())
    return bool(
        normalized_value
        and re.search(rf"(?<!\w){re.escape(normalized_value)}(?!\w)", normalized_claim)
    )


def current_successor(old: Memory, memories: list[Memory]) -> Memory | None:
    """Return the unique valid current successor of a superseded memory, if present."""
    if old.status is not MemoryStatus.SUPERSEDED:
        return None

    cursor = old
    visited = {old.id}
    while True:
        children = [memory for memory in memories if memory.supersedes_id == cursor.id]
        if len(children) != 1:
            return None
        child = children[0]
        if (
            child.id in visited
            or child.timestamp < cursor.timestamp
            or not _compatible(cursor, child)
        ):
            return None
        if child.status is MemoryStatus.CURRENT:
            return child
        if child.status is not MemoryStatus.SUPERSEDED:
            return None
        visited.add(child.id)
        cursor = child


def validate_verdict(
    verdict: Verdict,
    memories: list[Memory],
    *,
    include_history: bool = False,
) -> Verdict:
    """Downgrade verdicts lacking current or explicitly verified historical source evidence."""
    counts = Counter(memory.id for memory in memories)
    by_id = {memory.id: memory for memory in memories if counts[memory.id] == 1}
    cited = [
        by_id[memory_id]
        for memory_id in dict.fromkeys(verdict.memory_ids)
        if memory_id in by_id
    ]

    if verdict.label is VerdictLabel.UNSUPPORTED:
        return verdict.model_copy(update={"memory_ids": []})

    if verdict.label in {VerdictLabel.SUPPORTED, VerdictLabel.CONTRADICTED}:
        current_ids = [
            memory.id
            for memory in cited
            if memory.status is MemoryStatus.CURRENT
            and memory.subject.strip()
            and memory.value.strip()
            and memory.source_quote.strip()
        ]
        if current_ids:
            return verdict.model_copy(update={"memory_ids": current_ids})
        historical_ids = []
        if include_history and verdict.label is VerdictLabel.SUPPORTED:
            historical_ids = [
                memory.id
                for memory in cited
                if memory.status is MemoryStatus.SUPERSEDED
                and current_successor(memory, memories) is not None
                and _claim_mentions_value(verdict.claim, memory.value)
            ]
        if historical_ids:
            return verdict.model_copy(update={"memory_ids": historical_ids})
        return verdict.model_copy(
            update={
                "label": VerdictLabel.UNSUPPORTED,
                "memory_ids": [],
                "rationale": "no valid current source evidence",
            }
        )

    valid_old_ids = [
        memory.id
        for memory in cited
        if memory.status is MemoryStatus.SUPERSEDED
        and current_successor(memory, memories) is not None
    ]
    if not valid_old_ids:
        return verdict.model_copy(
            update={
                "label": VerdictLabel.UNSUPPORTED,
                "memory_ids": [],
                "rationale": "outdated verdict lacks an unambiguous valid successor",
            }
        )
    return verdict.model_copy(update={"memory_ids": valid_old_ids})
