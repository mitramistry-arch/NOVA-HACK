"""Storage boundary: one connection exposing memories, embeddings, supersession and audit."""

from nova.audit.logger import SqlAuditStore
from nova.core.tenant import validate_tenant_id
from nova.store.base import StoreLimitError
from nova.store.database import Database
from nova.store.limits import MAX_EXPORT_ROWS
from nova.store.receipts import DeletionReceipt, DeletionScope, SqlReceiptStore
from nova.store.schema import current_version, migrate
from nova.store.sql_store import SqlMemoryStore


class StorageSession:
    """Tenant-scoped access to whichever backend holds the data (SQLite or PostgreSQL)."""

    def __init__(self, db: Database, tenant_id: str, kind: str) -> None:
        """Bind a connection to a tenant; the session owns and closes the connection."""
        self.db = db
        self.tenant_id = validate_tenant_id(tenant_id)
        self.kind = kind
        self.memories = SqlMemoryStore(db, tenant_id)
        self.audit = SqlAuditStore(db, tenant_id)
        self.receipts = SqlReceiptStore(db, tenant_id)

    def initialize(self) -> int:
        """Create or upgrade the schema; returns the schema version."""
        return migrate(self.db)

    def check(self) -> None:
        """Raise StoreError if the backend cannot answer a trivial query."""
        self.db.query("SELECT 1 AS ok")

    def purge(
        self,
        *,
        scope: DeletionScope = "records",
        actor: str = "system",
        request_id: str | None = None,
        retain_receipt: bool = True,
    ) -> DeletionReceipt | None:
        """Erase tenant data atomically and record a content-free receipt.

        ``scope='records'`` erases memories and audit records but keeps prior receipts, so the
        erasure stays auditable. ``scope='account'`` also removes the receipts themselves; only a
        single tombstone is written, and only when ``retain_receipt`` is true.
        """
        with self.db.transaction():
            memories = self.memories.delete_all()
            audit = self.audit.delete_all()
            receipts_deleted = self.receipts.delete_all() if scope == "account" else 0
            if not retain_receipt:
                return None
            return self.receipts.write(
                scope=scope,
                actor=actor,
                request_id=request_id,
                memories_deleted=memories,
                audit_deleted=audit,
                receipts_deleted=receipts_deleted,
            )

    def delete_tenant_data(self) -> dict[str, int]:
        """Erase this tenant's memories (with embeddings and links) and audit records.

        Kept for callers that only need the counts; ``purge`` is the lifecycle entry point.
        """
        receipt = self.purge(scope="records", actor="system")
        assert receipt is not None
        return {"memories": receipt.memories_deleted, "audit_records": receipt.audit_deleted}

    def export_tenant_data(self, limit: int = MAX_EXPORT_ROWS) -> dict[str, object]:
        """Return this tenant's memories, audit records, and receipts as JSON-ready data."""
        if limit > MAX_EXPORT_ROWS:
            raise StoreLimitError(MAX_EXPORT_ROWS)
        memories = [m.model_dump(mode="json") for m in self.memories.export(limit)]
        audit = [r.model_dump(mode="json") for r in self.audit.export(limit)]
        receipts = [r.model_dump(mode="json") for r in self.receipts.export(limit)]
        counts = {
            "memories": self.memories.count(),
            "audit_records": self.audit.count(),
            "deletion_receipts": self.receipts.count(),
        }
        returned = {
            "memories": len(memories),
            "audit_records": len(audit),
            "deletion_receipts": len(receipts),
        }
        return {
            "tenant_id": self.tenant_id,
            "schema_version": self.current_schema_version(),
            "limit": limit,
            "counts": counts,
            "returned": returned,
            "truncated": any(counts[key] > returned[key] for key in returned),
            "memories": memories,
            "audit_records": audit,
            "deletion_receipts": receipts,
        }

    def current_schema_version(self) -> int:
        """Return the schema version recorded in this database."""
        return current_version(self.db)

    def close(self) -> None:
        """Close the connection."""
        self.db.close()
