"""Unit tests for benchmark runner: pipeline, caching, outputs, and failure detection."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from benchmark.runner import CachingLLM, _filter_cases, run_benchmark
from benchmark.schema import Case, Expected, Scenario, Split
from nova.core.llm import FakeLLM
from nova.core.schemas import DecisionAction, Message


def _make_case(
    case_id: str,
    scenario: Scenario,
    split: Split,
    context_dropped: bool = True,
    key_fact: str | None = "PostgreSQL 16.2",
) -> Case:
    """Helper that returns a minimal Case with one planted message."""
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    planted_id = f"planted_{case_id}"
    text = f"The database is {key_fact}." if key_fact else "No relevant fact mentioned."
    msgs = [
        Message(id=planted_id, session_id="s1", role="user", text=text, timestamp=now),
        *[
            Message(
                id=f"fill_{case_id}_{i}",
                session_id="s1",
                role="user",
                text=f"Neutral {i}.",
                timestamp=now,
            )
            for i in range(3)
        ],
    ]
    return Case(
        id=case_id,
        scenario=scenario,
        split=split,
        sessions=[msgs],
        context_dropped=context_dropped,
        question="What is the database?",
        expected=Expected(
            action=DecisionAction.ANSWER
            if scenario != Scenario.UNANSWERABLE
            else DecisionAction.ABSTAIN,
            key_fact=key_fact,
            source_msg_id=planted_id if key_fact else None,
        ),
    )


# ─── CachingLLM ──────────────────────────────────────────────────────────────


def test_caching_llm_writes_and_reads_cache(tmp_path: Path) -> None:
    """Verify CachingLLM writes response to disk and returns it on second call."""
    inner = FakeLLM(default_response="cached answer")
    cache = CachingLLM(inner, cache_dir=tmp_path / "cache")

    r1 = cache.complete("hello prompt")
    assert r1 == "cached answer"
    assert cache.misses == 1
    assert cache.hits == 0

    # Second call: must hit cache, not inner
    inner2 = FakeLLM(default_response="different answer")
    cache2 = CachingLLM(inner2, cache_dir=tmp_path / "cache")
    r2 = cache2.complete("hello prompt")
    assert r2 == "cached answer"
    assert cache2.hits == 1
    assert cache2.misses == 0


def test_caching_llm_key_is_sha256_of_full_configuration(tmp_path: Path) -> None:
    """Cache identity includes namespaced configuration, prompt text, and JSON mode."""
    inner = FakeLLM(default_response="y")
    cache = CachingLLM(inner, cache_dir=tmp_path / "c")
    prompt = "unique test prompt 42"
    cache.complete(prompt)
    expected_key = cache._key(prompt, False)
    assert len(expected_key) == 64
    assert (tmp_path / "c" / f"{expected_key}.json").exists()
    assert prompt not in expected_key


def test_caching_llm_different_prompts_separate_files(tmp_path: Path) -> None:
    """Verify each distinct prompt gets its own cache file."""
    inner = FakeLLM(responses=["a", "b"])
    cache = CachingLLM(inner, cache_dir=tmp_path / "c")
    cache.complete("prompt one")
    cache.complete("prompt two")
    files = list((tmp_path / "c").glob("*.json"))
    assert len(files) == 2


# ─── _filter_cases ───────────────────────────────────────────────────────────


def test_filter_cases_dev(tmp_path: Path) -> None:
    """Verify filter returns only dev cases when split='dev'."""
    cases = [
        _make_case("c1", Scenario.BURIED_FACT, Split.DEV),
        _make_case("c2", Scenario.BURIED_FACT, Split.TEST),
        _make_case("c3", Scenario.BURIED_FACT, Split.DEV),
    ]
    filtered = _filter_cases(cases, "dev")
    assert all(c.split == Split.DEV for c in filtered)
    assert len(filtered) == 2


def test_filter_cases_none_returns_all(tmp_path: Path) -> None:
    """Verify filter with split=None returns all cases."""
    cases = [
        _make_case("c1", Scenario.BURIED_FACT, Split.DEV),
        _make_case("c2", Scenario.BURIED_FACT, Split.TEST),
    ]
    assert _filter_cases(cases, None) == cases


# ─── run_benchmark outputs ───────────────────────────────────────────────────


def test_run_benchmark_writes_csv_and_markdown(tmp_path: Path) -> None:
    """Verify run_benchmark writes results.csv, summary.md, and failures.md."""
    run_dir = run_benchmark(
        seed=42,
        n=14,
        split="dev",
        system_names=["baseline", "rag"],
        llm_mode="fake",
        limit=7,
        cases_dir=tmp_path / "cases",
        results_root=tmp_path / "results",
        run_id="test-run",
    )

    assert (run_dir / "results.csv").exists()
    assert (run_dir / "summary.md").exists()
    assert (run_dir / "failures.md").exists()


def test_results_csv_has_expected_columns(tmp_path: Path) -> None:
    """Verify results.csv contains all expected column headers."""
    run_dir = run_benchmark(
        seed=42,
        n=14,
        split="dev",
        system_names=["baseline"],
        llm_mode="fake",
        limit=7,
        cases_dir=tmp_path / "cases",
        results_root=tmp_path / "results",
        run_id="col-check",
    )

    csv_path = run_dir / "results.csv"
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

    required = {
        "case_id",
        "scenario",
        "split",
        "system",
        "expected_action",
        "got_action",
        "answer",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
    }
    assert required.issubset(set(headers))


def test_results_csv_row_count(tmp_path: Path) -> None:
    """Verify results.csv has one row per case per system."""
    run_dir = run_benchmark(
        seed=42,
        n=14,
        split="dev",
        system_names=["baseline", "rag"],
        llm_mode="fake",
        limit=6,
        cases_dir=tmp_path / "cases",
        results_root=tmp_path / "results",
        run_id="row-count",
    )

    csv_path = run_dir / "results.csv"
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # 6 cases × 2 systems = 12 rows
    assert len(rows) == 12


def test_summary_md_contains_metric_table(tmp_path: Path) -> None:
    """Verify summary.md contains expected metric labels."""
    run_dir = run_benchmark(
        seed=42,
        n=14,
        split="dev",
        system_names=["baseline"],
        llm_mode="fake",
        limit=7,
        cases_dir=tmp_path / "cases",
        results_root=tmp_path / "results",
        run_id="summary-check",
    )

    text = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert "Recall" in text
    assert "Contradiction Precision" in text
    assert "False Warning Rate" in text
    assert "Latency" in text
    assert "Per-Scenario" in text


def test_failures_md_dev_only(tmp_path: Path) -> None:
    """Verify failures.md exists and is generated for dev split."""
    run_dir = run_benchmark(
        seed=42,
        n=14,
        split="dev",
        system_names=["baseline"],
        llm_mode="fake",
        limit=7,
        cases_dir=tmp_path / "cases",
        results_root=tmp_path / "results",
        run_id="fail-check",
    )

    text = (run_dir / "failures.md").read_text(encoding="utf-8")
    assert "Failure Cases (dev split)" in text


def test_limit_flag_restricts_cases(tmp_path: Path) -> None:
    """Verify --limit flag restricts the number of evaluated cases."""
    run_dir = run_benchmark(
        seed=42,
        n=14,
        split=None,
        system_names=["baseline"],
        llm_mode="fake",
        limit=3,
        cases_dir=tmp_path / "cases",
        results_root=tmp_path / "results",
        run_id="limit-check",
    )

    csv_path = run_dir / "results.csv"
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 3  # 3 cases × 1 system


def test_benchmark_fake_llm_emits_valid_empty_extraction_without_changing_core_fake_llm() -> None:
    from benchmark.runner import ABSTAIN_ANSWER, _make_fake_llm
    from nova.core.llm import FakeLLM
    from nova.extraction.prompts import EXTRACTION_PROMPT

    client = _make_fake_llm()
    assert client.complete(EXTRACTION_PROMPT, json=True) == "[]"
    assert client.complete("answer prompt") == ABSTAIN_ANSWER
    assert FakeLLM(default_response="unchanged").complete("answer prompt") == "unchanged"


def test_anchor_fake_benchmark_smoke_has_no_system_errors_and_uses_its_own_cache(
    tmp_path: Path, monkeypatch
) -> None:
    """Anchor smoke uses valid empty extraction output without fabricating memory facts."""
    import benchmark.runner as runner

    monkeypatch.setattr(runner, "CACHE_DIR", tmp_path / "cache")
    run_dir = runner.run_benchmark(
        seed=7,
        n=1,
        split="dev",
        system_names=["anchor"],
        llm_mode="fake",
        limit=1,
        cases_dir=tmp_path / "cases",
        results_root=tmp_path / "results",
        run_id="anchor-smoke",
    )

    with (run_dir / "results.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["error"] == ""
    assert "NOT a real-model evaluation" in (run_dir / "summary.md").read_text(encoding="utf-8")
    cache_files = list((tmp_path / "cache").glob("*.json"))
    assert cache_files
