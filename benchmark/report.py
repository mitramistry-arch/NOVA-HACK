"""Benchmark report generation: summary.md, failures.md."""

from __future__ import annotations

from typing import Any

from benchmark.metrics import (
    BenchmarkMetrics,
    MetricScore,
    check_correctness,
    normalize_action,
)
from benchmark.schema import Case, Scenario, Split
from benchmark.systems.base import Result

METRIC_LABELS: dict[str, str] = {
    "recall": "Recall (buried/para/cross)",
    "accuracy_after_context_loss": "Accuracy (ctx dropped)",
    "contradiction_precision": "Contradiction Precision",
    "contradiction_recall": "Contradiction Recall",
    "unsupported_claim_rate": "Unsupported Claim Rate ↓",
    "correct_abstention": "Correct Abstention",
    "retrieval_accuracy": "Retrieval Accuracy",
    "false_warning_rate": "False Warning Rate ↓",
}

SCENARIO_METRICS: dict[str, list[Scenario]] = {
    "Buried Fact": [Scenario.BURIED_FACT],
    "Decision Reversed": [Scenario.DECISION_REVERSED],
    "Unanswerable": [Scenario.UNANSWERABLE],
    "Contradicting Draft": [Scenario.CONTRADICTING_DRAFT],
    "Paraphrased": [Scenario.PARAPHRASED],
    "Cross-Session": [Scenario.CROSS_SESSION],
    "Valid Answer": [Scenario.VALID_ANSWER],
}

FAILURE_CATEGORIES = {
    "missed_buried_fact": "Missed buried fact (answer wrong, scenario=buried_fact)",
    "missed_reversal": "Missed decision reversal (wrong answer)",
    "false_abstain": "False abstain (unanswerable but did not abstain)",
    "missed_contradiction": "Missed contradiction (no flag/correct)",
    "false_warning": "False warning (valid answer flagged)",
    "retrieval_miss": "Retrieval miss (source_msg_id not in retrieved_ids)",
    "system_error": "System error (exception while running the case; counted as incorrect)",
}


def _fmt_score(score: MetricScore) -> str:
    if score.value is None:
        return "—"
    pct = f"{score.value * 100:.1f}%"
    if score.ci_low is not None and score.ci_high is not None:
        lo = f"{score.ci_low * 100:.1f}"
        hi = f"{score.ci_high * 100:.1f}"
        return f"{pct} [{lo}–{hi}]"
    return pct


def _scenario_correctness(
    pairs: list[tuple[Case, Result]],
    scenarios: list[Scenario],
) -> str:
    subset = [(c, r) for c, r in pairs if c.scenario in scenarios]
    if not subset:
        return "—"
    n_correct = sum(1 for c, r in subset if check_correctness(c, r))
    pct = n_correct / len(subset) * 100
    return f"{pct:.1f}% ({n_correct}/{len(subset)})"


def _collect_failures(
    pairs_by_system: dict[str, list[tuple[Case, Result]]],
    max_per_cat: int = 10,
) -> dict[str, list[dict]]:
    failures: dict[str, list[dict]] = {cat: [] for cat in FAILURE_CATEGORIES}

    for sys_name, pairs in pairs_by_system.items():
        for case, result in pairs:
            if result.error is not None and len(failures["system_error"]) < max_per_cat:
                failures["system_error"].append(
                    {
                        "case_id": case.id,
                        "system": sys_name,
                        "scenario": case.scenario.value,
                        "expected_action": str(case.expected.action),
                        "got_action": "error",
                        "expected_key_fact": case.expected.key_fact or "",
                        "answer": result.error,
                        "retrieved_ids": [],
                    }
                )
            if case.split != Split.DEV or result.error is not None:
                continue

            action = normalize_action(result.action)
            base = {
                "case_id": case.id,
                "system": sys_name,
                "scenario": case.scenario.value,
                "expected_action": str(case.expected.action),
                "got_action": action,
                "expected_key_fact": case.expected.key_fact or "",
                "answer": result.answer[:200],
                "retrieved_ids": result.retrieved_ids[:5],
            }

            if case.scenario == Scenario.BURIED_FACT and not check_correctness(case, result) and len(failures["missed_buried_fact"]) < max_per_cat:
                failures["missed_buried_fact"].append(base)

            if case.scenario == Scenario.DECISION_REVERSED and not check_correctness(case, result) and len(failures["missed_reversal"]) < max_per_cat:
                failures["missed_reversal"].append(base)

            if case.scenario == Scenario.UNANSWERABLE and action != "abstain" and len(failures["false_abstain"]) < max_per_cat:
                failures["false_abstain"].append(base)

            if case.scenario == Scenario.CONTRADICTING_DRAFT and action not in ("flag_conflict", "correct") and len(failures["missed_contradiction"]) < max_per_cat:
                failures["missed_contradiction"].append(base)

            if case.scenario == Scenario.VALID_ANSWER and action != "answer" and len(failures["false_warning"]) < max_per_cat:
                failures["false_warning"].append(base)

            if (
                case.expected.source_msg_id is not None
                and result.retrieved_ids
                and case.expected.source_msg_id not in result.retrieved_ids
                and len(failures["retrieval_miss"]) < max_per_cat
            ):
                failures["retrieval_miss"].append(base)

    return failures


