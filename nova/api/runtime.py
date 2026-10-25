"""Application runtime: shared services, authentication, readiness, and per-request wiring."""

import hmac
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from nova.api.errors import PublicError
from nova.audit.logger import AuditError
from nova.core.config import Settings
from nova.core.llm import LLMClient
from nova.core.tenant import DEFAULT_TENANT, InvalidTenantError, validate_tenant_id
from nova.extraction.extractor import LLMExtractor
from nova.identity.config import IdentitySettings
from nova.identity.service import IdentityService, backend_credentials
from nova.identity.session import SessionStore
from nova.pipeline import Anchor
from nova.providers.service import ProviderService
from nova.retrieval.embedder import HashEmbedder
from nova.retrieval.hybrid import HybridRetriever
from nova.security.secrets import AesGcmCipher, ControlStore, SecretStoreUnavailableError
from nova.store.base import StoreError
from nova.store.schema import open_sqlite
from nova.store.service import StorageService
from nova.store.timeutil import format_ts

logger = logging.getLogger("nova.api.runtime")


class LazyLLM:
    """Defers provider resolution until a completion is actually requested.

    Read-only endpoints therefore never need provider credentials, while any call that does
    need a model fails with a typed error instead of silently using a fake.
    """

    def __init__(self, providers: ProviderService, tenant_id: str) -> None:
        """Remember how to build the tenant's client."""
        self._providers = providers
        self._tenant_id = tenant_id
        self._client: LLMClient | None = None

    def complete(self, prompt: str, *, json: bool = False) -> str:
        """Resolve the tenant's provider on first use and delegate."""
        if self._client is None:
            self._client = self._providers.build_llm(self._tenant_id)
        return self._client.complete(prompt, json=json)

    @property
    def usage(self) -> Any:
        """Expose the underlying client's reported-usage tally, if it has one."""
        return getattr(self._client, "usage", None)

    def close(self) -> None:
        """Close the underlying client if it needs closing."""
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()


