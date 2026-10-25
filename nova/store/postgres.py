"""PostgreSQL backend: the first supported user-managed external storage.

Requires the optional ``psycopg`` dependency (``pip install nova[postgres]``). Remote
connections must use TLS, hosts are vetted against SSRF rules and the validated address is
pinned, and every error is reduced to a stable code before it can reach a caller.
"""

import logging
from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from nova.security.net import UnsafeEndpointError, resolve_public_address
from nova.security.redact import redact
from nova.store.base import StorageUnavailableError, StoreError
from nova.store.database import Database, Row
from nova.store.schema import POSTGRES_SCHEMA_VERSION, SCHEMA_VERSION, migrate

logger = logging.getLogger("nova.store.postgres")

PG_SCHEMA_VERSION = POSTGRES_SCHEMA_VERSION
SslMode = Literal["require", "verify-ca", "verify-full"]


class PostgresSettings(BaseModel):
    """Non-secret connection settings for a tenant's own PostgreSQL database."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_.-]+$")
    user: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_.@-]+$")
    sslmode: SslMode = "verify-full"
    connect_timeout_s: int = Field(default=5, ge=1, le=30)
    statement_timeout_ms: int = Field(default=15_000, ge=100, le=60_000)


class PostgresConfigRequest(PostgresSettings):
    """Payload for configuring PostgreSQL storage; ``password`` is write-only."""

    password: SecretStr | None = Field(default=None, min_length=1, max_length=512)

    def settings_only(self) -> PostgresSettings:
        """Return the non-secret part."""
        return PostgresSettings(**self.model_dump(exclude={"password"}))


def classify_error(exc: BaseException) -> StorageUnavailableError:
    """Map a driver exception to a stable public code without exposing its text."""
    text = str(exc).lower()
    if "password" in text or "authentication" in text or "role" in text:
        return StorageUnavailableError("storage_auth_failed", "Database authentication failed.")
    if "ssl" in text or "tls" in text or "certificate" in text:
        return StorageUnavailableError("storage_tls_failed", "Secure (TLS) connection failed.")
    if "timeout" in text or "timed out" in text:
        return StorageUnavailableError("storage_timeout", "The database connection timed out.")
    if "does not exist" in text and "database" in text:
        return StorageUnavailableError("storage_database_missing", "The database was not found.")
    return StorageUnavailableError("storage_unreachable", "The database could not be reached.")


def _translate(sql: str) -> str:
    return sql.replace("?", "%s")


class PostgresDatabase(Database):
    """A psycopg connection in autocommit mode with explicit transactions."""

    dialect = "postgres"
    order_column = "seq"

    def __init__(self, conn: Any) -> None:
        """Wrap an already-open psycopg connection."""
        super().__init__()
        self._conn = conn

    def _run(self, sql: str, params: Sequence[Any]) -> tuple[list[Row], int]:
        try:
            with self._conn.cursor() as cur:
                if params:
                    cur.execute(_translate(sql), tuple(params))
                else:
                    cur.execute(sql)
                rows = [dict(r) for r in cur.fetchall()] if cur.description else []
                for r in rows:
                    for key, val in r.items():
                        if isinstance(val, memoryview):
                            r[key] = bytes(val)
                return rows, cur.rowcount
        except Exception as e:  # driver-specific exception hierarchy; reduced to StoreError
            logger.warning("postgres statement failed: %s", redact(f"{type(e).__name__}: {e}"))
            raise StoreError(f"Database error: {type(e).__name__}") from e

    def _begin(self) -> None:
        self._run("BEGIN", ())

    def _commit(self) -> None:
        self._run("COMMIT", ())

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except Exception:  # connection may already be broken; nothing more to do
            logger.debug("rollback failed", exc_info=True)

    def _close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # closing a broken connection must not raise
            logger.debug("close failed", exc_info=True)


ConnectFn = Callable[..., Any]


def open_postgres(
    settings: PostgresSettings,
    password: str,
    *,
    allow_private: bool = False,
    connect_fn: ConnectFn | None = None,
) -> PostgresDatabase:
    """Vet the host, connect over TLS with time limits, and return a PostgresDatabase."""
    try:
        hostaddr = resolve_public_address(settings.host, allow_private=allow_private)
    except (UnsafeEndpointError, OSError) as e:
        raise StorageUnavailableError(
            "storage_endpoint_blocked", "The database host is not allowed."
        ) from e
    if connect_fn is None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:
            raise StorageUnavailableError(
                "storage_driver_missing", "PostgreSQL support is not installed on this server."
            ) from e
        connect_fn = psycopg.connect
        extra: dict[str, Any] = {"row_factory": dict_row}
    else:
        extra = {}
    kwargs: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "dbname": settings.database,
        "user": settings.user,
        "password": password,
        "sslmode": settings.sslmode,
        "connect_timeout": settings.connect_timeout_s,
        "application_name": "nova",
        "autocommit": True,
        "options": f"-c statement_timeout={settings.statement_timeout_ms} -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=30000",
        **extra,
    }
    if hostaddr is not None and hostaddr != settings.host.strip("[]"):
        kwargs["hostaddr"] = hostaddr
    try:
        conn = connect_fn(**kwargs)
    except Exception as e:  # noqa: BLE001 - driver-specific; classified into a safe public code
        public = classify_error(e)
        logger.warning("postgres connect failed: %s (%s)", public.code, type(e).__name__)
        raise public from None
    return PostgresDatabase(conn)


def schema_ready(db: Database) -> bool:
    """Return True if the Postgres schema has been initialized."""
    try:
        rows = db.query("SELECT MAX(version) AS v FROM schema_version")
    except StoreError:
        return False
    return bool(rows and rows[0]["v"] is not None and int(rows[0]["v"]) >= PG_SCHEMA_VERSION)


__all__ = [
    "PG_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "PostgresConfigRequest",
    "PostgresDatabase",
    "PostgresSettings",
    "classify_error",
    "migrate",
    "open_postgres",
    "schema_ready",
]
