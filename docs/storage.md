# Storage, migrations, export, and deletion

**Phase 4 deliverable.** Status: **COMPLETE in source** for the application side. One item stays
human-only: `STORE-1`, a recorded contract run against a **real** TLS PostgreSQL server (`AD-4`).

This page documents what the code does today. Where a report or a design note disagrees, **the
source is truth** and this page is what gets fixed.

---

## 1. Ownership boundaries and assumptions

Written down so other agents can work in parallel without guessing.

| Boundary | Owner | Contract |
|---|---|---|
| `nova/store/*` — schema, migrations, SQL, repositories, limits | this phase | Behaviour below is the contract; nothing outside imports a concrete store class |
| Memory/evidence **column semantics** (`subject`, `status`, `supersedes_id`, evidence rules) | Person 2 (memory/evidence owner) | Phase 4 is **additive only**: no column was renamed, retyped, or removed, and the supersession triggers are untouched |
| Embedding **metadata** (`embedding`, `embedding_model`, `embedding_dim`) | Person 2 / Phase 5 (`EMB-1`) | Phase 4 adds no embedding column and performs no re-embedding; compatibility checks stay where they are |
| Audit record **fields** | Person 2 / Phase 6 (`API-1`) | Unchanged. Phase 4 adds a separate `deletion_receipts` table, not audit columns |
| Background-job state, event/usage contracts | Person 3 | Nothing added: storage exposes no job table and no usage stream in this phase |
| Which database a tenant uses | operator (`AD-4`) | SQLite by default; PostgreSQL only when the tenant configures it |

**Assumption:** SQL is written once with `?` placeholders; each backend adapts placeholders and
DDL. Nothing in application logic may assume SQLite.

---

## 2. The storage interface

```
API routes ──► StorageService ──► StorageSession ──┬──► SqlMemoryStore   (memories)
                (per tenant)      (one connection) ├──► SqlAuditStore    (audit_log)
                                                  ├──► SqlReceiptStore  (deletion_receipts)
                                                  └──► Database (ABC)   ── SQLite | PostgreSQL
```

- **`Database`** (`nova/store/database.py`) — one connection, an explicit re-entrant
  `transaction()` scope, `execute`/`query`, `dialect`, and `order_column`. Both backends implement
  it; that is the only thing SQL code depends on.
- **`MemoryStore` protocol** (`nova/core/protocols.py`) — what extraction, retrieval, and the
  pipeline code against. `SqlMemoryStore` satisfies it.
- **`StorageSession`** (`nova/store/session.py`) — binds one connection to exactly one tenant and
  exposes `memories`, `audit`, `receipts`, plus `initialize`, `check`, `purge`, and
  `export_tenant_data`.
- **`StorageService`** (`nova/store/service.py`) — the tenant's storage lifecycle (status, configure,
  test, initialize, open, export, purge, receipts, disconnect), including the SQLite default.

A caller never sees a driver, a DSN, or a password.

---

## 3. Backends

| Backend | When it is used | Setup |
|---|---|---|
| SQLite (`nova.db`, WAL) | the default, and every tenant without a storage configuration | none — local development needs no server |
| PostgreSQL | only for a tenant that configured it | `PUT /storage` with host/port/database/user/password/sslmode |

**No silent fallback.** A tenant that configured PostgreSQL and cannot reach it gets
`storage_unreachable`/`storage_timeout`/`storage_auth_failed` — never a SQLite database that would
quietly look empty. A tenant with no configuration uses SQLite by design, and `GET /storage` says so
(`kind: "sqlite", configured: false`).

PostgreSQL connections require TLS (`sslmode` is one of `require`, `verify-ca`, `verify-full`,
default `verify-full`), vet the host through the SSRF rules, pin the resolved address, and set
`connect_timeout`, `statement_timeout`, `lock_timeout`, and `idle_in_transaction_session_timeout`.
Credential handling matches the provider path: write-only `SecretStr`, encrypted at rest, masked as
`****last4`, never returned or logged.

---

## 4. Migrations

**Versioned.** `schema_version(version, applied_at)` is the ledger. Each backend has an ordered
list — `SQLITE_MIGRATIONS` (v1, v2, v3) and `POSTGRES_MIGRATIONS` (v1, v2) — and
`known_version(dialect)` is derived from those lists, so a version constant cannot drift from the
migrations that exist.

