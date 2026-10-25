"""Deletion receipts: proof that erasure happened, with no retained content.

A receipt records *that* a tenant's records were deleted and *how many* rows went, plus who asked
and when. It never stores a memory value, quote, question, answer, subject, or record id: those are
the sensitive payloads the deletion was meant to remove. The table therefore survives a
records-scope deletion while remaining safe to keep.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nova.store.base import StoreLimitError
from nova.store.database import Database, Row
from nova.store.limits import MAX_EXPORT_ROWS, MAX_RECEIPT_ROWS

DeletionScope = Literal["records", "account"]

_COLS = (
    "tenant_id, id, scope, actor, request_id, deleted_at, "
    "memories_deleted, audit_deleted, receipts_deleted"
)


class DeletionReceipt(BaseModel):
    """A content-free record of one deletion operation."""

    model_config = ConfigDict(extra="forbid")

    id: str
    scope: DeletionScope
    actor: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=100)
    deleted_at: str
    memories_deleted: int = Field(ge=0)
    audit_deleted: int = Field(ge=0)
    receipts_deleted: int = Field(ge=0, default=0)


def _row_to_receipt(row: Row) -> DeletionReceipt:
    return DeletionReceipt(
        id=str(row["id"]),
        scope=row["scope"],
        actor=str(row["actor"]),
        request_id=row["request_id"],
        deleted_at=str(row["deleted_at"]),
        memories_deleted=int(row["memories_deleted"]),
        audit_deleted=int(row["audit_deleted"]),
        receipts_deleted=int(row["receipts_deleted"]),
    )


class SqlReceiptStore:
    """Tenant-scoped access to deletion receipts."""

    def __init__(self, db: Database, tenant_id: str) -> None:
        """Bind the repository to one connection and one tenant."""
        self.db = db
        self.tenant_id = tenant_id

    def write(
        self,
        *,
        scope: DeletionScope,
        actor: str,
        request_id: str | None,
        memories_deleted: int,
        audit_deleted: int,
        receipts_deleted: int = 0,
    ) -> DeletionReceipt:
        """Insert one receipt and return it."""
        payload = DeletionReceipt(
            id=_new_id(),
            scope=scope,
            actor=actor,
            request_id=request_id,
            deleted_at=datetime.now(UTC).isoformat(),
            memories_deleted=memories_deleted,
            audit_deleted=audit_deleted,
            receipts_deleted=receipts_deleted,
        )
        self.db.execute(
            f"INSERT INTO deletion_receipts ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.tenant_id,
                payload.id,
                payload.scope,
                payload.actor,
                payload.request_id,
                payload.deleted_at,
                payload.memories_deleted,
                payload.audit_deleted,
                payload.receipts_deleted,
            ),
        )
        return payload

    def recent(self, limit: int = 20) -> list[DeletionReceipt]:
        """Return the newest receipts for this tenant, newest first."""
        if limit > MAX_RECEIPT_ROWS:
            raise StoreLimitError(MAX_RECEIPT_ROWS)
        rows = self.db.query(
            f"SELECT {_COLS} FROM deletion_receipts WHERE tenant_id = ? "
            "ORDER BY deleted_at DESC, id DESC LIMIT ?",
            (self.tenant_id, limit),
        )
        return [_row_to_receipt(r) for r in rows]

    def export(self, limit: int = MAX_EXPORT_ROWS) -> list[DeletionReceipt]:
        """Return receipts for an export, oldest first (same bound as the other tables)."""
        if limit > MAX_EXPORT_ROWS:
            raise StoreLimitError(MAX_EXPORT_ROWS)
        rows = self.db.query(
            f"SELECT {_COLS} FROM deletion_receipts WHERE tenant_id = ? "
            "ORDER BY deleted_at ASC, id ASC LIMIT ?",
            (self.tenant_id, limit),
        )
        return [_row_to_receipt(r) for r in rows]

    def count(self) -> int:
        """Return how many receipts this tenant has."""
        rows = self.db.query(
            "SELECT COUNT(*) AS c FROM deletion_receipts WHERE tenant_id = ?", (self.tenant_id,)
        )
        return int(rows[0]["c"])

    def delete_all(self) -> int:
        """Delete every receipt for this tenant (used only by an account-scope erasure)."""
        return self.db.execute(
            "DELETE FROM deletion_receipts WHERE tenant_id = ?", (self.tenant_id,)
        )


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex


__all__ = ["DeletionReceipt", "DeletionScope", "SqlReceiptStore"]
