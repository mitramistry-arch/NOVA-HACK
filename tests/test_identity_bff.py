"""HTTP-level BFF tests: sessions, cookies, CSRF, origin checks, roles, and tenant isolation.

The identity provider is a local fake transport; no network call or real identity vendor is used.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from nova.api.main import close_runtime, create_app, get_runtime
from nova.core.config import ConfigError
from nova.core.llm import FakeLLM
from nova.identity.oidc import OIDCClient
from nova.providers.service import ProviderService
from nova.security.secrets import generate_key
from nova.store.timeutil import format_ts

ISSUER = "https://idp.example.test"
BASE_URL = "https://testserver"
ORIGIN = BASE_URL
SESSION_COOKIE = "__Host-nova_session"
CSRF_COOKIE = "__Host-nova_csrf"
KEY_A = "tenant-a-backend-credential-AAAA"
KEY_B = "tenant-b-backend-credential-BBBB"
USERS = (
    "alice@example.test:tenant-a:admin,"
    "bob@example.test:tenant-b:admin,"
    "vera@example.test:tenant-a:viewer"
)
EXTRACTION = json.dumps(
    [
        {
            "type": "decision",
            "subject": "project_database",
            "value": "PostgreSQL",
            "reason": "team standard",
            "source_msg_id": "m1",
            "source_quote": "PostgreSQL for the project database",
            "confidence": 0.9,
        }
    ]
)


class ScriptedLLM(FakeLLM):
    """Routes prompts to fixed extraction and judge payloads; no model is called."""

    def __init__(self) -> None:
        """Start with no scripted responses."""
        super().__init__()

    def complete(self, prompt: str, *, json: bool = False) -> str:  # noqa: A002
        """Return verdict-free judge JSON, or the extraction payload."""
        if "You verify claims against stored memories" in prompt:
            return "[]"
        return EXTRACTION


class FakeIdP:
    """OpenID Provider double whose token endpoint selects a subject by authorization code."""

    def __init__(self) -> None:
        """Create the signing key and request state."""
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.nonce: str | None = None
        self.subjects = {
            "code-alice": "alice@example.test",
            "code-bob": "bob@example.test",
            "code-vera": "vera@example.test",
        }

    def transport(self) -> httpx.MockTransport:
        """Return a transport serving discovery, JWKS, and token responses."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/.well-known/openid-configuration":
                return httpx.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "authorization_endpoint": f"{ISSUER}/authorize",
                        "token_endpoint": f"{ISSUER}/token",
                        "jwks_uri": f"{ISSUER}/jwks",
                    },
                )
            if path == "/jwks":
                numbers = self.key.public_key().public_numbers()
                return httpx.Response(
                    200,
                    json={
                        "keys": [
                            {
                                "kty": "RSA",
                                "kid": "k1",
                                "use": "sig",
                                "alg": "RS256",
                                "n": _b64(
                                    numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
                                ),
                                "e": _b64(
                                    numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
                                ),
                            }
                        ]
                    },
                )
            if path == "/token":
                form = parse_qs(request.content.decode())
                code = (form.get("code") or [""])[0]
                subject = self.subjects.get(code, "unknown@example.test")
                now = datetime.now(UTC)
                claims = {
                    "iss": ISSUER,
                    "aud": "nova-workspace",
                    "sub": subject,
                    "email": subject,
                    "iat": int(now.timestamp()),
                    "exp": int((now + timedelta(minutes=5)).timestamp()),
                    "nonce": self.nonce,
                }
                token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
                return httpx.Response(200, json={"id_token": token, "token_type": "Bearer"})
            return httpx.Response(404, json={})

        return httpx.MockTransport(handler)


