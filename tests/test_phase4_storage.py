"""Phase 4: storage boundary, versioned migrations, lifecycle, and bounded reads.

Everything here runs against SQLite by default. The PostgreSQL contract suite at the end runs only
when ``NOVA_TEST_POSTGRES_DSN`` names a reachable server, so a missing server is reported as a skip
rather than passed off as a green run.
"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nova.audit.logger import AuditRecord
from nova.core.schemas import MemoryType
from nova.store import schema
from nova.store.base import (
    SchemaMigrationError,
    SchemaVersionError,
    StoreError,
    StoreLimitError,
)
from nova.store.database import SqliteDatabase
from nova.store.limits import (
    MAX_CANDIDATE_ROWS,
    MAX_EMBEDDING_BATCH,
    MAX_EXPORT_ROWS,
    MAX_RECENT_AUDIT_ROWS,
    MAX_SESSION_ROWS,
)
from nova.store.schema import (
    POSTGRES_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SQLITE_SCHEMA_VERSION,
    current_version,
    migrate,
    migration_plan,
)
from nova.store.session import StorageSession
from tests.helpers import mem

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _session(tmp_path: Path, tenant: str = "t1") -> StorageSession:
    """Open a migrated SQLite storage session for one tenant."""
    session = StorageSession(SqliteDatabase(tmp_path / "data.db"), tenant, "sqlite")
    session.initialize()
    return session


def _seed(session: StorageSession) -> None:
    """Put one memory and one audit record into the session."""
    session.memories.add(mem("m1", MemoryType.FACT, value="PostgreSQL"))
    session.audit.write(AuditRecord(id="r1", question="Which database?", answer="PostgreSQL"))


# --------------------------------------------------------------------------------------
# migrations: versioned, repeatable, refuse newer, atomic on failure
# --------------------------------------------------------------------------------------


def test_fresh_database_reaches_the_current_version(tmp_path: Path) -> None:
    """A new database is migrated to the current schema version exactly once."""
    db = SqliteDatabase(tmp_path / "fresh.db")
    try:
        assert migration_plan(db) == list(range(1, SCHEMA_VERSION + 1))
        assert migrate(db) == SCHEMA_VERSION
        assert current_version(db) == SCHEMA_VERSION
        assert migration_plan(db) == []
    finally:
        db.close()


def test_migration_is_repeatable(tmp_path: Path) -> None:
    """Running migrations again changes nothing: same version, same ledger rows."""
    db = SqliteDatabase(tmp_path / "repeat.db")
    try:
        assert migrate(db) == SCHEMA_VERSION
        rows = db.query("SELECT version, applied_at FROM schema_version ORDER BY version")
        assert migrate(db) == SCHEMA_VERSION
        assert db.query("SELECT version, applied_at FROM schema_version ORDER BY version") == rows
    finally:
        db.close()


def test_newer_schema_is_refused_rather_than_used(tmp_path: Path) -> None:
    """A database written by a newer build must not be opened and written by this one."""
    db = SqliteDatabase(tmp_path / "future.db")
    try:
        migrate(db)
        db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 5, "2030-01-01T00:00:00+00:00"),
        )
        with pytest.raises(SchemaVersionError) as excinfo:
            migrate(db)
        assert excinfo.value.code == "schema_version_unsupported"
        # nothing was applied, and the newer ledger row is still there
        assert current_version(db) == SCHEMA_VERSION + 5
    finally:
        db.close()


def test_failed_migration_leaves_the_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception mid-migration rolls the whole transaction back (no half-applied schema)."""
    path = tmp_path / "interrupted.db"
    db = SqliteDatabase(path)
    try:
        # first bring the database to v2 only
        monkeypatch.setattr(schema, "SQLITE_MIGRATIONS", schema.SQLITE_MIGRATIONS[:2])
        assert migrate(db) == 2

        def boom(_: Any) -> None:
            raise StoreError("simulated failure half-way through v3")

        monkeypatch.undo()
        monkeypatch.setattr(schema, "SQLITE_MIGRATIONS", [(3, boom)])
        with pytest.raises(SchemaMigrationError):
            migrate(db)
        assert current_version(db) == 2
        assert not db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='deletion_receipts'"
        )
    finally:
        db.close()
    # the file is still a usable v2 database once the simulated failure is removed
    monkeypatch.undo()
    db2 = SqliteDatabase(path)
    try:
        assert current_version(db2) == 2
        assert migrate(db2) == SCHEMA_VERSION
    finally:
        db2.close()


