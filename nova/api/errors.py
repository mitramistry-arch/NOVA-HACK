"""Public error mapping: stable codes and safe messages; details go to server logs only."""

import logging
from dataclasses import dataclass

from nova.audit.logger import AuditError
from nova.core.tenant import InvalidTenantError
from nova.extraction.extractor import ExtractionError
from nova.identity.errors import (
    AuthenticationRequired,
    AuthorizationDenied,
    CSRFError,
    IdentityConfigError,
    IdentityError,
    IdentityMappingError,
    OIDCError,
    OriginRejected,
    RateLimited,
    TenantNotProvisioned,
)
from nova.providers.errors import (
    ProviderAuthError,
    ProviderBudgetError,
    ProviderConfigError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from nova.retrieval.embedder import EmbeddingError
from nova.retrieval.hybrid import RetrievalError
from nova.security.secrets import (
    SecretDecryptionError,
    SecretNotFoundError,
    SecretStoreUnavailableError,
)
from nova.store.base import (
    EmbeddingCompatibilityError,
    SchemaVersionError,
    StorageUnavailableError,
    StoreError,
    StoreLimitError,
)
from nova.verification.claim_splitter import VerificationError

logger = logging.getLogger("nova.api")


class PublicError(Exception):
    """An error that is safe to return to clients as-is."""

    def __init__(self, status: int, error: str, detail: str) -> None:
        """Create an error with an HTTP status, stable name, and safe detail."""
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


@dataclass(frozen=True)
class Mapped:
    """HTTP status, stable error name, and safe message for an exception."""

    status: int
    error: str
    detail: str


def map_exception(exc: BaseException) -> Mapped:
    """Translate any exception into a public (status, error, detail); unknowns become 500."""
    if isinstance(exc, PublicError):
        return Mapped(exc.status, exc.error, exc.detail)
    if isinstance(exc, RateLimited):
        return Mapped(429, exc.code, exc.public_message)
    if isinstance(exc, CSRFError | OriginRejected | AuthorizationDenied | IdentityMappingError):
        return Mapped(403, exc.code, exc.public_message)
    if isinstance(exc, AuthenticationRequired):
        return Mapped(401, exc.code, exc.public_message)
    if isinstance(exc, TenantNotProvisioned | IdentityConfigError):
        return Mapped(503, exc.code, exc.public_message)
    if isinstance(exc, OIDCError):
        return Mapped(502, exc.code, exc.public_message)
    if isinstance(exc, IdentityError):
        return Mapped(401, exc.code, exc.public_message)
    if isinstance(exc, ExtractionError):
        return Mapped(422, "ExtractionError", "The AI response could not be turned into memories.")
    if isinstance(exc, VerificationError):
        return Mapped(422, "VerificationError", "The answer could not be verified.")
    if isinstance(exc, ProviderNotConfiguredError):
        return Mapped(409, exc.code, exc.public_message)
    if isinstance(exc, ProviderConfigError):
        return Mapped(400, exc.code, exc.public_message)
    if isinstance(exc, ProviderBudgetError):
        return Mapped(413, exc.code, exc.public_message)
    if isinstance(exc, ProviderRateLimitError):
        return Mapped(429, exc.code, exc.public_message)
    if isinstance(exc, ProviderTimeoutError):
        return Mapped(504, exc.code, exc.public_message)
    if isinstance(exc, ProviderAuthError | ProviderResponseError):
        return Mapped(502, exc.code, exc.public_message)
    if isinstance(exc, ProviderError):
        return Mapped(502, exc.code, exc.public_message)
    if isinstance(exc, SecretStoreUnavailableError):
        return Mapped(503, "secret_store_unavailable", "Secret storage is not available.")
    if isinstance(exc, SecretNotFoundError | SecretDecryptionError):
        return Mapped(409, "secret_unavailable", "A stored credential is unavailable.")
    if isinstance(exc, StorageUnavailableError):
        return Mapped(502, exc.code, exc.public_message)
    if isinstance(exc, EmbeddingCompatibilityError):
        return Mapped(409, "embedding_incompatible", "Stored embeddings need to be regenerated.")
    if isinstance(exc, EmbeddingError):
        return Mapped(502, "embedding_failed", "Embedding generation failed.")
    if isinstance(exc, RetrievalError | InvalidTenantError):
        return Mapped(400, "invalid_request", "The request parameters are invalid.")
    if isinstance(exc, SchemaVersionError):
        return Mapped(503, exc.code, exc.public_message)
    if isinstance(exc, StoreLimitError):
        return Mapped(422, exc.code, exc.public_message)
    if isinstance(exc, StoreError):
        return Mapped(500, "StoreError", "A storage error occurred.")
    if isinstance(exc, AuditError):
        return Mapped(500, "AuditError", "An audit logging error occurred.")
    if isinstance(exc, NotImplementedError):
        return Mapped(501, "NotImplementedError", "This feature is not implemented.")
    return Mapped(500, "internal_error", "An internal error occurred.")
