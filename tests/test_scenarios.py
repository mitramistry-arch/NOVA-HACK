"""Unit tests for individual scenario builders, phrasing variety, and mix proportions."""

import random
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from benchmark.generator import DOMAINS, UNANSWERABLE_DOMAINS, generate, validate_case
from benchmark.scenarios import (
    BURIED_FACT_TEMPLATES,
    BURIED_QUESTION_TEMPLATES,
    CONTRADICTING_DRAFT_ANSWERS,
    CONTRADICTING_DRAFT_TEMPLATES,
    CROSS_SESSION_PLANTED_TEMPLATES,
    CROSS_SESSION_QUESTIONS,
    DECISION_CURRENT_QUESTIONS,
    DECISION_INITIAL_TEMPLATES,
    DECISION_OLD_QUESTIONS,
    DECISION_REVERSED_TEMPLATES,
    PARAPHRASE_PLANTED_TEMPLATES,
    PARAPHRASE_QUESTION_TEMPLATES,
    SYNONYM_BANK,
    UNANSWERABLE_QUESTIONS,
    VALID_ANSWER_PLANTED_TEMPLATES,
    VALID_DRAFT_TEMPLATES,
    build_buried_fact,
    build_contradicting_draft,
    build_cross_session,
    build_decision_reversed,
    build_paraphrased,
    build_unanswerable,
    build_valid_answer,
)
from benchmark.schema import Scenario, Split
from nova.core.schemas import DecisionAction


def _get_test_context() -> tuple[str, Split, dict, dict, datetime]:
    """Helper providing standard test fixtures for builder testing."""
    case_id = "case_scenario_test_001"
    split = Split.DEV
    domain = DOMAINS[0]
    unans = UNANSWERABLE_DOMAINS[0]
    base_time = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    return case_id, split, domain, unans, base_time


def test_builder_buried_fact() -> None:
    """Verify build_buried_fact yields a valid Case with action=answer."""
    case_id, split, domain, _, base_time = _get_test_context()
    rng = random.Random(42)
    case = build_buried_fact(case_id, split, rng, domain, base_time)

    validate_case(case)
    assert case.scenario == Scenario.BURIED_FACT
    assert case.expected.action == DecisionAction.ANSWER
    assert case.expected.key_fact == domain["fact_value"]
    assert case.expected.source_msg_id is not None
    assert case.context_dropped is True
    assert case.draft_answer is None


def test_builder_decision_reversed() -> None:
    """Verify build_decision_reversed yields valid Cases with action=answer and history branch."""
    case_id, split, domain, _, base_time = _get_test_context()

    # Sample multiple random seeds to test both current (80%) and old (20%) branches
    found_current = False
    found_old = False
    for seed in range(50):
        rng = random.Random(seed)
        case = build_decision_reversed(case_id, split, rng, domain, base_time)
        validate_case(case)
        assert case.scenario == Scenario.DECISION_REVERSED
        assert case.expected.action == DecisionAction.ANSWER

        if case.include_history:
            found_old = True
            assert case.expected.key_fact == domain["old_value"]
        else:
            found_current = True
            assert case.expected.key_fact == domain["fact_value"]

        if found_current and found_old:
            break

    assert found_current
    assert found_old


def test_builder_unanswerable() -> None:
    """Verify build_unanswerable yields a valid Case with action=abstain."""
    case_id, split, domain, unans, base_time = _get_test_context()
    rng = random.Random(42)
    case = build_unanswerable(case_id, split, rng, unans, domain, base_time)

    validate_case(case)
    assert case.scenario == Scenario.UNANSWERABLE
    assert case.expected.action == DecisionAction.ABSTAIN
    assert case.expected.key_fact is None
    assert case.expected.source_msg_id is None
    assert case.context_dropped is False


def test_builder_contradicting_draft() -> None:
    """Verify build_contradicting_draft yields a valid Case with action=flag_conflict or correct."""
    case_id, split, domain, _, base_time = _get_test_context()
    rng = random.Random(42)
    case = build_contradicting_draft(case_id, split, rng, domain, base_time)

    validate_case(case)
    assert case.scenario == Scenario.CONTRADICTING_DRAFT
    assert case.expected.action in (DecisionAction.FLAG_CONFLICT, DecisionAction.CORRECT)
    assert case.expected.key_fact == domain["fact_value"]
    assert case.draft_answer is not None


