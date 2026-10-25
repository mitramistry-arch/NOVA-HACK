"""Unit tests for benchmark case generator, schema, fillers, and validation."""

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from benchmark.fillers import generate_filler
from benchmark.generator import (
    generate,
    get_split_by_hash,
    validate_case,
    validate_cases,
)
from benchmark.schema import Case, Expected, Scenario, Split
from nova.core.schemas import DecisionAction, Message


def test_same_seed_byte_identical_output(tmp_path: Path) -> None:
    """Verify that generating with identical seeds produces byte-identical JSONL output."""
    out_dir1 = tmp_path / "run1"
    out_dir2 = tmp_path / "run2"

    cases1 = generate(seed=42, n=14, output_dir=out_dir1)
    cases2 = generate(seed=42, n=14, output_dir=out_dir2)

    bytes1 = (out_dir1 / "42.jsonl").read_bytes()
    bytes2 = (out_dir2 / "42.jsonl").read_bytes()

    assert bytes1 == bytes2
    assert len(cases1) == len(cases2) == 14
    for c1, c2 in zip(cases1, cases2, strict=True):
        assert c1.id == c2.id
        assert c1.scenario == c2.scenario
        assert c1.question == c2.question
        assert c1.expected == c2.expected


def test_different_seeds_different_output(tmp_path: Path) -> None:
    """Verify that different seeds produce distinct benchmark outputs."""
    generate(seed=1, n=7, output_dir=tmp_path)
    generate(seed=2, n=7, output_dir=tmp_path)

    bytes1 = (tmp_path / "1.jsonl").read_bytes()
    bytes2 = (tmp_path / "2.jsonl").read_bytes()

    assert bytes1 != bytes2


def test_fillers_never_leak_key_fact(tmp_path: Path) -> None:
    """Verify that filler messages never contain planted key facts."""
    cases = generate(seed=99, n=14, output_dir=tmp_path)

    for case in cases:
        if case.expected.key_fact is None:
            continue
        key_fact_lower = case.expected.key_fact.lower()

        for session in case.sessions:
            for msg in session:
                if msg.id == case.expected.source_msg_id:
                    continue
                if msg.id.startswith("planted_"):
                    continue
                assert key_fact_lower not in msg.text.lower(), (
                    f"Planted fact '{case.expected.key_fact}' leaked in filler msg '{msg.id}'"
                )

    rng = random.Random(12345)
    probe_facts = {"PostgreSQL 16.2", "AES-256-GCM", "nova-cold-archive-01"}
    for _ in range(200):
        filler = generate_filler(rng, probe_facts)
        for fact in probe_facts:
            assert fact.lower() not in filler.lower()


def test_validation_catches_missing_source_msg_id() -> None:
    """Verify validation raises ValueError when source_msg_id does not exist."""
    now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    case_id = "case_test_001"
    split = get_split_by_hash(case_id)

    broken_case = Case(
        id=case_id,
        scenario=Scenario.BURIED_FACT,
        split=split,
        sessions=[[Message(id="msg_1", session_id="s1", role="user", text="hello", timestamp=now)]],
        context_dropped=True,
        question="What is the db?",
        expected=Expected(
            action=DecisionAction.ANSWER,
            key_fact="PostgreSQL",
            source_msg_id="nonexistent_id",
        ),
    )

    with pytest.raises(ValueError, match="source_msg_id nonexistent_id not found"):
        validate_case(broken_case)


def test_validation_catches_leaked_key_fact() -> None:
    """Verify validation raises ValueError when key_fact leaks into a filler message."""
    now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    case_id = "case_test_002"
    split = get_split_by_hash(case_id)

    broken_case = Case(
        id=case_id,
        scenario=Scenario.BURIED_FACT,
        split=split,
        sessions=[[
            Message(id="msg_source", session_id="s1", role="user", text="DB is PostgreSQL 16.2", timestamp=now),
            Message(id="msg_filler", session_id="s1", role="user", text="Leaking PostgreSQL 16.2 here", timestamp=now),
        ]],
        context_dropped=True,
        question="What is the db?",
        expected=Expected(
            action=DecisionAction.ANSWER,
            key_fact="PostgreSQL 16.2",
            source_msg_id="msg_source",
        ),
    )

    with pytest.raises(ValueError, match="leaked in message msg_filler"):
        validate_case(broken_case)


