"""Typed browser-identity failures.

Messages are safe to log and never contain tokens, codes, or provider response bodies.
The API layer maps each class to a stable HTTP status and error code.
"""


class IdentityError(Exception):
    """Base class for browser-identity failures."""

    code = "identity_error"
    public_message = "Authentication failed."


class IdentityConfigError(IdentityError):
    """Browser identity is enabled but not safely configured."""

    code = "identity_not_configured"
    public_message = "Browser sign-in is not configured."


class AuthenticationRequired(IdentityError):
    """No valid session was presented."""

    code = "authentication_required"
    public_message = "Sign in to continue."


class SessionInvalid(AuthenticationRequired):
    """The session is unknown, expired, or revoked."""

    code = "session_invalid"
    public_message = "Your session has ended. Sign in again."

    def __init__(self, reason: str = "unknown") -> None:
        """Record a non-sensitive reason for server-side logs."""
        super().__init__(reason)
        self.reason = reason


class CSRFError(IdentityError):
    """The CSRF token is missing or does not match the active session."""

    code = "csrf_failed"
    public_message = "The request could not be verified. Reload and try again."


class OriginRejected(IdentityError):
    """The request origin or fetch metadata is not acceptable."""

    code = "origin_rejected"
    public_message = "The request did not come from this application."


class AuthorizationDenied(IdentityError):
    """The authenticated principal lacks the required permission."""

    code = "forbidden"
    public_message = "Your role does not allow this action."


class IdentityMappingError(IdentityError):
    """The authenticated subject has no tenant/role grant configured."""

    code = "identity_not_mapped"
    public_message = "This account is not registered for any workspace."


class TenantNotProvisioned(IdentityError):
    """The granted tenant has no server-side backend credential."""

    code = "tenant_not_provisioned"
    public_message = "This workspace is not provisioned for browser access."


class OIDCError(IdentityError):
    """An identity-provider discovery, token, or claim check failed."""

    code = "identity_provider_error"
    public_message = "Sign-in could not be completed."


class RateLimited(IdentityError):
    """Too many attempts in the configured window."""

    code = "rate_limited"
    public_message = "Too many requests. Try again shortly."

    def __init__(self, retry_after_seconds: int = 60) -> None:
        """Record how long the caller should wait before retrying."""
        super().__init__(self.public_message)
        self.retry_after_seconds = retry_after_seconds


__all__ = [
    "AuthenticationRequired",
    "AuthorizationDenied",
    "CSRFError",
    "IdentityConfigError",
    "IdentityError",
    "IdentityMappingError",
    "OIDCError",
    "OriginRejected",
    "RateLimited",
    "SessionInvalid",
    "TenantNotProvisioned",
]
