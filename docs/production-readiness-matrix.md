# Production-readiness matrix and gap map

**Phase 1 deliverable.** Status: **COMPLETE** (assessment only — no production behaviour changed).
**Repository revision assessed:** `624e8033fb2142367a687cfd09561442f9ca1adc` (`main`), assessed on
branch `arena/775ac705-nova-hack`.
**Target design:** `context_anchor_architecture.pdf` (v1.0, 24 Oct 2026) plus
`Production_Roadmap_10_Phases.md`. Where the PDF and the source disagree, **the source is treated as
truth** and the disagreement is recorded in §6.

This document is an honest snapshot, not a certification. A green test suite proves only that the
tested behaviours hold in one environment. Read §8 before quoting any part of this file.

> **Status update (Phase 4, 2026-10-25).** Storage now has a versioned schema (SQLite v3 /
> PostgreSQL v2) with a single-transaction migration that refuses newer schemas, explicit
> export/delete-records/delete-account semantics with content-free deletion receipts, and hard
> bounds on every read path. `STORE-2` is closed; `STORE-1` still needs a human run against a real
> TLS PostgreSQL (the contract test exists and skips without one). See §12 and
> [`storage.md`](storage.md).
>
> **Status update (Phase 3, 2026-10-25).** Provider adapters now sit behind a registry with declared
> capabilities, bounded retries with backoff, and labelled usage/cost. `PROV-1` is closed;
> `PROV-2` (a real key, a recorded round trip) remains human-only. See §11 and
> [`providers.md`](providers.md).
>
> **Status update (Phase 2, 2026-10-25).** `SEC-1` — the missing browser identity/BFF boundary — is now
> implemented and tested in source; `TEST-1` is covered by new HTTP-level isolation tests. The backend
> suite is now **599 passed** (492 at the Phase 1 baseline plus 107 identity/BFF tests). `CI-1` is still
> open: the repository-wide `ruff` gate remains red from pre-existing files, although every Phase 2
> file is lint- and format-clean, and a dedicated `chore:` commit has since made the **whole repository**
> lint- and format-clean, so the CI `lint` job is green. See §10 for the exact delta and
> [`browser-identity-bff.md`](browser-identity-bff.md) for the security model.

---

## 1. Baseline: exact commands and outcomes

Environment: Debian, Python **3.11.2** (no other interpreter available), an isolated virtualenv
(`pip install -e ".[dev,server]"`), Node **22.22.3** / npm **10.9.8**, ruff **0.17.2**,
pytest **8.4.2**, pydantic **2.14.1**, fastapi **0.162.0**. No `.env` was created; every Python run
used an explicit `NOVA_ENV=test|development` with temporary SQLite paths. No credential was supplied
or required. `frontend/node_modules` was installed with `npm install --no-package-lock` so that **no
lockfile or other repository file was created** by measurement; `node_modules` is ignored.

### 1.1 Passed

| Check | Command | Result |
|---|---|---|
| Backend tests | `pytest -q -W error::ResourceWarning` | **492 passed**, 1 warning in 16.4 s |
| Bytecode compile | `python -m compileall -q nova benchmark scripts tests` | exit 0 |
| Distribution build | `python -m build` | wheel + sdist built (one setuptools `project.license` deprecation warning) |
| Wheel content guard | `python scripts/check_wheel.py dist/*.whl` | `OK: dist/nova-0.2.0-py3-none-any.whl contains the intended distribution` |
| Clean runtime install | fresh venv → `pip install dist/*.whl` → `pip check` → import API → assert no pytest | `No broken requirements found.`, `import ok: Nova API Gateway`, pytest absent |
| Benchmark smoke | `python -m benchmark.runner --seed 7 --n 30 --split dev --systems baseline,rag --llm fake --limit 30` | exit 0, **0/15 system errors**; scores ~0 **by design** in fake mode (see §8) |
| CI hygiene grep | `git ls-files` vs generated-file pattern from `ci.yml` | no tracked caches, DBs, bytecode, `.env`, or build output |
| Working tree | `git status --short`, `git diff --check` | clean before edits |
| Frontend dependency install | `npm install --no-package-lock --no-audit --no-fund` | **307 packages installed** — the registry *is* reachable in this environment |
| Frontend lint | `npm run lint` (`eslint .`) | exit 0 |
| Frontend E2E ***discovery*** | `npx playwright test --list` | 9 tests in 1 file listed |

The single pytest warning is `StarletteDeprecationWarning: Using httpx with starlette.testclient is
deprecated` emitted from `site-packages/fastapi/testclient.py`, not from project code, and it is not
a `ResourceWarning`.

### 1.2 Failed