def test_validation_catches_duplicate_message_ids() -> None:
    """Verify validation raises ValueError when duplicate message ids exist in a case."""
    now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    case_id = "case_test_003"
    split = get_split_by_hash(case_id)

    broken_case = Case(
        id=case_id,
        scenario=Scenario.BURIED_FACT,
        split=split,
        sessions=[[
            Message(id="dup_id", session_id="s1", role="user", text="first", timestamp=now),
            Message(id="dup_id", session_id="s1", role="user", text="second", timestamp=now),
        ]],
        context_dropped=True,
        question="What is it?",
        expected=Expected(action=DecisionAction.ABSTAIN),
    )

    with pytest.raises(ValueError, match="Duplicate message id dup_id"):
        validate_case(broken_case)


def test_validation_catches_invalid_split() -> None:
    """Verify validation raises ValueError when case split does not match hash."""
    now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    case_id = "case_test_004"
    actual_split = get_split_by_hash(case_id)
    wrong_split = Split.TEST if actual_split == Split.DEV else Split.DEV

    broken_case = Case(
        id=case_id,
        scenario=Scenario.UNANSWERABLE,
        split=wrong_split,
        sessions=[[Message(id="msg_1", session_id="s1", role="user", text="neutral", timestamp=now)]],
        context_dropped=True,
        question="What is the framework?",
        expected=Expected(action=DecisionAction.ABSTAIN),
    )

    with pytest.raises(ValueError, match="split mismatch"):
        validate_case(broken_case)


def test_validation_catches_duplicate_case_ids(tmp_path: Path) -> None:
    """Verify validation raises ValueError when duplicate case ids exist across cases."""
    cases = generate(seed=7, n=14, output_dir=tmp_path)
    duplicate_cases = [cases[0], cases[0]]

    with pytest.raises(ValueError, match="Duplicate case id"):
        validate_cases(duplicate_cases)


def test_dev_test_split_50_50(tmp_path: Path) -> None:
    """Verify that generated cases have an exact 50/50 dev/test split."""
    cases = generate(seed=88, n=14, output_dir=tmp_path)
    dev_cases = [c for c in cases if c.split == Split.DEV]
    test_cases = [c for c in cases if c.split == Split.TEST]

    assert len(dev_cases) == 7
    assert len(test_cases) == 7
    for c in cases:
        assert c.split == get_split_by_hash(c.id)


def test_all_seven_scenarios_generated(tmp_path: Path) -> None:
    """Verify that all seven benchmark scenarios are generated and well-formed."""
    cases = generate(seed=2026, n=14, output_dir=tmp_path)
    scenarios_present = {c.scenario for c in cases}

    assert scenarios_present == {
        Scenario.BURIED_FACT,
        Scenario.DECISION_REVERSED,
        Scenario.UNANSWERABLE,
        Scenario.CONTRADICTING_DRAFT,
        Scenario.PARAPHRASED,
        Scenario.CROSS_SESSION,
        Scenario.VALID_ANSWER,
    }

    for case in cases:
        total_msgs = sum(len(s) for s in case.sessions)
        assert 50 <= total_msgs <= 210
        assert case.question
        if case.scenario == Scenario.CROSS_SESSION:
            assert len(case.sessions) == 3
        else:
            assert len(case.sessions) == 1
        if case.scenario in (Scenario.CONTRADICTING_DRAFT, Scenario.VALID_ANSWER):
            assert case.draft_answer is not None


def test_case_json_serialization_roundtrip(tmp_path: Path) -> None:
    """Verify JSON serialization round-trip preservation."""
    cases = generate(seed=55, n=7, output_dir=tmp_path)
    for case in cases:
        json_str = case.model_dump_json()
        restored = Case.model_validate_json(json_str)
        assert restored.id == case.id
        assert restored.scenario == case.scenario
        assert restored.split == case.split
        assert restored.expected == case.expected
        assert len(restored.sessions) == len(case.sessions)