def _b64(raw: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Configure an isolated test deployment with browser identity enabled."""
    env = {
        "NOVA_ENV": "test",
        "NOVA_DB_PATH": str(tmp_path / "data.db"),
        "NOVA_CONTROL_DB_PATH": str(tmp_path / "control.db"),
        "NOVA_ENCRYPTION_KEY": generate_key(),
        "NOVA_IDENTITY_MODE": "oidc",
        "NOVA_OIDC_ISSUER": ISSUER,
        "NOVA_OIDC_CLIENT_ID": "nova-workspace",
        "NOVA_OIDC_CLIENT_SECRET": "client-secret-value",
        "NOVA_OIDC_REDIRECT_URI": f"{BASE_URL}/api/bff/callback",
        "NOVA_BFF_ORIGIN": ORIGIN,
        "NOVA_IDENTITY_USERS": USERS,
        "NOVA_TENANT_API_KEYS": f"tenant-a:{KEY_A},tenant-b:{KEY_B}",
        "NOVA_BFF_LOGIN_RATE_PER_MINUTE": "50",
        "NOVA_BFF_MUTATION_RATE_PER_MINUTE": "100",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NOVA_API_KEY", raising=False)
    monkeypatch.delenv("NOVA_ALLOW_PRIVATE_ENDPOINTS", raising=False)
    return env


@pytest.fixture
def make_client(app_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build fresh apps on demand, each with a fake identity provider and scripted LLM."""
    monkeypatch.setattr(ProviderService, "build_llm", lambda self, tenant_id: ScriptedLLM())
    stack = ExitStack()
    created: list[TestClient] = []

    def _make(**overrides: str | None) -> tuple[TestClient, FakeIdP]:
        for key, value in overrides.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        close_runtime()
        application = create_app()
        client = stack.enter_context(
            TestClient(application, base_url=BASE_URL, raise_server_exceptions=False)
        )
        idp = FakeIdP()
        runtime = get_runtime()
        assert runtime.identity is not None
        runtime.identity.oidc = OIDCClient(runtime.identity_settings, transport=idp.transport())
        created.append(client)
        return client, idp

    yield _make
    for client in created:
        client.close()
    stack.close()
    close_runtime()


def _login(
    client: TestClient, idp: FakeIdP, code: str = "code-alice", next_path: str | None = None
):
    """Run the full login redirect and callback, returning the callback response."""
    url = "/api/bff/login" + (f"?next={next_path}" if next_path else "")
    start = client.get(url, follow_redirects=False)
    assert start.status_code == 302, start.text
    query = parse_qs(urlparse(start.headers["location"]).query)
    idp.nonce = query["nonce"][0]
    state = query["state"][0]
    return client.get(f"/api/bff/callback?code={code}&state={state}", follow_redirects=False)


def _mutation_headers(
    client: TestClient, *, origin: str = ORIGIN, csrf: str | None = None
) -> dict[str, str]:
    """Build headers for a same-origin, CSRF-protected mutation."""
    token = client.cookies.get(CSRF_COOKIE) if csrf is None else csrf
    headers = {"origin": origin}
    if token:
        headers["x-csrf-token"] = token
    return headers


def _message(text: str = "We use PostgreSQL for the project database.") -> dict[str, Any]:
    """Build one ingest message."""
    return {
        "id": "m1",
        "session_id": "s1",
        "role": "user",
        "text": text,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _export(client: TestClient) -> dict[str, Any]:
    """Fetch the current tenant's export through the BFF."""
    response = client.get("/api/bff/data/export")
    assert response.status_code == 200, response.text
    return response.json()


def test_anonymous_requests_are_rejected(make_client: Any) -> None:
    """No session cookie means no identity, on both BFF metadata and proxied routes."""
    client, _ = make_client()
    whoami = client.get("/api/bff/whoami")
    assert whoami.status_code == 401
    assert whoami.json()["error"] == "authentication_required"
    ask = client.post(
        "/api/bff/ask", json={"question": "What database?", "draft_answer": "PostgreSQL."}
    )
    assert ask.status_code == 401


def test_login_redirects_to_provider_with_pkce(make_client: Any) -> None:
    """Sign-in starts with a redirect carrying state, nonce, and an S256 challenge."""
    client, _ = make_client()
    response = client.get("/api/bff/login", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/authorize?")
    query = parse_qs(urlparse(location).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"][0] != query["nonce"][0]
    assert query["redirect_uri"] == [f"{BASE_URL}/api/bff/callback"]


def test_callback_sets_hardened_cookies_and_whoami_works(make_client: Any) -> None:
    """The callback issues __Host- cookies with Secure/HttpOnly/SameSite and a usable session."""
    client, idp = make_client()
    response = _login(client, idp)
    assert response.status_code == 303
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
    csrf_cookie = next(c for c in cookies if c.startswith(f"{CSRF_COOKIE}="))
    for cookie in (session_cookie, csrf_cookie):
        assert "Secure" in cookie
        assert "Path=/" in cookie
        assert "SameSite=lax" in cookie
        assert "Domain=" not in cookie
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie

    whoami = client.get("/api/bff/whoami")
    assert whoami.status_code == 200
    body = whoami.json()
    assert body["authenticated"] is True
    assert body["tenant_id"] == "tenant-a"
    assert body["role"] == "admin"
    assert "provider.manage" in body["permissions"]
    assert whoami.headers["cache-control"] == "no-store"


def test_identity_is_mapped_server_side_not_from_the_token_tenant(
    monkeypatch: pytest.MonkeyPatch, app_env: dict[str, str], make_client: Any
) -> None:
    """A subject absent from NOVA_IDENTITY_USERS is denied instead of defaulting to a tenant."""
    client, idp = make_client(NOVA_IDENTITY_USERS="alice@example.test:tenant-a:admin")
    response = _login(client, idp, code="code-bob")
    assert response.status_code == 403
    assert response.json()["error"] == "identity_not_mapped"
    assert client.get("/api/bff/whoami").status_code == 401


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, "csrf_failed"),
        ({"x-csrf-token": "wrong-token"}, "csrf_failed"),
    ],
)
def test_mutations_require_a_matching_csrf_token(
    make_client: Any, headers: dict[str, str], expected: str
) -> None:
    """Missing or incorrect CSRF tokens are rejected on state-changing requests."""
    client, idp = make_client()
    _login(client, idp)
    response = client.post(
        "/api/bff/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers={"origin": ORIGIN, **headers},
    )
    assert response.status_code == 403
    assert response.json()["error"] == expected


def test_csrf_token_from_another_session_is_rejected(make_client: Any) -> None:
    """A CSRF token that does not belong to the active session fails the binding check."""
    first, idp = make_client()
    _login(first, idp)
    stolen = first.cookies.get(CSRF_COOKIE)
    second, idp2 = make_client()
    _login(second, idp2)
    response = second.post(
        "/api/bff/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers={"origin": ORIGIN, "x-csrf-token": stolen},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_failed"


@pytest.mark.parametrize("origin", ["https://evil.example.test", "", "null"])
def test_cross_origin_mutations_are_rejected(make_client: Any, origin: str) -> None:
    """A mutation must come from the configured same origin."""
    client, idp = make_client()
    _login(client, idp)
    headers = _mutation_headers(client)
    if origin:
        headers["origin"] = origin
    else:
        headers.pop("origin")
    response = client.post(
        "/api/bff/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"] == "origin_rejected"


def test_cross_site_fetch_metadata_is_rejected(make_client: Any) -> None:
    """Sec-Fetch-Site other than same-origin is refused even with a valid Origin."""
    client, idp = make_client()
    _login(client, idp)
    headers = _mutation_headers(client)
    headers["sec-fetch-site"] = "cross-site"
    response = client.post(
        "/api/bff/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"] == "origin_rejected"


def test_same_origin_mutation_succeeds(make_client: Any) -> None:
    """A same-origin POST with the session's CSRF token is forwarded to the API."""
    client, idp = make_client()
    _login(client, idp)
    response = client.post(
        "/api/bff/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers=_mutation_headers(client),
    )
    assert response.status_code == 200, response.text
    assert "action" in response.json()


def test_role_denial_blocks_ingest_but_allows_questions(make_client: Any) -> None:
    """A viewer can ask questions but cannot ingest."""
    client, idp = make_client()
    _login(client, idp, code="code-vera")
    ingest = client.post(
        "/api/bff/ingest",
        json={"session_id": "s1", "messages": [_message()]},
        headers=_mutation_headers(client),
    )
    assert ingest.status_code == 403
    assert ingest.json()["error"] == "forbidden"
    ask = client.post(
        "/api/bff/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers=_mutation_headers(client),
    )
    assert ask.status_code == 200


def test_forged_tenant_headers_and_keys_are_ignored(make_client: Any) -> None:
    """Client-provided X-Tenant-ID/X-API-Key never change the tenant scope or leak."""
    client, idp = make_client()
    _login(client, idp)
    headers = _mutation_headers(client)
    headers.update({"x-tenant-id": "tenant-b", "x-api-key": "forged-client-key"})
    ingest = client.post(
        "/api/bff/ingest",
        json={"session_id": "s1", "messages": [_message()]},
        headers=headers,
    )
    assert ingest.status_code == 200, ingest.text
    assert ingest.json()["added"] == 1
    exported = _export(client)
    assert exported["tenant_id"] == "tenant-a"
    assert len(exported["memories"]) == 1
    assert "forged-client-key" not in json.dumps(exported)
    assert KEY_A not in ingest.text and KEY_B not in ingest.text


def test_two_sessions_are_isolated_across_tenants(make_client: Any) -> None:
    """Tenant A's memories and audit records are invisible to tenant B through the BFF."""
    alice, idp = make_client()
    _login(alice, idp)
    alice.post(
        "/api/bff/ingest",
        json={"session_id": "s1", "messages": [_message()]},
        headers=_mutation_headers(alice),
    )
    alice.post(
        "/api/bff/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers=_mutation_headers(alice),
    )
    export_a = _export(alice)
    assert len(export_a["memories"]) == 1
    assert export_a["audit_records"], "tenant A should have an audit record"
    audit_id = export_a["audit_records"][0]["id"]

    bob, idp_b = make_client()
    _login(bob, idp_b, code="code-bob")
    export_b = _export(bob)
    assert export_b["tenant_id"] == "tenant-b"
    assert export_b["memories"] == []
    assert export_b["audit_records"] == []
    cross = bob.get(f"/api/bff/audit/{audit_id}")
    assert cross.status_code == 404
    assert audit_id not in cross.text


def test_logout_revokes_the_session(make_client: Any) -> None:
    """Logout requires CSRF, clears cookies, and makes the old token unusable."""
    client, idp = make_client()
    _login(client, idp)
    token = client.cookies.get(SESSION_COOKIE)
    assert token

    without_csrf = client.post("/api/bff/logout", headers={"origin": ORIGIN})
    assert without_csrf.status_code == 403

    response = client.post("/api/bff/logout", headers=_mutation_headers(client))
    assert response.status_code == 204
    assert client.cookies.get(SESSION_COOKIE) in (None, "")
    assert client.get("/api/bff/whoami").status_code == 401

    client.cookies.clear()
    replay = client.get("/api/bff/whoami", headers={"cookie": f"{SESSION_COOKIE}={token}"})
    assert replay.status_code == 401


def test_idle_expired_session_is_rejected(make_client: Any, app_env: dict[str, str]) -> None:
    """An idle session stops working without any client cooperation."""
    client, idp = make_client()
    _login(client, idp)
    with sqlite3.connect(app_env["NOVA_CONTROL_DB_PATH"]) as db:
        stale = format_ts(datetime.now(UTC) - timedelta(minutes=45))
        db.execute("UPDATE browser_sessions SET last_seen_at = ?", (stale,))
    assert client.get("/api/bff/whoami").status_code == 401


def test_unknown_or_replayed_state_is_rejected(make_client: Any) -> None:
    """A callback with an unknown or reused state value cannot create a session."""
    client, idp = make_client()
    response = _login(client, idp)
    assert response.status_code == 303
    assert "state" not in parse_qs(urlparse(response.headers["location"]).query)
    replay = client.get("/api/bff/callback?code=code-alice&state=replayed", follow_redirects=False)
    assert replay.status_code == 401
    assert replay.json()["error"] == "session_invalid"


def test_open_redirect_targets_are_ignored(make_client: Any) -> None:
    """A hostile ?next= value cannot turn the callback into an open redirect."""
    client, idp = make_client()
    response = _login(client, idp, next_path="//evil.example.test/steal")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_only_allowlisted_routes_are_proxied(make_client: Any) -> None:
    """Docs, the OpenAPI document, and unlisted paths are not reachable through the BFF."""
    client, idp = make_client()
    _login(client, idp)
    for path in (
        "/api/bff/docs",
        "/api/bff/openapi.json",
        "/api/bff/readyz",
        "/api/bff/healthz",
        "/api/bff/storage/initialize/extra",
    ):
        assert client.get(path).status_code == 404, path
    assert client.delete("/api/bff/data/../memories").status_code in {403, 404}


def test_login_is_rate_limited(make_client: Any) -> None:
    """Repeated sign-in attempts from one client are throttled."""
    client, _ = make_client(NOVA_BFF_LOGIN_RATE_PER_MINUTE="2")
    assert client.get("/api/bff/login", follow_redirects=False).status_code == 302
    assert client.get("/api/bff/login", follow_redirects=False).status_code == 302
    limited = client.get("/api/bff/login", follow_redirects=False)
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"


def test_bff_is_absent_when_identity_mode_is_api_key(
    monkeypatch: pytest.MonkeyPatch, app_env: dict[str, str]
) -> None:
    """API-key deployments keep the current behaviour and expose no browser login."""
    monkeypatch.setenv("NOVA_IDENTITY_MODE", "api_key")
    close_runtime()
    application = create_app()
    with TestClient(application, base_url=BASE_URL, raise_server_exceptions=False) as client:
        assert client.get("/api/bff/whoami").status_code == 404
        assert client.get("/api/bff/login", follow_redirects=False).status_code == 404
        assert client.get("/healthz").json() == {"status": "ok"}
    close_runtime()


def test_static_api_keys_still_work_for_trusted_clients(
    make_client: Any, app_env: dict[str, str]
) -> None:
    """The existing API-key authentication path is unchanged by the BFF."""
    client, _ = make_client()
    ok = client.post(
        "/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers={"x-api-key": KEY_A},
    )
    assert ok.status_code == 200
    denied = client.post(
        "/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers={"x-api-key": "not-a-configured-key"},
    )
    assert denied.status_code == 401
    # A tenant key already fixes the tenant; the header is ignored rather than honoured.
    forged = client.post(
        "/ask",
        json={"question": "What database?", "draft_answer": "PostgreSQL."},
        headers={"x-api-key": KEY_A, "x-tenant-id": "tenant-b"},
    )
    assert forged.status_code == 200


def test_production_startup_fails_without_identity_configuration(
    monkeypatch: pytest.MonkeyPatch, app_env: dict[str, str]
) -> None:
    """A production deployment cannot enable browser identity without OIDC settings."""
    monkeypatch.setenv("NOVA_ENV", "production")
    monkeypatch.setenv("NOVA_API_KEY", "a" * 40)
    monkeypatch.delenv("NOVA_TENANT_API_KEYS", raising=False)
    monkeypatch.delenv("NOVA_IDENTITY_USERS", raising=False)
    monkeypatch.delenv("NOVA_IDENTITY_GROUPS", raising=False)
    monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
    close_runtime()
    with pytest.raises(ConfigError), TestClient(create_app(), base_url=BASE_URL):
        pass
    close_runtime()


def test_session_cookie_is_not_readable_from_javascript(make_client: Any) -> None:
    """The session value never appears in a JSON response or a non-script-visible place."""
    client, idp = make_client()
    response = _login(client, idp)
    token = client.cookies.get(SESSION_COOKIE)
    assert token
    whoami = client.get("/api/bff/whoami")
    assert token not in whoami.text
    assert KEY_A not in whoami.text and KEY_B not in whoami.text
    assert KEY_A not in response.text
    assert client.cookies.get(CSRF_COOKIE) != token
