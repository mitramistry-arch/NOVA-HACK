"""Browser identity service: login, session cookies, CSRF, origin checks, and authorization."""

import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import Response

from nova.core.config import Settings
from nova.core.tenant import DEFAULT_TENANT
from nova.identity.config import IdentitySettings
from nova.identity.errors import (
    AuthenticationRequired,
    AuthorizationDenied,
    CSRFError,
    IdentityMappingError,
    OriginRejected,
    TenantNotProvisioned,
)
from nova.identity.models import (
    IdentityClaims,
    IssuedSession,
    Principal,
    SessionRecord,
    TenantGrant,
)
from nova.identity.oidc import (
    OIDCClient,
    code_challenge_for,
    new_code_verifier,
    new_nonce,
)
from nova.identity.ratelimit import SlidingWindowLimiter
from nova.identity.roles import Permission
from nova.identity.session import SessionStore

logger = logging.getLogger("nova.identity")

CSRF_HEADER = "x-csrf-token"
SAFE_NEXT_PATTERN = re.compile(r"^/(?!/)[A-Za-z0-9\-._~%/?#=&+]*$")


def backend_credentials(settings: Settings) -> dict[str, str]:
    """Map tenant ids to the server-held backend API keys used by the BFF."""
    if settings.tenant_api_keys:
        return {tenant: key for key, tenant in settings.tenant_api_keys.items()}
    if settings.api_key:
        return {DEFAULT_TENANT: settings.api_key}
    return {}


def safe_next_path(candidate: str | None, default: str = "/") -> str:
    """Accept only a same-origin absolute path as a post-login redirect target."""
    if not candidate:
        return default
    value = candidate.strip()
    if len(value) > 512 or not SAFE_NEXT_PATTERN.match(value):
        return default
    if "\\" in value or value.startswith("//"):
        return default
    return value


@dataclass(frozen=True)
class CookieNames:
    """Cookie names for the active security mode."""

    session: str
    csrf: str


def cookie_names(secure: bool) -> CookieNames:
    """Return ``__Host-`` prefixed cookies when Secure is enabled, dev names otherwise."""
    if secure:
        return CookieNames(session="__Host-nova_session", csrf="__Host-nova_csrf")
    return CookieNames(session="nova_session", csrf="nova_csrf")


