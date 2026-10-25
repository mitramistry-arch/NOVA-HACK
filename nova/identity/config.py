"""Browser-identity configuration, validated fail-closed for production deployments.

Kept beside the identity package rather than in ``nova.core.config`` so the frozen core contract is
untouched; the API layer reads it at startup and refuses to serve a browser surface without it.
"""

import os
from datetime import timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from nova.core.config import ConfigError
from nova.core.tenant import InvalidTenantError, validate_tenant_id
from nova.identity.roles import Role, parse_role

DEFAULT_ABSOLUTE_SECONDS = 8 * 60 * 60
DEFAULT_IDLE_SECONDS = 30 * 60
DEFAULT_LOGIN_RATE_PER_MINUTE = 10
DEFAULT_MUTATION_RATE_PER_MINUTE = 60
DEFAULT_PROXY_TIMEOUT_SECONDS = 60.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
DEFAULT_JWKS_CACHE_SECONDS = 300
PRODUCTION_ENVIRONMENTS = {"production", "prod"}


class IdentityMode(StrEnum):
    """How browser clients authenticate; API-key clients always use the direct API."""

    API_KEY = "api_key"
    OIDC = "oidc"


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable, rejecting typos instead of guessing."""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean value")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Parse a bounded integer environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    """Parse a bounded float environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_origin(value: str | None, name: str) -> str | None:
    """Normalize a configured browser origin (scheme, host, optional port; no path)."""
    if not value or not value.strip():
        return None
    raw = value.strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ConfigError(f"{name} must be an absolute http(s) origin")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"{name} must contain only scheme, host, and port")
    return f"{parts.scheme}://{parts.netloc}"


