# Phase 1 report — repository baseline, architecture contract, production gap map

**Status: COMPLETE** (assessment phase; no production behaviour was changed).

| Field | Value |
|---|---|
| Branch | `arena/775ac705-nova-hack` |
| Base revision | `624e8033fb2142367a687cfd09561442f9ca1adc` |
| Phase | 1 of 10 — baseline, architecture conformance, gap map |
| Environment | Python 3.11.2 (isolated venv, `pip install -e ".[dev,server]"`), Node 22.22.3, npm 10.9.8, ruff 0.17.2, pytest 8.4.2 |

## Deliverables

- `docs/production-readiness-matrix.md` — exact baseline commands/results (§1), request-path trace
  (§2), verified-fact confirmations (§3), architecture conformance (§4), P0/P1/P2 gap matrix with
  evidence and one owning phase per item (§5), doc-vs-source discrepancies (§6), dependency order
  (§7), explicit non-claims (§8), readiness statement (§9).
- `docs/architecture-decisions.md` — twelve human decisions (`AD-1`…`AD-12`) with the safe
  provider-neutral boundary already implemented for each.
- `README.md` — the frontend status paragraph corrected to the measured outcome (it previously stated
  dependencies *could not* be installed).
- `CLAUDE.md` — the stale "ruff and build are not installed / index unreachable" limitation note and
  the frontend validation note corrected to measured facts.

No test, fixture, configuration, dependency, or security check was changed, weakened, or removed.

## Commands run and results

Passed:

```
pip install -e ".[dev,server]"                          # in an isolated venv (system Python is externally managed)
pytest -q -W error::ResourceWarning                     # 492 passed, 1 warning (StarletteDeprecationWarning from site-packages)
python -m compileall -q nova benchmark scripts tests    # exit 0
python -m build                                         # wheel + sdist (setuptools license-table deprecation warning)
python scripts/check_wheel.py dist/*.whl                # OK
# clean venv: pip install dist/*.whl && pip check && import nova.api.main   -> No broken requirements; import ok; pytest absent
python -m benchmark.runner --seed 7 --n 30 --split dev --systems baseline,rag --llm fake --limit 30   # exit 0, 0/15 system errors
git ls-files | grep -E '<ci.yml hygiene pattern>'       # no tracked caches/DBs/bytecode/.env/build output
git diff --check                                        # clean
cd frontend && npm install --no-package-lock            # 307 packages (registry reachable)
cd frontend && npm run lint                             # exit 0
```

Failed (real, reproducible, all pre-existing at the base revision):

```
ruff check .                    # 87 errors (78 E501, 3 I001, 2 SIM905, 1 SIM102, 1 SIM117, 1 UP037, 1 F841)
ruff format --check .           # 25 files would be reformatted, 82 already formatted
cd frontend && npm run typecheck  # exit 2: vite.config.ts(30,13) TS2769 (nested vite 5.4.21 vs root vite 6.0.7)
cd frontend && npm test           # exit 1: 3 failed / 9 passed of 12, plus e2e spec collected by Vitest
cd frontend && npm run build      # exit 2 (blocked at its own typecheck step)
```

Blocked / **not run** (not "passed"): Python 3.12 and 3.13 matrix; PostgreSQL against a real server;
any real provider call; Playwright E2E execution; `npm ci` (no lockfile); coverage; browser/visual
and accessibility review; deployed smoke test.

Healthy local API smoke (temporary `NOVA_ENV=test`, throwaway SQLite paths, no credentials):
`/healthz` → `{"status":"ok"}`, `/readyz` → `ready` with `control_store` and `storage` ok,
`/provider` → `configured:false`, `/storage` → `kind:"sqlite", configured:false`,
`POST /ask` → 200 with `action`/`verdicts`/`final_answer`. `GET /audit` and `GET /memories` → **404**,
confirming no list APIs exist.

## Confirmed facts from the roadmap's checklist

All seven were verified against source, not docs: the provider adapter is OpenAI-compatible only and
`agent_id` is rejected; provider *and* PostgreSQL storage endpoints exist; storage defaults to SQLite
only when unconfigured (a configured-but-unreachable PostgreSQL does not fall back); `anchor_for()`
constructs `HashEmbedder()` (`nova/api/runtime.py:152`); the frontend has exactly the five
Overview/Ingest/Ask/Verify/Audit-lookup screens; hosted browser use requires a same-origin BFF/session
layer that does not exist; `frontend/package-lock.json` is absent.

## Not fixed on purpose

`CI-1` (ruff), `FE-2`/`FE-3` (frontend build and unit tests) are real failures, but fixing them would
mean a 25-file reformat of other contributors' files and a devDependency major-version choice. Phase 1
is scoped to assessment and explicitly forbids unrelated churn; both are recorded with evidence and
owners (`CI-1` → Phase 9, `FE-2`/`FE-3` → Phase 7, `AD-9` needs a human). **Recommendation:** land
`CI-1` as a single `chore:` commit before Phase 2 so every later phase starts from a green gate.

## Residual risks and honest limitations of this report

- All measurements are from one Linux environment with Python 3.11 only; the CI matrix (3.12/3.13) is
  unverified.
- The frontend was measured with `npm install --no-package-lock` (no lockfile in the tree). A real
  install with a fresh lockfile could resolve slightly differently; the Vitest/Vite split is a declared
  version conflict and is expected to reproduce, but that is an expectation, not a measurement.
- No PostgreSQL server, provider key, browser, or deployment target was available, so those remain
  genuinely unverified.
- Fake-mode benchmark scores were observed as ~0 by design and are explicitly **not** quality evidence.

## Human actions required next (see `docs/architecture-decisions.md`)

1. `AD-11` — decide which `AGENTS.md` is authoritative (the short repo file and the full supplied
   document conflict). **Blocks Phase 2 planning.**
2. `AD-1` — choose the identity/session mechanism for browser access. **Blocks Phase 2.**
3. `AD-9` — approve the frontend dependency/toolchain direction (Vite/Vitest split). Blocks `FE-2`.
4. `CI-1` — approve a dedicated lint/format hygiene commit now rather than at Phase 9.
5. `AD-3`, `AD-4`, `AD-5`, `AD-6` — provider account/key-spend policy, PostgreSQL availability,
   retention/deletion policy, encryption-key custody. Needed before Phases 3, 4, and release.
6. `AD-8`, `AD-12` — confirm the UI source of truth and commit the `for_di_arena/` folder if it holds
   anything beyond the three artefacts supplied. Needed before Phase 7.

Never paste provider keys, database passwords, encryption keys, or session secrets into chat or a
commit; configure them in a running deployment or a secret manager.
