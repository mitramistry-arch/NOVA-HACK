"""Table-driven tests for the deterministic decision engine."""

from datetime import UTC, datetime
from itertools import product

import pytest

from nova.core.schemas import (
    Decision,
    Memory,
    MemoryStatus,
    MemoryType,
    Verdict,
)
from nova.core.schemas import (
    DecisionAction as A,
)
from nova.core.schemas import (
    VerdictLabel as L,
)
from nova.verification.decision import ABSTAIN_TEXT, decide

T = datetime(2026, 1, 1, tzinfo=UTC)


def mem(id_: str, value: str, status=MemoryStatus.CURRENT, sup: str | None = None) -> Memory:
    return Memory(
        id=id_,
        type=MemoryType.DECISION,
        subject="db",
        value=value,
        source_msg_id="m",
        source_quote=f"use {value}",
        session_id="s",
        timestamp=T,
        status=status,
        supersedes_id=sup,
    )


OLD = mem("old", "mysql", MemoryStatus.SUPERSEDED)
NEW = mem("new", "postgres", sup="old")
MEMS = [OLD, NEW]


def v(label: L, ids: list[str] | None = None, claim: str | None = None) -> Verdict:
    return Verdict(claim=claim or f"c-{label.value}", label=label, memory_ids=ids or [])


def expected(labels: tuple[L, ...]) -> A:
    s = set(labels)
    if L.CONTRADICTED in s:
        return A.FLAG_CONFLICT
    if L.OUTDATED in s:
        return A.CORRECT
    if L.UNSUPPORTED in s:
        return A.QUALIFY if L.SUPPORTED in s else A.ABSTAIN
    return A.ANSWER


COMBOS = [c for n in (1, 2, 3) for c in product(L, repeat=n)]


@pytest.mark.parametrize("labels", COMBOS)
def test_every_label_combination(labels):
    verdicts = [
        v(
            label,
            (
                ["old"]
                if label is L.OUTDATED
                else ["new"]
                if label in {L.SUPPORTED, L.CONTRADICTED}
                else []
            ),
        )
        for label in labels
    ]
    d = decide(verdicts, MEMS)
    assert d.action is expected(labels)
    assert d.verdicts == verdicts
    assert d.final_answer
    if set(labels) == {L.SUPPORTED}:
        assert d.action is A.ANSWER
        assert "conflict" not in d.final_answer.lower()
        assert "unverified" not in d.final_answer.lower()


def test_empty_abstains():
    d = decide([], MEMS)
    assert d == Decision(action=A.ABSTAIN, verdicts=[], final_answer=ABSTAIN_TEXT)


def test_conflict_lists_both_sources():
    d = decide([v(L.CONTRADICTED, ["new", "ghost"], "uses mysql")], MEMS)
    assert 'Claim "uses mysql"' in d.final_answer
    assert '"use postgres"' in d.final_answer
    assert "ghost" not in d.final_answer


def test_conflict_without_stored_source_abstains():
    decision = decide([v(L.CONTRADICTED)], MEMS)
    assert decision.action is A.ABSTAIN
    assert decision.verdicts[0].label is L.UNSUPPORTED


def test_correct_from_superseded_id():
    d = decide([v(L.OUTDATED, ["old"])], MEMS)
    assert "Current: postgres" in d.final_answer
    assert "replaced mysql" in d.final_answer


def test_outdated_verdict_citing_current_memory_abstains():
    decision = decide([v(L.OUTDATED, ["new"])], MEMS)
    assert decision.action is A.ABSTAIN
    assert decision.verdicts[0].label is L.UNSUPPORTED


def test_outdated_verdict_without_successor_abstains():
    decision = decide([v(L.OUTDATED, ["old"])], [OLD])
    assert decision.action is A.ABSTAIN
    assert decision.verdicts[0].label is L.UNSUPPORTED


def test_outdated_verdict_without_known_memory_abstains():
    decision = decide([v(L.OUTDATED, ["x"])], MEMS)
    assert decision.action is A.ABSTAIN
    assert decision.verdicts[0].label is L.UNSUPPORTED


def test_correct_skips_other_labels():
    d = decide([v(L.SUPPORTED, ["new"], "ok"), v(L.OUTDATED, ["old"], "x")], MEMS)
    assert "ok" not in d.final_answer


def test_qualify_text():
    d = decide([v(L.SUPPORTED, ["new"], claim="a"), v(L.UNSUPPORTED, claim="b")], MEMS)
    assert d.final_answer == "Supported: a. Unverified: b."


def test_answer_cites_quotes():
    d = decide([v(L.SUPPORTED, ["new"], "uses postgres")], MEMS)
    assert d.final_answer == 'uses postgres Sources: "use postgres".'


def test_correction_is_independent_of_cited_id_order():
    decision_a = decide([v(L.OUTDATED, ["old", "new"])], MEMS)
    decision_b = decide([v(L.OUTDATED, ["new", "old"])], MEMS)
    assert decision_a.action is A.CORRECT
    assert decision_a.final_answer == decision_b.final_answer
    assert "Current: postgres" in decision_a.final_answer
    assert "replaced mysql" in decision_a.final_answer


def test_ambiguous_successor_chain_abstains():
    competing = mem("branch", "sqlite", MemoryStatus.CURRENT, "old")
    decision = decide([v(L.OUTDATED, ["old"])], [OLD, NEW, competing])
    assert decision.action is A.ABSTAIN
    assert decision.verdicts[0].label is L.UNSUPPORTED


def test_mismatched_subject_successor_is_rejected():
    unrelated = Memory(
        id="unrelated",
        type=MemoryType.DECISION,
        subject="auth",
        value="JWT",
        source_msg_id="m2",
        source_quote="use JWT",
        session_id="s",
        timestamp=T,
        supersedes_id="old",
    )
    decision = decide([v(L.OUTDATED, ["old"])], [OLD, unrelated])
    assert decision.action is A.ABSTAIN


def test_duplicate_memory_ids_are_not_accepted_as_evidence():
    conflicting_duplicate = NEW.model_copy(update={"value": "oracle"})
    decision = decide([v(L.SUPPORTED, ["new"])], [NEW, conflicting_duplicate])
    assert decision.action is A.ABSTAIN
    assert decision.verdicts[0].label is L.UNSUPPORTED
