"""Benchmark runner: evaluate systems against generated cases and emit results."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json as _json_mod
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from benchmark.counting_llm import CountingLLM
from benchmark.generator import generate
from benchmark.metrics import check_correctness, compute_metrics
from benchmark.report import write_report
from benchmark.schema import Case, Split
from benchmark.systems.anchor import AnchorSystem
from benchmark.systems.base import Result
from benchmark.systems.baseline import BaselineSystem
from benchmark.systems.rag import RAGSystem
from benchmark.validate_inputs import VALID_SYSTEMS
from nova.core.llm import FakeLLM, LLMClient
from nova.retrieval.embedder import HashEmbedder

CACHE_DIR = Path("benchmark/.cache")
RESULTS_DIR = Path("benchmark/results")

ABSTAIN_ANSWER = "I do not have enough information to answer."
FAKE_LLM_LABEL = "fake (simulated responses; NOT a real-model evaluation)"


class BenchmarkError(Exception):
    """Base class for benchmark configuration and execution failures."""


class RealLLMUnsupportedError(BenchmarkError, NotImplementedError):
    """Raised when --llm real is requested but no real provider is implemented."""


class SystemInitError(BenchmarkError):
    """Raised when a requested system cannot be initialized."""


class BenchmarkRunError(BenchmarkError):
    """Raised after a run completes with system errors when failing on errors."""

    def __init__(self, run_dir: Path, n_errors: int) -> None:
        """Record the run directory and the number of failed case executions."""
        super().__init__(f"{n_errors} case execution(s) failed with system errors; see {run_dir}")
        self.run_dir = run_dir
        self.n_errors = n_errors


class CachingLLM:
    """Disk cache whose key covers namespace, provider, model, prompt, JSON mode, settings."""

    def __init__(
        self,
        inner: LLMClient,
        cache_dir: Path = CACHE_DIR,
        *,
        provider: str = "fake",
        model: str = "fake-model",
        settings: dict[str, Any] | None = None,
        namespace: str | None = None,
    ) -> None:
        """Initialize caching wrapper around an LLMClient."""
        self.inner = inner
        self.cache_dir = cache_dir
        self.provider = provider
        self.model = model
        self.settings = dict(settings or {})
        self.namespace = namespace or ("fake" if provider.startswith("fake") else "real")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.hits: int = 0
        self.misses: int = 0

    def _key(self, prompt: str, json_mode: bool) -> str:
        identity = _json_mod.dumps(
            {
                "namespace": self.namespace,
                "provider": self.provider,
                "model": self.model,
                "json": json_mode,
                "settings": self.settings,
                "prompt": prompt,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _cache_path(self, prompt: str, json_mode: bool = False) -> Path:
        return self.cache_dir / f"{self._key(prompt, json_mode)}.json"

    def _read(self, path: Path, key: str) -> str | None:
        """Return the cached response, or None if missing, corrupt, or mismatched."""
        try:
            data = _json_mod.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("key") != key:
            return None
        response = data.get("response")
        return response if isinstance(response, str) else None

    def complete(self, prompt: str, *, json: bool = False) -> str:
        """Return a cached response for this exact configuration, else call the inner LLM."""
        key = self._key(prompt, json)
        path = self.cache_dir / f"{key}.json"
        cached = self._read(path, key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        response = self.inner.complete(prompt, json=json)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(_json_mod.dumps({"key": key, "response": response}), encoding="utf-8")
        os.replace(tmp, path)
        return response


class _BenchmarkSmokeLLM:
    """Adapt FakeLLM for a no-network smoke run without pretending to extract facts.

    The real FakeLLM remains unchanged. In anchor benchmark mode, ingestion uses JSON output;
    returning an empty, valid extraction list lets the pipeline exercise its storage/retrieval/
    verification path without fabricating source memories. Answer generation remains the same
    canned abstention used by the other fake benchmark systems. These results are not a quality
    evaluation and must not be compared with a real model.
    """

    def __init__(self) -> None:
        self._inner = FakeLLM(default_response=ABSTAIN_ANSWER)

    def complete(self, prompt: str, *, json: bool = False) -> str:
        if json and prompt.startswith("Extract durable memories from the conversation turns."):
            return "[]"
        return self._inner.complete(prompt, json=json)


def _make_fake_llm() -> LLMClient:
    return _BenchmarkSmokeLLM()


def _build_anchor(llm: LLMClient) -> AnchorSystem:
    """Build the Anchor system with a fresh in-memory pipeline per case."""
    from nova.extraction.extractor import LLMExtractor
    from nova.pipeline import Anchor
    from nova.retrieval.hybrid import HybridRetriever
    from nova.store.sqlite_store import SqliteMemoryStore

    counting = CountingLLM(llm)

    def factory() -> Anchor:
        store = SqliteMemoryStore(":memory:")
        retriever = HybridRetriever(store=store, embedder=HashEmbedder())
        return Anchor(store=store, extractor=LLMExtractor(llm=counting), retriever=retriever)

    factory()
    return AnchorSystem(llm=counting, anchor_factory=factory)


def _build_systems(system_names: list[str], llm: LLMClient) -> dict[str, Any]:
    """Build every requested system; raise if any is unknown or fails to initialize."""
    unknown = [n for n in system_names if n not in VALID_SYSTEMS]
    if unknown:
        raise SystemInitError(f"Unknown system(s): {', '.join(unknown)}; valid: {VALID_SYSTEMS}")
    systems: dict[str, Any] = {}
    for name in system_names:
        try:
            if name == "baseline":
                systems[name] = BaselineSystem(llm)
            elif name == "rag":
                systems[name] = RAGSystem(llm, embedder=HashEmbedder(), k=5)
            else:
                systems[name] = _build_anchor(llm)
        except Exception as exc:  # noqa: BLE001
            raise SystemInitError(f"System '{name}' failed to initialize: {exc}") from exc
    return systems


def _filter_cases(cases: list[Case], split: str | None) -> list[Case]:
    if split is None:
        return cases
    wanted = Split.DEV if split == "dev" else Split.TEST
    return [c for c in cases if c.split == wanted]


def _run_case(system: Any, case: Case) -> Result:
    return system.run(case)


def _case_row(
    case: Case,
    system_name: str,
    result: Result,
    expected_correct: bool,
) -> dict[str, str]:
    return {
        "case_id": case.id,
        "scenario": case.scenario.value,
        "split": case.split.value,
        "context_dropped": str(case.context_dropped),
        "system": system_name,
        "expected_action": str(case.expected.action),
        "got_action": str(result.action),
        "expected_key_fact": case.expected.key_fact or "",
        "answer": result.answer,
        "correct": str(expected_correct),
        "retrieved_ids": ";".join(result.retrieved_ids),
        "latency_ms": "" if result.error is not None else f"{result.latency_ms:.2f}",
        "prompt_tokens": "" if result.prompt_tokens is None else str(result.prompt_tokens),
        "completion_tokens": (
            "" if result.completion_tokens is None else str(result.completion_tokens)
        ),
        "tokens_estimated": str(result.tokens_estimated),
        "error": result.error or "",
    }


def run_benchmark(
    seed: int = 7,
    n: int = 150,
    split: str | None = None,
    system_names: list[str] | None = None,
    llm_mode: str = "fake",
    limit: int | None = None,
    cases_dir: Path | None = None,
    results_root: Path | None = None,
    run_id: str | None = None,
    fail_on_error: bool = False,
) -> Path:
    """Execute benchmark run and write results to disk.

    Raises RealLLMUnsupportedError for llm_mode="real" and SystemInitError if a requested
    system cannot start. With fail_on_error, raises BenchmarkRunError after writing outputs
    if any case execution raised.
    """
    if system_names is None:
        system_names = ["baseline", "rag"]

    if llm_mode == "real":
        raise RealLLMUnsupportedError(
            "--llm real is not implemented: no real LLM provider is wired into the benchmark. "
            "Use --llm fake (results are simulated, not a real-model evaluation)."
        )
    if llm_mode != "fake":
        raise BenchmarkError(f"Unknown llm mode: {llm_mode!r}")
    if not system_names:
        raise SystemInitError("No systems requested")

    llm: LLMClient = CachingLLM(
        _make_fake_llm(),
        cache_dir=CACHE_DIR,
        provider="fake",
        model="fake-smoke-adapter",
        settings={"mode": "offline-smoke"},
        namespace="fake-anchor-smoke-v2",
    )
    systems = _build_systems(system_names, llm)

    cases_output_dir = cases_dir or Path("benchmark/cases")
    cases = generate(seed=seed, n=n, output_dir=cases_output_dir)
    cases = _filter_cases(cases, split)

    if limit is not None:
        cases = cases[:limit]

    run_id = run_id or str(uuid.uuid4())[:8]
    results_base = results_root or RESULTS_DIR
    run_dir = results_base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, str]] = []
    pairs_by_system: dict[str, list[tuple[Case, Result]]] = {name: [] for name in systems}

    n_errors = 0
    for case_idx, case in enumerate(cases):
        for sys_name, system in systems.items():
            t_case = time.perf_counter()
            try:
                result = _run_case(system, case)
            except Exception as exc:  # noqa: BLE001
                n_errors += 1
                print(f"[error] case {case.id} system {sys_name}: {exc!r}", file=sys.stderr)
                result = Result(
                    answer="",
                    action="error",
                    retrieved_ids=[],
                    latency_ms=(time.perf_counter() - t_case) * 1000.0,
                    prompt_tokens=None,
                    completion_tokens=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            pairs_by_system[sys_name].append((case, result))

            correct = check_correctness(case, result)
            row = _case_row(case, sys_name, result, correct)
            all_rows.append(row)

        if (case_idx + 1) % 10 == 0:
            print(f"  [{case_idx + 1}/{len(cases)}] cases processed", file=sys.stderr)

    csv_path = run_dir / "results.csv"
    if all_rows:
        fieldnames = list(all_rows[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)

    metrics_by_system: dict[str, Any] = {}
    for sys_name, pairs in pairs_by_system.items():
        metrics_by_system[sys_name] = compute_metrics(pairs)

    write_report(
        run_dir=run_dir,
        systems=list(systems.keys()),
        pairs_by_system=pairs_by_system,
        metrics_by_system=metrics_by_system,
        seed=seed,
        n=n,
        split=split,
        llm_mode=FAKE_LLM_LABEL,
        limit=limit,
    )

    if fail_on_error and n_errors:
        raise BenchmarkRunError(run_dir, n_errors)
    return run_dir


def _parse_systems(value: str) -> list[str]:
    """Parse and validate a comma-separated system list."""
    names = [x.strip() for x in value.split(",") if x.strip()]
    bad = [x for x in names if x not in VALID_SYSTEMS]
    if bad or not names:
        raise argparse.ArgumentTypeError(
            f"invalid systems {bad or value!r}; choose from {','.join(VALID_SYSTEMS)}"
        )
    return names


def main(argv: list[str] | None = None) -> int:
    """Entry point for CLI benchmark runner; returns a process exit code."""
    parser = argparse.ArgumentParser(description="Nova Context Anchor benchmark runner")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--n", type=int, default=150)
    parser.add_argument("--split", choices=["dev", "test"], default=None)
    parser.add_argument("--systems", type=_parse_systems, default=["baseline", "rag"])
    parser.add_argument("--llm", choices=["fake", "real"], default="fake", dest="llm_mode")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--allow-errors", action="store_true", help="exit 0 even if system errors occurred"
    )
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    try:
        run_dir = run_benchmark(
            seed=args.seed,
            n=args.n,
            split=args.split,
            system_names=args.systems,
            llm_mode=args.llm_mode,
            limit=args.limit,
            fail_on_error=not args.allow_errors,
        )
    except BenchmarkRunError as exc:
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        return 1
    except BenchmarkError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    elapsed = time.perf_counter() - t0

    print(f"\nRun complete in {elapsed:.1f}s → {run_dir}")
    summary = run_dir / "summary.md"
    if summary.exists():
        print(summary.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
