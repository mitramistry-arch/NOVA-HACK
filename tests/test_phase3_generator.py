"""Phase 3 regression tests: seeded generator validity, balance, context labels, history."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from benchmark.generator import DOMAINS, generate, validate_case, validate_cases
from benchmark.scenarios import build_decision_reversed
from benchmark.schema import BASELINE_WINDOW, Case, Scenario, Split, compute_context_dropped
from benchmark.systems.baseline import BaselineSystem
from nova.core.llm import FakeLLM
from nova.core.schemas import (
    DecisionAction,
    Memory,
    MemoryStatus,
    MemoryType,
    Verdict,
    VerdictLabel,
)
from nova.verification.service import AnchorVerifier

SEEDS = range(200)
EXPECTED_ACTIONS: dict[Scenario, set[DecisionAction]] = {
    Scenario.BURIED_FACT: {DecisionAction.ANSWER},
    Scenario.DECISION_REVERSED: {DecisionAction.ANSWER},
    Scenario.UNANSWERABLE: {DecisionAction.ABSTAIN},
    Scenario.CONTRADICTING_DRAFT: {DecisionAction.CORRECT, DecisionAction.FLAG_CONFLICT},
    Scenario.PARAPHRASED: {DecisionAction.ANSWER},
    Scenario.CROSS_SESSION: {DecisionAction.ANSWER},
    Scenario.VALID_ANSWER: {DecisionAction.ANSWER},
}
ALWAYS_DROPPED = {
    Scenario.BURIED_FACT,
    Scenario.DECISION_REVERSED,
    Scenario.PARAPHRASED,
    Scenario.CROSS_SESSION,
}


@pytest.fixture(scope="module")
def many_cases(tmp_path_factory: pytest.TempPathFactory) -> list[Case]:
    """Generate 14 cases for each of 200 seeds (validation runs inside generate)."""
    out = tmp_path_factory.mktemp("cases")
    cases: list[Case] = []
    for seed in SEEDS:
        cases.extend(generate(seed=seed, n=14, output_dir=out))
    return cases


def test_all_seeds_generate_valid_cases(many_cases: list[Case]) -> None:
    """Required IDs, expected actions, and splits hold across hundreds of seeds."""
    assert len(many_cases) == 14 * len(SEEDS)
    for case in many_cases:
        ids = [m.id for s in case.sessions for m in s]
        assert len(ids) == len(set(ids))
        assert case.expected.action in EXPECTED_ACTIONS[case.scenario]
        if case.scenario is Scenario.UNANSWERABLE:
            assert case.expected.source_msg_id is None and case.expected.key_fact is None
        else:
            assert case.expected.source_msg_id in ids
            assert case.expected.key_fact
        assert case.domain is not None


def test_context_dropped_labels_are_true(many_cases: list[Case]) -> None:
    """The flag equals actual placement outside the baseline window; core scenarios are dropped."""
    for case in many_cases:
        flat = [m.id for s in case.sessions for m in s]
        src = case.expected.source_msg_id
        if src is None:
            assert case.context_dropped is False
            continue
        outside = src not in flat[-BASELINE_WINDOW:]
        assert case.context_dropped is outside
        if case.scenario in ALWAYS_DROPPED:
            assert case.context_dropped is True


def test_dropped_facts_are_invisible_to_baseline(tmp_path: Path) -> None:
    """For dropped cases the baseline prompt lacks the fact; for non-dropped it has it."""
    for seed in range(25):
        for case in generate(seed=seed, n=7, output_dir=tmp_path):
            if case.expected.key_fact is None:
                continue
            llm = FakeLLM(default_response="x")
            BaselineSystem(llm).run(case)
            prompt = llm.calls[0]["prompt"]
            assert (case.expected.key_fact in prompt) is (not case.context_dropped)


def test_validate_case_rejects_wrong_context_label(many_cases: list[Case]) -> None:
    """A flipped context_dropped flag is caught by validation."""
    case = next(c for c in many_cases if c.scenario is Scenario.BURIED_FACT)
    with pytest.raises(ValueError, match="context_dropped"):
        validate_case(case.model_copy(update={"context_dropped": False}))


def test_compute_context_dropped_boundaries() -> None:
    """Window boundary: the 20th-from-last message is inside, the 21st outside."""
    now = datetime(2026, 10, 1, tzinfo=UTC)
    from nova.core.schemas import Message

    msgs = [Message(id=f"m{i}", session_id="s", role="user", text="t", timestamp=now) for i in range(30)]
    assert compute_context_dropped([msgs], "m10") is False
    assert compute_context_dropped([msgs], "m9") is True
    assert compute_context_dropped([msgs], None) is False


def test_decision_reversed_valid_across_many_seeds() -> None:
    """Reversal generator never drops a message, for any seed or domain."""
    for seed in range(400):
        case = build_decision_reversed(
            "case_scenario_test_001",
            Split.DEV,
            random.Random(seed),
            DOMAINS[seed % len(DOMAINS)],
            datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        )
        validate_case(case)
        ids = [m.id for m in case.sessions[0]]
        old_id = "planted_case_scenario_test_001_old"
        rev_id = "planted_case_scenario_test_001_rev"
        assert old_id in ids and rev_id in ids
        assert ids.index(old_id) < ids.index(rev_id)
        assert case.context_dropped is True


def test_every_scenario_covers_multiple_domains(tmp_path: Path) -> None:
    """With two rounds, each scenario is tested on at least two distinct domains, every seed."""
    for seed in range(60):
        seen: dict[Scenario, set[str]] = defaultdict(set)
        for case in generate(seed=seed, n=14, output_dir=tmp_path):
            seen[case.scenario].add(case.domain or "")
        for scenario in Scenario:
            assert len(seen[scenario]) >= 2, (seed, scenario)


def test_full_round_covers_every_domain_per_scenario(tmp_path: Path) -> None:
    """After 7 rounds every scenario meets every domain, and pairing varies by seed."""
    pairings = set()
    for seed in (1, 2, 3):
        seen: dict[Scenario, set[str]] = defaultdict(set)
        cases = generate(seed=seed, n=49, output_dir=tmp_path)
        validate_cases(cases)
        for case in cases:
            seen[case.scenario].add(case.domain or "")
        for scenario in Scenario:
            assert len(seen[scenario]) == len(DOMAINS)
        pairings.add(tuple(c.domain for c in cases[:7]))
    assert len(pairings) > 1


# ─── 35: historical queries ──────────────────────────────────────────────────


def _mem(mid: str, value: str, status: MemoryStatus, supersedes: str | None = None) -> Memory:
    return Memory(
        id=mid,
        type=MemoryType.DECISION,
        subject="database",
        value=value,
        source_msg_id=f"src_{mid}",
        source_quote=f"use {value}",
        session_id="s",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        status=status,
        supersedes_id=supersedes,
    )


class _Retriever:
    def __init__(self, memories: list[Memory]) -> None:
        self.memories = memories
        self.calls: list[bool] = []

    def retrieve(
        self, query: str, k: int = 5, include_history: bool = False, session_id: str | None = None
    ) -> list[Memory]:
        self.calls.append(include_history)
        return self.memories


class _OutdatedJudge:
    def judge_claims(self, question: str, claims: list[str], memories: list[Memory]) -> list[Verdict]:
        return [Verdict(claim=c, label=VerdictLabel.OUTDATED, memory_ids=["old"]) for c in claims]


def _verifier(llm: FakeLLM | None = None) -> tuple[AnchorVerifier, _Retriever]:
    old = _mem("old", "MySQL 8.4", MemoryStatus.SUPERSEDED)
    new = _mem("new", "PostgreSQL 16.2", MemoryStatus.CURRENT, supersedes="old")
    retriever = _Retriever([old, new])
    verifier = AnchorVerifier(
        retriever,
        splitter=lambda _llm, answer: [answer],
        judge=_OutdatedJudge(),
        llm=llm or FakeLLM(default_response="MySQL 8.4"),
    )
    return verifier, retriever


def test_current_question_flags_old_value_as_outdated() -> None:
    """Without the historical flag, a superseded value is corrected."""
    verifier, _ = _verifier()
    assert verifier.verify("What is the database?", "It is MySQL 8.4.").action is DecisionAction.CORRECT


def test_historical_question_accepts_superseded_value() -> None:
    """With the flag, a claim matching the superseded value is supported, not outdated."""
    verifier, _ = _verifier()
    decision = verifier.verify(
        "What was the previous database?", "It was MySQL 8.4.", include_history=True
    )
    assert decision.action is DecisionAction.ANSWER


def test_ask_retrieves_history_only_for_historical_questions() -> None:
    """ask() passes include_history to retrieval and labels superseded memories in the prompt."""
    llm = FakeLLM(default_response="MySQL 8.4")
    verifier, retriever = _verifier(llm)
    verifier.ask("What is the database?")
    assert retriever.calls == [False, True]
    assert "[superseded]" not in llm.calls[0]["prompt"]

    llm2 = FakeLLM(default_response="MySQL 8.4")
    verifier2, retriever2 = _verifier(llm2)
    decision = verifier2.ask("What was the previous database?", include_history=True)
    assert retriever2.calls == [True, True]
    assert "[superseded]" in llm2.calls[0]["prompt"]
    assert decision.action is DecisionAction.ANSWER