class IdentityService:
    """Owns the browser authentication and authorization decisions for one deployment."""

    def __init__(
        self,
        settings: IdentitySettings,
        sessions: SessionStore,
        *,
        oidc: OIDCClient | None = None,
        tenant_credentials: Mapping[str, str] | None = None,
    ) -> None:
        """Wire the service; ``oidc`` and ``tenant_credentials`` are injectable for tests."""
        self.settings = settings
        self.sessions = sessions
        self.oidc = oidc or OIDCClient(settings)
        self._tenant_credentials = dict(tenant_credentials or {})
        self.cookies = cookie_names(settings.cookie_secure)
        self.login_limiter = SlidingWindowLimiter(settings.login_rate_per_minute)
        self.mutation_limiter = SlidingWindowLimiter(settings.mutation_rate_per_minute)

    # ---- login --------------------------------------------------------------------------
    def start_login(self, next_path: str | None) -> str:
        """Create a login transaction and return the provider authorization URL."""
        nonce = new_nonce()
        verifier = new_code_verifier()
        state = self.sessions.start_login(
            nonce=nonce, code_verifier=verifier, next_path=safe_next_path(next_path)
        )
        return self.oidc.authorization_url(
            state=state, nonce=nonce, code_challenge=code_challenge_for(verifier)
        )

    def complete_login(self, *, code: str, state: str) -> tuple[IssuedSession, str]:
        """Validate the callback, map identity to a tenant, and issue a session."""
        transaction = self.sessions.consume_login(state)
        claims = self.oidc.exchange_code(
            code=code, code_verifier=transaction.code_verifier, expected_nonce=transaction.nonce
        )
        grant = self.resolve_grant(claims)
        if grant.tenant_id not in self._tenant_credentials:
            logger.warning(
                "identity login rejected: tenant has no backend credential",
                extra={"tenant_id": grant.tenant_id},
            )
            raise TenantNotProvisioned()
        principal = Principal(
            subject=claims.subject,
            tenant_id=grant.tenant_id,
            role=grant.role,
            email=claims.email,
        )
        issued = self.sessions.create(principal)
        logger.info(
            "identity login succeeded",
            extra={
                "tenant_id": grant.tenant_id,
                "role": grant.role.value,
                "grant_source": grant.source,
                "subject_ref": _fingerprint(claims.subject),
            },
        )
        return issued, safe_next_path(transaction.next_path)

    def resolve_grant(self, claims: IdentityClaims) -> TenantGrant:
        """Resolve the server-side tenant and role grant for verified claims."""
        exact = self.settings.users.get(claims.subject)
        if exact is not None:
            return TenantGrant(exact.tenant_id, exact.role, "subject")
        users = self.settings.grants_for("user")
        for candidate in (claims.subject.lower(), (claims.email or "").lower()):
            if candidate and candidate in users:
                grant = users[candidate]
                return TenantGrant(grant.tenant_id, grant.role, "email")
        groups = self.settings.grants_for("group")
        for group in claims.groups:
            grant = groups.get(group.lower())
            if grant is not None:
                return TenantGrant(grant.tenant_id, grant.role, "group")
        raise IdentityMappingError()

    # ---- request authentication ---------------------------------------------------------
    def authenticate(self, request: Request) -> SessionRecord:
        """Return the session record for a request, raising when it is missing or invalid."""
        token = request.cookies.get(self.cookies.session)
        if not token:
            raise AuthenticationRequired()
        return self.sessions.load(token)

    def require(self, principal: Principal, permission: Permission) -> None:
        """Raise AuthorizationDenied when the principal may not perform the operation."""
        if not principal.allows(permission):
            logger.info(
                "authorization denied",
                extra={
                    "tenant_id": principal.tenant_id,
                    "role": principal.role.value,
                    "permission": permission.value,
                },
            )
            raise AuthorizationDenied()

    def enforce_origin(self, request: Request) -> None:
        """Reject state-changing requests that do not come from the configured origin."""
        fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise OriginRejected()
        origin = (request.headers.get("origin") or "").strip().rstrip("/")
        if not origin or not self.settings.bff_origin:
            raise OriginRejected()
        if not hmac.compare_digest(origin.lower(), self.settings.bff_origin.lower()):
            raise OriginRejected()

    def enforce_csrf(self, request: Request, record: SessionRecord) -> None:
        """Require the double-submit token to match both the cookie and the session."""
        present = request.headers.get(CSRF_HEADER) or ""
        cookie = request.cookies.get(self.cookies.csrf) or ""
        if not present or not cookie:
            raise CSRFError()
        if not hmac.compare_digest(present, cookie):
            raise CSRFError()
        if not record.verify_csrf(present):
            raise CSRFError()

    def enforce_mutation(self, request: Request, record: SessionRecord) -> None:
        """Apply origin, CSRF, and per-session rate limits to a state-changing request."""
        self.enforce_origin(request)
        self.enforce_csrf(request, record)
        self.mutation_limiter.hit(record.token_hash)

    def limit_login(self, request: Request) -> None:
        """Rate limit sign-in attempts by peer address (proxy headers are not trusted)."""
        client = request.client.host if request.client else "unknown"
        self.login_limiter.hit(client)

    # ---- proxy credentials --------------------------------------------------------------
    def tenant_credential(self, tenant_id: str) -> str:
        """Return the server-held backend credential for a tenant, never a browser value."""
        key = self._tenant_credentials.get(tenant_id)
        if not key:
            raise TenantNotProvisioned()
        return key

    # ---- cookies ------------------------------------------------------------------------
    def set_session_cookies(self, response: Response, issued: IssuedSession) -> None:
        """Attach the session and CSRF cookies with hardened attributes."""
        secure = self.settings.cookie_secure
        max_age = self.settings.absolute_ttl_seconds
        response.set_cookie(
            self.cookies.session,
            issued.token,
            max_age=max_age,
            path="/",
            secure=secure,
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            self.cookies.csrf,
            issued.csrf_token,
            max_age=max_age,
            path="/",
            secure=secure,
            httponly=False,
            samesite="lax",
        )

    def clear_session_cookies(self, response: Response) -> None:
        """Expire both cookies."""
        secure = self.settings.cookie_secure
        for name, http_only in (
            (self.cookies.session, True),
            (self.cookies.csrf, False),
        ):
            response.delete_cookie(
                name, path="/", secure=secure, httponly=http_only, samesite="lax"
            )

    def logout(self, request: Request) -> None:
        """Revoke the caller's session if one is presented."""
        token = request.cookies.get(self.cookies.session)
        if token:
            self.sessions.revoke(token)


def _fingerprint(value: str) -> str:
    """Return a short, non-reversible reference for log correlation."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


__all__ = [
    "CSRF_HEADER",
    "CookieNames",
    "IdentityService",
    "backend_credentials",
    "cookie_names",
    "safe_next_path",
]
