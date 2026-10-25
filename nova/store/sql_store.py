"""Tenant-scoped memory store implemented once over the Database abstraction."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from itertools import pairwise
from typing import Any

from nova.core.policy import CHAINED_TYPES
from nova.core.schemas import EmbeddingRecord, Memory, MemoryStatus, MemoryType
from nova.core.tenant import DEFAULT_TENANT, validate_tenant_id
from nova.store.base import StoreError, StoreLimitError, SupersessionError
from nova.store.database import Database, Row
from nova.store.limits import (
    MAX_CANDIDATE_ROWS,
    MAX_EMBEDDING_BATCH,
    MAX_EXPORT_ROWS,
    MAX_SESSION_ROWS,
)
from nova.store.timeutil import format_ts, parse_ts

_MAX_CHAIN_WALK = 10_000
_ID_CHUNK = 400

_COLUMNS = (
    "tenant_id, id, type, subject, value, reason, source_msg_id, source_quote, session_id, "
    "timestamp, status, supersedes_id, confidence, embedding, embedding_model, embedding_dim"
)
_INSERT = (
    f"INSERT INTO memories ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (tenant_id, id) DO NOTHING"
)

EmbeddingInput = bytes | EmbeddingRecord | None


def _coerce_embedding(value: EmbeddingInput) -> EmbeddingRecord | None:
    """Accept raw float32 bytes or a full EmbeddingRecord and validate the byte layout."""
    if value is None:
        return None
    if isinstance(value, EmbeddingRecord):
        record = value
    else:
        if len(value) == 0 or len(value) % 4 != 0:
            raise StoreError("embedding must be a non-empty float32 byte string")
        record = EmbeddingRecord(vector=bytes(value), model=None, dim=len(value) // 4)
    if len(record.vector) != record.dim * 4:
        raise StoreError("embedding byte length does not match its declared dimension")
    return record


def _order_key(row: Row | Memory) -> tuple[str, str]:
    if isinstance(row, Memory):
        return (format_ts(row.timestamp), row.id)
    return (row["timestamp"], row["id"])


class SqlMemoryStore:
    """MemoryStore for one tenant over a Database; all queries are tenant-filtered."""

    def __init__(
        self,
        db: Database,
        tenant_id: str = DEFAULT_TENANT,
        *,
        owns_db: bool = False,
    ) -> None:
        """Bind the store to a database connection and a tenant."""
        self.db = db
        self.tenant_id = validate_tenant_id(tenant_id)
        self._owns_db = owns_db

    # ---- transactions -------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the enclosed operations atomically (joins an outer transaction if present)."""
        with self.db.transaction():
            yield

    # ---- conversion ---------------------------------------------------------------------
    @staticmethod
    def _row_to_memory(row: Row) -> Memory:
        return Memory(
            id=row["id"],
            type=MemoryType(row["type"]),
            subject=row["subject"],
            value=row["value"],
            reason=row["reason"],
            source_msg_id=row["source_msg_id"],
            source_quote=row["source_quote"],
            session_id=row["session_id"],
            timestamp=parse_ts(row["timestamp"]),
            status=MemoryStatus(row["status"]),
            supersedes_id=row["supersedes_id"],
            confidence=float(row["confidence"]),
        )

    @staticmethod
    def _row_embedding(row: Row) -> EmbeddingRecord | None:
        blob = row["embedding"]
        if blob is None:
            return None
        return EmbeddingRecord(
            vector=bytes(blob), model=row["embedding_model"], dim=int(row["embedding_dim"])
        )

    def _memory_params(self, mem: Memory, emb: EmbeddingRecord | None) -> tuple[Any, ...]:
        return (
            self.tenant_id,
            mem.id,
            mem.type.value,
            mem.subject,
            mem.value,
            mem.reason,
            mem.source_msg_id,
            mem.source_quote,
            mem.session_id,
            format_ts(mem.timestamp),
            mem.status.value,
            mem.supersedes_id,
            mem.confidence,
            emb.vector if emb else None,
            emb.model if emb else None,
            emb.dim if emb else None,
        )

    # ---- writes -------------------------------------------------------------------------
    def add(
        self,
        memory: Memory,
        embedding: EmbeddingInput = None,
        *,
        embedding_model: str | None = None,
    ) -> None:
        """Insert a memory idempotently by id, optionally with its embedding."""
        record = _coerce_embedding(embedding)
        if record is not None and embedding_model is not None and record.model is None:
            record = record.model_copy(update={"model": embedding_model})
        self.db.execute(_INSERT, self._memory_params(memory, record))

    def add_many(
        self,
        memories: list[Memory],
        embeddings: Sequence[EmbeddingInput] | None = None,
    ) -> None:
        """Insert multiple memories atomically; embeddings, if given, must match 1:1."""
        if embeddings is not None and len(embeddings) != len(memories):
            raise StoreError(
                f"embeddings length ({len(embeddings)}) does not match memories ({len(memories)})"
            )
        records = [_coerce_embedding(e) for e in embeddings] if embeddings is not None else None
        with self.transaction():
            for idx, mem in enumerate(memories):
                self.db.execute(
                    _INSERT, self._memory_params(mem, records[idx] if records else None)
                )

    def set_embeddings(self, records: dict[str, EmbeddingRecord]) -> int:
        """Attach or replace embeddings for existing memories; returns rows updated."""
        updated = 0
        with self.transaction():
            for memory_id, rec in records.items():
                checked = _coerce_embedding(rec)
                assert checked is not None
                updated += self.db.execute(
                    "UPDATE memories SET embedding = ?, embedding_model = ?, embedding_dim = ? "
                    "WHERE tenant_id = ? AND id = ?",
                    (checked.vector, checked.model, checked.dim, self.tenant_id, memory_id),
                )
        return updated

    # ---- reads --------------------------------------------------------------------------
    def get(self, memory_id: str) -> Memory | None:
        """Retrieve memory by id."""
        rows = self.db.query(
            "SELECT * FROM memories WHERE tenant_id = ? AND id = ?", (self.tenant_id, memory_id)
        )
        return self._row_to_memory(rows[0]) if rows else None

    def current_all_by_subject(self, subject: str) -> list[Memory]:
        """Return every current memory for a subject, newest first (ties broken by id)."""
        rows = self.db.query(
            "SELECT * FROM memories WHERE tenant_id = ? AND subject = ? AND status = 'current' "
            "ORDER BY timestamp DESC, id DESC",
            (self.tenant_id, subject),
        )
        return [self._row_to_memory(r) for r in rows]

    def current_by_subject(self, subject: str) -> Memory | None:
        """Return the newest current memory for a subject (deterministic on ties)."""
        found = self.current_all_by_subject(subject)
        return found[0] if found else None

    def chain_by_subject(self, subject: str) -> list[Memory]:
        """Return all supersession-eligible memories of a subject, oldest first."""
        types = sorted(t.value for t in CHAINED_TYPES)
        marks = ", ".join("?" for _ in types)
        rows = self.db.query(
            f"SELECT * FROM memories WHERE tenant_id = ? AND subject = ? AND type IN ({marks}) "
            "ORDER BY timestamp ASC, id ASC",
            (self.tenant_id, subject, *types),
        )
        return [self._row_to_memory(r) for r in rows]

    def list_by_session(self, session_id: str, limit: int = MAX_SESSION_ROWS) -> list[Memory]:
        """List a session's memories ordered by timestamp then id; bounded, never truncated."""
        if limit > MAX_SESSION_ROWS:
            raise StoreLimitError(MAX_SESSION_ROWS)
        rows = self.db.query(
            "SELECT * FROM memories WHERE tenant_id = ? AND session_id = ? "
            "ORDER BY timestamp ASC, id ASC LIMIT ?",
            (self.tenant_id, session_id, limit + 1),
        )
        if len(rows) > limit:
            raise StoreLimitError(limit)
        return [self._row_to_memory(r) for r in rows]

    def candidates(
        self,
        include_history: bool = False,
        session_id: str | None = None,
        limit: int = MAX_CANDIDATE_ROWS,
    ) -> list[tuple[Memory, EmbeddingRecord | None]]:
        """Return retrieval candidates with embeddings, in a stable order.

        The scan is bounded: exceeding ``limit`` raises ``StoreLimitError`` instead of silently
        dropping rows, so a growing tenant surfaces a scoping problem rather than wrong answers.
        """
        if limit > MAX_CANDIDATE_ROWS:
            raise StoreLimitError(MAX_CANDIDATE_ROWS)
        sql = "SELECT * FROM memories WHERE tenant_id = ?"
        params: list[Any] = [self.tenant_id]
        if not include_history:
            sql += " AND status = 'current'"
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY timestamp ASC, id ASC LIMIT ?"
        params.append(limit + 1)
        rows = self.db.query(sql, params)
        if len(rows) > limit:
            raise StoreLimitError(limit)
        return [(self._row_to_memory(r), self._row_embedding(r)) for r in rows]

    def missing_embeddings(
        self,
        model: str,
        dim: int,
        limit: int | None = None,
    ) -> list[Memory]:
        """Return memories with no embedding, or one from a different model/dimension.

        ``limit`` defaults to, and may not exceed, ``MAX_EMBEDDING_BATCH`` so a backfill pass stays
        bounded; the caller loops until the list comes back empty.
        """
        effective = MAX_EMBEDDING_BATCH if limit is None else limit
        if effective > MAX_EMBEDDING_BATCH:
            raise StoreLimitError(MAX_EMBEDDING_BATCH)
        rows = self.db.query(
            "SELECT * FROM memories WHERE tenant_id = ? AND (embedding IS NULL "
            "OR embedding_model IS NULL OR embedding_model <> ? OR embedding_dim <> ?) "
            "ORDER BY timestamp ASC, id ASC LIMIT ?",
            (self.tenant_id, model, dim, effective),
        )
        return [self._row_to_memory(r) for r in rows]

    def count(self) -> int:
        """Return the number of memories owned by this tenant."""
        return int(
            self.db.query(
                "SELECT COUNT(*) AS c FROM memories WHERE tenant_id = ?", (self.tenant_id,)
            )[0]["c"]
        )

    # ---- supersession -------------------------------------------------------------------
    def supersede(self, old_id: str, new_id: str) -> None:
        """Mark ``old_id`` superseded by ``new_id`` after validating chain integrity."""
        with self.transaction():
            old = self._raw(old_id)
            if old is None:
                raise StoreError(f"Memory to supersede not found: {old_id}")
            new = self._raw(new_id)
            if new is None:
                raise StoreError(f"Replacement memory not found: {new_id}")
            if old_id == new_id:
                raise SupersessionError("a memory cannot supersede itself")
            if old["subject"] != new["subject"]:
                raise SupersessionError("memories with different subjects cannot be linked")
            if _order_key(old) >= _order_key(new):
                raise SupersessionError("a memory can only be superseded by a later memory")
            if new["supersedes_id"] not in (None, old_id):
                raise SupersessionError("replacement already supersedes a different memory")
            forks = self.db.query(
                "SELECT id FROM memories WHERE tenant_id = ? AND supersedes_id = ? AND id <> ?",
                (self.tenant_id, old_id, new_id),
            )
            if forks:
                raise SupersessionError("memory is already superseded by a different memory")
            self._reject_cycle(old, new_id)
            self.db.execute(
                "UPDATE memories SET status = 'superseded' WHERE tenant_id = ? AND id = ?",
                (self.tenant_id, old_id),
            )
            self.db.execute(
                "UPDATE memories SET supersedes_id = ? WHERE tenant_id = ? AND id = ?",
                (old_id, self.tenant_id, new_id),
            )

    def _reject_cycle(self, start: Row, new_id: str) -> None:
        """Walk predecessors of ``start``; reaching ``new_id`` would close a cycle."""
        cursor: Row | None = start
        for _ in range(_MAX_CHAIN_WALK):
            parent_id = cursor["supersedes_id"] if cursor else None
            if parent_id is None:
                return
            if parent_id == new_id:
                raise SupersessionError("supersession would create a cycle")
            cursor = self._raw(parent_id)
        raise SupersessionError("supersession chain is unreasonably long")

    def _raw(self, memory_id: str) -> Row | None:
        rows = self.db.query(
            "SELECT * FROM memories WHERE tenant_id = ? AND id = ?", (self.tenant_id, memory_id)
        )
        return rows[0] if rows else None

    def link_chain(self, ordered_ids: list[str]) -> int:
        """Chain ``ordered_ids``: each supersedes its predecessor; only the last is current.

        The ids must be the complete chain for one subject in strictly ascending
        (timestamp, id) order. Returns the number of supersession links newly created.
        """
        if not ordered_ids:
            return 0
        if len(set(ordered_ids)) != len(ordered_ids):
            raise SupersessionError("chain contains duplicate memories")
        with self.transaction():
            rows: list[Row] = []
            for memory_id in ordered_ids:
                row = self._raw(memory_id)
                if row is None:
                    raise StoreError(f"Memory not found for chain: {memory_id}")
                rows.append(row)
            if len({r["subject"] for r in rows}) != 1:
                raise SupersessionError("memories with different subjects cannot be chained")
            keys = [_order_key(r) for r in rows]
            if any(a >= b for a, b in pairwise(keys)):
                raise SupersessionError("chain must be in strictly ascending time order")

            last = len(rows) - 1
            changes: list[tuple[Row, str | None, str]] = []
            new_links = 0
            for idx, row in enumerate(rows):
                want_parent = rows[idx - 1]["id"] if idx > 0 else None
                want_status = (
                    MemoryStatus.CURRENT.value if idx == last else MemoryStatus.SUPERSEDED.value
                )
                if row["supersedes_id"] != want_parent or row["status"] != want_status:
                    changes.append((row, want_parent, want_status))
                if want_parent is not None and row["supersedes_id"] != want_parent:
                    new_links += 1
            for row, _, _ in changes:
                self.db.execute(
                    "UPDATE memories SET supersedes_id = NULL WHERE tenant_id = ? AND id = ?",
                    (self.tenant_id, row["id"]),
                )
            for row, parent, status in changes:
                self.db.execute(
                    "UPDATE memories SET supersedes_id = ?, status = ? "
                    "WHERE tenant_id = ? AND id = ?",
                    (parent, status, self.tenant_id, row["id"]),
                )
            return new_links

    # ---- lifecycle ----------------------------------------------------------------------
    def delete_all(self) -> int:
        """Delete every memory owned by this tenant; returns the number removed."""
        with self.transaction():
            self.db.execute(
                "UPDATE memories SET supersedes_id = NULL WHERE tenant_id = ?", (self.tenant_id,)
            )
            return self.db.execute("DELETE FROM memories WHERE tenant_id = ?", (self.tenant_id,))

    def export(self, limit: int = MAX_EXPORT_ROWS) -> list[Memory]:
        """Return up to ``limit`` memories, oldest first; over-limit requests are rejected."""
        if limit > MAX_EXPORT_ROWS:
            raise StoreLimitError(MAX_EXPORT_ROWS)
        """Return up to ``limit`` of this tenant's memories (embeddings excluded)."""
        rows = self.db.query(
            "SELECT * FROM memories WHERE tenant_id = ? ORDER BY timestamp ASC, id ASC LIMIT ?",
            (self.tenant_id, limit),
        )
        return [self._row_to_memory(r) for r in rows]

    def close(self) -> None:
        """Close the database connection when this store owns it."""
        if self._owns_db:
            self.db.close()
