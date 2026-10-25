"""Shared storage exceptions."""


class StoreError(Exception):
    """Exception raised for storage layer errors."""


class SupersessionError(StoreError):
    """Raised when a supersession link would violate chain integrity."""


class EmbeddingCompatibilityError(StoreError):
    """Raised when stored embeddings do not match the active embedder."""


class StorageUnavailableError(StoreError):
    """Raised when a storage backend cannot be reached or authenticated."""

    def __init__(self, code: str, message: str) -> None:
        """Create the error with a stable public ``code`` and a safe ``message``."""
        super().__init__(message)
        self.code = code
        self.public_message = message


class SchemaVersionError(StoreError):
    """Raised when the stored schema version is newer than this server understands."""

    code = "schema_version_unsupported"
    public_message = "The database schema version is not supported by this server."


class SchemaMigrationError(StoreError):
    """Raised when a migration cannot be applied; the database is left unchanged."""

    code = "schema_migration_failed"
    public_message = "The database migration failed; no changes were applied."


class StoreLimitError(StoreError):
    """Raised when a query would return more rows than the configured hard limit."""

    code = "query_limit_exceeded"
    public_message = "The query would return too many rows; narrow the request."

    def __init__(self, limit: int) -> None:
        """Create the error naming the hard limit that was exceeded."""
        super().__init__(f"query exceeds the hard limit of {limit} rows")
        self.limit = limit