def test_builder_paraphrased() -> None:
    """Verify build_paraphrased shares <= 1 content word with planted text."""
    case_id, split, domain, _, base_time = _get_test_context()
    rng = random.Random(42)
    case = build_paraphrased(case_id, split, rng, domain, base_time)

    validate_case(case)
    assert case.scenario == Scenario.PARAPHRASED
    assert case.expected.action == DecisionAction.ANSWER

    source_msg = next(m for s in case.sessions for m in s if m.id == case.expected.source_msg_id)
    stopwords = {
        "which",
        "what",
        "is",
        "the",
        "a",
        "an",
        "for",
        "in",
        "on",
        "at",
        "to",
        "of",
        "and",
    }
    planted_words = {w.lower().strip(".,?!") for w in source_msg.text.split()} - stopwords
    question_words = {w.lower().strip(".,?!") for w in case.question.split()} - stopwords
    overlap = planted_words & question_words

    assert len(overlap) <= 1


def test_builder_cross_session() -> None:
    """Verify build_cross_session spans 3 sessions with fact in session 1."""
    case_id, split, domain, _, base_time = _get_test_context()
    rng = random.Random(42)
    case = build_cross_session(case_id, split, rng, domain, base_time)

    validate_case(case)
    assert case.scenario == Scenario.CROSS_SESSION
    assert case.expected.action == DecisionAction.ANSWER
    assert len(case.sessions) == 3
    assert case.context_dropped is True

    s1_ids = {m.id for m in case.sessions[0]}
    assert case.expected.source_msg_id in s1_ids


def test_builder_valid_answer() -> None:
    """Verify build_valid_answer yields a valid Case with action=answer and matching draft."""
    case_id, split, domain, _, base_time = _get_test_context()
    rng = random.Random(42)
    case = build_valid_answer(case_id, split, rng, domain, base_time)

    validate_case(case)
    assert case.scenario == Scenario.VALID_ANSWER
    assert case.expected.action == DecisionAction.ANSWER
    assert case.draft_answer is not None
    assert domain["fact_value"] in case.draft_answer


def test_template_variety_and_synonym_bank() -> None:
    """Verify >=3 templates per scenario and synonym bank size >= 40 pairs."""
    assert len(SYNONYM_BANK) >= 40
    assert len(BURIED_FACT_TEMPLATES) >= 3
    assert len(BURIED_QUESTION_TEMPLATES) >= 3
    assert len(DECISION_INITIAL_TEMPLATES) >= 3
    assert len(DECISION_REVERSED_TEMPLATES) >= 3
    assert len(DECISION_CURRENT_QUESTIONS) >= 3
    assert len(DECISION_OLD_QUESTIONS) >= 3
    assert len(UNANSWERABLE_QUESTIONS) >= 3
    assert len(CONTRADICTING_DRAFT_TEMPLATES) >= 3
    assert len(CONTRADICTING_DRAFT_ANSWERS) >= 3
    assert len(PARAPHRASE_PLANTED_TEMPLATES) >= 3
    assert len(PARAPHRASE_QUESTION_TEMPLATES) >= 3
    assert len(CROSS_SESSION_PLANTED_TEMPLATES) >= 3
    assert len(CROSS_SESSION_QUESTIONS) >= 3
    assert len(VALID_ANSWER_PLANTED_TEMPLATES) >= 3
    assert len(VALID_DRAFT_TEMPLATES) >= 3


def test_mix_proportions(tmp_path: Path) -> None:
    """Verify that default mix scales proportionally across all 7 scenarios."""
    # Test n=175 (exactly 25 per scenario)
    cases_175 = generate(seed=42, n=175, output_dir=tmp_path / "175")
    counts_175 = Counter(c.scenario for c in cases_175)
    for scenario in Scenario:
        assert counts_175[scenario] == 25

    # Test n=150 (proportional scaling: 21 or 22 each)
    cases_150 = generate(seed=42, n=150, output_dir=tmp_path / "150")
    counts_150 = Counter(c.scenario for c in cases_150)
    for scenario in Scenario:
        assert counts_150[scenario] in (21, 22)
    assert sum(counts_150.values()) == 150
