"""Tenant storage lifecycle: configure, test, initialize, open, export, delete, disconnect.

Tenants without a configuration use the deployment's local SQLite database (dev/default).
A tenant that configures PostgreSQL never silently falls back to SQLite if it is unreachable.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from nova.core.config import Settings
from nova.providers.config import ConnectionTestResult
from nova.security.net import UnsafeEndpointError, validate_host
from nova.security.secrets import ControlStore, SecretNotFoundError
from nova.store.base import StorageUnavailableError, StoreError
from nova.store.postgres import (
    PostgresConfigRequest,
    PostgresSettings,
    open_postgres,
    schema_ready,
)
from nova.store.schema import open_sqlite
from nova.store.session import StorageSession

logger = logging.getLogger("nova.store.service")
_KIND = "storage"
STORAGE_PASSWORD_SECRET = "storage-password"


class StorageStatus(BaseModel):
    """Public view of a tenant's storage: never the password or connection string."""

    kind: str
    configured: bool
    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    sslmode: str | None = None
    password_mask: str | None = None


class StorageService:
    """Opens tenant storage sessions and manages external storage configuration."""

    def __init__(
        self,
        control: ControlStore,
        settings: Settings,
        *,
        connect_fn: Callable[..., Any] | None = None,
    ) -> None:
        """Bind to the control store; ``connect_fn`` lets tests inject a fake PG driver."""
        self.control = control
        self.settings = settings
        self._connect_fn = connect_fn

    def _pg_settings(self, tenant_id: str) -> PostgresSettings | None:
        body = self.control.load_config(tenant_id, _KIND)
        return PostgresSettings(**body) if body else None  # type: ignore[arg-type]

    def status(self, tenant_id: str) -> StorageStatus:
        """Describe where this tenant's data lives (masked)."""
        pg = self._pg_settings(tenant_id)
        if pg is None:
            return StorageStatus(kind="sqlite", configured=False)
        return StorageStatus(
            kind="postgres",
            configured=True,
            host=pg.host,
            port=pg.port,
            database=pg.database,
            user=pg.user,
            sslmode=pg.sslmode,
            password_mask=self.control.secret_mask(tenant_id, STORAGE_PASSWORD_SECRET),
        )

    def configure(self, tenant_id: str, req: PostgresConfigRequest) -> StorageStatus:
        """Validate and save PostgreSQL settings (TLS required, host vetted); no connection made."""
        cfg = req.settings_only()
        try:
            validate_host(cfg.host, allow_private=self.settings.allow_private_endpoints)
        except UnsafeEndpointError as e:
            raise StorageUnavailableError(
                "storage_endpoint_blocked", "The database host is not allowed."
            ) from e
        has_pw = self.control.secret_mask(tenant_id, STORAGE_PASSWORD_SECRET) is not None
        if req.password is None and not has_pw:
            raise StorageUnavailableError(
                "storage_config_invalid", "A password is required to configure storage."
            )
        if req.password is not None:
            self.control.put_secret(
                tenant_id, STORAGE_PASSWORD_SECRET, req.password.get_secret_value()
            )
        self.control.save_config(tenant_id, _KIND, cfg.model_dump(mode="json"))
        return self.status(tenant_id)

    def disconnect(self, tenant_id: str) -> bool:
        """Forget the connection; the data in the user's own database is left untouched."""
        a = self.control.delete_config(tenant_id, _KIND)
        b = self.control.delete_secret(tenant_id, STORAGE_PASSWORD_SECRET)
        return a or b

    def _open_pg(self, tenant_id: str, settings: PostgresSettings, password: str) -> StorageSession:
        db = open_postgres(
            settings,
            password,
            allow_private=self.settings.allow_private_endpoints,
            connect_fn=self._connect_fn,
        )
        return StorageSession(db, tenant_id, "postgres")

    def open(self, tenant_id: str, *, require_initialized: bool = True) -> StorageSession:
        """Open this tenant's storage session (caller must close it)."""
        pg = self._pg_settings(tenant_id)
        if pg is None:
            try:
                return StorageSession(open_sqlite(self.settings.db_path), tenant_id, "sqlite")
            except StoreError as e:
                raise StorageUnavailableError(
                    "storage_unreachable", "The database could not be reached."
                ) from e
        try:
            password = self.control.get_secret(tenant_id, STORAGE_PASSWORD_SECRET)
        except SecretNotFoundError as e:
            raise StorageUnavailableError(
                "storage_config_invalid", "Storage password is missing."
            ) from e
        session = self._open_pg(tenant_id, pg, password)
        if require_initialized and not schema_ready(session.db):
            session.close()
            raise StorageUnavailableError(
                "storage_not_initialized", "Storage has not been initialized."
            )
        return session

    def initialize(self, tenant_id: str) -> int:
        """Create/upgrade the schema in the tenant's storage; returns the schema version."""
        session = self.open(tenant_id, require_initialized=False)
        try:
            return session.initialize()
        except StoreError as e:
            logger.warning("storage initialize failed: %s", type(e).__name__)
            raise StorageUnavailableError(
                "storage_migration_failed", "Storage initialization failed."
            ) from e
        finally:
            session.close()

    def test(
        self, tenant_id: str, candidate: PostgresConfigRequest | None = None
    ) -> ConnectionTestResult:
        """Connect with time limits and run SELECT 1; failures yield stable codes only."""
        started = time.perf_counter()
        session: StorageSession | None = None
        try:
            if candidate is not None:
                cfg = candidate.settings_only()
                if candidate.password is not None:
                    pw = candidate.password.get_secret_value()
                else:
                    pw = self.control.get_secret(tenant_id, STORAGE_PASSWORD_SECRET)
                session = self._open_pg(tenant_id, cfg, pw)
            else:
                session = self.open(tenant_id, require_initialized=False)
            session.check()
            ready = session.kind == "sqlite" or schema_ready(session.db)
        except SecretNotFoundError:
            return ConnectionTestResult(
                ok=False, code="storage_config_invalid", message="Storage password is missing."
            )
        except StorageUnavailableError as e:
            return ConnectionTestResult(ok=False, code=e.code, message=e.public_message)
        except StoreError:
            return ConnectionTestResult(
                ok=False, code="storage_unreachable", message="The database could not be reached."
            )
        finally:
            if session is not None:
                session.close()
        return ConnectionTestResult(
            ok=True,
            code="ok" if ready else "ok_not_initialized",
            message="Connection succeeded."
            if ready
            else "Connection succeeded; storage is not initialized yet.",
            latency_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )

    def delete_data(self, tenant_id: str) -> dict[str, int]:
        """Erase the tenant's memories and audit records from its storage."""
        session = self.open(tenant_id)
        try:
            return session.delete_tenant_data()
        finally:
            session.close()

    def export(self, tenant_id: str, limit: int = 10_000) -> dict[str, object]:
        """Return the tenant's data as JSON-ready content (bounded)."""
        session = self.open(tenant_id)
        try:
            return session.export_tenant_data(limit)
        finally:
            session.close()
