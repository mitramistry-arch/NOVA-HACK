TOKEN RULES (highest priority, override everything else):
1. Read only files named in the task. No tree dumps, no cat of large files; use grep/head -n 40.
2. No plans, explanations, or recaps. Final reply <=5 lines: files changed, test result, assumptions.
3. Write each file once. Edit with str_replace/diffs, never rewrite a whole file for a small change.
4. Tests: `pytest -q -x 2>&1 | tail -20`. Never paste full logs. Max 3 fix attempts, then report the blocker.
5. One-line docstrings. No comments restating code. No unrequested features, files, or refactors.
6. Do exactly one phase, commit once (`phase-N: <what>`), then STOP.

PROJECT RULES:
7. Read nova/core/schemas.py + protocols.py first. Never change core/ without asking the human; propose it instead.
8. Edit only your assigned directories. Other people's code is read-only.
9. All LLM calls go through nova.core.llm.LLMClient. Tests use FakeLLM/HashEmbedder only, never a real API.
10. Absence of evidence is "unsupported", never "false". Every memory keeps a verbatim source_quote.
11. Branch on your fork, small PRs, `ruff` + `pytest` clean before every PR.

QUALITY BAR: Python 3.11, full type hints, Pydantic v2 validation at every boundary, parameterized SQL,
no secrets in code (env via nova/core/config.py), typed exceptions.
