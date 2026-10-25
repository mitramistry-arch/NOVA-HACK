"""Audit log: one record per query (ids, verdicts, action, latency, tokens), tenant-scoped.

Privacy and access policy
-------------------------
* Every row carries a ``tenant_id``; reads, retention and deletion are always filtered by the
  tenant the log was opened for, so one tenant can never read or purge another's records.
* Question/answer/verdict text is truncated to ``MAX_TEXT`` characters and scrubbed of
  configured keys and common credential shapes before it is stored.
* Token counts are stored only when the model provider actually reported them (``None``
  otherwise); a missing count is never recorded as zero.
* Retention: ``purge_older_than`` deletes expired rows (the API runs it at startup using
  ``NOVA_AUDIT_RETENTION_DAYS``); ``delete_all`` erases a tenant's whole log on request.
"""

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel, Field

from nova.core.config import get_settings
from nova.core.schemas import DecisionAction, Verdict
from nova.core.tenant import DEFAULT_TENANT, validate_tenant_id
from nova.security.redact import redact
from nova.store.base import StoreError, StoreLimitError
from nova.store.database import Database, SqliteDatabase
from nova.store.limits import MAX_RECENT_AUDIT_ROWS
from nova.store.schema import migrate
from nova.store.timeutil import format_ts, parse_ts

MAX_TEXT = 2000
MAX_RECENT = MAX_RECENT_AUDIT_ROWS  # single bound shared with the store limits

_COLS = (
    "id, timestamp, question, answer, retrieved_ids, verdicts, decision_action, "
    "latency_ms, prompt_tokens, completion_tokens"
)


class AuditError(Exception):
    """Raised when the audit log cannot be read or written."""


