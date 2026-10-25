"""Unit tests for temporal memory supersession rules."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nova.core.schemas import Memory, MemoryStatus, MemoryType
from nova.store.sqlite_store import SqliteMemoryStore
from nova.temporal.supersession import apply


def _make_mem(
    mem_id: str,
    mem_type: MemoryType,
    subject: str,
    value: str,
    timestamp: datetime,
) -> Memory:
    """Helper to create a memory instance."""
    return Memory(
        id=mem_id,
        type=mem_type,
        subject=subject,
        value=value,
        source_msg_id=f"msg-{mem_id}",
        source_quote=value,
        session_id="sess-1",
        timestamp=timestamp,
    )


def test_reversal(tmp_path: Path) -> None:
    """Verify later decision supersedes earlier decision with the same subject."""
    store = SqliteMemoryStore(tmp_path / "rev.db")
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)

    m1 = _make_mem("m1", MemoryType.DECISION, "db", "MySQL", t1)
    m2 = _make_mem("m2", MemoryType.DECISION, "db", "PostgreSQL", t2)

    assert apply(store, [m1]) == 0
    assert apply(store, [m2]) == 1

    m1_loaded = store.get("m1")
    m2_loaded = store.get("m2")
    assert m1_loaded is not None and m1_loaded.status == MemoryStatus.SUPERSEDED
    assert m2_loaded is not None and m2_loaded.status == MemoryStatus.CURRENT
    assert m2_loaded.supersedes_id == "m1"
    assert store.current_by_subject("db").id == "m2"
    store.close()


def test_double_reversal_chain(tmp_path: Path) -> None:
    """Verify sequential reversals build a connected supersession chain."""
    store = SqliteMemoryStore(tmp_path / "chain.db")
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)
    t3 = t2 + timedelta(hours=1)

    m1 = _make_mem("m1", MemoryType.DECISION, "db", "MySQL", t1)
    m2 = _make_mem("m2", MemoryType.CORRECTION, "db", "PostgreSQL", t2)
    m3 = _make_mem("m3", MemoryType.DECISION, "db", "SQLite", t3)

    count = apply(store, [m1, m2, m3])
    assert count == 2

    m1_l = store.get("m1")
    m2_l = store.get("m2")
    m3_l = store.get("m3")

    assert m1_l is not None and m1_l.status == MemoryStatus.SUPERSEDED
    assert m2_l is not None and m2_l.status == MemoryStatus.SUPERSEDED
    assert m2_l.supersedes_id == "m1"
    assert m3_l is not None and m3_l.status == MemoryStatus.CURRENT
    assert m3_l.supersedes_id == "m2"
    assert store.current_by_subject("db").id == "m3"
    store.close()


def test_out_of_order_ingest(tmp_path: Path) -> None:
    """Verify older memory arriving after a newer current memory is stored as superseded."""
    store = SqliteMemoryStore(tmp_path / "ooo.db")
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=2)

    m_newer = _make_mem("m2", MemoryType.DECISION, "db", "PostgreSQL", t2)
    m_older = _make_mem("m1", MemoryType.DECISION, "db", "MySQL", t1)

    assert apply(store, [m_newer]) == 0
    assert apply(store, [m_older]) == 1

    m1_l = store.get("m1")
    m2_l = store.get("m2")

    assert m1_l is not None and m1_l.status == MemoryStatus.SUPERSEDED
    assert m2_l is not None and m2_l.status == MemoryStatus.CURRENT
    assert m2_l.supersedes_id == "m1"
    assert store.current_by_subject("db").id == "m2"
    store.close()


def test_facts_and_commitments_never_auto_supersede(tmp_path: Path) -> None:
    """Verify facts and commitments are not superseded by subsequent entries."""
    store = SqliteMemoryStore(tmp_path / "facts.db")
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)

    f1 = _make_mem("f1", MemoryType.FACT, "db", "PostgreSQL", t1)
    f2 = _make_mem("f2", MemoryType.FACT, "db", "PostgreSQL v16", t2)
    c1 = _make_mem("c1", MemoryType.COMMITMENT, "sla", "99.9%", t1)
    c2 = _make_mem("c2", MemoryType.COMMITMENT, "sla", "99.99%", t2)

    count = apply(store, [f1, f2, c1, c2])
    assert count == 0

    assert store.get("f1").status == MemoryStatus.CURRENT
    assert store.get("f2").status == MemoryStatus.CURRENT
    assert store.get("c1").status == MemoryStatus.CURRENT
    assert store.get("c2").status == MemoryStatus.CURRENT
    store.close()


def test_idempotency_on_rerun(tmp_path: Path) -> None:
    """Verify re-running supersession with identical memories produces no side effects."""
    store = SqliteMemoryStore(tmp_path / "idemp.db")
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)

    m1 = _make_mem("m1", MemoryType.DECISION, "db", "MySQL", t1)
    m2 = _make_mem("m2", MemoryType.DECISION, "db", "PostgreSQL", t2)

    count1 = apply(store, [m1, m2])
    assert count1 == 1

    count2 = apply(store, [m1, m2])
    assert count2 == 0

    assert store.get("m1").status == MemoryStatus.SUPERSEDED
    assert store.get("m2").status == MemoryStatus.CURRENT
    assert store.get("m2").supersedes_id == "m1"
    store.close()


def test_unrelated_subjects_untouched(tmp_path: Path) -> None:
    """Verify supersession on one subject does not alter memories of another subject."""
    store = SqliteMemoryStore(tmp_path / "unrelated.db")
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)
    t3 = t2 + timedelta(hours=1)

    m_db1 = _make_mem("m1", MemoryType.DECISION, "db", "MySQL", t1)
    m_auth = _make_mem("m2", MemoryType.PREFERENCE, "auth", "JWT", t2)
    m_db2 = _make_mem("m3", MemoryType.DECISION, "db", "PostgreSQL", t3)

    apply(store, [m_db1, m_auth])
    assert store.current_by_subject("db").value == "MySQL"
    assert store.current_by_subject("auth").value == "JWT"

    assert apply(store, [m_db2]) == 1
    assert store.current_by_subject("db").value == "PostgreSQL"
    assert store.current_by_subject("auth").value == "JWT"
    assert store.get("m2").status == MemoryStatus.CURRENT
    assert store.get("m2").supersedes_id is None
    store.close()
