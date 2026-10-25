"""SQLite-backed MemoryStore implementation."""

from pathlib import Path
from types import TracebackType
from typing import Self

from nova.core.tenant import DEFAULT_TENANT
from nova.store.base import StoreError
from nova.store.database import SqliteDatabase
from nova.store.schema import SCHEMA_VERSION, migrate
from nova.store.sql_store import SqlMemoryStore

__all__ = ["SqliteMemoryStore", "StoreError"]


class SqliteMemoryStore(SqlMemoryStore):
    """MemoryStore backed by SQLite (WAL mode) with versioned, tenant-aware schema."""

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: str | Path = ":memory:", tenant_id: str = DEFAULT_TENANT) -> None:
        """Open the database, run migrations, and release the connection if setup fails."""
        db = SqliteDatabase(db_path)
        try:
            migrate(db)
            super().__init__(db, tenant_id, owns_db=True)
        except BaseException:
            db.close()
            raise
        self.db_path = str(db_path)

    @property
    def conn(self):  # type: ignore[no-untyped-def]
        """Return the underlying sqlite3 connection (used by diagnostics and tests)."""
        return self.db.conn  # type: ignore[attr-defined]

    def migrate(self) -> None:
        """Run idempotent database migrations."""
        migrate(self.db)

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
