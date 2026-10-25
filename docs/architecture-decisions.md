# Architecture decisions requiring a human

**Phase 1 deliverable.** These are the choices a coding agent must not invent. Each entry records
what is needed, why it is a human decision, the options that exist (without preferring one on the
human's behalf), the **safe, provider-neutral boundary already implemented** in the repository, and
what stays blocked until the answer arrives.

Related: [`production-readiness-matrix.md`](production-readiness-matrix.md) for the measured state.

Rules applied while writing this file: no vendor, account, policy, price, or claim is chosen here;
nothing here authorises creating a repository, service, account, or credential.

---

## Decision register

| ID | Decision | Owner phase | Blocks next phase? |
|---|---|---|---|
| `AD-1` | Identity provider / session strategy for browser access | 2 | **Yes** — Phase 2 cannot be designed without it |
| `AD-2` | Deployment target and hosting topology | 10 (informs 2) | No, but it constrains `AD-1` |
| `AD-3` | AI provider account, key, model, and spend policy | 3 | **Yes** for Phases 3 and 9 |
| `AD-4` | Customer-owned PostgreSQL availability (server, CA/TLS, network) | 4 | **Yes** for Phase 4's real-server gate |
| `AD-5` | Retention, deletion, and export policy | 4 | No (safe defaults exist), but release-blocking |
| `AD-6` | Encryption-key and secret custody | 2/10 | No (dev/temp keys are fine locally), release-blocking |
| `AD-7` | Domain, TLS certificates, DNS, egress firewall | 10 | No |
| `AD-8` | Target UI source of truth | 7 | No |
| `AD-9` | Frontend dependency and toolchain version policy | 7 | **Yes** — the frontend build is currently broken |
| `AD-10` | Evaluation dataset and gold labels | 9 | No (Phase 9 owns it), release-blocking |
| `AD-11` | Which `AGENTS.md` is authoritative | 1 (answer needed before Phase 2) | **Yes** — governs every phase's reporting |
| `AD-12` | Missing `for_di_arena/` reference folder | 1 | No |

---

## `AD-1` — Browser identity and session strategy

**Decision needed.** How a real user proves who they are to a hosted Content Anchor AI deployment, and
how that identity maps to one tenant.

**Why it is human-only.** It selects a vendor, creates accounts, sets cookie/CSRF policy, and shapes
the privacy statement. Different choices imply different compliance surface and cost.

**Options that exist** (not ranked): an OIDC/OAuth identity provider; a self-hosted identity service;
existing corporate SSO; a managed auth product; or an explicitly single-tenant "operator-owned API
key in the BFF" deployment for a private pilot. Each implies a different session lifetime, MFA story,
and account-recovery policy.

**What is implemented today (safe boundary).** Static server-side API-key authentication only:
`NOVA_API_KEY` (default tenant) or `NOVA_TENANT_API_KEYS` (key→tenant map), resolved in
`nova/api/runtime.py:164 resolve_tenant`. Tenant identity is never read from a client header; a
shared key can only reach `default`. Production refuses to start without authentication
(`nova/core/config.py:143`). The Vite dev proxy can attach the key from the Node process environment
only, and rejects non-loopback targets (`frontend/vite.config.ts`). No browser storage of secrets
exists anywhere in the frontend.

**Blocked until answered.** Any hosted browser deployment. Phase 2 cannot choose a session format,
cookie attributes, CSRF mechanism, or identity→tenant mapping table without this answer.

**Exact input required.** The chosen identity mechanism, the environment/tenant naming convention, who
administers it, and whether any end user will be external to the organisation.

---

## `AD-2` — Deployment target and hosting topology

**Decision needed.** Which cloud/host the API, the BFF, and any tenant-managed database live in; whether
the deployment is single-tenant, per-customer instance, or shared with per-tenant storage.

**Why it is human-only.** It selects a provider, incurs cost, determines egress rules, and interacts
with data-residency commitments. `AGENTS.md` §9 forbids the agent from selecting a cloud or opening
external data access.

**What is implemented today.** A single FastAPI process with SQLite defaults, optional per-tenant
PostgreSQL configured through authenticated endpoints, and a readiness probe (`/readyz`). No
Dockerfile, no compose file, no IaC. The design PDF mentions `docker-compose.yml`; it does not exist.

**Blocked until answered.** Phase 10 release, and the network half of Phase 2 (cookie domain, TLS
termination, same-origin guarantees).

---

## `AD-3` — AI provider account, key, model, and spend policy

**Decision needed.** Which provider(s) will be supported for generation, which model(s) may be called,
who pays, what the per-tenant and global spend ceilings are, and whether embeddings may use the same
account.

**Why it is human-only.** Creating and billing a provider account, supplying keys, and granting access
to private agents/workspaces are explicitly human actions (`AGENTS.md` §9). Model choice is also a
product/vendor policy.

**Options that exist.** Any OpenAI-compatible endpoint (the only adapter implemented), a
self-hosted OpenAI-compatible server, or later a new adapter for a differently-shaped API.

**What is implemented today.** A provider-neutral boundary: `nova/core/llm.py` defines `LLMClient`;
`nova/providers/openai_compat.py` is the only HTTP adapter; per-tenant configuration, rotation,
connection test, and masked status exist (`/provider`, `/provider/test`); keys are encrypted at rest
before storage and never returned (`nova/security/secrets.py`, `nova/providers/service.py:53`).
`agent_id` is deliberately rejected (`nova/providers/config.py:44-47`). Without a configured provider
the runtime raises a typed `ProviderNotConfiguredError`; it never silently substitutes `FakeLLM`
unless the operator sets `NOVA_ALLOW_FAKE_LLM`, which is forbidden in production
(`nova/core/config.py:151`).

**Blocked until answered.** Any real answer quality, any real cost figure, `PROV-2` (real round trip),
`EVAL-1` (model-quality evaluation), and any claim that the product "works" end to end.

**Exact input required.** Provider, model id(s), who owns the account, the spend ceiling, and
confirmation of the provider's data-retention terms. **Do not paste the key into chat or a commit** —
configure it through `/provider` in a running deployment or the deployment's secret manager.

---

## `AD-4` — Customer-owned PostgreSQL availability

**Decision needed.** Whether PostgreSQL support will be validated against a real server before release,
and if so: which server, which CA/TLS configuration, and which network path.

**Why it is human-only.** It requires creating a database instance/user and authorising network access
(`AGENTS.md` §9). The application must never imply it can create or reach a customer's database on its
own.

**What is implemented today.** `PUT /storage` stores a TLS-required PostgreSQL configuration with the
password encrypted; `POST /storage/test` performs a bounded `SELECT 1`; `POST /storage/initialize`
creates/upgrades the schema; `DELETE /storage` forgets the connection without touching the data. A
configured-but-unreachable PostgreSQL raises `StorageUnavailableError` — there is **no** silent
fallback to SQLite (`nova/store/service.py:120-140`). Host validation and SSRF protections apply
(`nova/security/net.py`).

**Known limitation.** Every PostgreSQL test uses a psycopg-shaped fake backed by SQLite
(`tests/test_phase2_integrations.py::test_sql_store_logic_runs_through_postgres_adapter`). This is not
evidence of PostgreSQL compatibility.

**Blocked until answered.** `STORE-1` acceptance, and therefore any statement that PostgreSQL storage
is production-supported.

---

## `AD-5` — Retention, deletion, export, and legal posture

**Decision needed.** Retention period for memories and audit records; what "delete my data" means to a
customer; whether export satisfies a data-portability obligation; who may approve a destructive
deletion.

**Why it is human-only.** These are legal/privacy/product commitments (`AGENTS.md` §9).

**What is implemented today.** A configurable audit retention of 90 days by default
(`NOVA_AUDIT_RETENTION_DAYS`, purged at startup for shared SQLite and every configured tenant store,
`nova/api/runtime.py:111`). `GET /data/export` returns bounded tenant data (≤10 000 rows).
`DELETE /data?confirm=delete-all-data` erases this product's memories and audit records for the tenant.
`DELETE /storage` only forgets a connection; `DELETE /provider` only forgets provider settings. Memory
history is never deleted by supersession — only marked superseded.

**Not implemented.** Any "delete the whole tenant/account" operation, any documented user-facing
retention policy, any restore strategy, and any guarantee about what a customer-owned database still
contains after deletion. Note the code comment at `nova/api/runtime.py:117-119`: the shared-SQLite
retention delete is intentionally not tenant-filtered because that file holds multiple tenants.

**Blocked until answered.** Release approval; the four-way semantic split
(disconnect / export / delete-records / delete-account) can be written now but cannot be *scoped* or
*approved* without this decision.

---

## `AD-6` — Encryption-key and secret custody

**Decision needed.** Where `NOVA_ENCRYPTION_KEY` lives (secret manager vs. environment), the rotation
cadence, who holds a copy, and the recovery procedure if it is lost.

**Why it is human-only.** Supplying and approving secrets, and changing key-rotation policy, are human
actions.

**What is implemented today.** AES-GCM encryption of provider keys and storage passwords
(`nova/security/secrets.py`) with previous-key support (`NOVA_ENCRYPTION_KEYS_PREVIOUS`) for rotation;
production refuses to start with a missing or invalid key (`nova/core/config.py:143-150`); secrets are
masked in status responses and redacted from audit text (`nova/security/redact.py`). Local development
may generate an ephemeral key.

**Warning to record.** Losing the key makes stored provider keys and storage passwords unrecoverable;
they must then be re-entered. Back up the key separately from the database.

---

## `AD-7` — Domain, TLS, DNS, egress, and monitoring

**Decision needed.** Domain and certificate, DNS changes, production firewall/egress rules, alerting
destinations, and who is on call.

**Why it is human-only.** Purchases, DNS, and firewall policy are human-controlled.

**Safe boundary today.** The Vite dev proxy is loopback-only by construction and refuses non-loopback
targets; outbound requests are validated and DNS-pinned (`nova/security/net.py`);
`NOVA_ALLOW_PRIVATE_ENDPOINTS` remains off by default because it weakens SSRF protection. Application
limits exist (`NOVA_MAX_BODY_BYTES`, request-size middleware) but **no rate limiting** is implemented,
so edge rate limits are required.

---

## `AD-8` — Target UI source of truth

**Decision needed.** Which artefact defines the target UI: the supplied "Content Anchor AI Dashboard
Showcase" image (Workspace dashboard with KPI cards, Knowledge Health donut, Recent Activity, Usage
chart, Memories table, Audit table, AI Provider Settings, Storage Settings), the architecture PDF's
screen list, or the current implemented 5-screen app.

