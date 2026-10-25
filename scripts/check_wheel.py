"""Verify that a built wheel contains runtime modules and no development-only dependencies."""

import re
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

REQUIRED_FILES = (
    "nova/__init__.py",
    "nova/api/main.py",
    "nova/core/config.py",
    "nova/providers/openai_compat.py",
    "nova/security/net.py",
    "nova/store/schema.py",
    "nova/store/sqlite_store.py",
    "nova/verification/claim_splitter.py",
    "nova/verification/judge.py",
)
FORBIDDEN_PREFIXES = ("tests/", "benchmark/", "scripts/", ".github/", "docs/")
REQUIRED_RUNTIME = ("pydantic", "numpy", "fastapi", "httpx", "httpcore", "cryptography")
DEV_ONLY = ("pytest", "ruff", "build", "uvicorn", "python-dotenv", "sentence-transformers")


def _name(requirement: str) -> str:
    """Return the normalized distribution name from a Requires-Dist metadata line."""
    return re.split(r"[\s;\[<>=!~(]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")


def check(wheel: Path) -> list[str]:
    """Return distribution problems; an empty list means the wheel passed all checks."""
    problems: list[str] = []
    try:
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
            for required in REQUIRED_FILES:
                if required not in names:
                    problems.append(f"missing required file: {required}")
            for name in names:
                if name.startswith(FORBIDDEN_PREFIXES):
                    problems.append(f"unexpected development file shipped: {name}")
            metadata_name = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
            if metadata_name is None:
                problems.append("no METADATA found")
                return problems
            meta = Parser().parsestr(zf.read(metadata_name).decode("utf-8"))
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        return [f"wheel could not be read: {type(exc).__name__}"]

    requirements = meta.get_all("Requires-Dist") or []
    unconditional = [_name(r) for r in requirements if "extra ==" not in r]
    for dep in REQUIRED_RUNTIME:
        if dep not in unconditional:
            problems.append(f"runtime dependency missing from metadata: {dep}")
    for dep in DEV_ONLY:
        if dep in unconditional:
            problems.append(f"non-runtime dependency leaked into runtime requirements: {dep}")
    return problems


def main(argv: list[str]) -> int:
    """CLI entry point; return a process exit code."""
    if len(argv) != 2:
        print("Usage: python scripts/check_wheel.py dist/nova-*.whl", file=sys.stderr)
        return 2
    problems = check(Path(argv[1]))
    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print(f"OK: {argv[1]} contains the intended distribution")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
