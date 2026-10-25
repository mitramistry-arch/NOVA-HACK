"""Tests for configuration parsing and production-safe startup policy."""

import pytest

from nova.core.config import ConfigError, Settings, _env_bool, _env_int, _parse_tenant_keys
from nova.security.secrets import generate_key


def test_default_runtime_settings_are_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_API_KEY", raising=False)
    monkeypatch.delenv("NOVA_TENANT_API_KEYS", raising=False)
    settings = Settings.from_env()
    assert settings.environment == "test"
    settings.validate_supported()
    assert settings.llm_provider == "fake"


def test_production_requires_authentication_and_encryption() -> None:
    with pytest.raises(ConfigError, match="API authentication"):
        Settings(environment="production").validate_supported()


def test_production_rejects_weak_api_keys() -> None:
    settings = Settings(
        environment="production", api_key="too-short", encryption_key=generate_key()
    )
    with pytest.raises(ConfigError, match="32 characters"):
        settings.validate_supported()


def test_production_requires_encryption_key() -> None:
    settings = Settings(environment="production", api_key="a" * 40)
    with pytest.raises(ConfigError, match="NOVA_ENCRYPTION_KEY"):
        settings.validate_supported()


def test_production_accepts_strong_key_and_valid_encryption() -> None:
    settings = Settings(environment="production", api_key="a" * 40, encryption_key=generate_key())
    settings.validate_supported()


def test_shared_and_tenant_specific_keys_are_mutually_exclusive() -> None:
    settings = Settings(environment="test", api_key="shared", tenant_api_keys={"key": "tenant-a"})
    with pytest.raises(ConfigError, match="either"):
        settings.validate_auth_configuration()


def test_production_rejects_fake_fallback() -> None:
    settings = Settings(
        environment="production",
        api_key="a" * 40,
        encryption_key=generate_key(),
        allow_fake_llm=True,
    )
    with pytest.raises(ConfigError, match="FAKE_LLM"):
        settings.validate_supported()


def test_parse_tenant_key_map_and_reject_duplicates() -> None:
    assert _parse_tenant_keys("tenant-a:key-one,tenant-b:key-two") == {
        "key-one": "tenant-a",
        "key-two": "tenant-b",
    }
    with pytest.raises(ConfigError, match="duplicate"):
        _parse_tenant_keys("a:duplicate,b:duplicate")
    with pytest.raises(ConfigError, match="tenant:key"):
        _parse_tenant_keys("not-a-pair")


@pytest.mark.parametrize("raw", ["maybe", "2", "enabled", "Falsey"])
def test_boolean_environment_parser_is_strict(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("TEST_BOOL", raw)
    with pytest.raises(ConfigError, match="boolean"):
        _env_bool("TEST_BOOL")


def test_boolean_environment_parser_accepts_explicit_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_BOOL", "off")
    assert _env_bool("TEST_BOOL", True) is False
    monkeypatch.setenv("TEST_BOOL", "YES")
    assert _env_bool("TEST_BOOL") is True


def test_integer_environment_parser_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INT", "invalid")
    with pytest.raises(ConfigError, match="integer"):
        _env_int("TEST_INT", 10)
    monkeypatch.setenv("TEST_INT", "25")
    assert _env_int("TEST_INT", 10) == 25


def test_invalid_log_level_and_body_limit_are_rejected() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings(environment="test", log_level="LOUD").validate_supported()
    with pytest.raises(ConfigError, match="NOVA_MAX_BODY_BYTES"):
        Settings(environment="test", max_body_bytes=0).validate_supported()


def test_database_url_is_rejected_instead_of_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    with pytest.raises(ConfigError, match="DATABASE_URL is unsupported"):
        Settings.from_env()