def test_partial_failure_inside_a_step_rolls_back_every_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a step that creates a table then fails cannot leave a stray table behind."""

    def half_step(db: SqliteDatabase) -> None:
        db.execute("CREATE TABLE half_applied (x INTEGER)")
        raise StoreError("failed after creating the table")

    db = SqliteDatabase(tmp_path / "half.db")
    try:
        monkeypatch.setattr(schema, "SQLITE_MIGRATIONS", schema.SQLITE_MIGRATIONS[:2])
        migrate(db)
        monkeypatch.undo()
        monkeypatch.setattr(schema, "SQLITE_MIGRATIONS", [(3, half_step)])
        with pytest.raises(SchemaMigrationError):
            migrate(db)
        assert not db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='half_applied'"
        )
    finally:
        db.close()


def test_legacy_v1_data_is_upgraded_without_loss(tmp_path: Path) -> None:
    """A v1 database keeps its rows, gains tenant scoping, and records the new version."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (1, 'x');
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, subject TEXT NOT NULL, value TEXT NOT NULL,
            reason TEXT, source_msg_id TEXT NOT NULL, source_quote TEXT NOT NULL,
            session_id TEXT NOT NULL, timestamp TEXT NOT NULL, status TEXT NOT NULL DEFAULT
            'current', supersedes_id TEXT, confidence REAL NOT NULL DEFAULT 1.0, embedding BLOB);
        INSERT INTO memories (id, type, subject, value, source_msg_id, source_quote, session_id,
            timestamp) VALUES ('old', 'fact', 'db', 'MySQL', 'msg', 'MySQL', 's',
            '2026-01-01T00:00:00+00:00');
        CREATE TABLE audit_log (
            id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, question TEXT NOT NULL,
            answer TEXT NOT NULL, retrieved_ids TEXT NOT NULL, verdicts TEXT NOT NULL,
            decision_action TEXT NOT NULL, latency_ms REAL NOT NULL,
            prompt_tokens INTEGER, completion_tokens INTEGER);
        INSERT INTO audit_log VALUES ('a1', '2026-01-01T00:00:00+00:00', 'q', 'a', '[]', '[]',
            'answer', 1.0, 0, 0);
        """
    )
    conn.commit()
    conn.close()

    db = SqliteDatabase(path)
    try:
        assert migrate(db) == SCHEMA_VERSION
        rows = db.query("SELECT tenant_id, value FROM memories")
        assert rows == [{"tenant_id": "default", "value": "MySQL"}]
        audit = db.query("SELECT tenant_id, prompt_tokens FROM audit_log")
        assert audit == [{"tenant_id": "default", "prompt_tokens": None}]  # 0/0 is "unknown"
    finally:
        db.close()


def test_dialect_versions_are_declared_per_backend() -> None:
    """SQLite and PostgreSQL carry their own version numbers, both from one migration list."""
    assert SQLITE_SCHEMA_VERSION == SCHEMA_VERSION
    assert POSTGRES_SCHEMA_VERSION >= 2  # receipts migration exists for PostgreSQL too


def test_schema_version_ledger_matches_applied_migrations(tmp_path: Path) -> None:
    """Every applied migration is recorded exactly once, in order."""
    db = SqliteDatabase(tmp_path / "ledger.db")
    try:
        migrate(db)
        versions = [
            r["version"] for r in db.query("SELECT version FROM schema_version ORDER BY version")
        ]
        assert versions == list(range(1, SCHEMA_VERSION + 1))
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# tenant and session scoping
# --------------------------------------------------------------------------------------


def test_two_tenants_cannot_see_or_delete_each_other(tmp_path: Path) -> None:
    """The same memory id in two tenants stays independent for reads, export, and deletion."""
    a = _session(tmp_path, "t1")
    b = _session(tmp_path, "t2")
    try:
        a.memories.add(mem("same", MemoryType.FACT, value="tenant one"))
        b.memories.add(mem("same", MemoryType.FACT, value="tenant two"))
        assert a.memories.get("same").value == "tenant one"  # type: ignore[union-attr]
        assert b.memories.get("same").value == "tenant two"  # type: ignore[union-attr]

        a.purge(scope="records", actor="test")
        assert a.memories.get("same") is None
        assert b.memories.get("same") is not None
        assert a.receipts.count() == 1
        assert b.receipts.count() == 0
    finally:
        a.close()
        b.close()


def test_receipts_are_tenant_scoped(tmp_path: Path) -> None:
    """One tenant's deletion receipts never appear in another tenant's reads."""
    a = _session(tmp_path, "t1")
    b = _session(tmp_path, "t2")
    try:
        a.purge(scope="records", actor="alice")
        assert [r.actor for r in a.receipts.recent()] == ["alice"]
        assert b.receipts.recent() == []
    finally:
        a.close()
        b.close()


def test_session_scoped_reads_are_bounded(tmp_path: Path) -> None:
    """Listing a session raises at the bound instead of silently dropping rows."""
    session = _session(tmp_path)
    try:
        session.memories.add(mem("m1", MemoryType.FACT, session="s1"))
        session.memories.add(mem("m2", MemoryType.FACT, session="s1", minutes=1))
        assert len(session.memories.list_by_session("s1")) == 2
        with pytest.raises(StoreLimitError):
            session.memories.list_by_session("s1", limit=1)
        with pytest.raises(StoreLimitError):
            session.memories.list_by_session("s1", limit=MAX_SESSION_ROWS + 1)
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# deletion lifecycle
# --------------------------------------------------------------------------------------


def test_records_scope_deletes_data_and_keeps_a_content_free_receipt(tmp_path: Path) -> None:
    """Deletion removes memories and audit rows, and the receipt proves it without content."""
    session = _session(tmp_path)
    try:
        _seed(session)
        receipt = session.purge(scope="records", actor="alice", request_id="req-1")
        assert receipt is not None
        assert (receipt.memories_deleted, receipt.audit_deleted) == (1, 1)
        assert session.memories.count() == 0
        assert session.audit.count() == 0

        payload = json.dumps(receipt.model_dump(mode="json"))
        for leaked in ("PostgreSQL", "Which database?", "source_quote", "msg-m1", "value"):
            assert leaked not in payload
    finally:
        session.close()


def test_receipts_survive_a_records_deletion_and_are_listable(tmp_path: Path) -> None:
    """A records-scope deletion leaves earlier receipts in place, newest first."""
    session = _session(tmp_path)
    try:
        _seed(session)
        session.purge(scope="records", actor="alice")
        _seed(session)
        session.purge(scope="records", actor="bob", request_id="req-2")
        actors = [r.actor for r in session.receipts.recent()]
        assert actors == ["bob", "alice"]
    finally:
        session.close()


def test_account_scope_purges_receipts_too(tmp_path: Path) -> None:
    """Account erasure removes prior receipts; one tombstone is written by default."""
    session = _session(tmp_path)
    try:
        _seed(session)
        session.purge(scope="records", actor="alice")
        tombstone = session.purge(scope="account", actor="alice", request_id="req-3")
        assert tombstone is not None and tombstone.scope == "account"
        assert tombstone.receipts_deleted == 1
        assert [r.scope for r in session.receipts.recent()] == ["account"]
        assert session.memories.count() == 0 and session.audit.count() == 0
    finally:
        session.close()


def test_account_scope_can_write_no_tombstone_at_all(tmp_path: Path) -> None:
    """With ``retain_receipt=False`` an account erasure leaves nothing behind."""
    session = _session(tmp_path)
    try:
        _seed(session)
        session.purge(scope="records", actor="alice")
        assert session.purge(scope="account", actor="alice", retain_receipt=False) is None
        assert session.receipts.count() == 0
    finally:
        session.close()


def test_deletion_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the receipt cannot be written, the deletion rolls back: counts and data stay."""
    session = _session(tmp_path)
    try:
        _seed(session)

        def boom(**_kwargs: Any) -> None:
            raise StoreError("receipt write failed")

        monkeypatch.setattr(session.receipts, "write", boom)
        with pytest.raises(StoreError):
            session.purge(scope="records", actor="alice")
        assert session.memories.count() == 1
        assert session.audit.count() == 1
        assert session.receipts.count() == 0
    finally:
        session.close()


