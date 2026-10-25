"""Phase 2 regression tests: persistence, transactions, validation, supersession, retrieval."""

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from nova.core.llm import FakeLLM
from nova.core.policy import SUPERSESSION_POLICY, is_chained
from nova.core.schemas import (
    Memory,
    MemoryStatus,
    MemoryType,
    Message,
)
from nova.extraction.extractor import ExtractionError, LLMExtractor
from nova.pipeline import Anchor
from nova.retrieval.embedder import (
    EmbeddingError,
    HashEmbedder,
    SentenceTransformerEmbedder,
    embed_records,
)
from nova.retrieval.hybrid import MAX_K, HybridRetriever, RetrievalError
from nova.store.base import EmbeddingCompatibilityError, StoreError, SupersessionError
from nova.store.sqlite_store import SqliteMemoryStore
from nova.temporal.supersession import apply
from tests.helpers import T0, mem, msg

# ---- 11: embedding persistence --------------------------------------------------------------


def _anchor(tmp_path: Path, llm_reply: str, embedder=None) -> tuple[Anchor, SqliteMemoryStore]:
    store = SqliteMemoryStore(tmp_path / "a.db")
    retriever = HybridRetriever(store, embedder or HashEmbedder())
    return Anchor(
        store=store, extractor=LLMExtractor(FakeLLM(llm_reply)), retriever=retriever
    ), store


_REPLY = json.dumps(
    [
        {
            "type": "decision",
            "subject": "db",
            "value": "PostgreSQL",
            "source_msg_id": "m1",
            "source_quote": "use PostgreSQL",
        }
    ]
)


def test_embeddings_survive_close_and_reopen(tmp_path: Path) -> None:
    anchor, store = _anchor(tmp_path, _REPLY)
    anchor.ingest("s1", [msg("m1", "We will use PostgreSQL here.")])
    store.close()

    reopened = SqliteMemoryStore(tmp_path / "a.db")
    rows = reopened.candidates(include_history=True)
    assert len(rows) == 1
    record = rows[0][1]
    assert record is not None and record.model == HashEmbedder().model_id and record.dim == 256
    found = HybridRetriever(reopened, HashEmbedder()).retrieve("which database", k=1)
    assert [m.subject for m in found] == ["db"]
    reopened.close()


