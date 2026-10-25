"""Fail if a built frontend contains deployment secrets, source maps, or client API-key headers."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SECRET_ENV_VARS = (
    "NOVA_API_KEY",
    "NOVA_TENANT_API_KEYS",
    "NOVA_ENCRYPTION_KEY",
    "NOVA_ENCRYPTION_KEYS_PREVIOUS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)
TEXT_SUFFIXES = {".html", ".js", ".css", ".json", ".svg", ".txt"}
API_KEY_HEADER = re.compile(r"x-api-key", re.IGNORECASE)
BROWSER_STORAGE_WRITE = re.compile(
    r"\b(?:localStorage|sessionStorage)\s*\.\s*(?:setItem|removeItem|clear)\s*\(|\bdocument\s*\.\s*cookie\b",
    re.IGNORECASE,
)


def check_assets(root: str | Path, *, environment: dict[str, str] | None = None) -> list[str]:
    """Return violations found in a compiled browser asset directory."""
    directory = Path(root)
    if not directory.is_dir():
        return [f"frontend build directory does not exist: {directory}"]

    files = [path for path in directory.rglob("*") if path.is_file()]
    if not files:
        return [f"frontend build directory is empty: {directory}"]
    if not any(path.name == "index.html" for path in files):
        return [f"frontend build is missing index.html: {directory}"]

    errors: list[str] = []
    if any(path.suffix == ".map" for path in files):
        errors.append("source maps are not allowed in the release frontend bundle")

    env = os.environ if environment is None else environment
    secrets = [env[name] for name in SECRET_ENV_VARS if env.get(name)]
    text_files = [path for path in files if path.suffix.lower() in TEXT_SUFFIXES]
    if not any(path.suffix.lower() == ".js" for path in text_files):
        errors.append("frontend build contains no JavaScript bundle")

    for path in text_files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            errors.append(f"could not read frontend asset as UTF-8: {path.relative_to(directory)}")
            continue
        relative_path = path.relative_to(directory)
        if API_KEY_HEADER.search(content):
            errors.append(
                f"browser bundle must not set or mention a server API-key header: {relative_path}"
            )
        if BROWSER_STORAGE_WRITE.search(content):
            errors.append(
                "browser bundle must not persist credentials or user content in "
                f"browser storage: {relative_path}"
            )
        for secret in secrets:
            if secret in content:
                errors.append(
                    "a configured secret value appears in a frontend asset: "
                    f"{path.relative_to(directory)}"
                )
                break
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            "Usage: python scripts/check_frontend_assets.py <frontend-dist-directory>",
            file=sys.stderr,
        )
        return 2
    errors = check_assets(args[0])
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Frontend asset check passed: {args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