def test_legacy_delete_helper_keeps_its_contract(tmp_path: Path) -> None:
    """``delete_tenant_data`` still returns the two counts callers already depend on."""
    session = _session(tmp_path)
    try:
        _seed(session)
        assert session.delete_tenant_data() == {"memories": 1, "audit_records": 1}
        assert session.receipts.count() == 1  # the deletion is still auditable
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------------------


def test_export_reports_counts_and_truncation(tmp_path: Path) -> None:
    """Export is bounded and states whether the bound cut the result."""
    session = _session(tmp_path)
    try:
        _seed(session)
        data = session.export_tenant_data()
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["counts"] == {"memories": 1, "audit_records": 1, "deletion_receipts": 0}
        assert data["truncated"] is False
        full = session.export_tenant_data(limit=1)
        assert full["truncated"] is False  # exactly one row fits
        session.memories.add(mem("m2", MemoryType.FACT, minutes=1))
        partial = session.export_tenant_data(limit=1)
        assert partial["truncated"] is True
        assert partial["counts"]["memories"] == 2 and partial["returned"]["memories"] == 1
    finally:
        session.close()


def test_export_includes_receipts_but_no_deleted_content(tmp_path: Path) -> None:
    """An export after deletion contains the receipt and nothing from the deleted rows."""
    session = _session(tmp_path)
    try:
        _seed(session)
        session.purge(scope="records", actor="alice")
        data = session.export_tenant_data()
        assert data["memories"] == [] and data["audit_records"] == []
        assert len(data["deletion_receipts"]) == 1  # type: ignore[arg-type]
        assert "PostgreSQL" not in json.dumps(data)
    finally:
        session.close()