class _FlakyEmbedder(HashEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding backend down")
        return super().embed_batch(texts)


def test_failed_embedding_writes_nothing_and_retry_populates(tmp_path: Path) -> None:
    flaky = _FlakyEmbedder()
    anchor, store = _anchor(tmp_path, _REPLY, flaky)
    messages = [msg("m1", "We will use PostgreSQL here.")]
    with pytest.raises(EmbeddingError):
        anchor.ingest("s1", messages)
    assert store.count() == 0

    flaky.fail = False
    anchor.extractor = LLMExtractor(FakeLLM(_REPLY))
    report = anchor.ingest("s1", messages)
    assert report.added == 1
    ((_, record),) = store.candidates(include_history=True)
    assert record is not None


def test_retry_backfills_memory_that_exists_without_embedding(tmp_path: Path) -> None:
    anchor, store = _anchor(tmp_path, _REPLY)
    anchor.ingest("s1", [msg("m1", "We will use PostgreSQL here.")])
    store.db.execute(
        "UPDATE memories SET embedding = NULL, embedding_model = NULL, embedding_dim = NULL"
    )
    assert store.candidates(include_history=True)[0][1] is None

    fresh = Anchor(
        store=store,
        extractor=LLMExtractor(FakeLLM(_REPLY)),
        retriever=HybridRetriever(store, HashEmbedder()),
    )
    report = fresh.ingest("s1", [msg("m1", "We will use PostgreSQL here.")])
    assert report.added == 0
    assert store.candidates(include_history=True)[0][1] is not None


def test_add_many_rejects_mismatched_embedding_list(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "x.db")
    with pytest.raises(StoreError):
        store.add_many([mem("a"), mem("b", subject="x")], [b"\x00\x00\x00\x00"])
    assert store.count() == 0
    with pytest.raises(StoreError):
        store.add(mem("c"), b"123")  # not a float32 multiple


# ---- 12: atomic supersession ----------------------------------------------------------------


def test_failure_midway_rolls_back_whole_supersession(tmp_path: Path, monkeypatch) -> None:
    store = SqliteMemoryStore(tmp_path / "tx.db")
    assert apply(store, [mem("m1", minutes=0)]) == 0

    real = store.link_chain
    calls = {"n": 0}

    def flaky(ids: list[str]) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise StoreError("injected failure")
        return real(ids)

    monkeypatch.setattr(store, "link_chain", flaky)
    with pytest.raises(StoreError):
        apply(store, [mem("m2", minutes=5), mem("m3", minutes=10)])

    assert store.get("m2") is None and store.get("m3") is None
    m1 = store.get("m1")
    assert m1 is not None and m1.status == MemoryStatus.CURRENT
    assert store.count() == 1


def test_failure_inside_link_chain_rolls_back_updates(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "tx2.db")
    store.add_many([mem("a", minutes=0), mem("b", minutes=1), mem("c", minutes=2)])
    original = store.db.execute
    state = {"updates": 0}

    def failing(sql, params=()):  # type: ignore[no-untyped-def]
        if sql.startswith("UPDATE memories SET supersedes_id = ?"):
            state["updates"] += 1
            if state["updates"] == 2:
                raise StoreError("boom")
        return original(sql, params)

    store.db.execute = failing  # type: ignore[method-assign]
    with pytest.raises(StoreError):
        store.link_chain(["a", "b", "c"])
    store.db.execute = original  # type: ignore[method-assign]
    assert all(store.get(i).status == MemoryStatus.CURRENT for i in "abc")  # type: ignore[union-attr]
    assert all(store.get(i).supersedes_id is None for i in "abc")  # type: ignore[union-attr]


def test_nested_failure_poisons_outer_transaction(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "nest.db")
    with store.transaction():
        store.add(mem("a"))
        with pytest.raises(StoreError), store.transaction():
            raise StoreError("inner")
    assert store.get("a") is None


# ---- 15/16: timestamps and validation -------------------------------------------------------


def test_mixed_offsets_normalize_to_utc_and_order_correctly(tmp_path: Path) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    a = mem("a").model_copy(update={"timestamp": datetime(2026, 1, 1, 15, 30, tzinfo=ist)})
    b = Memory(**{**mem("b").model_dump(), "timestamp": datetime(2026, 1, 1, 10, 5, tzinfo=UTC)})
    assert a.timestamp.utcoffset() == timedelta(0) or a.timestamp.tzinfo is not None
    parsed = Memory(**{**a.model_dump(), "timestamp": "2026-01-01T15:30:00+05:30"})
    assert parsed.timestamp == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert parsed.timestamp.utcoffset() == timedelta(0)
    store = SqliteMemoryStore(tmp_path / "tz.db")
    apply(store, [parsed, b])
    assert store.get("b").supersedes_id == "a"  # type: ignore[union-attr]
    assert store.get("a").timestamp == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)  # type: ignore[union-attr]


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError):
        Memory(**{**mem("a").model_dump(), "timestamp": datetime(2026, 1, 1)})  # noqa: DTZ001
    with pytest.raises(ValidationError):
        Message(id="m", session_id="s", role="user", text="x", timestamp="2026-01-01T00:00:00")


def test_timestamp_tie_is_broken_deterministically_by_id(tmp_path: Path) -> None:
    for order in (["x1", "x2"], ["x2", "x1"]):
        store = SqliteMemoryStore(tmp_path / f"tie-{order[0]}.db")
        for i in order:
            apply(store, [mem(i, minutes=0, value=i)])
        assert store.current_by_subject("db").id == "x2"  # type: ignore[union-attr]
        assert store.get("x2").supersedes_id == "x1"  # type: ignore[union-attr]
        assert store.get("x1").status == MemoryStatus.SUPERSEDED  # type: ignore[union-attr]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -0.1, 1.1])
def test_confidence_must_be_finite_unit_interval(bad: float) -> None:
    with pytest.raises(ValidationError):
        Memory(**{**mem("a").model_dump(), "confidence": bad})


@pytest.mark.parametrize(
    "field", ["id", "subject", "value", "source_msg_id", "source_quote", "session_id"]
)
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_fields_rejected(field: str, blank: str) -> None:
    with pytest.raises(ValidationError):
        Memory(**{**mem("a").model_dump(), field: blank})


def test_invalid_role_and_oversized_text_rejected() -> None:
    ok = {"id": "m", "session_id": "s", "text": "hi", "timestamp": T0}
    with pytest.raises(ValidationError):
        Message(role="wizard", **ok)
    with pytest.raises(ValidationError):
        Message(role="user", **{**ok, "text": "x" * 20_001})
    with pytest.raises(ValidationError):
        Message(role="user", **{**ok, "text": "   "})
    with pytest.raises(ValidationError):
        Message(role="user", **{**ok, "id": " "})


def test_self_supersession_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        Memory(**{**mem("a").model_dump(), "supersedes_id": "a"})


# ---- 18/19/20: retrieval --------------------------------------------------------------------


