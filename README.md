# Nova

Nova is a tenant-scoped, source-linked memory service. It ingests conversation messages,
extracts source-grounded memories, retrieves evidence for questions, and verifies proposed answers
before returning a decision. It provides a FastAPI interface, SQLite storage by default,
optional tenant-managed PostgreSQL storage, and optional per-tenant provider credentials.

## Status and operating principle

This repository combines the Phase 1–4 reference work and the latest verification hardening.
It is an actively remediated codebase, not a certification of production readiness. Consult
[`CLAUDE.md`](CLAUDE.md) and [`docs/release-checklist.md`](docs/release-checklist.md) for the current
implementation map, validation commands, and unresolved limitations. Never deploy with default
or unauthenticated production configuration.

## Requirements

- Python 3.11 or newer.
- Install the development and server extras for local development:

  ```bash
  python -m pip install -e ".[dev,server]"
  ```

- Optional PostgreSQL support: `python -m pip install -e ".[postgres]"`.
- Optional sentence-transformer embedding support: `python -m pip install -e ".[st]"`.

## Frontend (Phase 5 — source implemented, validation pending)

A React + TypeScript + Vite UI is available in `frontend/` for the supported ingest, ask, verify, and audit-ID workflows. See [`frontend/README.md`](frontend/README.md) for setup and its security boundary. The current API does not support a browser-accessible memory library or audit listing, so the UI does not invent those features. `FakeLLM` remains the default development/testing integration and is labeled as such.

The frontend dependencies could not be installed in the offline authoring environment; until the dependency lockfile is generated and the frontend build, lint, type-check, component tests, and Playwright suite pass, Phase 5 is not release-ready. Hosted browser deployment also requires a trusted same-origin BFF/session layer that maps an authenticated user to a tenant-specific backend credential. No server API key may be exposed to the browser.

## Configuration

Copy `.env.example` to a private `.env` file and set deployment-specific values. Production
startup intentionally fails closed unless API authentication and a valid encryption key are set.
Production API keys must contain at least 32 characters. Keep `.env` and database files out of
version control, and manage secrets through your deployment's secret manager where possible.

Set `NOVA_ENV=development` (or `test`) only for a deliberately isolated development/test
environment where keyless API access is acceptable. Do not expose that mode to an untrusted network.

There are two API authentication modes:

- `NOVA_API_KEY` is a shared key restricted to the `default` tenant.
- `NOVA_TENANT_API_KEYS` maps tenant-specific keys to tenant IDs, for example
  `tenant-a:<long-random-key>,tenant-b:<another-long-random-key>`. Do not set both modes.

Tenant identity is derived from the authenticated key mapping. `X-Tenant-ID` is not an authority
to impersonate another tenant. The deployment uses per-tenant storage configuration; `DATABASE_URL`
is intentionally unsupported. A tenant may use the shared SQLite database or configure PostgreSQL
through the storage API. PostgreSQL is optional and must use TLS; remote connection failures do not
silently fall back to SQLite.

Provider endpoint restrictions are enabled by default. `NOVA_ALLOW_PRIVATE_ENDPOINTS=true` weakens
SSRF protection and should only be used by an operator who controls and understands the network
boundary. Prefer public HTTPS endpoints and keep redirects disabled.

## Run locally

```bash
cp .env.example .env
# Edit .env for a non-production local setup.
python -m uvicorn nova.api.main:app --host 127.0.0.1 --port 8000
```

Health and readiness endpoints are `/healthz` and `/readyz`. Readiness checks the control and
shared SQLite storage dependencies; a configured remote tenant database is checked when the
storage connection/test operation is used.

## Quality checks

Run from the repository root:

```bash
ruff check .
ruff format --check .
pytest -ra
python -m compileall -q nova benchmark scripts tests
python -m build
python scripts/check_wheel.py dist/*.whl
```

The test configuration promotes `ResourceWarning` to errors. `benchmark/` contains deterministic
fake-LLM benchmark generation, execution, and reporting utilities; full benchmark inputs are
validated before execution. Generated cases, caches, and results are local artifacts and should not
be committed. Fake-LLM outputs are smoke-test signals only, not a model-quality evaluation.

## Security and data notes

- Do not place secrets in questions, messages, logs, or source text. Audit text is bounded and
  redacted against configured key values; audit records can still contain sensitive user content.
- Use TLS at the reverse proxy and apply network-level limits/rules in addition to the API's body
  and request limits. Do not assume application SSRF checks replace egress firewall controls.
- Back up SQLite/control databases and tenant-managed PostgreSQL independently. Test restores and
  encryption-key rotation before relying on them.
- Deleting a tenant's data does not delete the tenant's separately managed PostgreSQL database;
  disconnecting that database only removes Nova's connection configuration and stored password.