**Why it is human-only.** It fixes product scope — in particular whether dashboard metrics, activity
feeds, and usage charts are in scope at all, since **no API can currently supply them**.

**Reality check to accept before Phase 7.** The showcase shows counts (`2,847 memories`, `186
conversations`, `92 % healthy`, an activity feed, a usage chart). None of these can be produced from
the current or planned Phase 6 APIs without deciding what is actually measured. `AGENTS.md` §5 forbids
inventing memory counts, usage, cost, or provider connection status. The target palette in the showcase
matches the mandated cream/white + navy + blue + teal scheme with neutral/green/amber/red statuses and
no purple gradients — that part is already respected by `frontend/src/styles.css` and must stay.

**Note.** The `for_di_arena/` folder that was described as containing this UI and roadmap is not in
this checkout (see `AD-12`); the image and roadmap were supplied as chat attachments instead.

---

## `AD-9` — Frontend dependency and toolchain version policy

**Decision needed.** How to resolve the Vite/Vitest version split that currently breaks
`npm run typecheck` and therefore `npm run build`, and how dependency versions will be governed
afterwards.

**Why it is human-only (or at least human-approved).** Changing devDependency majors, and choosing
whether to move to a newer Vitest major versus pinning Vite down, changes the toolchain the team
supports. It also changes what the (not yet existing) lockfile will contain.