@pytest.mark.parametrize("k", [0, -1, -5])
def test_retrieval_rejects_non_positive_k(tmp_path: Path, k: int) -> None:
    retriever = HybridRetriever(SqliteMemoryStore(tmp_path / "k.db"), HashEmbedder())
    with pytest.raises(RetrievalError):
        retriever.retrieve("q", k=k)
    with pytest.raises(RetrievalError):
        retriever.retrieve("q", k=True)  # type: ignore[arg-type]


def test_retrieval_caps_excessive_k(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "cap.db")
    store.add_many(
        [mem(f"m{i}", MemoryType.FACT, subject=f"s{i}", value=f"v{i}") for i in range(MAX_K + 5)]
    )
    out = HybridRetriever(store, HashEmbedder()).retrieve("v", k=10_000)
    assert len(out) == MAX_K


@pytest.mark.parametrize("dim", [0, -3, 10**6])
def test_hash_embedder_rejects_bad_dimension(dim: int) -> None:
    with pytest.raises(ValueError):
        HashEmbedder(dimension=dim)


def test_sentence_transformer_rejects_blank_model_name() -> None:
    with pytest.raises(ValueError):
        SentenceTransformerEmbedder("  ")


def test_incompatible_dimension_or_model_fails_clearly(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "inc.db")
    small = HashEmbedder(dimension=64)
    m = mem("a", MemoryType.FACT)
    store.add(m, embed_records(small, ["db PostgreSQL"])[0])

    with pytest.raises(EmbeddingCompatibilityError):
        HybridRetriever(store, HashEmbedder(dimension=256)).retrieve("db")

    class OtherModel(HashEmbedder):
        model_id = "other-model"

    same_dim = HashEmbedder(dimension=64)
    OtherModel(dimension=64).model_id = "other"
    other = OtherModel(dimension=64)
    other.model_id = "other-model"
    with pytest.raises(EmbeddingCompatibilityError):
        HybridRetriever(store, other).retrieve("db")

    assert HybridRetriever(store, same_dim).retrieve("db")  # compatible works


def test_unlabeled_vectors_rejected_unless_opted_in(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "unl.db")
    emb = HashEmbedder()
    from nova.retrieval.embedder import serialize_embedding

    store.add(mem("a", MemoryType.FACT), serialize_embedding(emb.embed("db PostgreSQL")))
    with pytest.raises(EmbeddingCompatibilityError):
        HybridRetriever(store, emb).retrieve("db")
    assert HybridRetriever(store, emb, accept_unlabeled_embeddings=True).retrieve("db")


def test_reembed_repairs_incompatible_vectors(tmp_path: Path) -> None:
    _, store = _anchor(tmp_path, "[]", HashEmbedder(dimension=64))
    store.add(
        mem("a", MemoryType.FACT), embed_records(HashEmbedder(dimension=64), ["db PostgreSQL"])[0]
    )
    anchor2 = Anchor(
        store=store,
        extractor=LLMExtractor(FakeLLM("[]")),
        retriever=HybridRetriever(store, HashEmbedder(dimension=256)),
    )
    assert anchor2.reembed() == 1
    assert anchor2.retriever.retrieve("db")


def test_retriever_requires_public_candidate_api() -> None:
    class Bare:
        pass

    with pytest.raises(TypeError):
        HybridRetriever(Bare(), HashEmbedder())  # type: ignore[arg-type]


def test_embed_records_validates_embedder_output() -> None:
    class Bad(HashEmbedder):
        def embed_batch(self, texts):  # type: ignore[no-untyped-def]
            return [[float("nan")] * 4 for _ in texts]

    with pytest.raises(EmbeddingError):
        embed_records(Bad(), ["x"])


# ---- 21/25/26: supersession integrity, subject lookup, type policy -------------------------


def test_supersede_rejects_self_cycle_subject_and_direction(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "int.db")
    store.add_many([mem("a", minutes=0), mem("b", minutes=1), mem("o", subject="other", minutes=2)])
    with pytest.raises(SupersessionError):
        store.supersede("a", "a")
    with pytest.raises(SupersessionError):
        store.supersede("a", "o")  # different subject
    with pytest.raises(SupersessionError):
        store.supersede("b", "a")  # newer cannot be superseded by older
    store.supersede("a", "b")
    with pytest.raises(SupersessionError):
        store.supersede("b", "a")  # would cycle / wrong direction
    store.add(mem("c", minutes=2))
    with pytest.raises(SupersessionError):
        store.supersede("a", "c")  # a already has successor b (fork)
    assert store.get("a").status == MemoryStatus.SUPERSEDED  # type: ignore[union-attr]