| Version | Backend | What it does |
|---|---|---|
| 1 | SQLite | legacy `memories` table |
| 2 | SQLite | tenant scoping, constraints, embedding metadata, audit table, indexes, supersession triggers |
| 3 | SQLite | `deletion_receipts` + indexes (`receipts(tenant_id, deleted_at)`, `memories(tenant_id, timestamp, id)`) |
| 1 | PostgreSQL | full schema, indexes, PL/pgSQL supersession trigger |
| 2 | PostgreSQL | `deletion_receipts` + the same two indexes |

**Repeatable and safe:**
- All pending steps run inside **one transaction**. A failure anywhere rolls back everything —
  verified by tests that fail a step both before and after it creates a table.
- Re-running `migrate` on an up-to-date database is a no-op (the ledger rows are unchanged).
- **A newer schema is refused.** If the ledger says 4 and this build knows 3, `migrate` raises
  `SchemaVersionError` (`schema_version_unsupported`, HTTP 503) and writes nothing. Data is never
  read through a schema this build does not understand.
- The v2 upgrade repairs invalid legacy supersession links (logging **how many**, not what) and
  normalises timestamps to UTC. Legacy `0/0` token counts become `NULL` ("unknown"), never `0`.
- `migration_plan(db)` lists what would be applied without applying it; `current_version(db)`
  reports what is recorded.

### Rollback and recovery (runbook)

There are **no down-migrations**. Every migration is additive (new table, new column, new index, or
a rebuild that preserves rows), so the supported recovery paths are:

1. **Before any upgrade**: take a backup.
   - SQLite: stop the process, copy `NOVA_DB_PATH` (and checkpoint the WAL: `PRAGMA
     wal_checkpoint(TRUNCATE)`), or use `VACUUM INTO 'backup.db'` while running.
   - PostgreSQL: `pg_dump --format=custom` of the tenant database.
2. **Interrupted migration** (host died mid-way): nothing to clean up — the transaction never
   committed, so the database is still at the previous version. Restart the process and the same
   steps re-run from the ledger. Verify with `migration_plan(db)`.
3. **Failed migration** (`schema_migration_failed`): the database is unchanged. Fix the cause
   (disk space, permissions, a conflicting table) and retry. Do not hand-edit `schema_version`.
4. **Roll back to an older build**: restore the backup taken in step 1. A newer build's schema is
   refused by the older build rather than mis-read, which is what makes this predictable.
5. **Never** delete rows or tables to make a version mismatch disappear; that is data loss dressed
   up as a fix.

---

## 5. Schema reference

| Table | Key | Notable constraints |
|---|---|---|
| `schema_version` | `version` | append-only ledger |
| `memories` | `(tenant_id, id)` | `type` CHECK, `status` CHECK, `confidence` 0–1, `supersedes_id <> id`, embedding/dim agree, self-FK `(tenant_id, supersedes_id)` **DEFERRABLE INITIALLY DEFERRED**, triggers enforce same-subject and forward-only supersession |
| `audit_log` | `(tenant_id, id)` | token columns nullable (unknown ≠ 0) |
| `deletion_receipts` | `(tenant_id, id)` | `scope` CHECK (`records`/`account`), non-negative counts, **no content columns** |

Indexes: `memories(tenant_id, subject, status)`, `memories(tenant_id, session_id)`,
`memories(tenant_id, timestamp, id)`, unique partial `memories(tenant_id, supersedes_id)`,
`audit_log(tenant_id, timestamp)`, `deletion_receipts(tenant_id, deleted_at)`.

Foreign keys are enforced on SQLite (`PRAGMA foreign_keys = ON` is set on every connection) and
declared on PostgreSQL. `audit_log` has no foreign key to `memories` on purpose: an audit record
must outlive the memories it references, and a cascade would silently delete audit history.

**Tenant scoping is not optional.** Every read, write, delete, and export statement in
`SqlMemoryStore`, `SqlAuditStore`, and `SqlReceiptStore` filters on `tenant_id`, and the value comes
from the validated tenant of the session that owns the connection. There is no query that can see
two tenants at once. Tests cover the same memory id in two tenants, deletion isolation, and
receipt isolation.

---

## 6. Data lifecycle

Four different operations, four different meanings. All of them are tenant-scoped and callable only
by an authenticated tenant; nothing here is available to a browser session without the BFF.

| Operation | Endpoint | What it removes | What remains |
|---|---|---|---|
| Forget the connection | `DELETE /storage` | the stored PostgreSQL settings + password | **all data** in the external database (untouched) |
| Export | `GET /data/export?limit=N` | nothing | returns memories, audit records, and deletion receipts, with `counts`, `returned`, `truncated`, `schema_version` |
| Delete records | `DELETE /data?confirm=delete-all-data` (default `scope=records`) | memories (+embeddings, links) and audit records | one **content-free deletion receipt** |
| Delete account | `DELETE /data?confirm=delete-all-data&scope=account` | the above **plus** prior receipts | one tombstone receipt (`retain_receipt=true`, the default) or nothing at all (`retain_receipt=false`) |

