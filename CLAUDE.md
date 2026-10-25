# Nova engineering handoff

## Read this first

This file replaces the original phased-assignment handoff below. That original text describes an
older repository state (including a stub `Anchor`, “not started” verification wiring, and an offline
Pydantic test shim). Those statements are historical, not current. Use the code and tests in this
checkout as the source of truth.

**Quality bar:** production-oriented engineering, no duplicate implementations, no test weakening,
no unsupported claims of completeness. Read existing modules and tests before writing new code. Make
small changes, preserve public contracts unless a migration is planned, test root causes, and report
any unverified behavior honestly. `FakeLLM`'s scripted response behavior is a test contract; do not
replace it with a live model or change it as a shortcut.

## Current integrated architecture

This checkout merges compatible work from the updated verification ZIP and the four phase reference
archives. They were separate branches, not consecutive snapshots, so do not copy one archive over
the whole repository. Phase 2 is the storage/API architecture baseline; Phase 4's old single-tenant
SQLite migration and lifecycle implementation is not compatible and must not be pasted over the
current storage-session design.

- **API and configuration:** `nova/api/main.py`, `runtime.py`, `errors.py`, `middleware.py`,
  `limits.py`; `nova/core/config.py`, `tenant.py`, `policy.py`.
- **Storage:** shared `Database` abstraction, tenant-bound `StorageSession`, SQLite default, optional
  tenant-owned PostgreSQL (`nova/store/*`). A PostgreSQL-configured tenant must not silently fall
  back to SQLite if its database is unavailable.
- **Secrets and integrations:** `nova/security/secrets.py`, `redact.py`; provider config/service and
  OpenAI-compatible HTTP adapter in `nova/providers/`.
- **Outbound security:** `nova/security/net.py` vets DNS results and pins outbound connections for
  public provider hosts. Its HTTPX transport uses a constrained `httpcore` 1.0.x interface, so retain
'the `<1.1` dependency bound until the transport is migrated to a supported public hook or verified
against a newer version. Egress firewall rules remain necessary defense in depth.
- **Ingestion and retrieval:** `nova/extraction/*`, `nova/retrieval/*`, `nova/temporal/supersession.py`,
  `nova/pipeline.py`. Embedding records are prepared before ingest write transactions; missing or
  incompatible vectors may be re-embedded through bounded maintenance.
- **Verification (existing code—do not recreate):** `nova/verification/claim_splitter.py`,
  `judge.py`, `decision.py`, `evidence.py`, `prompts.py`, `service.py`. Claim splitting is bounded and
  fails explicitly rather than silently truncating; model-provided IDs/verdicts are validated in
  code; prompts encode user and memory text as data; historical claims require explicit
  `include_history` intent and verified supersession links. `nova/pipeline.py` wires `ask()` and
  `verify()` through this service.
- **Audit:** `nova/audit/logger.py` provides bounded/redacted per-tenant records and usage/latency
  fields. Audit retention runs at startup for shared SQLite and configured tenant storage, continuing
  past an unavailable remote tenant.
- **Benchmark:** `benchmark/` provides deterministic fake-LLM datasets, generation, execution,
  reporting, cache fingerprints, and validated inputs. Workflow inputs are checked in the manual
  benchmark job before command execution.
- **Packaging/CI:** `pyproject.toml`, `.github/workflows/ci.yml`,
  `.github/workflows/benchmark.yml`, `scripts/check_wheel.py`. Generated cache and benchmark result
  data must stay untracked.

## Configuration and security contracts

- Missing API authentication fails production startup. Keyless operation is limited to explicit
  development/test/local modes.
- Configure either `NOVA_API_KEY` (default tenant only) or `NOVA_TENANT_API_KEYS` (each key maps to
  one fixed tenant). `X-Tenant-ID` must never override key ownership.
- Production requires a valid encryption key; production `FakeLLM` is prohibited.
- `DATABASE_URL` is deliberately rejected. Configure user-managed PostgreSQL using the supported
  tenant storage integration. Remote PostgreSQL requires TLS and pinned DNS/host validation.
- Never include actual API keys, connection strings, passwords, or user content in logs, fixtures,
  docs, or commits. Avoid broad exception swallowing. Public errors should remain stable and must
  not disclose internal connection details.
- The `NOVA_ALLOW_PRIVATE_ENDPOINTS` opt-in weakens outbound endpoint restrictions; do not enable it
  unless the operator explicitly controls that network boundary.
- Keep all SQL parameterized. Tenant identity must be applied by store-layer queries, not only by
  API routing.

## Test and release workflow

Run from the repository root after any code change:

```bash
ruff check .
ruff format --check .
pytest -ra
python -m compileall -q nova benchmark scripts tests
python -m build
python scripts/check_wheel.py dist/*.whl
```

`ResourceWarning` is configured as an error. Do not suppress it to turn a failed run green. CI also
uses Python 3.11, 3.12, and 3.13; benchmark smoke and isolated wheel install are part of CI. The
reference ZIPs' independent test counts are historical and do not establish the integrated result.

Current environment limitations must be reported accurately. Measured on 2026-10-25 with an isolated
Python 3.11 venv: `ruff` and `build` install and run, `python -m build` and `scripts/check_wheel.py`
pass, but `ruff check .` (87 errors) and `ruff format --check .` (25 files) fail, so the CI `lint` job
is red at this revision. `pytest` passes 492 tests. The frontend installs from the registry, and
`npm run lint` passes, but `npm run typecheck`, `npm test`, and `npm run build` fail. Python 3.12/3.13,
real PostgreSQL, real provider calls, and Playwright remain unverified. A passing pytest run does not
equal production certification. See `docs/production-readiness-matrix.md`.
See `docs/release-checklist.md` for the deployment gate.

## Phase status and remaining review

This is a merged working tree, not an assertion that all phases are complete. Backend tests cover behavior exercised in this environment; production readiness still needs the complete release checklist and target-deployment validation. A React/TypeScript Phase 5 frontend source tree is now present under `frontend/` with typed API calls, accessible workflow screens, component tests, and Playwright smoke tests. Dependencies install from the registry and lint passes, but type-checking fails on a duplicated Vite/Vitest type identity, three component tests fail, the production build stops at type-checking, Playwright has not been executed, and no dependency lockfile is committed. In particular:

- Run Ruff lint and formatting after the final merge; fix issues rather than loosening rules.
- Generate and commit `frontend/package-lock.json` using a successful `npm install`, then run frontend typecheck, lint, component tests, production build, and Playwright E2E (including the local FastAPI contract smoke test). Do not call Phase 5 complete until these pass.
- Keep the Vite proxy local-only. Hosted UI deployment is unsupported until an authenticated same-origin BFF/session layer maps each user to a permitted tenant credential; never expose backend API keys in browser code.
- Run a clean wheel/sdist build and install the wheel in a runtime-only environment.
- Exercise PostgreSQL integration against a real supported server in addition to fake-driver tests.
- Verify backup/restore, encryption-key rotation, operational monitoring, rate limits and network
  egress policy in the actual deployment.
- Recheck tenant isolation, DNS rebinding, redirect handling, resource cleanup, current-vs-history
  verification semantics, retention deletion, and schema migrations whenever touching those paths.
- Do not claim zero vulnerabilities or complete production readiness from a green unit-test suite.

When a change is made, update tests and this file only when the architecture/status meaningfully
changes. Preserve `CLAUDE.md` as a useful handoff, not as a transcript of stale work notes.
