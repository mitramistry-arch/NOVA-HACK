"""Unit tests for hybrid retrieval, embeddings, and ranking."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nova.core.protocols import Retriever
from nova.core.schemas import Memory, MemoryStatus, MemoryType
from nova.retrieval.embedder import (
    Embedder,
    HashEmbedder,
    SentenceTransformerEmbedder,
    deserialize_embedding,
    serialize_embedding,
)
from nova.retrieval.hybrid import HybridRetriever
from nova.store.sqlite_store import SqliteMemoryStore


def _make_mem(
    mem_id: str,
    subject: str,
    value: str,
    session_id: str = "sess-1",
    status: MemoryStatus = MemoryStatus.CURRENT,
    timestamp: datetime | None = None,
) -> Memory:
    """Helper to create a test memory."""
    ts = timestamp or datetime.now(UTC)
    return Memory(
        id=mem_id,
        type=MemoryType.FACT,
        subject=subject,
        value=value,
        source_msg_id=f"msg-{mem_id}",
        source_quote=value,
        session_id=session_id,
        timestamp=ts,
        status=status,
    )


def test_embedder_protocol_and_hashing() -> None:
    """Verify HashEmbedder and SentenceTransformerEmbedder adhere to the protocol."""
    embedder = HashEmbedder(dimension=256)
    assert isinstance(embedder, Embedder)

    vec = embedder.embed("test sentence")
    assert len(vec) == 256
    assert isinstance(vec[0], float)

    st_embedder = SentenceTransformerEmbedder()
    assert isinstance(st_embedder, Embedder)

    blob = serialize_embedding(vec)
    restored = deserialize_embedding(blob)
    assert len(restored) == 256
    assert abs(float(restored[0]) - vec[0]) < 1e-6


def test_empty_store_returns_empty_list(tmp_path: Path) -> None:
    """Verify retrieve on an empty memory store returns an empty list."""
    store = SqliteMemoryStore(tmp_path / "empty.db")
    embedder = HashEmbedder()
    retriever = HybridRetriever(store, embedder)
    assert isinstance(retriever, Retriever)

    assert retriever.retrieve("any query") == []
    store.close()


def test_paraphrased_query_finds_fact(tmp_path: Path) -> None:
    """Verify hybrid scoring retrieves the relevant fact for a paraphrased query."""
    store = SqliteMemoryStore(tmp_path / "paraphrase.db")
    embedder = HashEmbedder()
    retriever = HybridRetriever(store, embedder)

    m1 = _make_mem("m1", "database", "PostgreSQL is our chosen relational database")
    m2 = _make_mem("m2", "auth", "JWT bearer tokens authenticate api users")
    m3 = _make_mem("m3", "cache", "Redis handles distributed key value caching")

    for m in [m1, m2, m3]:
        blob = serialize_embedding(embedder.embed(f"{m.subject} {m.value}"))
        store.add(m, embedding=blob, embedding_model=embedder.model_id)

    results = retriever.retrieve("Which relational database system is deployed?", k=1)
    assert len(results) == 1
    assert results[0].id == "m1"
    assert results[0].subject == "database"
    store.close()


def test_superseded_excluded_and_included(tmp_path: Path) -> None:
    """Verify history flag controls inclusion of superseded memories."""
    store = SqliteMemoryStore(tmp_path / "history.db")
    embedder = HashEmbedder()
    retriever = HybridRetriever(store, embedder)

    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)
    m_old = _make_mem(
        "m1", "database", "MySQL was used", status=MemoryStatus.SUPERSEDED, timestamp=t1
    )
    m_cur = _make_mem(
        "m2", "database", "PostgreSQL is now used", status=MemoryStatus.CURRENT, timestamp=t2
    )

    for m in [m_old, m_cur]:
        blob = serialize_embedding(embedder.embed(f"{m.subject} {m.value}"))
        store.add(m, embedding=blob, embedding_model=embedder.model_id)

    cur_only = retriever.retrieve("database", include_history=False)
    assert len(cur_only) == 1
    assert cur_only[0].id == "m2"
    assert cur_only[0].status == MemoryStatus.CURRENT

    with_history = retriever.retrieve("database", include_history=True)
    assert len(with_history) == 2
    ids = [m.id for m in with_history]
    assert "m2" in ids and "m1" in ids
    statuses = {m.id: m.status for m in with_history}
    assert statuses["m2"] == MemoryStatus.CURRENT
    assert statuses["m1"] == MemoryStatus.SUPERSEDED
    store.close()


def test_cross_session_and_scoped_retrieval(tmp_path: Path) -> None:
    """Verify session_id filtering vs default cross-session recovery."""
    store = SqliteMemoryStore(tmp_path / "cross_session.db")
    embedder = HashEmbedder()
    retriever = HybridRetriever(store, embedder)

    m1 = _make_mem("m1", "database", "PostgreSQL in session 1", session_id="session-1")
    m2 = _make_mem("m2", "cache", "Redis in session 2", session_id="session-2")

    for m in [m1, m2]:
        blob = serialize_embedding(embedder.embed(f"{m.subject} {m.value}"))
        store.add(m, embedding=blob, embedding_model=embedder.model_id)

    all_sessions = retriever.retrieve("PostgreSQL Redis", session_id=None)
    assert len(all_sessions) == 2

    session1_only = retriever.retrieve("PostgreSQL Redis", session_id="session-1")
    assert len(session1_only) == 1
    assert session1_only[0].session_id == "session-1"
    store.close()


def test_deterministic_ordering_on_ties(tmp_path: Path) -> None:
    """Verify deterministic tie-breaking based on timestamp and id."""
    store = SqliteMemoryStore(tmp_path / "ties.db")
    embedder = HashEmbedder()
    retriever = HybridRetriever(store, embedder)

    t = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    m1 = _make_mem("m-b", "config", "same value", timestamp=t)
    m2 = _make_mem("m-a", "config", "same value", timestamp=t)

    for m in [m1, m2]:
        blob = serialize_embedding(embedder.embed(f"{m.subject} {m.value}"))
        store.add(m, embedding=blob, embedding_model=embedder.model_id)

    results = retriever.retrieve("config same value", k=5)
    assert len(results) == 2
    assert [m.id for m in results] == ["m-a", "m-b"]
    store.close()
