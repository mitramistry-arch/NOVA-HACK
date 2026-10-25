"""Deterministic decision engine: claim verdicts to a final action."""

from nova.core.schemas import (
    Decision,
    DecisionAction,
    Memory,
    Verdict,
    VerdictLabel,
)
from nova.verification.evidence import current_successor, validate_verdict

ABSTAIN_TEXT = "Insufficient evidence in memory to answer."


def _by_id(memories: list[Memory]) -> dict[str, Memory]:
    return {m.id: m for m in memories}


def _quotes(verdict: Verdict, index: dict[str, Memory]) -> list[str]:
    return [f'"{index[i].source_quote}"' for i in verdict.memory_ids if i in index]


def _conflict(verdicts: list[Verdict], index: dict[str, Memory]) -> str:
    lines = []
    for v in verdicts:
        if v.label is VerdictLabel.CONTRADICTED:
            quotes = ", ".join(_quotes(v, index)) or "no stored source"
            lines.append(f'Claim "{v.claim}" conflicts with memory: {quotes}.')
    return "Conflict between sources. " + " ".join(lines)


def _correction(verdicts: list[Verdict], memories: list[Memory]) -> str | None:
    """Build corrections only when each outdated claim has one validated successor."""
    index = _by_id(memories)
    lines = []
    for verdict in verdicts:
        if verdict.label is not VerdictLabel.OUTDATED:
            continue
        old_memories = [index[m_id] for m_id in verdict.memory_ids if m_id in index]
        successors = {
            successor.id: successor
            for old in old_memories
            if (successor := current_successor(old, memories)) is not None
        }
        if len(successors) != 1:
            return None
        current = next(iter(successors.values()))
        predecessor = index.get(current.supersedes_id) if current.supersedes_id else None
        replaced = (
            f" It replaced {predecessor.value}."
            if predecessor is not None and predecessor.subject == current.subject
            else ""
        )
        lines.append(f'Claim "{verdict.claim}" is outdated. Current: {current.value}.{replaced}')
    return " ".join(lines)


def _qualify(verdicts: list[Verdict], index: dict[str, Memory]) -> str:
    sup = [v for v in verdicts if v.label is VerdictLabel.SUPPORTED]
    uns = [v for v in verdicts if v.label is VerdictLabel.UNSUPPORTED]
    return (
        "Supported: " + "; ".join(v.claim for v in sup) + ". "
        "Unverified: " + "; ".join(v.claim for v in uns) + "."
    )


def _answer(verdicts: list[Verdict], index: dict[str, Memory]) -> str:
    claims = "; ".join(v.claim for v in verdicts)
    quotes = [q for v in verdicts for q in _quotes(v, index)]
    return claims + (" Sources: " + ", ".join(quotes) + "." if quotes else "")


def decide(
    verdicts: list[Verdict],
    memories: list[Memory],
    *,
    include_history: bool = False,
) -> Decision:
    """Pick the action by priority contradicted > outdated > unsupported > supported."""
    verdicts = [
        validate_verdict(verdict, memories, include_history=include_history) for verdict in verdicts
    ]
    if not verdicts:
        return Decision(action=DecisionAction.ABSTAIN, verdicts=[], final_answer=ABSTAIN_TEXT)
    labels = {v.label for v in verdicts}
    index = _by_id(memories)
    if VerdictLabel.CONTRADICTED in labels:
        action, text = DecisionAction.FLAG_CONFLICT, _conflict(verdicts, index)
    elif VerdictLabel.OUTDATED in labels:
        correction = _correction(verdicts, memories)
        if correction is None:
            action, text = DecisionAction.ABSTAIN, ABSTAIN_TEXT
        else:
            action, text = DecisionAction.CORRECT, correction
    elif VerdictLabel.UNSUPPORTED in labels:
        if VerdictLabel.SUPPORTED in labels:
            action, text = DecisionAction.QUALIFY, _qualify(verdicts, index)
        else:
            action, text = DecisionAction.ABSTAIN, ABSTAIN_TEXT
    else:
        action, text = DecisionAction.ANSWER, _answer(verdicts, index)
    return Decision(action=action, verdicts=verdicts, final_answer=text)
