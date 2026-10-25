"""Identity configuration parsing and fail-closed production validation."""

import pytest

from nova.core.config import ConfigError
from nova.identity.config import IdentityMode, IdentitySettings

COMPLETE = {
    "mode": "oidc",
    "issuer": "https://idp.example.test",
    "client_id": "nova-workspace",
    "client_secret": "client-secret",
    "redirect_uri": "https://app.example.test/api/bff/callback",
    "bff_origin": "https://app.example.test",
    "users": {"alice@example.test": {"tenant_id": "tenant-a", "role": "admin"}},
}


def _settings(**overrides: object) -> IdentitySettings:
    """Build settings from the complete baseline with overrides."""
    base = dict(COMPLETE)
    base.update(overrides)
    return IdentitySettings(**base)


def test_default_mode_is_api_key_and_requires_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An API-only deployment keeps working without any identity configuration."""
    monkeypatch.delenv("NOVA_IDENTITY_MODE", raising=False)
    settings = IdentitySettings.from_env()
    assert settings.mode is IdentityMode.API_KEY
    assert settings.browser_enabled is False
    settings.validate_for_environment("production")


def test_complete_configuration_is_accepted_in_production() -> None:
    """A fully specified OIDC deployment passes the production gate."""
    _settings().validate_for_environment("production")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", None),
        ("client_id", None),
        ("client_secret", None),
        ("redirect_uri", None),
        ("bff_origin", None),
    ],
)
def test_missing_oidc_configuration_fails_production_startup(field: str, value: object) -> None:
    """Every required OIDC setting is mandatory: no partial or anonymous browser auth."""
    with pytest.raises(ConfigError):
        _settings(**{field: value}).validate_for_environment("production")


def test_missing_configuration_also_fails_in_development() -> None:
    """Enabling OIDC without configuration is a bug in any environment."""
    with pytest.raises(ConfigError):
        _settings(issuer=None).validate_for_environment("test")


def test_insecure_issuer_is_rejected_in_production() -> None:
    """Plaintext identity endpoints are not acceptable in production."""
    with pytest.raises(ConfigError):
        _settings(
            issuer="http://idp.example.test",
            redirect_uri="http://app.example.test/api/bff/callback",
            bff_origin="http://app.example.test",
        ).validate_for_environment("production")


def test_insecure_cookies_are_rejected_in_production() -> None:
    """Session cookies must stay Secure (and therefore __Host- prefixed) in production."""
    with pytest.raises(ConfigError):
        _settings(cookie_secure=False).validate_for_environment("production")


def test_redirect_uri_must_share_the_bff_origin() -> None:
    """An authorization code must not be delivered to a different origin."""
    with pytest.raises(ConfigError):
        _settings(
            redirect_uri="https://elsewhere.example.test/api/bff/callback"
        ).validate_for_environment("production")


def test_production_requires_at_least_one_grant() -> None:
    """A deployment that can never authorize anyone is treated as misconfigured."""
    with pytest.raises(ConfigError):
        _settings(users={}, groups={}).validate_for_environment("production")


def test_none_algorithm_is_forbidden() -> None:
    """The unsigned JWT algorithm is never accepted, even if configured by mistake."""
    with pytest.raises(ConfigError):
        _settings(allowed_algs=("none",)).validate_for_environment("test")


def test_scopes_must_include_openid() -> None:
    """Without the openid scope there is no ID token to verify."""
    with pytest.raises(ConfigError):
        _settings(scopes=("email",)).validate_for_environment("test")


def test_from_env_parses_grants_limits_and_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment parsing produces typed grants, bounded limits, and normalized origin."""
    monkeypatch.setenv("NOVA_IDENTITY_MODE", "oidc")
    monkeypatch.setenv("NOVA_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("NOVA_OIDC_CLIENT_ID", "nova-workspace")
    monkeypatch.setenv("NOVA_OIDC_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("NOVA_OIDC_REDIRECT_URI", "https://app.example.test/api/bff/callback")
    monkeypatch.setenv("NOVA_BFF_ORIGIN", "https://app.example.test/")
    monkeypatch.setenv(
        "NOVA_IDENTITY_USERS", "alice@example.test:tenant-a:admin,bob@example.test:tenant-b:viewer"
    )
    monkeypatch.setenv("NOVA_IDENTITY_GROUPS", "team-alpha:tenant-a:editor")
    monkeypatch.setenv("NOVA_SESSION_IDLE_SECONDS", "900")
    monkeypatch.setenv("NOVA_BFF_LOGIN_RATE_PER_MINUTE", "5")
    settings = IdentitySettings.from_env()
    assert settings.browser_enabled is True
    assert settings.bff_origin == "https://app.example.test"
    assert settings.idle_ttl_seconds == 900
    assert settings.login_rate_per_minute == 5
    assert settings.users["alice@example.test"].tenant_id == "tenant-a"
    assert settings.users["bob@example.test"].role.value == "viewer"
    assert settings.groups["team-alpha"].role.value == "editor"


@pytest.mark.parametrize(
    "raw",
    [
        "alice@example.test:tenant-a",
        "alice@example.test:tenant-a:superuser",
        "alice@example.test:INVALID TENANT:admin",
        "alice@example.test:tenant-a:admin,alice@example.test:tenant-b:admin",
    ],
)
def test_malformed_grant_maps_are_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Malformed or duplicated grants fail configuration instead of being ignored."""
    monkeypatch.setenv("NOVA_IDENTITY_USERS", raw)
    with pytest.raises(ConfigError):
        IdentitySettings.from_env()


@pytest.mark.parametrize("value", ["sometimes", "2"])
def test_malformed_boolean_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A misspelled boolean must not silently disable a security control."""
    monkeypatch.setenv("NOVA_SESSION_COOKIE_SECURE", value)
    with pytest.raises(ConfigError):
        IdentitySettings.from_env()


def test_origin_must_not_contain_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser origin is a scheme plus host, so a path is a configuration error."""
    monkeypatch.setenv("NOVA_BFF_ORIGIN", "https://app.example.test/workspace")
    with pytest.raises(ConfigError):
        IdentitySettings.from_env()


def test_unknown_identity_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the documented identity modes are accepted."""
    monkeypatch.setenv("NOVA_IDENTITY_MODE", "saml-homemade")
    with pytest.raises(ConfigError):
        IdentitySettings.from_env()