def test_export_refuses_an_unbounded_limit(tmp_path: Path) -> None:
    """A caller cannot ask for more rows than the documented maximum."""
    session = _session(tmp_path)
    try:
        with pytest.raises(StoreLimitError):
            session.export_tenant_data(limit=MAX_EXPORT_ROWS + 1)
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# bounded reads
# --------------------------------------------------------------------------------------


def test_candidate_scan_refuses_to_grow_unbounded(tmp_path: Path) -> None:
    """The retrieval working set raises at its bound instead of returning a partial view."""
    session = _session(tmp_path)
    try:
        for i in range(3):
            session.memories.add(mem(f"m{i}", MemoryType.FACT, minutes=i))
        assert len(session.memories.candidates()) == 3
        with pytest.raises(StoreLimitError):
            session.memories.candidates(limit=2)
        with pytest.raises(StoreLimitError):
            session.memories.candidates(limit=MAX_CANDIDATE_ROWS + 1)
    finally:
        session.close()


def test_embedding_backfill_is_bounded_by_default(tmp_path: Path) -> None:
    """``missing_embeddings`` always carries a LIMIT, even when the caller passes none."""
    session = _session(tmp_path)
    try:
        for i in range(3):
            session.memories.add(mem(f"m{i}", MemoryType.FACT, minutes=i))
        assert len(session.memories.missing_embeddings("hash", 8)) == 3
        assert len(session.memories.missing_embeddings("hash", 8, limit=2)) == 2
        with pytest.raises(StoreLimitError):
            session.memories.missing_embeddings("hash", 8, limit=MAX_EMBEDDING_BATCH + 1)
    finally:
        session.close()