def test_database_constraints_backstop_python_checks(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "db.db")
    store.add_many([mem("a", minutes=0), mem("b", minutes=1)])
    with pytest.raises(StoreError):
        store.db.execute("UPDATE memories SET supersedes_id = id WHERE id = 'a'")
    with pytest.raises(StoreError):  # direction: older cannot point at newer
        store.db.execute("UPDATE memories SET supersedes_id = 'b' WHERE id = 'a'")
    with pytest.raises(StoreError):
        store.db.execute("UPDATE memories SET confidence = 7 WHERE id = 'a'")
    with pytest.raises(StoreError):
        store.db.execute("UPDATE memories SET type = 'bogus' WHERE id = 'a'")
    with pytest.raises(StoreError):  # dangling reference caught at commit (deferred FK)
        store.add(mem("z", minutes=3).model_copy(update={"supersedes_id": "missing"}))
        store.db.execute("SELECT 1")
        with store.transaction():
            store.db.execute("UPDATE memories SET supersedes_id = 'nope' WHERE id = 'b'")


def test_current_lookup_is_deterministic_with_multiple_current(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "multi.db")
    store.add_many(
        [
            mem("f1", MemoryType.FACT, minutes=1),
            mem("f2", MemoryType.FACT, minutes=1),
            mem("f0", MemoryType.FACT, minutes=0),
        ]
    )
    assert [m.id for m in store.current_all_by_subject("db")] == ["f2", "f1", "f0"]
    assert store.current_by_subject("db").id == "f2"  # type: ignore[union-attr]


def test_multiple_current_in_chain_are_healed(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "heal.db")
    store.add_many([mem("a", minutes=0), mem("b", minutes=1)])  # both 'current' (dirty)
    apply(store, [mem("c", minutes=2)])
    assert [m.id for m in store.current_all_by_subject("db")] == ["c"]
    assert store.get("b").supersedes_id == "a"  # type: ignore[union-attr]
    assert store.get("c").supersedes_id == "b"  # type: ignore[union-attr]


def test_subjects_are_isolated_per_tenant(tmp_path: Path) -> None:
    a = SqliteMemoryStore(tmp_path / "t.db", tenant_id="tenant-a")
    b = SqliteMemoryStore(tmp_path / "t.db", tenant_id="tenant-b")
    apply(a, [mem("m1", minutes=0, value="MySQL")])
    apply(b, [mem("m1", minutes=5, value="Postgres")])  # same id, different tenant
    assert a.current_by_subject("db").value == "MySQL"  # type: ignore[union-attr]
    assert b.current_by_subject("db").value == "Postgres"  # type: ignore[union-attr]
    assert a.get("m1").supersedes_id is None  # type: ignore[union-attr]
    assert a.candidates(include_history=True)[0][0].value == "MySQL"
    assert a.delete_all() == 1
    assert b.count() == 1


@pytest.mark.parametrize("mtype", list(MemoryType))
def test_type_policy_each_memory_type(tmp_path: Path, mtype: MemoryType) -> None:
    store = SqliteMemoryStore(tmp_path / f"pol-{mtype}.db")
    count = apply(
        store, [mem("old", mtype, minutes=0, value="v1"), mem("new", mtype, minutes=5, value="v2")]
    )
    assert SUPERSESSION_POLICY[mtype].rationale
    if is_chained(mtype):
        assert count == 1
        assert store.get("old").status == MemoryStatus.SUPERSEDED  # type: ignore[union-attr]
        assert store.get("new").supersedes_id == "old"  # type: ignore[union-attr]
    else:
        assert count == 0
        assert store.get("old").status == MemoryStatus.CURRENT  # type: ignore[union-attr]
        assert store.get("new").supersedes_id is None  # type: ignore[union-attr]


def test_policy_table_is_explicit() -> None:
    assert {t for t, p in SUPERSESSION_POLICY.items() if not p.chained} == {
        MemoryType.FACT,
        MemoryType.COMMITMENT,
    }
    assert set(SUPERSESSION_POLICY) == set(MemoryType)


def test_middle_insertion_relinks_chain(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "mid.db")
    apply(store, [mem("a", minutes=0), mem("c", minutes=10)])
    apply(store, [mem("b", minutes=5)])
    assert store.get("b").supersedes_id == "a"  # type: ignore[union-attr]
    assert store.get("c").supersedes_id == "b"  # type: ignore[union-attr]
    assert store.current_by_subject("db").id == "c"  # type: ignore[union-attr]


# ---- migration from the v1 schema -------------------------------------------------------------


