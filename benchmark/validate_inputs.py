"""Validate benchmark workflow inputs (stdlib only; runs before dependencies install)."""

from __future__ import annotations

import os
import re
import sys

VALID_SYSTEMS = ("baseline", "rag", "anchor")
VALID_SPLITS = ("dev", "test")
VALID_LLMS = ("fake", "real")
MAX_N = 10_000

_DIGITS = re.compile(r"[0-9]{1,9}")
_SYSTEMS = re.compile(r"[a-z]+(?:,[a-z]+)*")

DEFAULTS = {"n": "150", "split": "dev", "systems": "baseline,rag", "llm": "fake", "seed": "7"}


def validate(n: str, split: str, systems: str, llm: str, seed: str) -> list[str]:
    """Return the names of invalid fields; an empty list means all inputs are acceptable."""
    bad: list[str] = []
    if not _DIGITS.fullmatch(n) or not 1 <= int(n) <= MAX_N:
        bad.append("n")
    if not _DIGITS.fullmatch(seed):
        bad.append("seed")
    if split not in VALID_SPLITS:
        bad.append("split")
    if llm not in VALID_LLMS:
        bad.append("llm")
    if not _SYSTEMS.fullmatch(systems) or any(s not in VALID_SYSTEMS for s in systems.split(",")):
        bad.append("systems")
    return bad


def main() -> int:
    """Read INPUT_* environment variables; exit non-zero if any value is invalid."""
    values = {k: os.environ.get(f"INPUT_{k.upper()}") or d for k, d in DEFAULTS.items()}
    bad = validate(**values)
    if bad:
        print(f"Invalid workflow input(s): {', '.join(bad)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