def write_report(
    run_dir: Any,
    systems: list[str],
    pairs_by_system: dict[str, list[tuple[Case, Result]]],
    metrics_by_system: dict[str, BenchmarkMetrics],
    seed: int,
    n: int,
    split: str | None,
    llm_mode: str,
    limit: int | None,
) -> None:
    """Write summary.md and failures.md to the run directory."""
    from pathlib import Path

    run_dir = Path(run_dir)

    lines = ["# Benchmark Summary\n"]
    lines.append(f"- **Seed**: {seed}  **n**: {n}  **split**: {split or 'all'}  **llm**: {llm_mode}")
    if llm_mode.startswith("fake"):
        lines.append("- **Note**: FAKE LLM responses. These numbers are NOT a real-model evaluation.")
    if limit:
        lines.append(f"  **limit**: {limit}")
    lines.append("")

    # --- Metric × System table ---
    lines.append("## Metric × System\n")
    header = "| Metric | " + " | ".join(systems) + " |"
    sep = "|---|" + "---|" * len(systems)
    lines += [header, sep]

    for key, label in METRIC_LABELS.items():
        cells = []
        for sys_name in systems:
            m = metrics_by_system.get(sys_name)
            score = getattr(m, key, None) if m else None
            cells.append(_fmt_score(score) if isinstance(score, MetricScore) else "—")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    # latency
    lat_row = []
    for sys_name in systems:
        m = metrics_by_system.get(sys_name)
        if m and m.latency_p50 is not None:
            lat_row.append(f"p50={m.latency_p50:.0f}ms p95={m.latency_p95:.0f}ms")
        else:
            lat_row.append("—")
    lines.append("| Latency | " + " | ".join(lat_row) + " |")

    # errors
    err_row = []
    for sys_name in systems:
        m = metrics_by_system.get(sys_name)
        err_row.append(f"{m.error_count}/{m.case_count}" if m else "—")
    lines.append("| System errors | " + " | ".join(err_row) + " |")

    # tokens
    tok_row = []
    for sys_name in systems:
        m = metrics_by_system.get(sys_name)
        if m and m.total_tokens_per_case is not None:
            mark = "~" if m.tokens_estimated else ""
            suffix = " (est.)" if m.tokens_estimated else ""
            tok_row.append(f"{mark}{m.total_tokens_per_case:.0f} tok/case{suffix}")
        else:
            tok_row.append("unknown")
    lines.append("| Tokens/case | " + " | ".join(tok_row) + " |")

    # cost
    cost_row = []
    for sys_name in systems:
        m = metrics_by_system.get(sys_name)
        if m and m.total_cost is not None:
            mark = "~" if m.tokens_estimated else ""
            cost_row.append(f"{mark}${m.total_cost:.4f}")
        else:
            cost_row.append("unknown")
    lines.append("| Total cost | " + " | ".join(cost_row) + " |")

    lines.append("")

    # --- Per-scenario breakdown ---
    lines.append("## Per-Scenario Correctness\n")
    scen_header = "| Scenario | " + " | ".join(systems) + " |"
    scen_sep = "|---|" + "---|" * len(systems)
    lines += [scen_header, scen_sep]

    for scen_label, scenarios in SCENARIO_METRICS.items():
        cells = []
        for sys_name in systems:
            pairs = pairs_by_system.get(sys_name, [])
            cells.append(_scenario_correctness(pairs, scenarios))
        lines.append(f"| {scen_label} | " + " | ".join(cells) + " |")

    lines.append("")

    summary_path = run_dir / "summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")

    # --- failures.md ---
    failures = _collect_failures(pairs_by_system)
    fail_lines = ["# Failure Cases (dev split)\n"]
    fail_lines.append("**Note**: max 10 per category. Read by team after every run.\n")

    any_failures = False
    for cat, items in failures.items():
        if not items:
            continue
        any_failures = True
        label = FAILURE_CATEGORIES[cat]
        fail_lines.append(f"## {label}\n")
        fail_lines.append(f"Total shown: {len(items)}\n")
        for entry in items:
            fail_lines.append(f"- **case_id**: `{entry['case_id']}`  system=`{entry['system']}`")
            fail_lines.append(f"  - expected: `{entry['expected_key_fact']}` (action={entry['expected_action']})")
            fail_lines.append(f"  - got: `{entry['answer'][:120]}` (action={entry['got_action']})")
            if entry["retrieved_ids"]:
                ids_str = ", ".join(f"`{i}`" for i in entry["retrieved_ids"])
                fail_lines.append(f"  - retrieved: {ids_str}")
        fail_lines.append("")

    if not any_failures:
        fail_lines.append("*No failures recorded.*\n")

    failures_path = run_dir / "failures.md"
    failures_path.write_text("\n".join(fail_lines), encoding="utf-8")