**Measured problem.** `package.json` pins `vite@6.0.7` and `vitest@2.1.8`. A fresh install places a
nested `vite@5.4.21` under `node_modules/vitest/`, so `vitest/config`'s `defineConfig` and
`@vitejs/plugin-react` resolve two distinct Vite type identities, producing
`vite.config.ts(30,13): TS2769 No overload matches this call`. `npm run build` runs `typecheck`
first, so no bundle is produced, so the built-asset secrets check cannot run either.

**Options that exist.** (a) upgrade Vitest to a major that supports Vite 6; (b) pin Vite to the major
Vitest 2 supports; (c) split the Vitest config out of `vite.config.ts` so the two toolchains do not
share a config file. Each has different upgrade/regression cost and none is chosen here.

**Constraint regardless of choice.** The lockfile must be generated by a real install and committed;
hand-writing or guessing one is forbidden.

---

## `AD-10` — Evaluation dataset and gold labels

**Decision needed.** Who supplies the curated, non-synthetic evaluation data (real conversations,
claim/evidence pairs with human labels), and what quality bar is required before release.

**Why it is human-only.** It requires access to real user content and human annotation effort, plus
product decisions about acceptable error rates.

**What is implemented today.** A deterministic seeded synthetic benchmark with three systems, seven
scenarios, a dev/test split, metrics with confidence intervals, and a fake-LLM mode used only as a
smoke test. Its fake-mode scores are not evidence of quality.

