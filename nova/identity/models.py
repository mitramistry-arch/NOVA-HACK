"""Immutable identity value objects shared by sessions, OIDC, and the API layer."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime

from nova.identity.roles import Permission, Role, permissions_for


@dataclass(frozen=True)
class IdentityClaims:
    """Normalized claims taken from a verified ID token; no vendor-specific fields."""

    subject: str
    issuer: str
    email: str | None = None
    groups: tuple[str, ...] = ()
    expires_at: datetime | None = None


@dataclass(frozen=True)
class TenantGrant:
    """The server-side tenant and role grant resolved for an authenticated subject."""

    tenant_id: str
    role: Role
    source: str


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, always derived server-side from a session record."""

    subject: str
    tenant_id: str
    role: Role
    email: str | None = None
    session_expires_at: datetime | None = None

    @property
    def permissions(self) -> frozenset[Permission]:
        """Return every permission this principal holds."""
        return permissions_for(self.role)

    def allows(self, permission: Permission) -> bool:
        """Return True when the principal may perform the operation."""
        return permission in self.permissions


@dataclass(frozen=True)
class SessionRecord:
    """A server-side session row; the raw session token is never stored."""

    token_hash: str
    subject: str
    tenant_id: str
    role: Role
    email: str | None
    csrf_hash: str
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime

    @property
    def principal(self) -> Principal:
        """Build the principal this session authenticates."""
        return Principal(
            subject=self.subject,
            tenant_id=self.tenant_id,
            role=self.role,
            email=self.email,
            session_expires_at=self.absolute_expires_at,
        )

    def verify_csrf(self, token: str) -> bool:
        """Compare a presented CSRF token against this session's stored hash."""
        if not token:
            return False
        return hmac.compare_digest(hash_csrf_token(token), self.csrf_hash)


@dataclass(frozen=True)
class IssuedSession:
    """A freshly minted session: raw values are returned once and never persisted."""

    token: str
    csrf_token: str
    record: SessionRecord


@dataclass(frozen=True)
class LoginTransaction:
    """Server-side state for one in-flight OIDC authorization request (single use)."""

    nonce: str
    code_verifier: str
    next_path: str
    expires_at: datetime


def hash_csrf_token(token: str) -> str:
    """Hash a CSRF token for storage; binding is by hash, never by plaintext."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "IdentityClaims",
    "IssuedSession",
    "LoginTransaction",
    "Principal",
    "SessionRecord",
    "TenantGrant",
    "hash_csrf_token",
]
