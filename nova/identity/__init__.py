"""Browser identity: vendor-neutral OIDC, opaque sessions, roles, and CSRF-protected BFF state."""

from nova.identity.config import IdentityMode, IdentitySettings
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
    SessionInvalid,
    TenantNotProvisioned,
)
from nova.identity.models import (
    IdentityClaims,
    IssuedSession,
    Principal,
    SessionRecord,
    TenantGrant,
)
from nova.identity.roles import ROLE_PERMISSIONS, Permission, Role, permissions_for, role_allows
from nova.identity.service import IdentityService, backend_credentials, cookie_names, safe_next_path
from nova.identity.session import SessionStore

__all__ = [
    "ROLE_PERMISSIONS",
    "AuthenticationRequired",
    "AuthorizationDenied",
    "CSRFError",
    "IdentityClaims",
    "IdentityConfigError",
    "IdentityError",
    "IdentityMappingError",
    "IdentityMode",
    "IdentityService",
    "IdentitySettings",
    "IssuedSession",
    "OIDCError",
    "OriginRejected",
    "Permission",
    "Principal",
    "RateLimited",
    "Role",
    "SessionInvalid",
    "SessionRecord",
    "SessionStore",
    "TenantGrant",
    "TenantNotProvisioned",
    "backend_credentials",
    "cookie_names",
    "permissions_for",
    "role_allows",
    "safe_next_path",
]