def test_audit_recent_is_bounded(tmp_path: Path) -> None:
    """The activity read cannot be asked for an unbounded page."""
    session = _session(tmp_path)
    try:
        session.audit.write(AuditRecord(id="r1"))
        assert len(session.audit.recent(1)) == 1
        with pytest.raises(StoreLimitError):
            session.audit.recent(MAX_RECENT_AUDIT_ROWS + 1)
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------------------

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nova.api.main import app, close_runtime  # noqa: E402
from nova.security.secrets import generate_key  # noqa: E402


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A client over a fresh local SQLite deployment."""
    monkeypatch.setenv("NOVA_DB_PATH", str(tmp_path / "data.db"))
    monkeypatch.setenv("NOVA_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("NOVA_ENCRYPTION_KEY", generate_key())
    monkeypatch.delenv("NOVA_API_KEY", raising=False)
    monkeypatch.delenv("NOVA_ALLOW_FAKE_LLM", raising=False)
    close_runtime()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    close_runtime()


def _seed_api(value: str = "Supabase is banned for this project.") -> None:
    """Write one memory and one audit row through the runtime's own storage boundary.

    Data is seeded through the storage interface rather than /ingest so these tests exercise the
    lifecycle endpoints without depending on a model provider.
    """
    from nova.api.main import get_runtime
    from nova.core.tenant import DEFAULT_TENANT

    session = get_runtime().storage.open(DEFAULT_TENANT)
    try:
        session.memories.add(mem("m1", MemoryType.FACT, value=value))
        session.audit.write(AuditRecord(id="r1", question="Which database?", answer="PostgreSQL"))
    finally:
        session.close()


def test_delete_requires_explicit_confirmation(client: TestClient) -> None:
    """Nothing is deleted without the confirmation token."""
    assert client.delete("/data").status_code == 400
    assert client.delete("/data?confirm=nope").status_code == 400


def test_delete_records_returns_a_receipt(client: TestClient) -> None:
    """The default scope deletes records and reports the receipt that proves it."""
    _seed_api()
    res = client.delete("/data?confirm=delete-all-data")
    assert res.status_code == 200
    body = res.json()
    assert body["scope"] == "records"
    assert body["receipt_retained"] is True
    assert body["receipt_id"]
    assert "memories" in body and "audit_records" in body


def test_delete_account_scope_purges_receipts(client: TestClient) -> None:
    """The account scope is accepted and removes the receipts it created."""
    _seed_api()
    client.delete("/data?confirm=delete-all-data")
    res = client.delete("/data?confirm=delete-all-data&scope=account")
    assert res.status_code == 200
    body = res.json()
    assert body["scope"] == "account" and body["receipts_deleted"] == 1
    # only the tombstone for this erasure remains; the records-scope receipt is gone
    remaining = client.get("/data/deletions").json()
    assert [row["scope"] for row in remaining] == ["account"]
    assert [row["id"] for row in remaining] == [body["receipt_id"]]


def test_account_scope_can_retain_nothing(client: TestClient) -> None:
    """``retain_receipt=false`` on an account erasure leaves no receipt at all."""
    _seed_api()
    res = client.delete("/data?confirm=delete-all-data&scope=account&retain_receipt=false")
    assert res.status_code == 200
    assert res.json()["receipt_retained"] is False
    assert client.get("/data/deletions").json() == []


def test_unknown_scope_is_rejected(client: TestClient) -> None:
    """An unrecognised scope is a 422, not a silent fallback to delete-everything."""
    res = client.delete("/data?confirm=delete-all-data&scope=everything")
    assert res.status_code == 422


def test_deletion_receipts_endpoint_is_content_free(client: TestClient) -> None:
    """The receipts listing exposes counts and actor, never deleted text."""
    _seed_api()
    client.delete("/data?confirm=delete-all-data")
    res = client.get("/data/deletions")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["actor"] == "tenant_api_key"
    assert rows[0]["request_id"]
    assert "Supabase" not in json.dumps(rows)


def test_export_is_bounded_and_self_describing(client: TestClient) -> None:
    """Export carries counts, the schema version, and a truncation flag."""
    _seed_api()
    body = client.get("/data/export").json()
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["truncated"] is False
    assert body["counts"]["memories"] >= 1
    assert client.get("/data/export?limit=0").status_code == 422
    assert client.get("/data/export?limit=999999").status_code == 422


def test_receipt_model_cannot_carry_content() -> None:
    """The receipt schema is the guarantee: only metadata fields exist, now and by test."""
    from nova.store.receipts import DeletionReceipt

    assert set(DeletionReceipt.model_fields) == {
        "id",
        "scope",
        "actor",
        "request_id",
        "deleted_at",
        "memories_deleted",
        "audit_deleted",
        "receipts_deleted",
    }


def test_lifecycle_payloads_never_include_configured_secrets(client: TestClient) -> None:
    """A stored provider key is absent from export, deletion, and receipt payloads."""
    key = "sk-test-0123456789abcdefWXYZ"
    res = client.put(
        "/provider",
        json={"kind": "openai_compatible", "model": "gpt-4o-mini", "api_key": key},
    )
    assert res.status_code == 200, res.text
    assert key not in res.text  # write-only: the key is masked on the way back
    assert res.json()["api_key_mask"].endswith("WXYZ")

    _seed_api()
    export = client.get("/data/export").text
    deletion = client.delete("/data?confirm=delete-all-data").text
    receipts = client.get("/data/deletions").text
    for payload in (export, deletion, receipts):
        assert key not in payload
    assert "WXYZ" not in client.get("/data/deletions").text  # not even the mask


def test_store_limit_error_maps_to_422(tmp_path: Path) -> None:
    """A row-limit breach is a client-visible 422 with a stable code, not a 500."""
    from nova.api.errors import map_exception

    mapped = map_exception(StoreLimitError(MAX_EXPORT_ROWS))
    assert mapped.status == 422 and mapped.error == "query_limit_exceeded"


def test_newer_schema_maps_to_503() -> None:
    """A database from a newer build fails closed with 503 rather than being used."""
    from nova.api.errors import map_exception

    mapped = map_exception(SchemaVersionError("too new"))
    assert mapped.status == 503 and mapped.error == "schema_version_unsupported"


# --------------------------------------------------------------------------------------
# PostgreSQL contract suite (opt-in: needs a real server)
# --------------------------------------------------------------------------------------

PG_DSN = os.environ.get("NOVA_TEST_POSTGRES_DSN")


@pytest.mark.skipif(
    not PG_DSN,
    reason="NOVA_TEST_POSTGRES_DSN is not set: no real PostgreSQL server in this environment",
)
def test_postgres_contract_matches_sqlite(tmp_path: Path) -> None:
    """The same lifecycle must hold on a real server (opt-in, see docs/storage.md).

    Deliberately not faked: STORE-1 stays open until a human runs this against a real TLS server.
    """
    import psycopg  # noqa: F401  (import fails loudly if the driver is absent)

    from nova.store.postgres import PostgresDatabase

    conn = psycopg.connect(PG_DSN, autocommit=True)
    db = PostgresDatabase(conn)
    session = StorageSession(db, "contract", "postgres")
    try:
        assert session.initialize() == POSTGRES_SCHEMA_VERSION
        session.memories.add(mem("p1", MemoryType.FACT, value="PostgreSQL"))
        receipt = session.purge(scope="records", actor="contract")
        assert receipt is not None and receipt.memories_deleted == 1
        assert session.memories.count() == 0
        assert session.receipts.count() == 1
    finally:
        session.close()
