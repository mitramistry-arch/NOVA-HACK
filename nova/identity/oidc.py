"""Vendor-neutral OpenID Connect authorization-code flow with PKCE.

Only the standard OpenID Provider metadata, token, and JWKS endpoints are used, so any conformant
provider can be configured. Signature verification is delegated to PyJWT; claim and nonce checks
are enforced here. Nothing in this module logs or returns a raw token, code, or client secret.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
import jwt

from nova.identity.config import IdentitySettings
from nova.identity.errors import OIDCError
from nova.identity.models import IdentityClaims
from nova.security.net import UnsafeEndpointError, validate_https_url

logger = logging.getLogger("nova.identity.oidc")

CLOCK_LEEWAY_SECONDS = 60
MIN_JWKS_REFETCH_SECONDS = 10
STATE_BYTES = 32
VERIFIER_BYTES = 64


def _b64url(raw: bytes) -> str:
    """Return unpadded base64url text (RFC 7636 encoding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def code_challenge_for(verifier: str) -> str:
    """Return the S256 PKCE challenge for a code verifier."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_nonce() -> str:
    """Return a fresh, high-entropy OIDC nonce value."""
    return secrets.token_urlsafe(STATE_BYTES)


def new_code_verifier() -> str:
    """Return a PKCE code verifier of the maximum recommended length."""
    return secrets.token_urlsafe(VERIFIER_BYTES)


class OIDCClient:
    """Talks to one configured OpenID Provider and verifies its ID tokens."""

    def __init__(
        self,
        settings: IdentitySettings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind to the configured provider; ``transport`` is a test seam for a fake IdP."""
        self.settings = settings
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._metadata: dict[str, Any] | None = None
        self._metadata_fetched_at: datetime | None = None
        self._jwks: dict[str, dict[str, Any]] = {}
        self._jwks_fetched_at: datetime | None = None

    # ---- endpoints ----------------------------------------------------------------------
    def _validate_endpoint(self, url: str, label: str) -> str:
        """Reject unsafe discovered endpoints (SSRF, credentials, non-TLS in production)."""
        try:
            return validate_https_url(
                url,
                allow_private=self.settings.allow_private_endpoints,
                resolve=self._transport is None,
            )
        except UnsafeEndpointError as exc:
            raise OIDCError(f"{label} is not an acceptable endpoint") from exc

    def _client(self) -> httpx.Client:
        """Return an HTTP client with redirects disabled and a bounded timeout."""
        return httpx.Client(
            timeout=self.settings.http_timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
            headers={"accept": "application/json"},
        )

    def metadata(self) -> dict[str, Any]:
        """Return cached OpenID Provider metadata, discovering it when stale."""
        with self._lock:
            if self._metadata is not None and self._metadata_fresh(self._metadata_fetched_at):
                return self._metadata
        if not self.settings.issuer:
            raise OIDCError("issuer is not configured")
        discovery_base = self.settings.issuer.rstrip("/") + "/"
        discovery_url = urljoin(discovery_base, ".well-known/openid-configuration")
        self._validate_endpoint(discovery_url, "discovery endpoint")
        try:
            with self._client() as client:
                response = client.get(discovery_url)
        except httpx.HTTPError as exc:
            raise OIDCError("identity provider discovery failed") from exc
        if response.status_code != 200:
            raise OIDCError("identity provider discovery failed")
        try:
            body = response.json()
        except ValueError as exc:
            raise OIDCError("identity provider metadata is not JSON") from exc
        if not isinstance(body, dict):
            raise OIDCError("identity provider metadata is invalid")
        if str(body.get("issuer", "")).rstrip("/") != str(self.settings.issuer).rstrip("/"):
            raise OIDCError("identity provider issuer does not match configuration")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = body.get(field)
            if not isinstance(value, str) or not value:
                raise OIDCError(f"identity provider metadata is missing {field}")
            self._validate_endpoint(value, field)
        with self._lock:
            self._metadata = body
            self._metadata_fetched_at = self._clock()
        return body

    def _metadata_fresh(self, fetched_at: datetime | None) -> bool:
        """Return True while cached metadata is inside its TTL."""
        if fetched_at is None:
            return False
        age = (self._clock() - fetched_at).total_seconds()
        return age < self.settings.jwks_cache_seconds

    # ---- authorization request ----------------------------------------------------------
    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        """Build the provider authorization URL for a confidential-client code flow."""
        metadata = self.metadata()
        if not self.settings.client_id or not self.settings.redirect_uri:
            raise OIDCError("client configuration is incomplete")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": " ".join(self.settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{metadata['authorization_endpoint']}?{query}"

    # ---- token exchange -----------------------------------------------------------------
    def exchange_code(
        self, *, code: str, code_verifier: str, expected_nonce: str
    ) -> IdentityClaims:
        """Exchange an authorization code and return verified identity claims."""
        metadata = self.metadata()
        if not self.settings.client_id or not self.settings.client_secret:
            raise OIDCError("client configuration is incomplete")
        if not self.settings.redirect_uri:
            raise OIDCError("redirect URI is not configured")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": code_verifier,
        }
        try:
            with self._client() as client:
                response = client.post(
                    metadata["token_endpoint"],
                    data=form,
                    auth=(self.settings.client_id, self.settings.client_secret),
                )
        except httpx.HTTPError as exc:
            raise OIDCError("token exchange failed") from exc
        if response.status_code != 200:
            raise OIDCError(f"token exchange rejected ({response.status_code})")
        try:
            body = response.json()
        except ValueError as exc:
            raise OIDCError("token response is not JSON") from exc
        if not isinstance(body, dict):
            raise OIDCError("token response is invalid")
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OIDCError("token response has no id_token")
        token_type = str(body.get("token_type", "")).lower()
        if token_type and token_type != "bearer":
            raise OIDCError("token response has an unexpected token_type")
        return self.verify_id_token(id_token, expected_nonce=expected_nonce)

    # ---- ID token verification ----------------------------------------------------------
    def verify_id_token(self, token: str, *, expected_nonce: str) -> IdentityClaims:
        """Verify signature, issuer, audience, lifetime, and nonce of an ID token."""
        if not token or len(token) > 20_000:
            raise OIDCError("id_token is malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OIDCError("id_token header is invalid") from exc
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in self.settings.allowed_algs:
            raise OIDCError("id_token algorithm is not allowed")
        key = self._signing_key(algorithm, header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.settings.client_id,
                issuer=self.settings.issuer,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise OIDCError("id_token verification failed") from exc
        self._check_nonce(claims, expected_nonce)
        self._check_authorized_party(claims)
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise OIDCError("id_token subject is missing")
        return IdentityClaims(
            subject=subject,
            issuer=str(claims.get("iss", "")),
            email=self._email(claims),
            groups=self._groups(claims),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC),
        )

    def _check_nonce(self, claims: Mapping[str, Any], expected_nonce: str) -> None:
        """Reject a token that was not minted for this authorization request."""
        actual = claims.get("nonce")
        if not isinstance(actual, str) or not actual or not expected_nonce:
            raise OIDCError("id_token nonce is missing")
        if not hmac.compare_digest(actual, expected_nonce):
            raise OIDCError("id_token nonce does not match")

    def _check_authorized_party(self, claims: Mapping[str, Any]) -> None:
        """Reject a token whose authorized party is a different client."""
        audience = claims.get("aud")
        if (
            isinstance(audience, list)
            and len(audience) > 1
            and claims.get("azp") != self.settings.client_id
        ):
            raise OIDCError("id_token authorized party does not match")

    def _email(self, claims: Mapping[str, Any]) -> str | None:
        """Return the email claim when present and plausible."""
        value = claims.get(self.settings.email_claim)
        if isinstance(value, str) and value.strip() and len(value) <= 320:
            return value.strip()
        return None

    def _groups(self, claims: Mapping[str, Any]) -> tuple[str, ...]:
        """Return group claims as a bounded tuple of strings."""
        value = claims.get(self.settings.groups_claim)
        if isinstance(value, str):
            return tuple(part for part in value.split() if part)[:64]
        if isinstance(value, list):
            return tuple(str(item) for item in value if isinstance(item, str))[:64]
        return ()

    # ---- signing keys -------------------------------------------------------------------
    def _signing_key(self, algorithm: str, kid: Any) -> Any:
        """Return the verification key for a token header, refreshing JWKS once if unknown."""
        keys = self._keys()
        key = self._select_key(keys, kid)
        if key is None:
            keys = self._keys(force=True)
            key = self._select_key(keys, kid)
        if key is None:
            raise OIDCError("id_token signing key is unknown")
        try:
            return jwt.PyJWK(key, algorithm=algorithm).key
        except jwt.PyJWTError as exc:
            raise OIDCError("id_token signing key is unusable") from exc

    @staticmethod
    def _select_key(keys: Mapping[str, dict[str, Any]], kid: Any) -> dict[str, Any] | None:
        """Return the JWK matching a key id, or the only key when the id is absent."""
        if not keys:
            return None
        if isinstance(kid, str) and kid in keys:
            return keys[kid]
        if len(keys) == 1 and kid is None:
            return next(iter(keys.values()))
        return None

    def _keys(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        """Return cached JWKS keys, refreshing on demand with a minimum interval."""
        with self._lock:
            if force and self._jwks_fetched_at is not None:
                age = (self._clock() - self._jwks_fetched_at).total_seconds()
                if age < MIN_JWKS_REFETCH_SECONDS:
                    return self._jwks
            if self._jwks and not force and self._metadata_fresh(self._jwks_fetched_at):
                return self._jwks
        jwks_uri = str(self.metadata()["jwks_uri"])
        try:
            with self._client() as client:
                response = client.get(jwks_uri)
        except httpx.HTTPError as exc:
            raise OIDCError("signing key download failed") from exc
        if response.status_code != 200:
            raise OIDCError("signing key download failed")
        try:
            body = response.json()
        except ValueError as exc:
            raise OIDCError("signing keys are not JSON") from exc
        raw_keys = body.get("keys") if isinstance(body, dict) else None
        if not isinstance(raw_keys, list) or not raw_keys:
            raise OIDCError("signing key set is empty")
        keys: dict[str, dict[str, Any]] = {}
        for entry in raw_keys:
            if not isinstance(entry, dict):
                continue
            if entry.get("use") not in (None, "sig"):
                continue
            kid = entry.get("kid")
            if isinstance(kid, str) and kid:
                keys[kid] = entry
            elif len(raw_keys) == 1:
                keys[""] = entry
        if not keys:
            raise OIDCError("signing key set has no usable keys")
        with self._lock:
            self._jwks = keys
            self._jwks_fetched_at = self._clock()
        return keys


__all__ = [
    "OIDCClient",
    "code_challenge_for",
    "new_code_verifier",
    "new_nonce",
]
