"""OIDC tests against a locally signed fake provider (no network, no real identity vendor)."""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from nova.identity.config import IdentitySettings
from nova.identity.errors import OIDCError
from nova.identity.oidc import OIDCClient, code_challenge_for, new_code_verifier

ISSUER = "https://idp.example.test"
CLIENT_ID = "nova-workspace"
REDIRECT_URI = "https://app.example.test/api/bff/callback"


def _b64(raw: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwk_for(key: Any, kid: str) -> dict[str, Any]:
    """Export the public half of an RSA or EC key as a JWK."""
    public = key.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        numbers = public.public_numbers()
        return {
            "kty": "RSA",
            "kid": kid,
            "use": "sig",
            "alg": "RS256",
            "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }
    numbers_ec = public.public_numbers()
    size = (public.curve.key_size + 7) // 8
    return {
        "kty": "EC",
        "kid": kid,
        "use": "sig",
        "alg": "ES256",
        "crv": "P-256",
        "x": _b64(numbers_ec.x.to_bytes(size, "big")),
        "y": _b64(numbers_ec.y.to_bytes(size, "big")),
    }


def _pem(key: Any) -> bytes:
    """Serialize a private key to PEM for signing."""
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class FakeProvider:
    """Minimal OpenID Provider: discovery, JWKS, and token endpoints over a mock transport."""

    def __init__(self) -> None:
        """Create a signing key and the counters used by the assertions."""
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwks_calls = 0
        self.token_status = 200
        self.token_body: dict[str, Any] = {}
        self.claim_overrides: dict[str, Any] = {}
        self.issuer = ISSUER
        self.extra_keys: list[dict[str, Any]] = []

    def id_token(
        self, *, nonce: str, key: Any = None, algorithm: str = "RS256", kid: str = "k1"
    ) -> str:
        """Mint a signed ID token with the given nonce and overrides."""
        now = datetime.now(UTC)
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "user-123",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "nonce": nonce,
            "email": "User@Example.test",
            "groups": ["team-alpha"],
        }
        claims.update(self.claim_overrides)
        return jwt.encode(claims, key or self.key, algorithm=algorithm, headers={"kid": kid})

    def transport(self) -> httpx.MockTransport:
        """Return a mock transport serving this provider's endpoints."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/.well-known/openid-configuration":
                return httpx.Response(
                    200,
                    json={
                        "issuer": self.issuer,
                        "authorization_endpoint": f"{self.issuer}/authorize",
                        "token_endpoint": f"{self.issuer}/token",
                        "jwks_uri": f"{self.issuer}/jwks",
                    },
                )
            if path == "/jwks":
                self.jwks_calls += 1
                return httpx.Response(
                    200, json={"keys": [_jwk_for(self.key, "k1"), *self.extra_keys]}
                )
            if path == "/token":
                if self.token_status != 200:
                    return httpx.Response(self.token_status, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self.token_body)
            return httpx.Response(404, json={})

        return httpx.MockTransport(handler)


def _settings(**overrides: Any) -> IdentitySettings:
    """Build OIDC settings for the fake provider."""
    base: dict[str, Any] = {
        "mode": "oidc",
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "client-secret-value",
        "redirect_uri": REDIRECT_URI,
        "bff_origin": "https://app.example.test",
    }
    base.update(overrides)
    return IdentitySettings(**base)


@pytest.fixture
def provider() -> FakeProvider:
    """Provide a fresh fake provider."""
    return FakeProvider()


@pytest.fixture
def client(provider: FakeProvider) -> OIDCClient:
    """Provide an OIDC client wired to the fake provider."""
    return OIDCClient(_settings(), transport=provider.transport())


def test_pkce_challenge_matches_rfc7636_vector() -> None:
    """S256 challenge derivation matches the published RFC 7636 example."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_code_verifier_is_long_and_random() -> None:
    """PKCE verifiers are unique and within the RFC length range."""
    verifiers = {new_code_verifier() for _ in range(20)}
    assert len(verifiers) == 20
    assert all(43 <= len(value) <= 128 for value in verifiers)


def test_authorization_url_contains_required_parameters(client: OIDCClient) -> None:
    """The authorization request carries state, nonce, and an S256 PKCE challenge."""
    url = client.authorization_url(state="state-1", nonce="nonce-1", code_challenge="challenge-1")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{ISSUER}/authorize"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["state"] == ["state-1"]
    assert query["nonce"] == ["nonce-1"]
    assert query["code_challenge"] == ["challenge-1"]
    assert query["code_challenge_method"] == ["S256"]
    assert "openid" in query["scope"][0].split()


def test_exchange_code_returns_normalized_claims(
    client: OIDCClient, provider: FakeProvider
) -> None:
    """A valid code exchange yields vendor-neutral claims."""
    provider.token_body = {"id_token": provider.id_token(nonce="nonce-1"), "token_type": "Bearer"}
    claims = client.exchange_code(code="code-1", code_verifier="verifier", expected_nonce="nonce-1")
    assert claims.subject == "user-123"
    assert claims.issuer == ISSUER
    assert claims.email == "User@Example.test"
    assert claims.groups == ("team-alpha",)
    assert claims.expires_at is not None


def test_nonce_mismatch_is_rejected(client: OIDCClient, provider: FakeProvider) -> None:
    """A token minted for another authorization request is refused (replay defence)."""
    provider.token_body = {
        "id_token": provider.id_token(nonce="other-nonce"),
        "token_type": "Bearer",
    }
    with pytest.raises(OIDCError):
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")


def test_missing_nonce_is_rejected(client: OIDCClient, provider: FakeProvider) -> None:
    """Tokens without a nonce claim are refused."""
    provider.claim_overrides = {"nonce": ""}
    provider.token_body = {"id_token": provider.id_token(nonce=""), "token_type": "Bearer"}
    with pytest.raises(OIDCError):
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")


def test_tampered_signature_is_rejected(client: OIDCClient, provider: FakeProvider) -> None:
    """A token signed by an unknown key does not verify."""
    forged = provider.id_token(nonce="nonce-1", key=provider.other_key)
    provider.token_body = {"id_token": forged, "token_type": "Bearer"}
    with pytest.raises(OIDCError):
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")


def test_unsigned_algorithm_is_rejected(client: OIDCClient, provider: FakeProvider) -> None:
    """alg=none (and any algorithm outside the allowlist) never verifies."""
    unsigned = jwt.encode({"sub": "attacker", "nonce": "nonce-1"}, key=None, algorithm="none")
    provider.token_body = {"id_token": unsigned, "token_type": "Bearer"}
    with pytest.raises(OIDCError):
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")


def test_expired_token_is_rejected(client: OIDCClient, provider: FakeProvider) -> None:
    """Expired ID tokens are refused even when the signature is valid."""
    provider.claim_overrides = {
        "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
    }
    provider.token_body = {"id_token": provider.id_token(nonce="nonce-1"), "token_type": "Bearer"}
    with pytest.raises(OIDCError):
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")


def test_wrong_audience_is_rejected(client: OIDCClient, provider: FakeProvider) -> None:
    """A token minted for a different client is refused."""
    provider.claim_overrides = {"aud": "another-client"}
    provider.token_body = {"id_token": provider.id_token(nonce="nonce-1"), "token_type": "Bearer"}
    with pytest.raises(OIDCError):
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")


def test_wrong_issuer_is_rejected(client: OIDCClient, provider: FakeProvider) -> None:
    """A token from another issuer is refused."""
    provider.claim_overrides = {"iss": "https://evil.example.test"}
    provider.token_body = {"id_token": provider.id_token(nonce="nonce-1"), "token_type": "Bearer"}
    with pytest.raises(OIDCError):
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")


def test_discovery_issuer_mismatch_is_rejected(provider: FakeProvider) -> None:
    """Provider metadata that claims a different issuer is rejected (mix-up defence)."""
    provider.issuer = "https://other-issuer.example.test"
    client = OIDCClient(_settings(), transport=provider.transport())
    with pytest.raises(OIDCError):
        client.metadata()


def test_unknown_key_id_triggers_one_jwks_refresh(provider: FakeProvider) -> None:
    """Provider key rotation is picked up by refetching JWKS for an unknown kid."""
    now = {"value": datetime.now(UTC)}
    client = OIDCClient(_settings(), transport=provider.transport(), clock=lambda: now["value"])
    client.verify_id_token(provider.id_token(nonce="n"), expected_nonce="n")
    assert provider.jwks_calls == 1
    provider.extra_keys = [_jwk_for(provider.other_key, "k2")]
    rotated = provider.id_token(nonce="n", key=provider.other_key, kid="k2")
    now["value"] += timedelta(seconds=30)
    claims = client.verify_id_token(rotated, expected_nonce="n")
    assert claims.subject == "user-123"
    assert provider.jwks_calls == 2


def test_es256_token_is_verified_when_allowed(provider: FakeProvider) -> None:
    """EC keys and the ES256 algorithm work for providers that use them."""
    ec_key = ec.generate_private_key(ec.SECP256R1())
    provider.extra_keys = [_jwk_for(ec_key, "ec1")]
    client = OIDCClient(_settings(), transport=provider.transport())
    token = provider.id_token(nonce="n", key=ec_key, algorithm="ES256", kid="ec1")
    assert client.verify_id_token(token, expected_nonce="n").subject == "user-123"


def test_token_endpoint_failure_is_typed_and_opaque(
    client: OIDCClient, provider: FakeProvider
) -> None:
    """A rejected exchange raises a typed error without echoing the provider body."""
    provider.token_status = 400
    with pytest.raises(OIDCError) as excinfo:
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")
    assert "invalid_grant" not in str(excinfo.value)


def test_token_response_without_id_token_is_rejected(
    client: OIDCClient, provider: FakeProvider
) -> None:
    """An access-token-only response is not enough to authenticate a browser session."""
    provider.token_body = {"access_token": "opaque", "token_type": "Bearer"}
    with pytest.raises(OIDCError):
        client.exchange_code(code="c", code_verifier="v", expected_nonce="nonce-1")


def test_metadata_endpoints_must_be_https(provider: FakeProvider) -> None:
    """Non-TLS discovered endpoints are refused for the token and key fetches."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": "http://insecure.example.test/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        return httpx.Response(200, json={"keys": []})

    client = OIDCClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(OIDCError):
        client.metadata()


def test_provider_metadata_is_cached(provider: FakeProvider) -> None:
    """Repeated verification does not re-run discovery or JWKS on every request."""
    calls = {"discovery": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        base = provider.transport()
        if request.url.path == "/.well-known/openid-configuration":
            calls["discovery"] += 1
        return base.handle_request(request)

    client = OIDCClient(_settings(), transport=httpx.MockTransport(handler))
    for _ in range(5):
        client.verify_id_token(provider.id_token(nonce="n"), expected_nonce="n")
    assert calls["discovery"] == 1
    assert provider.jwks_calls == 1