class Runtime:
    """Long-lived services for one settings snapshot."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        connect_fn: Any = None,
        identity_settings: IdentitySettings | None = None,
    ) -> None:
        """Open the control store and build the provider, storage, and identity services."""
        self.settings = settings
        self.identity_settings = identity_settings or IdentitySettings.from_env()
        self.identity: IdentityService | None = None
        cipher: AesGcmCipher | None = None
        if settings.encryption_key:
            try:
                cipher = AesGcmCipher(settings.encryption_key, settings.encryption_keys_previous)
            except SecretStoreUnavailableError:
                logger.error("NOVA_ENCRYPTION_KEY is invalid; secret operations are disabled")
        self.control = ControlStore(settings.control_db_path, cipher)
        try:
            self.providers = ProviderService(self.control, settings, transport=transport)
            self.storage = StorageService(self.control, settings, connect_fn=connect_fn)
            self.identity = self._build_identity()
        except BaseException:
            self.control.close()
            raise

    def _build_identity(self) -> IdentityService | None:
        """Build the browser identity service, failing closed when it is misconfigured."""
        if not self.identity_settings.browser_enabled:
            return None
        self.identity_settings.validate_for_environment(self.settings.environment)
        sessions = SessionStore(
            self.control.db,
            absolute_ttl=self.identity_settings.absolute_ttl,
            idle_ttl=self.identity_settings.idle_ttl,
        )
        logger.info(
            "browser identity enabled",
            extra={
                "issuer": self.identity_settings.issuer,
                "cookie_secure": self.identity_settings.cookie_secure,
            },
        )
        return IdentityService(
            self.identity_settings,
            sessions,
            tenant_credentials=backend_credentials(self.settings),
        )

    def close(self) -> None:
        """Release the control database connection."""
        self.control.close()

    def ready(self) -> dict[str, str]:
        """Check required dependencies; values are only 'ok' or 'fail' (no internals)."""
        checks: dict[str, str] = {}
        try:
            self.control.check()
            checks["control_store"] = "ok"
        except StoreError:
            checks["control_store"] = "fail"
        try:
            db = open_sqlite(self.settings.db_path)
            try:
                db.query("SELECT 1 AS ok")
            finally:
                db.close()
            checks["storage"] = "ok"
        except StoreError:
            checks["storage"] = "fail"
        return checks

    def purge_expired_sessions(self, now: datetime | None = None) -> int:
        """Delete expired and revoked browser sessions from the control store."""
        if self.identity is None:
            return 0
        return self.identity.sessions.purge_expired()

    def purge_expired_audit(self, now: datetime | None = None) -> int:
        """Prune expired audit records in shared SQLite and configured tenant stores."""
        days = self.settings.audit_retention_days
        if not days:
            return 0
        cutoff_time = (now or datetime.now(UTC)) - timedelta(days=days)
        cutoff = format_ts(cutoff_time)
        db = open_sqlite(self.settings.db_path)
        try:
            # The shared SQLite file holds data for every tenant that has not configured
            # external storage, so the retention delete is intentionally not tenant-filtered.
            deleted = db.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
        finally:
            db.close()

        # External stores are independent databases: a SQLite-only purge would otherwise leave
        # their audit records indefinitely. Continue across tenants if one remote store is down.
        configured = self.control.db.query(
            "SELECT tenant_id FROM integration_configs WHERE kind = ?", ("storage",)
        )
        for row in configured:
            session = None
            try:
                session = self.storage.open(str(row["tenant_id"]))
                deleted += session.audit.purge_older_than(cutoff_time)
            except (StoreError, AuditError) as exc:
                logger.warning(
                    "audit retention purge skipped for configured tenant storage (%s)",
                    type(exc).__name__,
                )
            finally:
                if session is not None:
                    session.close()
        return deleted

    @contextmanager
    def anchor_for(self, tenant_id: str) -> Iterator[Anchor]:
        """Yield an Anchor wired to the tenant's storage and provider; always cleans up."""
        session = self.storage.open(tenant_id)
        llm = LazyLLM(self.providers, tenant_id)
        try:
            retriever = HybridRetriever(session.memories, HashEmbedder())
            yield Anchor(
                store=session.memories,
                extractor=LLMExtractor(llm),
                retriever=retriever,
                audit_log=session.audit,
            )
        finally:
            llm.close()
            session.close()


def resolve_tenant(settings: Settings, api_key: str | None, tenant_header: str | None) -> str:
    """Authenticate the caller and resolve tenant ownership only from trusted configuration.

    Tenant-specific keys map to exactly one tenant; the optional tenant header is never an
    authority. A shared key is restricted to the default tenant. Keyless requests are allowed
    only in explicitly named development/test environments.
    """
    try:
        settings.validate_auth_configuration()
    except ValueError as exc:
        raise PublicError(
            503, "auth_not_configured", "API authentication is not safely configured."
        ) from exc

    supplied = api_key or ""
    if settings.tenant_api_keys:
        matched_tenant: str | None = None
        for key, tenant in settings.tenant_api_keys.items():
            if hmac.compare_digest(key.encode(), supplied.encode()):
                matched_tenant = tenant
        if matched_tenant is None:
            raise PublicError(401, "unauthorized", "Invalid or missing API key")
        return matched_tenant

    if settings.api_key:
        if not hmac.compare_digest(settings.api_key.encode(), supplied.encode()):
            raise PublicError(401, "unauthorized", "Invalid or missing API key")
        if tenant_header and tenant_header != DEFAULT_TENANT:
            raise PublicError(
                403, "tenant_key_mismatch", "This API key is restricted to the default tenant."
            )
        return DEFAULT_TENANT

    try:
        return validate_tenant_id(tenant_header or DEFAULT_TENANT)
    except InvalidTenantError as exc:
        raise PublicError(400, "invalid_tenant", "The tenant id is invalid.") from exc