- Deletion runs in **one transaction**: if the receipt cannot be written, the data is *not* deleted
  (tested). There is no state where rows vanished without a record.
- **A receipt never contains content.** It stores scope, actor, request id, timestamp, and counts —
  no memory value, quote, subject, question, answer, or deleted row id. Tests assert deleted text
  cannot be found in the receipt payload, the receipts endpoint, or an export.
- `actor` is a non-secret label: `session:<subject>` when a browser session is present, otherwise
  `tenant_api_key`. `request_id` is the API request id, so a deletion can be matched to a log line.
- The legacy `storage.delete_data()` helper keeps returning `{"memories": n, "audit_records": n}`
  and still writes a receipt.
- Read receipts at `GET /data/deletions?limit=N` (bounded to 200).

**Retention:** audit retention (`NOVA_AUDIT_RETENTION_DAYS`, default 90) is unchanged and only
touches `audit_log`. Receipts are not auto-purged; they are the proof of erasure and are removed
only by an account-scope deletion. The retention *policy* itself is still `AD-5` (operator decision,
see the matrix).

---

## 7. Limits and bounded reads

Every read path that can grow with tenant data is bounded in `nova/store/limits.py`. Exceeding a
bound raises `StoreLimitError` (`query_limit_exceeded`, HTTP 422) — **the code never truncates
silently**, because a short list would look like missing data.

| Read | Bound |
|---|---|
| Export, per table | `MAX_EXPORT_ROWS` = 10 000 (API `limit` is capped at the same value) |
| Retrieval candidate scan | `MAX_CANDIDATE_ROWS` = 5 000 |
| Embedding backfill batch | `MAX_EMBEDDING_BATCH` = 1 000 |
| Session listing | `MAX_SESSION_ROWS` = 2 000 |
| Recent audit records | `MAX_RECENT_AUDIT_ROWS` = 200 |
| Deletion receipts listing | `MAX_RECEIPT_ROWS` = 1 000 (API caps at 200) |

Export responses carry `counts` and `truncated`, so a caller can tell a complete export from a
capped one. Pagination for the public list APIs is Phase 6 (`API-1`).

---

## 8. Testing

```bash
pytest -q tests/test_phase4_storage.py     # 34 passed, 1 skipped (no PostgreSQL server)
pytest -q                                  # full suite
```

Covered: migration to the current version, idempotent re-run, ledger contents, refusal of a newer
schema, rollback of a failed step and of a step that fails after creating a table, legacy v1 → v3
upgrade without loss, tenant/session isolation, every deletion scope, atomic deletion, receipts
content and survival rules, export bounds/truncation, all read bounds, the HTTP endpoints, and the
error mapping (422/503).

### The real-PostgreSQL contract suite (opt-in, `STORE-1`)

`tests/test_phase4_storage.py::test_postgres_contract_matches_sqlite` runs the same lifecycle
(migrate → insert → purge → receipt) against a **real** server when `NOVA_TEST_POSTGRES_DSN` is set:

```bash
NOVA_TEST_POSTGRES_DSN="postgresql://nova:pw@db.example.com:5432/nova_test?sslmode=verify-full" \
  pytest -q tests/test_phase4_storage.py -k postgres -v
```

Without the variable the test **skips** with an explicit reason. This environment has no PostgreSQL
server, so the suite is **NOT RUN** here: `STORE-1` stays open until a human runs it against TLS.
Note that a skipped test is not evidence — the matrix says so, and so does this page.

---

## 9. Known limitations

- **No down-migrations.** Rollback means restoring the backup from §4. Deliberate: an automatic
  down-migration that drops columns would be a data-loss feature.
- **A receipt is retained on purpose.** It proves erasure happened; it holds no content. Operators
  who need zero residue can pass `retain_receipt=false` for an account-scope deletion.
- **`audit_log` and `memories` are not cascaded.** Deleting a memory leaves audit history intact
  until a lifecycle operation removes it; that is the intended audit behaviour, not an oversight.
- **Reads are bounded, not paged.** Exceeding a bound is an error, not a next-page cursor.
- **SQLite is single-writer.** Fine for development and small deployments; a multi-tenant production
  workload needs the PostgreSQL path.
- **Encryption at rest is the database's job.** Nova encrypts stored *credentials*, not tenant rows.
