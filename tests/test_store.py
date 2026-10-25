"""Unit tests for SQLite memory store implementation."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nova.core.protocols import MemoryStore
from nova.core.schemas import Memory, MemoryStatus, MemoryType
from nova.store.sqlite_store import SqliteMemoryStore, StoreError


def _make_memory(
    memory_id: str,
    subject: str = "database",
    value: str = "PostgreSQL",
    session_id: str = "sess-1",
    status: MemoryStatus = MemoryStatus.CURRENT,
    supersedes_id: str | None = None,
    minutes_ago: int = 0,
) -> Memory:
    """Helper creating a test Memory instance."""
    ts = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return Memory(
        id=memory_id,
        type=MemoryType.FACT,
        subject=subject,
        value=value,
        reason="Performance and reliability",
        source_msg_id=f"msg-{memory_id}",
        source_quote="we decided to use PostgreSQL",
        session_id=session_id,
        timestamp=ts,
        status=status,
        supersedes_id=supersedes_id,
        confidence=1.0,
    )


def test_sqlite_store_satisfies_protocol() -> None:
    """Verify SqliteMemoryStore conforms to MemoryStore protocol."""
    store = SqliteMemoryStore(":memory:")
    assert isinstance(store, MemoryStore)
    store.close()


def test_roundtrip(tmp_path: Path) -> None:
    """Verify storing and retrieving a memory preserves all attributes and embedding."""
    db_file = tmp_path / "test.db"
    store = SqliteMemoryStore(db_file)
    mem = _make_memory("mem-rt-1")
    store.add(mem, embedding=b"\x00\x01\x02\x03")

    loaded = store.get("mem-rt-1")
    assert loaded is not None
    assert loaded.id == mem.id
    assert loaded.type == mem.type
    assert loaded.subject == mem.subject
    assert loaded.value == mem.value
    assert loaded.reason == mem.reason
    assert loaded.source_msg_id == mem.source_msg_id
    assert loaded.source_quote == mem.source_quote
    assert loaded.session_id == mem.session_id
    assert loaded.timestamp == mem.timestamp
    assert loaded.status == mem.status
    assert loaded.supersedes_id == mem.supersedes_id
    assert loaded.confidence == mem.confidence

    cur = store.conn.execute("SELECT embedding FROM memories WHERE id = ?;", (mem.id,))
    row = cur.fetchone()
    assert row["embedding"] == b"\x00\x01\x02\x03"
    store.close()


def test_idempotent_add(tmp_path: Path) -> None:
    """Verify adding the same memory multiple times does not duplicate or raise."""
    db_file = tmp_path / "idempotent.db"
    store = SqliteMemoryStore(db_file)
    mem = _make_memory("mem-idemp-1")

    store.add(mem)
    store.add(mem)

    loaded = store.get("mem-idemp-1")
    assert loaded is not None

    count_row = store.conn.execute("SELECT COUNT(*) AS c FROM memories;").fetchone()
    assert count_row["c"] == 1

    store.add_many([mem, mem])
    count_row2 = store.conn.execute("SELECT COUNT(*) AS c FROM memories;").fetchone()
    assert count_row2["c"] == 1
    store.close()


def test_atomic_supersede_and_rollback(tmp_path: Path) -> None:
    """Verify atomic supersede updates both records and rolls back on failure."""
    db_file = tmp_path / "supersede.db"
    store = SqliteMemoryStore(db_file)
    old_mem = _make_memory("old-1", value="MySQL")
    new_mem = _make_memory("new-1", value="PostgreSQL")
    store.add_many([old_mem, new_mem])

    store.supersede("old-1", "new-1")

    old_loaded = store.get("old-1")
    new_loaded = store.get("new-1")
    assert old_loaded is not None and old_loaded.status == MemoryStatus.SUPERSEDED
    assert new_loaded is not None and new_loaded.supersedes_id == "old-1"

    old_mem2 = _make_memory("old-2", value="Redis")
    store.add(old_mem2)

    with pytest.raises(StoreError):
        store.supersede("old-2", "non-existent-replacement")

    old2_loaded = store.get("old-2")
    assert old2_loaded is not None and old2_loaded.status == MemoryStatus.CURRENT

    with pytest.raises(StoreError):
        store.supersede("non-existent-old", "new-1")

    store.close()


def test_concurrent_safe_reads(tmp_path: Path) -> None:
    """Verify concurrent reads on WAL mode database operate safely without locking."""
    db_file = tmp_path / "concurrent.db"
    store = SqliteMemoryStore(db_file)
    mems = [_make_memory(f"mem-c-{i}", value=f"val-{i}") for i in range(10)]
    store.add_many(mems)

    def reader_task(reader_id: int) -> int:
        local_store = SqliteMemoryStore(db_file)
        count = 0
        for i in range(10):
            res = local_store.get(f"mem-c-{i}")
            if res is not None:
                count += 1
        local_store.close()
        return count

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(reader_task, i) for i in range(10)]
        results = [f.result() for f in futures]

    assert all(r == 10 for r in results)
    store.close()


def test_current_by_subject_and_list_by_session(tmp_path: Path) -> None:
    """Verify query by subject status and session ordering."""
    db_file = tmp_path / "query.db"
    store = SqliteMemoryStore(db_file)
    m1 = _make_memory("m1", subject="auth", value="JWT", minutes_ago=10)
    m2 = _make_memory("m2", subject="auth", value="OAuth2", minutes_ago=5)
    m3 = _make_memory("m3", subject="cache", value="Redis", session_id="sess-2")
    store.add_many([m1, m2, m3])

    store.supersede("m1", "m2")

    current_auth = store.current_by_subject("auth")
    assert current_auth is not None
    assert current_auth.id == "m2"
    assert current_auth.value == "OAuth2"

    sess1_mems = store.list_by_session("sess-1")
    assert [m.id for m in sess1_mems] == ["m1", "m2"]

    store.close()


def test_idempotent_migration(tmp_path: Path) -> None:
    """Verify migrate can be invoked repeatedly without error."""
    db_file = tmp_path / "migration.db"
    store = SqliteMemoryStore(db_file)
    store.migrate()
    store.migrate()

    cur = store.conn.execute("SELECT COUNT(*) AS c, MAX(version) AS v FROM schema_version;")
    row = cur.fetchone()
    assert row["v"] == SqliteMemoryStore.SCHEMA_VERSION
    assert row["c"] == SqliteMemoryStore.SCHEMA_VERSION
    store.close()


def test_context_manager(tmp_path: Path) -> None:
    """Verify context manager closes database connection."""
    db_file = tmp_path / "ctx.db"
    with SqliteMemoryStore(db_file) as store:
        store.add(_make_memory("ctx-1"))
        assert store.get("ctx-1") is not None

    with pytest.raises(sqlite3.ProgrammingError):
        store.conn.execute("SELECT 1;")