class AuditRecord(BaseModel):
    """One verification query and its outcome."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    question: str = ""
    answer: str = ""
    retrieved_ids: list[str] = Field(default_factory=list)
    verdicts: list[Verdict] = Field(default_factory=list)
    decision_action: DecisionAction = DecisionAction.ABSTAIN
    latency_ms: float = 0.0
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class Timer:
    """Holds elapsed milliseconds once the timed block exits."""

    elapsed_ms: float = 0.0


@contextmanager
def timed() -> Iterator[Timer]:
    """Measure wall-clock milliseconds of the enclosed block."""
    timer = Timer()
    start = time.perf_counter()
    try:
        yield timer
    finally:
        timer.elapsed_ms = (time.perf_counter() - start) * 1000.0


def _secrets() -> list[str]:
    s = get_settings()
    return [k for k in (s.api_key, s.openai_api_key, s.anthropic_api_key) if k]


def _clean(text: str, secrets: list[str]) -> str:
    return redact(text, secrets)[:MAX_TEXT]


class SqlAuditStore:
    """Tenant-scoped audit log over a Database (SQLite or PostgreSQL)."""

    def __init__(
        self,
        db: Database,
        tenant_id: str = DEFAULT_TENANT,
        *,
        owns_db: bool = False,
        extra_secrets: tuple[str, ...] = (),
    ) -> None:
        """Bind the log to a connection and tenant; the schema must already be migrated."""
        self.db = db
        self.tenant_id = validate_tenant_id(tenant_id)
        self._owns_db = owns_db
        self._extra_secrets = extra_secrets

    def write(self, record: AuditRecord) -> str:
        """Persist the record (text truncated, secrets redacted) and return its id."""
        sec = [*_secrets(), *self._extra_secrets]
        verdicts = [
            {
                **v.model_dump(mode="json"),
                "claim": _clean(v.claim, sec),
                "rationale": _clean(v.rationale, sec),
            }
            for v in record.verdicts
        ]
        row = (
            self.tenant_id,
            record.id,
            format_ts(record.timestamp),
            _clean(record.question, sec),
            _clean(record.answer, sec),
            json.dumps(record.retrieved_ids),
            json.dumps(verdicts),
            record.decision_action.value,
            record.latency_ms,
            record.prompt_tokens,
            record.completion_tokens,
        )
        try:
            self.db.execute(
                "INSERT INTO audit_log (tenant_id, id, timestamp, question, answer, retrieved_ids,"
                " verdicts, decision_action, latency_ms, prompt_tokens, completion_tokens)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        except StoreError as e:
            raise AuditError(f"Failed to write audit record: {e}") from e
        return record.id

    def get(self, record_id: str) -> AuditRecord | None:
        """Return this tenant's record with this id, or None."""
        rows = self._query(
            f"SELECT {_COLS} FROM audit_log WHERE tenant_id = ? AND id = ?",
            (self.tenant_id, record_id),
        )
        return rows[0] if rows else None

    def recent(self, n: int = 20) -> list[AuditRecord]:
        """Return up to n of this tenant's records, newest first (bounded, never unbounded)."""
        if n <= 0:
            return []
        if n > MAX_RECENT_AUDIT_ROWS:
            raise StoreLimitError(MAX_RECENT_AUDIT_ROWS)
        order = self.db.order_column
        sql = (
            f"SELECT {_COLS} FROM audit_log WHERE tenant_id = ? "
            f"ORDER BY timestamp DESC, {order} DESC LIMIT ?"
        )
        return self._query(sql, (self.tenant_id, n))

    def purge_older_than(self, cutoff: datetime) -> int:
        """Delete this tenant's records older than ``cutoff``; returns the number removed."""
        try:
            return self.db.execute(
                "DELETE FROM audit_log WHERE tenant_id = ? AND timestamp < ?",
                (self.tenant_id, format_ts(cutoff)),
            )
        except StoreError as e:
            raise AuditError(f"Failed to purge audit log: {e}") from e

    def count(self) -> int:
        """Return how many audit records this tenant has."""
        rows = self.db.query(
            "SELECT COUNT(*) AS c FROM audit_log WHERE tenant_id = ?", (self.tenant_id,)
        )
        return int(rows[0]["c"])

    def delete_all(self) -> int:
        """Erase this tenant's entire audit log; returns the number removed."""
        try:
            return self.db.execute("DELETE FROM audit_log WHERE tenant_id = ?", (self.tenant_id,))
        except StoreError as e:
            raise AuditError(f"Failed to delete audit log: {e}") from e

    def export(self, limit: int = 10_000) -> list[AuditRecord]:
        """Return up to ``limit`` of this tenant's records, oldest first."""
        order = self.db.order_column
        return self._query(
            f"SELECT {_COLS} FROM audit_log WHERE tenant_id = ? "
            f"ORDER BY timestamp ASC, {order} ASC LIMIT ?",
            (self.tenant_id, limit),
        )

    def close(self) -> None:
        """Close the connection when this log owns it."""
        if self._owns_db:
            self.db.close()

    def __enter__(self) -> Self:
        """Context manager entry."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Context manager exit."""
        self.close()

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[AuditRecord]:
        try:
            rows = self.db.query(sql, params)
        except StoreError as e:
            raise AuditError(f"Failed to read audit log: {e}") from e
        return [
            AuditRecord(
                id=r["id"],
                timestamp=parse_ts(r["timestamp"]),
                question=r["question"],
                answer=r["answer"],
                retrieved_ids=json.loads(r["retrieved_ids"]),
                verdicts=[Verdict(**v) for v in json.loads(r["verdicts"])],
                decision_action=DecisionAction(r["decision_action"]),
                latency_ms=r["latency_ms"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
            )
            for r in rows
        ]


class AuditLog(SqlAuditStore):
    """SQLite-backed audit log in the configured database file."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None:
        """Open (and migrate) the database; release the connection if setup fails."""
        path = str(db_path if db_path is not None else get_settings().db_path)
        try:
            db = SqliteDatabase(path)
        except StoreError as e:
            raise AuditError(f"Failed to initialize audit log: {e}") from e
        try:
            migrate(db)
            super().__init__(db, tenant_id, owns_db=True)
        except StoreError as e:
            db.close()
            raise AuditError(f"Failed to initialize audit log: {e}") from e
        except BaseException:
            db.close()
            raise

    @property
    def conn(self):  # type: ignore[no-untyped-def]
        """Return the underlying sqlite3 connection (diagnostics and tests)."""
        return self.db.conn  # type: ignore[attr-defined]