def test_migrates_legacy_v1_database(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (1, 'x');
        CREATE TABLE memories (id TEXT PRIMARY KEY, type TEXT NOT NULL, subject TEXT NOT NULL,
          value TEXT NOT NULL, reason TEXT, source_msg_id TEXT NOT NULL, source_quote TEXT NOT NULL,
          session_id TEXT NOT NULL, timestamp TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'current',
          supersedes_id TEXT, confidence REAL NOT NULL DEFAULT 1.0, embedding BLOB);
        INSERT INTO memories VALUES ('a','decision','db','MySQL',NULL,'m','q','s',
          '2026-01-01T10:00:00',
          'superseded',NULL,1.0,NULL);
        INSERT INTO memories VALUES ('b','decision','db','PG',NULL,'m','q','s',
          '2026-01-01T11:00:00+00:00',
          'current','a',1.0,x'0000803f');
        INSERT INTO memories VALUES ('c','decision','db','bad',NULL,'m','q','s',
          '2026-01-01T12:00:00',
          'current','ghost',1.0,NULL);
        CREATE TABLE audit_log (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
          question TEXT NOT NULL,
          answer TEXT NOT NULL, retrieved_ids TEXT NOT NULL, verdicts TEXT NOT NULL,
          decision_action TEXT NOT NULL, latency_ms REAL NOT NULL, prompt_tokens INTEGER NOT NULL,
          completion_tokens INTEGER NOT NULL);
        INSERT INTO audit_log VALUES
          ('r1','2026-01-01T00:00:00+00:00','q','a','[]','[]','answer',1.0,0,0);
        """
    )
    conn.commit()
    conn.close()

    store = SqliteMemoryStore(path)
    b = store.get("b")
    assert b is not None and b.supersedes_id == "a" and b.timestamp.tzinfo is not None
    assert store.get("c").supersedes_id is None  # type: ignore[union-attr]  # dangling link cleared
    assert store.get("a").timestamp == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)  # type: ignore[union-attr]
    assert store.candidates(include_history=True)[1][1].dim == 1  # type: ignore[union-attr]
    from nova.audit.logger import AuditLog

    rec = AuditLog(path).get("r1")
    assert rec is not None and rec.prompt_tokens is None and rec.completion_tokens is None
    store.close()
    SqliteMemoryStore(path).close()  # reopening is idempotent


# ---- 14: malformed extraction JSON ----------------------------------------------------------


def _extract(llm_replies: list[str]) -> tuple[list[Memory], FakeLLM]:
    llm = FakeLLM(llm_replies)
    out = LLMExtractor(llm).extract([msg("m1", "We use PostgreSQL for the database.")])
    return out, llm


_GOOD = json.dumps(
    [
        {
            "type": "decision",
            "subject": "db",
            "value": "PostgreSQL",
            "source_msg_id": "m1",
            "source_quote": "use PostgreSQL",
        }
    ]
)


@pytest.mark.parametrize(
    "bad",
    [
        json.dumps([{"subject": "db", "value": "x", "source_msg_id": "m1"}]),  # missing type
        json.dumps([{"type": "decision", "value": "x", "source_msg_id": "m1"}]),  # missing subject
        json.dumps([{"type": "decision", "subject": "db", "source_msg_id": "m1"}]),  # missing value
        json.dumps([{"type": "decision", "subject": "db", "value": "x"}]),  # missing source id
        json.dumps([{"type": "nonsense", "subject": "db", "value": "x", "source_msg_id": "m1"}]),
        json.dumps(
            [
                {
                    "type": "fact",
                    "subject": "db",
                    "value": "x",
                    "source_msg_id": "m1",
                    "confidence": "NaN",
                }
            ]
        ),
        json.dumps(
            [
                {
                    "type": "fact",
                    "subject": "db",
                    "value": "x",
                    "source_msg_id": "m1",
                    "confidence": 9,
                }
            ]
        ),
        json.dumps({"not": "a list"}),
        json.dumps(["string item"]),
        "not json",
    ],
)
def test_malformed_extraction_triggers_bounded_repair_not_keyerror(bad: str) -> None:
    out, llm = _extract([bad, _GOOD])
    assert len(out) == 1 and len(llm.calls) == 2  # exactly one repair attempt

    with pytest.raises(ExtractionError):
        _extract([bad, bad])


def test_programming_errors_are_not_swallowed(monkeypatch) -> None:
    extractor = LLMExtractor(FakeLLM([_GOOD]))

    def broken(*a, **k):  # type: ignore[no-untyped-def]
        raise AttributeError("bug")

    monkeypatch.setattr(extractor, "_parse_items", broken)
    with pytest.raises(AttributeError):
        extractor.extract([msg("m1", "We use PostgreSQL for the database.")])