**Blocked until answered.** `EVAL-1`, and any customer-facing quality claim.

---

## `AD-11` — Which `AGENTS.md` governs this repository

**Decision needed.** The repository contains a short `AGENTS.md` ("TOKEN RULES": no plans, reply ≤5
lines, one phase per session, `pytest -q -x`, max 3 fix attempts). The roadmap was supplied together
with a much fuller `AGENTS.md` (10 sections: mandatory 10-phase workflow, required end-of-phase report,
security and tenant-isolation rules, human-approval gates, final-README rules). They conflict in tone
and in required output.

**Why it is human-only.** It is repository governance, and it determines what every future phase
report must contain.

**How this phase handled it.** The stricter document was followed: this phase produced documentation,
exact commands and results, changed-file and risk lists, and human action items, because a five-line
reply would not satisfy the roadmap's Phase 1 deliverables. **Recommendation:** replace the repository
`AGENTS.md` with the full document (or add it as `docs/AGENTS-full.md` and reference it), so future
agents are not given contradictory instructions. This was **not** done unilaterally.

---

## `AD-12` — Missing `for_di_arena/` reference folder

**Decision needed.** Whether the `for_di_arena/` folder (described as containing the target
architecture, the UI, and the roadmap) should be committed.

**Finding.** It is not in the working tree and not in git history (`find . -iname '*for_di*'` returns
nothing; the repository has a single commit). The equivalent inputs available were:
`context_anchor_architecture.pdf` (target architecture), the "Content Anchor AI Dashboard Showcase"
image (target UI), and `Production_Roadmap_10_Phases.md` (the roadmap), all read for this phase.

**Action for the human.** If the folder contains anything beyond those three artefacts, commit it (or
supply it) before Phase 7 so the UI work is not designed against a partial reference. It is **not**
required for Phase 1 or Phase 2.

---

## Standing prohibitions to carry into every later phase

- No credentials, keys, connection strings, or user content in source, prompts, logs, fixtures,
  browser bundles, source maps, docs, or committed `.env` files.
- No backend API key in browser JavaScript; no `VITE_*` secret; no secrets in local/session storage.
- No silent `FakeLLM` or storage fallback; failures stay typed and visible.
- No invented metrics, counts, activity, cost, provider status, or integration success. Unavailable
  data is labelled unavailable.
- No purple gradients; keep cream/white, navy, blue, teal with neutral/green/amber/red statuses.
- Do not weaken or delete tests, relax lint/format rules, or disable security checks to obtain a green
  run.