| Check | Command | Exact outcome | Owner |
|---|---|---|---|
| ~~Ruff lint~~ | `ruff check .` | ~~**87 errors**~~ — **resolved** in the `chore:` hygiene commit; now `All checks passed!`: 78×`E501` line-too-long, 3×`I001`, 2×`SIM905`, 1×`SIM102`, 1×`SIM117`, 1×`UP037`, 1×`F841` (8 auto-fixable). Worst files: `benchmark/generator.py` (25), `benchmark/report.py` (8), `tests/test_phase2_storage_core.py` (5), `nova/core/config.py` (5) | Phase 9 (`CI-1`) |
| ~~Ruff format~~ | `ruff format --check .` | ~~**25 files would be reformatted**~~ — **resolved**; now `128 files already formatted`, 82 already formatted | Phase 9 (`CI-1`) |
| Frontend typecheck | `npm run typecheck` | **1 error**, exit 2: `vite.config.ts(30,13): TS2769 No overload matches this call` — dual Vite typings (§5, `FE-2`) | Phase 7 |
| Frontend unit/component tests | `npm test` | **3 failed / 9 passed of 12**, exit 1, plus 1 file-load failure (`e2e/workflows.spec.ts` collected by Vitest and unable to load Playwright's `test`) | Phase 7 (`FE-3`) |
| Frontend production build | `npm run build` | exit 2 — blocked at its own `typecheck` step; no bundle is produced, so `check:assets` and any built-asset secret scan are unreachable | Phase 7 (`FE-2`) |

The lint/format commands are exactly what `.github/workflows/ci.yml` runs in its `lint` job, so **CI
is red at this revision** on that job. This is measured locally with the same tool family the
workflow installs (`pip install -e ".[dev]"`, i.e. `ruff>=0.6,<1`); the failures are true positives,
not formatter-version noise — e.g. `tests/test_systems.py:126` is 112 characters against the
project's own `line-length = 100`.

### 1.3 Blocked / not run (explicitly **not** "passed")

| Check | Why not run | Recovery |
|---|---|---|
| `pytest` on Python 3.12 and 3.13 (CI matrix) | only 3.11 is installed here | run the CI matrix |
| Real PostgreSQL integration | no PostgreSQL server available; the only coverage is a psycopg-shaped fake backed by SQLite (`tests/test_phase2_integrations.py::test_sql_store_logic_runs_through_postgres_adapter`) | provision a TLS PostgreSQL instance (human, `AD-4`) |
| Real provider (BYOK) call | requires a human-supplied provider key; none was requested or used | `AD-3` + Phase 3 |
| `npm run test:e2e` execution | Playwright browsers are not installed (`npx playwright install` downloads binaries; not available offline-permitted here) | install browsers, then run Phase 7 gate |
| `frontend/package-lock.json` / `npm ci` | no lockfile exists in the repository, and a lockfile must be generated by a real install, never hand-written | Phase 7 (`FE-1`) — a lockfile can now be generated because the registry is reachable |
| Coverage / 85 % gate | no coverage tool is installed or configured (`pyproject.toml` `[dev]` has pytest, ruff, build only; `ci.yml` has no coverage step) even though the target design states an 85 % release gate | Phase 9 (`CI-2`) |
| Visual/accessibility browser review of the built UI | the build does not succeed (`FE-2`) | Phase 7 |
| Staged deployment, backups, key rotation, ops | requires a deployment target | Phase 10 (`AD-7`) |

---

## 2. Request-path trace (code as truth)

| Path | Entry | Wiring | Modules | Primary tests |
|---|---|---|---|---|
| Authentication / tenant resolution | `main.py:90 get_tenant` | `runtime.py:164 resolve_tenant` | `core/config.py`, `core/tenant.py` | `test_api.py`, `test_api_phase2.py`, `test_config.py` |
| Per-request anchor | `main.py:98 get_anchor` | `runtime.py:147 anchor_for` (storage session + `LazyLLM` + `HybridRetriever(HashEmbedder())`) | `store/service.py`, `providers/service.py` | `test_api.py`, `test_api_lifecycle.py` |
| Ingest (write path) | `main.py:178` `POST /ingest` | `pipeline.py:42 Anchor.ingest` | `extraction/extractor.py`, `extraction/quote_validator.py`, `temporal/supersession.py`, `retrieval/embedder.py`, `retrieval/maintenance.py`, `store/sql_store.py` | `test_extraction.py`, `test_supersession.py`, `test_store.py`, `test_integration.py`, `test_phase2_storage_core.py` |
| Ask (read path) | `main.py:182` `POST /ask` | `pipeline.py:127 Anchor.ask` → `verification/service.py:128 AnchorVerifier.ask` (draft optional: 1 extra LLM call answers strictly from memory JSON, then verified) | `retrieval/hybrid.py`, `verification/claim_splitter.py`, `judge.py`, `decision.py`, `evidence.py` | `test_verification_integration.py`, `test_claim_splitter.py`, `test_judge.py`, `test_decision.py`, `test_retrieval.py` |
| Verify | `main.py:193` `POST /verify` | `pipeline.py:138` → `verification/service.py` `verify` | same as above + `audit/logger.py` | `test_verification_integration.py`, `test_audit.py` |
| Audit single record | `main.py:204` `GET /audit/{id}` | `pipeline.py:149` → `audit/logger.py:142` | `audit/logger.py` | `test_audit.py`, `test_api.py` |
| Provider BYOK | `main.py:212-227` | `providers/service.py:53/72/90` | `providers/config.py`, `openai_compat.py`, `security/net.py`, `security/secrets.py` | `test_api_phase2.py`, `test_phase2_integrations.py`, `test_security_net.py` |
| Storage lifecycle | `main.py:231-250` | `store/service.py:102/141/194/202` | `store/postgres.py`, `store/sql_store.py`, `store/schema.py`, `security/net.py` | `test_api_phase2.py`, `test_phase2_storage_core.py`, `test_store.py` |
| Export / delete | `main.py:253-266` | `store/service.py:194/202` → `StorageSession` | `store/session.py` | `test_api_phase2.py`, `test_phase2_storage_core.py` |
| Health/readiness | `main.py:268-277` | `runtime.py:92` | `store/schema.py`, `security/secrets.py` | `test_api_lifecycle.py` |
| Audit retention | app lifespan (`main.py` `create_app`) | `runtime.py:111 purge_expired_audit` | `audit/logger.py`, `store/service.py` | `test_audit.py`, `test_api_lifecycle.py` |

Observed route table (read from the app object, not from docs): `/ingest`, `/ask`, `/verify`,
`/audit/{id}`, `/provider` (GET/PUT/DELETE), `/provider/test`, `/storage` (GET/PUT/DELETE),
`/storage/test`, `/storage/initialize`, `/data/export`, `/data`, `/healthz`, `/readyz`.
`GET /audit` and `GET /memories` both return **404** — there is no list/search API for memories or
audit records.

---

## 3. Confirmations the roadmap asked for (all verified against source)

| Claim to check | Verdict | Evidence |
|---|---|---|
| Provider adapter is OpenAI-compatible, not arbitrary-agent | **Confirmed** | `providers/config.py:20 ProviderKind` has exactly one member, `openai_compatible`; `providers/openai_compat.py` speaks Chat Completions |
| `agent_id` is rejected | **Confirmed** | `providers/config.py:38` declares it, `providers/config.py:44-47` raises `agent_id is not supported by the openai_compatible adapter` |
| Provider configuration/test endpoints exist | **Confirmed** | `main.py:212-227` |
| PostgreSQL storage endpoints exist | **Confirmed** | `main.py:231-250` (`PUT /storage`, `POST /storage/test`, `POST /storage/initialize`, `DELETE /storage`) |
| Storage defaults to SQLite when unconfigured | **Confirmed** | `store/service.py:49-51 status()` → `kind="sqlite", configured=False`; `store/service.py:120-126 open()` falls back to shared SQLite **only** when no PostgreSQL config is stored — a configured PostgreSQL that is unreachable raises `StorageUnavailableError` instead of falling back |
| `Runtime.anchor_for()` constructs `HashEmbedder` | **Confirmed** | `api/runtime.py:152` `HybridRetriever(session.memories, HashEmbedder())`; `SentenceTransformerEmbedder` (`retrieval/embedder.py:115`) is never selected at runtime |
| Frontend has Overview/Ingest/Ask/Verify/Audit lookup only | **Confirmed** | `frontend/src/App.tsx:41-47` `NAV_ITEMS` lists exactly 5 items; page switch at `App.tsx:467-719` |
| No memory library, audit list, or Settings pages | **Confirmed** | no such nav items, no `/memories` or `/audit` list route, no settings screen; `frontend/src/api.ts:13` `API_ROOT='/api'` exposes only the 5 workflows |
| Browser deployment needs a trusted same-origin BFF/session layer | **Confirmed as a gap** | `frontend/README.md` "Hosted deployment"; `frontend/vite.config.ts` injects `NOVA_API_KEY` only in the **Node** dev proxy and rejects non-loopback targets; nothing server-side maps a user to a tenant |
| `frontend/package-lock.json` is absent | **Confirmed** | `ls frontend/` has no lockfile; `git ls-files frontend/` likewise |
| Frontend production checks unverified | **Confirmed, and now partly measured** | see §1.2 — lint passes; typecheck, tests, and build **fail**; E2E not executed |
| `FakeLLM` is never a silent production fallback | **Confirmed** | `providers/service.py:90-101`: unconfigured tenant raises `ProviderNotConfiguredError` unless `NOVA_ALLOW_FAKE_LLM` is set; `core/config.py:151` rejects that flag in production; `core/config.py:139` rejects any `NOVA_LLM_PROVIDER != "fake"` |
| Production fails closed without auth/encryption key | **Confirmed** | `core/config.py:143-158` (`validate_auth_configuration`), `:140-150` (`validate_supported`), exercised by `test_config.py` |
| Tenant scope is enforced at the store layer, not only at routing | **Confirmed** | every statement in `store/sql_store.py` filters `WHERE tenant_id = ?` (e.g. `:169, :176, :192, :201, :213, :233, :247`) |
| `X-Tenant-ID` cannot escalate a key | **Confirmed** | `runtime.py:164-196`; a shared key only ever yields `DEFAULT_TENANT` (`:190-192`) |

---

## 4. Conformance to the target architecture

Legend: **Implemented** = present and covered by tests in this checkout · **Partial** = present with
a material limitation · **Planned** = not built · **Blocked** = needs a human decision/service.

| Target component (`context_anchor_architecture.pdf`) | Status | Evidence / limitation |
|---|---|---|
| API gateway, typed errors, no business logic in routes | **Implemented** | `api/main.py`, `api/errors.py`, `api/limits.py`, `api/middleware.py` |
| Anchor facade `ingest/ask/verify` | **Implemented** | `pipeline.py` |
| Extractor (20-message windows, strict JSON, repair retry) | **Implemented** | `extraction/extractor.py`, `test_extraction.py` |
| Quote validator (verbatim source quote or drop) | **Implemented** | `extraction/quote_validator.py`, `test_extraction.py` |
| Supersession with per-type policy | **Implemented** | `temporal/supersession.py`, `core/policy.py`, `test_supersession.py` |
| Embeddings with model/version/dimension metadata | **Partial** | metadata + compatibility checks exist (`retrieval/embedder.py`, `retrieval/hybrid.py`), but the runtime uses `HashEmbedder` (`api/runtime.py:152`) — **not** semantic-quality production embeddings |
| Hybrid retriever 0.6/0.4, k=5, history on request | **Implemented** | `retrieval/hybrid.py`, `test_retrieval.py` |
| Claim splitter, batched judge, verdict guard, decision engine | **Implemented** | `verification/*`, `test_claim_splitter.py`, `test_judge.py`, `test_decision.py` |
| Audit log as system of record | **Partial** | write + single-record read + retention + export exist (`audit/logger.py`), but no HTTP list/search (`recent()` at `:150` is not exposed) |
| SQLite store / SQLite+pgvector production store | **Partial** | one `MemoryStore` contract on both (`store/sql_store.py` shared SQL); PostgreSQL is only exercised through a fake driver — no real-server run |
| Tenant isolation and per-tenant BYOK | **Implemented (API level)** | `store/sql_store.py`, `providers/*`, `security/*`, `test_api_phase2.py` |
| Audit `/audit/{id}` | **Implemented** | `main.py:204` |
| Memory library / audit list & search APIs | **Planned** | absent by design (404) — Phase 6 |
| Browser sessions / BFF | **Planned** | Phase 2 |
| Self-hosted provider-agnostic LLM interface | **Partial** | `core/llm.py` `LLMClient` protocol + `FakeLLM`; one HTTP adapter (`openai_compat.py`); `AnthropicLLM` is *not* implemented despite `anthropic_api_key` existing in `core/config.py:82` |
| Benchmark: 3 systems, seeded generator, metrics, CIs | **Implemented (fake mode)** | `benchmark/*`, `test_phase3_benchmark.py`, `test_scenarios.py`; **fake-mode scores are not model-quality evidence** |
| Real-model evaluation on a curated dataset | **Blocked** | needs a funded provider key and a gold-labelled set (`AD-3`, `AD-10`) |
| `docker-compose.yml` deployment topology in the design doc | **Planned** | file does not exist; Phase 10 |
| `docs/ARCHITECTURE.md` referenced by the design doc | **Planned** | file does not exist; this matrix is the interim mapping |
| UI screens: Workspace, Ingest, Ask, Verify, **Memories, Audit, Settings** | **Partial** (3 of 7 targets missing) | implemented: Overview, Ingest, Ask, Verify, Audit-ID lookup; missing: Memories, Audit list, Settings (`AD-8` for the design source) |
| Repository-wide green CI | **Failed** | §1.2 |

---

## 5. Gap matrix

Severity: **P0** = unsafe for real users or blocks release/CI now · **P1** = blocks a named later
phase's acceptance criteria · **P2** = hygiene/documentation. Every item has exactly **one** owning
phase.

### P0

| ID | Gap | Evidence | Depends on | Acceptance criteria | Owner |
|---|---|---|---|---|---|
| `SEC-1` | ~~No browser identity, session, or BFF.~~ **Implemented in Phase 2** (`nova/identity/*`, `nova/api/bff.py`). The only credential was a static deployment API key sent as `X-API-Key`; exposing the app to a browser meant shipping that key to the client. | `api/runtime.py:164-196`; `frontend/src/api.ts:13`; `frontend/README.md` "Hosted deployment"; `frontend/vite.config.ts` proxy comment | — | Authenticated session (secure, HttpOnly, SameSite cookie), CSRF protection, server-side identity→tenant mapping, two-tenant isolation test, no key in browser bundle or storage | **Phase 2 — done in source (§10); deployment needs `AD-1`** |
| `CI-1` | ~~Repository does not satisfy its own PR gate: `ruff check` 87 errors, `ruff format --check` 25 files.~~ **Resolved** by the dedicated `chore:` hygiene commit (formatting + 8 safe auto-fixes; no test, dependency, or rule changed). | §1.2; `.github/workflows/ci.yml` `lint` job | none (mechanical) | `ruff check .` and `ruff format --check .` both exit 0 with no rule relaxation; no test weakened; landed as one dedicated `chore:` commit so later phase diffs stay reviewable | **Phase 9** — pulled forward and **done** |
| `FE-2` | Frontend cannot produce a production bundle: `typecheck` fails, so `npm run build` fails before `check:assets`. | §1.2; root cause: `node_modules/vitest/node_modules/vite` **5.4.21** vs root `vite` **6.0.7** — `vitest/config`'s `defineConfig` and `@vitejs/plugin-react` resolve different Vite type identities (`vite.config.ts(30,13)`) | `AD-9` (dependency version policy) | `npm ci` from a committed lockfile; `typecheck`, `lint`, `test`, `build`, `check:assets` all green; bundle scanned for secrets/source maps | **Phase 7** |
| `FE-3` | Component suite is red and mis-scoped: 3/12 tests fail with "multiple elements" and Vitest collects the Playwright spec. | §1.2; failures at `frontend/tests/App.test.tsx:46,61,71`; `frontend/tsconfig.json:20` includes `e2e` and `vite.config.ts` sets no Vitest `include`/`exclude` | resolution of whether the duplicate element is a UI defect or a stale assertion | Vitest scope excludes `e2e/`; all assertions pass by fixing the real cause, not by loosening queries | **Phase 7** |

### P1

| ID | Gap | Evidence | Depends on | Acceptance criteria | Owner |
|---|---|---|---|---|---|
| `EMB-1` | Production runtime uses `HashEmbedder`; no configured, versioned production embedding model. Retrieval quality is therefore unmeasured and cannot be described as semantic. | `api/runtime.py:152`; `retrieval/embedder.py:68,115` | — | Explicitly configured embedder with model id/version/dimension, compatibility + re-embed migration path, bounded cost, retrieval-quality metric on a held-out split | **Phase 5** |
| `PROV-1` | Only one provider adapter; `agent_id` intentionally unsupported; `anthropic_api_key` is read but has no adapter. Provider *capability* claims must stay narrow. | `providers/config.py:20,44-47`; `core/config.py:108` | `AD-3` | Documented supported matrix; each added adapter has a real integration test against its API contract; no universal-agent claim | **Closed in Phase 3 for the existing adapter** — `docs/providers.md` states the supported matrix and exclusions; capabilities are declared per kind (`providers/capabilities.py`) and reported by `GET /provider`; the registry (`providers/registry.py`) is the only place a kind maps to a class. Each *future* adapter still owes its own contract test |
| `PROV-2` | No verified real-provider round trip exists (all provider tests use mock transports/fakes). | `tests/test_phase2_integrations.py`, `tests/test_provider_adapters.py`, `tests/test_security_net.py` | `AD-3` (key) | One recorded successful real completion *and* one recorded failure-mode run per supported adapter, executed by a human with a key | **Open — human-only.** Phase 3 added deterministic retry/capability/usage tests but cannot substitute for a real round trip; runbook in `docs/providers.md` §2 |
| `STORE-1` | PostgreSQL compatibility is asserted only through a psycopg-shaped fake over SQLite. TLS requirement, migrations, and cross-tenant isolation are unproven on a real server. | `tests/test_phase4_storage.py::test_postgres_contract_matches_sqlite` | `AD-4` (server) | Contract suite green against a real TLS PostgreSQL; migration up/down; two-tenant isolation; no silent fallback | **Phase 4: partly closed — still open, human-only.** A real-server contract test now exists and is opt-in via `NOVA_TEST_POSTGRES_DSN` (migrate → insert → purge → receipt); it **skips** here because no server exists, so no real-round-trip evidence is claimed. Down-migrations are deliberately not implemented; rollback is backup-restore, documented in `docs/storage.md` §4 |
| `STORE-2` | Data-lifecycle semantics are only partly surfaced: `DELETE /data` erases this product's records, `DELETE /storage` only forgets the connection, and there is no "delete the tenant/account" operation or documented retention policy. | `store/session.py` (`purge`), `store/receipts.py`, `main.py` (`/data`, `/data/deletions`) | `AD-5` (retention policy) | Written, user-visible semantics for disconnect vs. export vs. delete-records vs. delete-account, each with tests | **Closed in Phase 4** for the semantics: all four operations are documented in `docs/storage.md` §6 and tested, deletion writes a content-free receipt, and account-scope erasure purges receipts too. The *retention policy itself* (`AD-5`) remains an operator decision |
| `API-1` | No memory library API and no audit list/search API; `recent()`/`export()` exist internally but are unexposed. UI cannot show real memories or activity without fabricating them. | `main.py` route table; `audit/logger.py:150,178`; `GET /memories`, `GET /audit` → 404 | Phase 2 auth (tenant-scoped reads) | Tenant-scoped, paginated, bounded list + search endpoints for memories and audit, with contract tests and an OpenAPI document | **Phase 6** |
| `ING-1` | Ingest is synchronous inside the request; no durable job, retry, or queue. A slow provider blocks the HTTP request and a partial failure leaves no resumable state. | `main.py:178-180`; `pipeline.py:42-124` (no job store anywhere in `nova/`) | Phase 2 | Durable job record, idempotency key, bounded retries with typed failures, progress/status endpoint, restart-safe resume | **Phase 8** |
| `ING-2` | Message-level ingest idempotency does not survive a request: `_ingested_msg_ids` is per-`Anchor`, and a new `Anchor` is built per request, so re-posting the same messages re-runs extraction and re-spends provider tokens (memory-level dedupe via deterministic ids still prevents duplicate rows). | `pipeline.py:35,55,90`; `api/runtime.py:147-161` | Phase 8 job/state design | Re-ingest of identical messages performs **0** extraction calls, proven by a test that counts provider invocations across two HTTP requests | **Phase 8** |
| `OBS-1` | No operational telemetry beyond audit rows and application logs: no metrics, traces, cost accounting, or budget ceilings. | `nova/audit/logger.py`, `core/llm.py` (usage counters only) | Phase 8 | Metrics for latency/error/token spend per tenant; documented budget limits and behaviour on breach | **Phase 8** |
| `CI-2` | No coverage measurement despite a stated 85 % release gate; no frontend job in CI at all. | `pyproject.toml` `[dev]`; `.github/workflows/ci.yml` (no coverage step, no frontend job) | `FE-1`/`FE-2` | Coverage reported per run with a threshold that matches reality; frontend typecheck/lint/test/build job present | **Phase 9** |
| `EVAL-1` | No model-quality evidence. The fake benchmark deliberately scores ~0 and proves only error-free execution. | §1.1 benchmark row; `benchmark/README`-equivalent notes in `CLAUDE.md` | `AD-3`, `AD-10` | Curated gold set, dev/test split, judge-accuracy pairs, reported with confidence intervals and both splits side by side | **Phase 9** |

### P2

| ID | Gap | Evidence | Owner |
|---|---|---|---|
| `DOC-1` | The target architecture PDF still says "the repository is still empty, so nothing here is implemented yet" and names files that do not exist (`nova/store/pg_store.py`, `docker-compose.yml`, `docs/ARCHITECTURE.md`). A reader can mistake the target for the state. | `context_anchor_architecture.pdf` p.4, p.6; `ls nova/store`, `ls` | Phase 1 (this matrix, §4) / Phase 10 for a machine-readable `docs/ARCHITECTURE.md` |
| `DOC-2` | `CLAUDE.md` states ruff/build "were not installed"; they install and run here, and build passes. `README.md` states frontend dependencies "could not be installed"; they install, and the outcome is now measured. | `CLAUDE.md` "Current environment limitations"; `README.md:32` | Phase 1 (README paragraph updated; `CLAUDE.md` corrected) |
| `DOC-3` | `.env.example` omits `NOVA_LLM_PROVIDER` / `NOVA_LLM_MODEL`, which `Settings.from_env()` reads and `validate_supported()` constrains to `fake`. | `core/config.py:96-97,139`; `.env.example` | **Closed (chore)** — was Phase 3 (replace or remove the dead knob). Both knobs are now documented in `.env.example` as legacy-and-must-stay-`fake`, and `Settings` carries an inline note that provider selection is per tenant. The values are documented rather than deleted because tests and operators legitimately pin the default. |
| `DOC-4` | Two different `AGENTS.md` documents describe this work: the repository's short `TOKEN RULES` file and the fuller 10-section rules supplied with the roadmap. They conflict (e.g. reply length, per-phase reporting). | repo `AGENTS.md` vs. supplied `AGENTS.md` | `AD-11` (human) |
| `DOC-5` | The `for_di_arena/` folder the human referenced (target architecture + UI + roadmap) is **not present** in this checkout or in git history. If it exists locally it was never committed. | `find . -iname '*for_di*'` → none; `git log --all --oneline` → single commit | `AD-12` (human) |
| `DEP-1` | `project.license` uses the legacy TOML table form; the build emits a setuptools deprecation warning with a 2027 deadline. | `pyproject.toml` `license = { text = "Proprietary" }`; build output | Phase 9 |
| `DEP-2` | `frontend` has no lockfile, so dependency resolution is not reproducible and `npm ci` is impossible. | `frontend/package.json`; no `package-lock.json` | Phase 7 (`FE-1`) |
| `TEST-1` | ~~No test asserting two-tenant isolation through the HTTP API.~~ **Covered in Phase 2** by `tests/test_identity_bff.py` (two BFF sessions, separate tenants, memories + audit + export). | `tests/test_api_phase2.py` covers key→tenant mapping and header escalation, not full two-tenant data isolation through `/ingest` + `/ask` + `/audit/{id}` | **Phase 2 — done (§10)** |
| `UX-1` | No fabricated data exists today (good), but the target UI (dashboard counts, memories table, activity feed, provider/storage settings) has no data source. Any of those screens built before Phase 6 would be invention. | `frontend/src/App.tsx`; `GET /memories` 404 | Phase 7 (blocked by `API-1`) |

---

## 6. Docs vs. source discrepancies found and how they were treated

1. `CLAUDE.md` says ruff and build are unavailable and the index unreachable → **stale**; both install
   and run. Build passes; lint fails for real reasons. Docs updated.
2. `README.md:32` says the frontend could not be installed → **stale**; measured instead. Updated to
   state exactly which frontend checks pass and which fail.
3. `docs/phase-5-validation-status.md` is still accurate about *which* checks were previously
   unverified, but its central blocker ("the environment cannot resolve the npm package registry") no
   longer holds here. Its release-blocker list is superseded by §1.3; the file is left intact as a
   historical record, and this matrix is the current status. **Updated (chore):** the page now opens
   with an explicit historical banner that flags its three superseded claims (test count, ruff/build
   availability, npm reachability) and states that it is not the roadmap's Phase 5.
4. The architecture PDF's status sentence ("nothing here is implemented yet") describes the empty
   repository the design was written against → **historical**; §4 replaces it with a measured map.
5. The PDF names `nova/store/pg_store.py`; the code has `nova/store/postgres.py` + `sql_store.py` →
   **doc naming drift**, not a missing component.
6. `README.md` says the repo "combines the Phase 1–4 reference work"; `CLAUDE.md` explains phase 2's
   storage design superseded phase 4's → code matches `CLAUDE.md`. Unchanged.

---

## 7. Dependency order (why the phases cannot be reordered)

```
CI-1 (green gate)  ─────────────┐
                                ▼
AD-1 identity vendor ──► SEC-1 BFF/session ──► any hosted UI exposure ──┐
                                                                        │
AD-3 provider key ──► PROV-1/PROV-2 adapters ─┐                        │
AD-4 PostgreSQL   ──► STORE-1 real store ─────┼──► API-1 memory/audit APIs ──► FE screens
AD-5 lifecycle    ──► STORE-2 semantics ──────┘        (Phase 6)              (Phase 7)
                                                                              │
EMB-1 embeddings ──► retrieval quality ──────────────────────────────────────►│
                                                                              ▼
AD-3 + AD-10 ──► EVAL-1 real evaluation ──► CI-2 gates ──► release (Phase 10)
ING-1/OBS-1 (Phase 8) ───────────────────────────────────► release (Phase 10)
```

- **Auth/BFF precedes any public UI** (`SEC-1` → Phase 7). Without a server-side identity→tenant map,
  a hosted UI must either ship the deployment key to the browser or trust a client-selected tenant —
  both are explicitly forbidden.
- **Provider and storage choices precede end-to-end settings** (`PROV-1`, `STORE-1`, `STORE-2` → the
  Settings screen in Phase 7). A settings screen must show real `configured / unverified / unavailable
  / disconnected` states; those states only exist once the adapters and their failure modes are real.
- **Memory/audit APIs precede the Memories and Audit screens** (`API-1` → Phase 7). Otherwise the UI
  would fabricate counts and rows, which this repository forbids.
- **Evaluation precedes release** (`EVAL-1`, `CI-2` → Phase 10). A green unit suite plus a fake
  benchmark is not evidence of answer quality.
- **Durable ingestion precedes operational release** (`ING-1`, `OBS-1` → Phase 10): a synchronous
  ingest with no retry is not operable against a slow or flaky provider.

All human-owned inputs are in [`docs/architecture-decisions.md`](architecture-decisions.md).

---

## 8. What this assessment does **not** claim

- 492 passing tests are **not** production certification, and they do not cover the frontend (which
  is red), a real PostgreSQL server, a real provider, or any deployment environment.
- `HashEmbedder` is deterministic and test-oriented; the benchmark's retrieval and answer scores in
  fake mode are **not** model-quality measurements, and its deliberate zeros are a property of the
  fake extractor, not of the system's real ability.
- No live user, memory count, cost figure, provider connection, or integration success is asserted
  anywhere in this document or in the repository, because none was observed.
- The frontend "API reachable" indication comes from `/healthz` only; it says nothing about database,
  provider, or storage health.

## 9. Production-readiness statement

**Code present ≠ integration verified.** The backend implements the architecture's core write and
read paths — extraction with verbatim-quote validation, per-type supersession, hybrid retrieval,
batched judging with code-side verdict guards, deterministic decision-making, tenant-scoped storage,
encrypted per-tenant BYOK configuration, and bounded/redacted audit records — and 492 backend tests
pass with `ResourceWarning` promoted to errors. What has **not** been demonstrated is exactly what a
production release depends on: there is no browser identity or BFF (so the UI cannot be exposed),
the CI lint gate is red, the frontend cannot build and its component tests fail, production
embeddings are not configured, PostgreSQL and real providers have never been exercised against real
services, ingestion is not durable, and no model-quality evaluation exists. The repository is a
credible, honestly-labelled engineering baseline with a **blocked** path to release; Phase 2 should
not start until `CI-1` is green and the human decisions `AD-1`, `AD-3`, `AD-4`, `AD-5`, `AD-9`,
`AD-11` are answered.


---

## 10. Phase 2 delta (browser identity, BFF, tenant authorization)

| Item | Before | After |
|---|---|---|
| Browser auth | none — only static API keys | vendor-neutral OIDC (code + PKCE), opaque `__Host-` sessions, CSRF bound to the session, strict origin checks, roles, allowlisted same-origin BFF |
| Tenant scope for browsers | none possible without shipping a key | derived only from the server-side session row; the BFF injects the tenant credential server-side and strips client credential/tenant headers |
| API-key clients | supported | unchanged (regression tests assert 200/401/ignored-header behaviour) |
| Production fail-closed | auth + encryption key | additionally: `NOVA_IDENTITY_MODE=oidc` requires issuer, client id/secret, redirect URI, BFF origin, HTTPS, secure cookies, and at least one grant, or startup raises `ConfigError` |
| Backend tests | 492 passed | **599 passed** (107 new: sessions, roles, OIDC, config, BFF) |
| Lint | `ruff check .` red (87 pre-existing errors) | unchanged repository-wide; all new/edited Phase 2 files are lint- and format-clean |
| `CI-1` | open (87 ruff errors, 25 unformatted files) | **closed** in a separate `chore:` commit: `ruff check .` and `ruff format --check .` both pass; 599 tests still pass; generated SQL DDL and the benchmark dataset verified byte-identical |

Still open after Phase 2 (unchanged owners): `EMB-1` (§5), `PROV-1`/`PROV-2`, `STORE-1`/`STORE-2`,
`API-1`, `ING-1`/`ING-2`, `OBS-1`, `CI-1`/`CI-2`, `EVAL-1`, and the Phase 7 frontend items. The frontend
still calls `/api/*`; switching it to `/api/bff/*` and the remaining screens belong to Phase 7.

---

## 11. Phase 3 delta (provider adapters, BYOK, usage controls)

| Item | Before | After |
|---|---|---|
| Provider abstraction | `ProviderService` hard-coded `OpenAICompatibleLLM` in three places | `providers/registry.py` is the only mapping from `ProviderKind` to a class; the service depends on the registry and the `LLMClient` protocol |
| Capabilities | none declared; "provider" implicitly meant generation | `providers/capabilities.py`: `generation` and `embeddings` are separate; the Chat Completions adapter declares **generation only**; `GET /provider` reports the declared list; an undeclared capability is a `ProviderConfigError` before any call |
| Failures | one attempt, then a typed error | bounded retries (`max_attempts` 1–5) with exponential backoff, ±25 % jitter, `Retry-After` honoured, and a monotonic total-time budget; only 429/timeout/5xx are retried — auth, 4xx, budget, and config errors fail immediately |
| Usage | `UsageTally` counted provider-reported tokens | unchanged counting, plus `providers/usage.py` provenance (`provider` / `estimate` / `unavailable`) per call, reset per response so stale numbers cannot leak forward |
| Cost | none | optional, **only** from operator-configured prices, always flagged `cost_is_estimate`; no price means `null`, never `0` |
| Cancellation | not present | **not claimed** — the request path is synchronous; documented in `docs/providers.md` §5 with the bounding mechanisms (per-attempt timeout, total retry budget, disconnect) |
| Test doubles | ad-hoc inline fakes | `tests/provider_fakes.py` (scripted transport, recording sleep, fake clock) — test-only, never shipped; `FakeLLM` stays gated by `NOVA_ALLOW_FAKE_LLM` and refused in production |
| Provider docs | none | `docs/providers.md`: supported matrix, BYOK flow, env vars, limits, failure/retry table, usage labelling, extension path |
| Backend tests | 599 passed | **642 passed** (43 new adapter/retry/capability/usage tests) |
| `PROV-1` | open | **closed** for the existing adapter (documented matrix + declared capabilities + registry isolation) |
| `PROV-2` | open | **still open, human-only**: no real provider key exists in this environment; a recorded successful completion and a recorded failure-mode run are required |


---

## 12. Phase 4 delta (storage, migrations, export and deletion)

| Item | Before | After |
|---|---|---|
| Storage boundary | `Database` ABC + `StorageSession`, but lifecycle reads were partly unbounded and deletion had no audit trail | Unchanged abstractions, **extended**: `limit` + `StoreLimitError` on every growing read; `deletion_receipts` repository added alongside memories/audit; nothing outside `nova/store/` imports a concrete store |
| Schema versions | SQLite v2 / PostgreSQL v1, no guard against a newer database | SQLite **v3** / PostgreSQL **v2** (additive receipts table + two indexes); `known_version()`/`migration_plan()`/`current_version()`; a newer schema raises `SchemaVersionError` (503) and writes nothing |
| Migration safety | one transaction, but a half-applied step was untested | Tests prove rollback for a failing step *and* for a step that fails after creating a table; re-run is a no-op; v1 → v3 legacy upgrade keeps rows and repairs invalid links |
| Rollback guidance | none | `docs/storage.md` §4: backup before upgrade, interrupted/failed recovery, restore-to-roll-back; no down-migrations by design |
| Deletion | memories + audit erased by one operation; nothing recorded | `scope=records` (keeps a content-free receipt) vs `scope=account` (purges receipts, optional tombstone); atomic — a receipt failure rolls the deletion back; actor + request id recorded, never content |
| Export | memories + audit, fixed `limit` | adds `deletion_receipts`, `schema_version`, `counts`, `returned`, `truncated`; refuses limits above `MAX_EXPORT_ROWS` |
| Bounded reads | `candidates()` and `missing_embeddings()` could return everything; audit `recent()` silently clamped | Hard bounds in `nova/store/limits.py`; exceeding a bound raises `query_limit_exceeded` (422) instead of truncating; export states truncation explicitly |
| Indexes | `(tenant, subject, status)`, `(tenant, session)`, `(tenant, superseded_by)`, `audit(tenant, timestamp)` | plus `memories(tenant_id, timestamp, id)` and `deletion_receipts(tenant_id, deleted_at)` |
| Backend tests | 642 passed | **676 passed, 1 skipped** (34 new storage tests; the skip is the opt-in real-PostgreSQL contract suite) |
| `STORE-1` | open | **open, human-only** — real-server contract test added, not run |
| `STORE-2` | open | **closed** for lifecycle semantics; retention *policy* (`AD-5`) still an operator decision |
