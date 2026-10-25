# Phase 4 report — storage, migrations, export and deletion

**Status: COMPLETE** in source, with one human-only item outstanding: `STORE-1` (a recorded
contract run against a real TLS PostgreSQL server, `AD-4`). Nothing was deployed.

**Branch:** `arena/1de9796f-nova-hack` · **Base:** `93d7c34` (= `main` after PR #4)

---

## 1. Design summary

The persistence layer already had the right *shape* — a `Database` ABC with two implementations, a
tenant-scoped `StorageSession`, repositories for memories and audit, and SQL written once with `?`
placeholders. Phase 4 kept that shape and closed what was missing:

1. **Version safety.** `schema_version` is now the single source of truth and the version constants
   are *derived* from the migration lists, so they cannot drift. A database written by a newer build
   is refused (`SchemaVersionError` → HTTP 503) instead of being read through an older
   understanding of it. `migration_plan()` and `current_version()` expose what would happen.
2. **Atomic, repeatable migrations.** All pending steps run in one transaction. Tests prove a step
   that fails *before* creating a table and a step that fails *after* creating one both leave the
   database byte-for-byte at its previous version. There are no down-migrations; rollback is
   backup-restore, documented as a runbook.
3. **A real data lifecycle.** Delete previously erased memories and audit rows and left no record.
   Now `scope=records` (default) keeps a **content-free deletion receipt**, and `scope=account` also
   purges prior receipts with an optional tombstone. Deletion is atomic: if the receipt cannot be
   written, nothing is deleted. `DELETE /storage` still only forgets the connection.
4. **Bounded reads.** Every read that can grow with tenant data has a hard limit in one module
   (`nova/store/limits.py`) and **raises** `StoreLimitError` (HTTP 422) instead of truncating, because
   a short list looks like lost data. Export states `counts`/`returned`/`truncated`.
5. **Explicit PostgreSQL, unchanged local setup.** SQLite stays the zero-configuration default; a
   tenant that configures PostgreSQL never silently falls back to SQLite. A contract test now exists
   that runs the same lifecycle against a real server when `NOVA_TEST_POSTGRES_DSN` is set.
6. **Additive only.** No memory/evidence/audit column was renamed, retyped, or removed, and the
   supersession triggers are untouched — so the Person 2 boundary (memory/evidence/audit schema and
   embedding metadata) is respected. Phase 4 adds one new table and two indexes.

---

## 2. Changed files

**New**
- `nova/store/limits.py` — the single place read bounds are declared
- `nova/store/receipts.py` — `DeletionReceipt` (metadata only) + `SqlReceiptStore`
- `tests/test_phase4_storage.py` — 34 tests, 1 skipped (opt-in PostgreSQL)
- `docs/storage.md` — storage interface, migrations + rollback runbook, lifecycle, limits
- `docs/phase-4-report.md` — this file

**Modified**
- `nova/store/schema.py` — SQLite migration **v3** (`deletion_receipts` + two indexes), PostgreSQL
  migration **v2**, derived versions, `migration_plan`/`current_version`/`known_version`, newer-schema
  refusal, dialect-aware table detection, `SchemaMigrationError`
- `nova/store/session.py` — `purge(scope, actor, request_id, retain_receipt)`, receipts repository,
  export with counts and truncation, bounded export; `delete_tenant_data()` keeps its old contract
- `nova/store/sql_store.py` — bounded `candidates()`, `missing_embeddings()`, `list_by_session()`,
  `export()`
- `nova/store/service.py` — `purge()`, `receipts()`, bounded `export()`
- `nova/store/base.py` — `SchemaVersionError`, `SchemaMigrationError`, `StoreLimitError`
- `nova/store/postgres.py` — `PG_SCHEMA_VERSION` derived from the migration list
- `nova/audit/logger.py` — `count()`, bounded `recent()` (no more silent clamp)
- `nova/api/main.py` — `GET /data/deletions`; `DELETE /data` gains `scope`/`retain_receipt` and records
  a non-secret actor + request id; export returns counts and schema version
- `nova/api/errors.py` — `SchemaVersionError` → 503, `StoreLimitError` → 422
- `tests/test_phase2_integrations.py` — one stale assertion (`initialize == 2`) now compares against
  `SCHEMA_VERSION` instead of a hard-coded number
- `docs/production-readiness-matrix.md` — `STORE-2` closed, `STORE-1` restated as human-only, new §12

---

## 3. Exact commands and results (Python 3.11.2, this environment)

| Command | Result |
|---|---|
| `pytest -q -W error::ResourceWarning` | **676 passed, 1 skipped**, 19.1 s |
| `pytest -q tests/test_phase4_storage.py` | **34 passed, 1 skipped**, 0.73 s |
| `ruff check .` | `All checks passed!` |
| `ruff format --check .` | `140 files already formatted` |
| `python -m compileall -q nova benchmark scripts tests` | exit 0 |
| `python -m build` | `nova-0.2.0.tar.gz` + `nova-0.2.0-py3-none-any.whl` |
| `python scripts/check_wheel.py dist/*.whl` | `OK: … contains the intended distribution` |
| `python -m benchmark.runner --seed 7 --n 30 --split dev --systems baseline,rag --llm fake --limit 30` | exit 0 |
| `python -m benchmark.validate_inputs` | exit 0 |

### Skipped, and why

`tests/test_phase4_storage.py::test_postgres_contract_matches_sqlite` — **skipped**: no PostgreSQL
server and no `NOVA_TEST_POSTGRES_DSN` in this environment. It is a genuine skip with a stated
reason, not a pass. `STORE-1` therefore stays open.

### Not run

- Real PostgreSQL: migration, isolation, and deletion on a real server (`AD-4`).
- Browser E2E, hosted identity — unchanged from earlier phases and out of Phase 4 scope.

---

## 4. Evidence: secrets are not returned or logged

- **No secret can reach a receipt by construction.** `test_receipt_model_cannot_carry_content` pins
  the *exact* field set of `DeletionReceipt`; adding a content field breaks the test.
- **No secret appears in lifecycle payloads.** `test_lifecycle_payloads_never_include_configured_secrets`
  stores a provider key, then asserts it is absent from `GET /data/export`, `DELETE /data`, and
  `GET /data/deletions` — and that not even the `****last4` mask leaks into receipts.
- **No deleted content survives.** Receipts, the receipts endpoint, and exports are all scanned for
  the seeded memory text and question in
  `test_records_scope_deletes_data_and_keeps_a_content_free_receipt` and
  `test_deletion_receipts_endpoint_is_content_free`.
- **Storage credentials** keep their Phase 2 guarantees: write-only `SecretStr`, encrypted at rest,
  masked (`test_storage_configuration_lifecycle_and_masking`), host vetted
  (`test_configure_rejects_blocked_host_and_missing_password`), and driver errors reduced to stable
  codes (`test_postgres_errors_are_redacted_to_stable_codes`).
- **Logs** record the error *class* and a stable code only; `nova/audit/logger.py` continues to
  redact configured keys and credential shapes before storing audit text.

---

## 5. Export and deletion behaviour, and known limitations

| Operation | Removes | Leaves |
|---|---|---|
| `DELETE /storage` | connection settings + password | all tenant data in the external database |
| `GET /data/export?limit=N` | nothing | memories, audit, receipts + `counts`/`returned`/`truncated`/`schema_version` |
| `DELETE /data?confirm=delete-all-data` | memories, audit | one content-free receipt |
| `…&scope=account` | the above **and** prior receipts | one tombstone, or nothing with `retain_receipt=false` |

Limitations, stated plainly:

- **No down-migrations.** Rollback = restore the backup (§4 of `docs/storage.md`). Deliberate: an
  automatic down-migration that drops columns is a data-loss feature.
- **Receipts are retained on purpose** (they contain no content); use `retain_receipt=false` for an
  account erasure that must leave nothing.
- **Reads are bounded, not paged.** Over-limit is a 422, not a next-page cursor; public pagination is
  Phase 6 (`API-1`).
- **SQLite is single-writer**, fine for development and small deployments; production multi-tenant
  scale needs PostgreSQL.
- **`audit_log` has no FK to `memories`** on purpose — audit history must outlive the memories it
  describes; a cascade would delete evidence.
- **Retention policy is an operator decision** (`AD-5`). The mechanism (`NOVA_AUDIT_RETENTION_DAYS`,
  default 90) is unchanged.

---

## 6. Compatibility risks and handoff

| Risk | Severity | Note |
|---|---|---|
| Schema bump (SQLite 2 → 3, PG 1 → 2) | **Medium** | Migration is additive and runs on startup, but **take a backup first** in production. An older build refuses a v3 database rather than mis-reading it, so rolling back requires the restore path |
| `GET /data/export` response gained fields (`counts`, `returned`, `truncated`, `schema_version`, `deletion_receipts`) | Low | Additive; the previous keys are unchanged. Any consumer doing exact-shape validation must update |
| `DELETE /data` now returns a receipt-shaped object instead of only counts | Low | Still contains `memories`/`audit_records`, so existing readers keep working |
| `list_by_session` / `candidates` / `missing_embeddings` now raise above their bounds | Medium | A tenant with more than 5 000 current memories will get a 422 from `/ask` instead of an unbounded (slow) scan. This is intentional: it surfaces a scoping problem instead of hiding it — Phase 5/6 should add scoped retrieval and pagination |
| Real-PostgreSQL behaviour unverified | **High** | The PostgreSQL DDL, trigger, and migration v2 have never executed against a real server here |
| Receipts contain tenant id + actor | Low | Metadata only, no content; documented in `docs/storage.md` §6 for a privacy review |

**Handoff to Phase 5/6:** retrieval scoping vs `MAX_CANDIDATE_ROWS`; pagination and the public memory
audit list APIs (`API-1`); embedding re-index job shapes on top of `missing_embeddings(limit)`; and,
if background jobs need state, a job table belongs to Person 3's event contract, not here.

**Human-only actions:** (1) provide a PostgreSQL server and run the contract suite with
`NOVA_TEST_POSTGRES_DSN` (this closes `STORE-1`); (2) decide the retention policy (`AD-5`); (3) take
and verify a production backup before the first v3 migration; (4) confirm the deletion-receipt
metadata is acceptable to the privacy/legal owner (it holds tenant id, actor label, timestamp, and
counts only).
