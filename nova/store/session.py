"""Storage boundary: one connection exposing memories, embeddings, supersession and audit."""

from nova.audit.logger import SqlAuditStore
from nova.core.tenant import validate_tenant_id
from nova.store.database import Database
from nova.store.schema import migrate
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

    def initialize(self) -> int:
        """Create or upgrade the schema; returns the schema version."""
        return migrate(self.db)

    def check(self) -> None:
        """Raise StoreError if the backend cannot answer a trivial query."""
        self.db.query("SELECT 1 AS ok")

    def delete_tenant_data(self) -> dict[str, int]:
        """Erase this tenant's memories (with embeddings and links) and audit records."""
        with self.db.transaction():
            audit = self.audit.delete_all()
            memories = self.memories.delete_all()
        return {"memories": memories, "audit_records": audit}

    def export_tenant_data(self, limit: int = 10_000) -> dict[str, object]:
        """Return this tenant's memories and audit records as JSON-ready data."""
        return {
            "tenant_id": self.tenant_id,
            "memories": [m.model_dump(mode="json") for m in self.memories.export(limit)],
            "audit_records": [r.model_dump(mode="json") for r in self.audit.export(limit)],
        }

    def close(self) -> None:
        """Close the connection."""
        self.db.close()