class TenantGrantConfig(BaseModel):
    """A static server-side grant mapping an identity to one tenant and role."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    role: Role


def _parse_grants(raw: str | None, name: str) -> dict[str, TenantGrantConfig]:
    """Parse ``identity:tenant:role`` triples separated by commas."""
    grants: dict[str, TenantGrantConfig] = {}
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        parts = entry.rsplit(":", 2)
        if len(parts) != 3:
            raise ConfigError(f"{name} entries must be identity:tenant:role")
        identity, tenant, role_text = (part.strip() for part in parts)
        if not identity or not tenant or not role_text:
            raise ConfigError(f"{name} entries must be identity:tenant:role")
        try:
            validate_tenant_id(tenant)
        except InvalidTenantError as exc:
            raise ConfigError(f"{name} contains an invalid tenant id") from exc
        try:
            role = parse_role(role_text)
        except ValueError as exc:
            raise ConfigError(f"{name} contains an invalid role") from exc
        if identity in grants:
            raise ConfigError(f"{name} contains a duplicate identity")
        grants[identity] = TenantGrantConfig(tenant_id=tenant, role=role)
    return grants


class IdentitySettings(BaseModel):
    """Browser identity, session, cookie, and BFF proxy settings from the environment."""

    model_config = ConfigDict(extra="forbid")

    mode: IdentityMode = IdentityMode.API_KEY
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = Field(default=None, repr=False)
    redirect_uri: str | None = None
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    bff_origin: str | None = None
    cookie_secure: bool = True
    absolute_ttl_seconds: int = DEFAULT_ABSOLUTE_SECONDS
    idle_ttl_seconds: int = DEFAULT_IDLE_SECONDS
    users: dict[str, TenantGrantConfig] = Field(default_factory=dict)
    groups: dict[str, TenantGrantConfig] = Field(default_factory=dict)
    groups_claim: str = "groups"
    email_claim: str = "email"
    allowed_algs: tuple[str, ...] = ("RS256", "ES256", "PS256")
    jwks_cache_seconds: int = DEFAULT_JWKS_CACHE_SECONDS
    login_rate_per_minute: int = DEFAULT_LOGIN_RATE_PER_MINUTE
    mutation_rate_per_minute: int = DEFAULT_MUTATION_RATE_PER_MINUTE
    proxy_timeout_seconds: float = DEFAULT_PROXY_TIMEOUT_SECONDS
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    allow_private_endpoints: bool = False

    @classmethod
    def from_env(cls) -> "IdentitySettings":
        """Load identity settings from the environment, rejecting malformed values."""
        raw_mode = (os.getenv("NOVA_IDENTITY_MODE", "api_key") or "api_key").strip().lower()
        try:
            mode = IdentityMode(raw_mode)
        except ValueError as exc:
            raise ConfigError("NOVA_IDENTITY_MODE must be api_key or oidc") from exc
        scopes = tuple(
            scope
            for scope in (
                os.getenv("NOVA_OIDC_SCOPES", "openid,email,profile") or "openid,email,profile"
            )
            .replace(" ", ",")
            .split(",")
            if scope
        )
        algorithms = tuple(
            alg.strip().upper()
            for alg in (os.getenv("NOVA_OIDC_ALLOWED_ALGS", "RS256,ES256,PS256")).split(",")
            if alg.strip()
        )
        return cls(
            mode=mode,
            issuer=(os.getenv("NOVA_OIDC_ISSUER") or None),
            client_id=(os.getenv("NOVA_OIDC_CLIENT_ID") or None),
            client_secret=(os.getenv("NOVA_OIDC_CLIENT_SECRET") or None),
            redirect_uri=(os.getenv("NOVA_OIDC_REDIRECT_URI") or None),
            scopes=scopes,
            bff_origin=_parse_origin(os.getenv("NOVA_BFF_ORIGIN"), "NOVA_BFF_ORIGIN"),
            cookie_secure=_env_bool("NOVA_SESSION_COOKIE_SECURE", True),
            absolute_ttl_seconds=_env_int(
                "NOVA_SESSION_ABSOLUTE_SECONDS",
                DEFAULT_ABSOLUTE_SECONDS,
                minimum=300,
                maximum=86_400,
            ),
            idle_ttl_seconds=_env_int(
                "NOVA_SESSION_IDLE_SECONDS", DEFAULT_IDLE_SECONDS, minimum=60, maximum=86_400
            ),
            users=_parse_grants(os.getenv("NOVA_IDENTITY_USERS"), "NOVA_IDENTITY_USERS"),
            groups=_parse_grants(os.getenv("NOVA_IDENTITY_GROUPS"), "NOVA_IDENTITY_GROUPS"),
            groups_claim=(os.getenv("NOVA_OIDC_GROUPS_CLAIM") or "groups").strip(),
            email_claim=(os.getenv("NOVA_OIDC_EMAIL_CLAIM") or "email").strip(),
            allowed_algs=algorithms,
            jwks_cache_seconds=_env_int(
                "NOVA_OIDC_JWKS_CACHE_SECONDS",
                DEFAULT_JWKS_CACHE_SECONDS,
                minimum=30,
                maximum=86_400,
            ),
            login_rate_per_minute=_env_int(
                "NOVA_BFF_LOGIN_RATE_PER_MINUTE",
                DEFAULT_LOGIN_RATE_PER_MINUTE,
                minimum=1,
                maximum=600,
            ),
            mutation_rate_per_minute=_env_int(
                "NOVA_BFF_MUTATION_RATE_PER_MINUTE",
                DEFAULT_MUTATION_RATE_PER_MINUTE,
                minimum=1,
                maximum=6_000,
            ),
            proxy_timeout_seconds=_env_float(
                "NOVA_BFF_PROXY_TIMEOUT_SECONDS",
                DEFAULT_PROXY_TIMEOUT_SECONDS,
                minimum=1.0,
                maximum=300.0,
            ),
            http_timeout_seconds=_env_float(
                "NOVA_OIDC_HTTP_TIMEOUT_SECONDS",
                DEFAULT_HTTP_TIMEOUT_SECONDS,
                minimum=1.0,
                maximum=60.0,
            ),
            allow_private_endpoints=_env_bool("NOVA_ALLOW_PRIVATE_ENDPOINTS", False),
        )

    @property
    def absolute_ttl(self) -> timedelta:
        """Return the absolute session lifetime."""
        return timedelta(seconds=self.absolute_ttl_seconds)

    @property
    def idle_ttl(self) -> timedelta:
        """Return the idle session lifetime."""
        return timedelta(seconds=self.idle_ttl_seconds)

    @property
    def browser_enabled(self) -> bool:
        """Return True when the BFF and browser sessions should be mounted."""
        return self.mode is IdentityMode.OIDC

    def grants_for(self, kind: str) -> dict[str, TenantGrantConfig]:
        """Return the case-insensitive index of a grant map."""
        source = self.users if kind == "user" else self.groups
        return {key.lower(): grant for key, grant in source.items()}

    def validate_for_environment(self, environment: str) -> None:
        """Raise ConfigError when browser identity is enabled but unsafe or incomplete."""
        production = environment.strip().lower() in PRODUCTION_ENVIRONMENTS
        if not self.browser_enabled:
            if self.cookie_secure is False and production:
                raise ConfigError("NOVA_SESSION_COOKIE_SECURE must not be disabled in production")
            return
        problems: list[str] = []
        if not self.issuer:
            problems.append("NOVA_OIDC_ISSUER is required when NOVA_IDENTITY_MODE=oidc")
        if not self.client_id:
            problems.append("NOVA_OIDC_CLIENT_ID is required when NOVA_IDENTITY_MODE=oidc")
        if not self.client_secret:
            problems.append("NOVA_OIDC_CLIENT_SECRET is required when NOVA_IDENTITY_MODE=oidc")
        if not self.redirect_uri:
            problems.append("NOVA_OIDC_REDIRECT_URI is required when NOVA_IDENTITY_MODE=oidc")
        if not self.bff_origin:
            problems.append("NOVA_BFF_ORIGIN is required when NOVA_IDENTITY_MODE=oidc")
        if "openid" not in self.scopes:
            problems.append("NOVA_OIDC_SCOPES must include openid")
        if not self.allowed_algs:
            problems.append("NOVA_OIDC_ALLOWED_ALGS must list at least one asymmetric algorithm")
        if "none" in self.allowed_algs:
            problems.append("NOVA_OIDC_ALLOWED_ALGS must never include none")
        if production and not self.cookie_secure:
            problems.append("NOVA_SESSION_COOKIE_SECURE must be enabled in production")
        if production and not self.users and not self.groups:
            problems.append(
                "NOVA_IDENTITY_USERS or NOVA_IDENTITY_GROUPS must grant at least one workspace"
            )
        if self.issuer:
            problems.extend(_url_problems(self.issuer, "NOVA_OIDC_ISSUER", production))
        if self.redirect_uri:
            problems.extend(_url_problems(self.redirect_uri, "NOVA_OIDC_REDIRECT_URI", production))
            if self.bff_origin:
                origin = urlsplit(self.redirect_uri)
                if f"{origin.scheme}://{origin.netloc}" != self.bff_origin:
                    problems.append("NOVA_OIDC_REDIRECT_URI must share the NOVA_BFF_ORIGIN origin")
        if self.bff_origin:
            problems.extend(_url_problems(self.bff_origin, "NOVA_BFF_ORIGIN", production))
        if problems:
            raise ConfigError("; ".join(problems))


def _url_problems(value: str, name: str, production: bool) -> list[str]:
    """Return validation problems for an absolute endpoint URL."""
    parts = urlsplit(value)
    problems: list[str] = []
    if not parts.hostname:
        problems.append(f"{name} must be an absolute URL with a host")
    if parts.scheme not in {"https", "http"}:
        problems.append(f"{name} must use http or https")
    if production and parts.scheme != "https":
        problems.append(f"{name} must use https in production")
    if parts.username or parts.password:
        problems.append(f"{name} must not embed credentials")
    if parts.fragment:
        problems.append(f"{name} must not contain a fragment")
    return problems


__all__ = ["IdentityMode", "IdentitySettings", "TenantGrantConfig"]
