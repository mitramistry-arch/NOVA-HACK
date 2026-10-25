"""Environment-based settings with fail-closed validation for production deployments."""

from __future__ import annotations

import logging
import os
from typing import ClassVar

from pydantic import BaseModel, Field

from nova.core.tenant import InvalidTenantError, validate_tenant_id

logger = logging.getLogger("nova.config")


class ConfigError(ValueError):
    """Raised when deployment configuration is invalid or unsafe."""


_DEVELOPMENT_ENVS = {"development", "dev", "local", "test"}
_VALID_ENVS = _DEVELOPMENT_ENVS | {"production", "prod"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable without silently accepting typos."""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean value")


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable, rejecting malformed values."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _parse_tenant_keys(raw: str | None) -> dict[str, str]:
    """Parse ``tenantA:keyA,tenantB:keyB`` into an API-key-to-tenant mapping."""
    mapping: dict[str, str] = {}
    for part in (raw or "").split(","):
        tenant, sep, key = part.strip().partition(":")
        if not sep or not tenant or not key:
            if part.strip():
                raise ConfigError(
                    "NOVA_TENANT_API_KEYS must use tenant:key pairs separated by commas"
                )
            continue
        if key in mapping:
            raise ConfigError("NOVA_TENANT_API_KEYS contains a duplicate API key")
        try:
            validate_tenant_id(tenant)
        except InvalidTenantError as exc:
            raise ConfigError("NOVA_TENANT_API_KEYS contains an invalid tenant id") from exc
        mapping[key] = tenant
    return mapping


class Settings(BaseModel):
    """Configuration loaded from environment variables (secret values are hidden in repr)."""

    VALID_ENVIRONMENTS: ClassVar[set[str]] = _VALID_ENVS

    environment: str = Field(default="production")
    api_key: str | None = Field(default=None, repr=False)
    tenant_api_keys: dict[str, str] = Field(default_factory=dict, repr=False)
    db_path: str = Field(default="nova.db")
    control_db_path: str = Field(default="nova_control.db")
    # Legacy single-tenant knobs: providers are configured per tenant (see nova/providers/*), and
    # validate_supported() rejects any llm_provider other than "fake". Kept so the value is
    # explicit in configuration rather than silently ignored.
    llm_provider: str = Field(default="fake")
    llm_model: str = Field(default="fake-model")
    openai_api_key: str | None = Field(default=None, repr=False)
    anthropic_api_key: str | None = Field(default=None, repr=False)
    log_level: str = Field(default="INFO")
    allow_fake_llm: bool = Field(default=False)
    allow_private_endpoints: bool = Field(default=False)
    encryption_key: str | None = Field(default=None, repr=False)
    encryption_keys_previous: str | None = Field(default=None, repr=False)
    max_body_bytes: int = Field(default=1_048_576)
    audit_retention_days: int | None = Field(default=90)

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from environment variables, rejecting malformed values."""
        retention = _env_int("NOVA_AUDIT_RETENTION_DAYS", 90)
        if os.getenv("DATABASE_URL"):
            raise ConfigError(
                "DATABASE_URL is unsupported; configure per-tenant storage through Nova"
            )
        return cls(
            environment=os.getenv("NOVA_ENV", "production").strip().lower(),
            api_key=os.getenv("NOVA_API_KEY") or os.getenv("API_KEY"),
            tenant_api_keys=_parse_tenant_keys(os.getenv("NOVA_TENANT_API_KEYS")),
            db_path=os.getenv("NOVA_DB_PATH", "nova.db"),
            control_db_path=os.getenv("NOVA_CONTROL_DB_PATH", "nova_control.db"),
            llm_provider=os.getenv("NOVA_LLM_PROVIDER", "fake"),
            llm_model=os.getenv("NOVA_LLM_MODEL", "fake-model"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            allow_fake_llm=_env_bool("NOVA_ALLOW_FAKE_LLM"),
            allow_private_endpoints=_env_bool("NOVA_ALLOW_PRIVATE_ENDPOINTS"),
            encryption_key=os.getenv("NOVA_ENCRYPTION_KEY"),
            encryption_keys_previous=os.getenv("NOVA_ENCRYPTION_KEYS_PREVIOUS"),
            max_body_bytes=_env_int("NOVA_MAX_BODY_BYTES", 1_048_576),
            audit_retention_days=retention if retention > 0 else None,
        )

    @property
    def is_development(self) -> bool:
        """Return whether unauthenticated local/test operation is explicitly permitted."""
        return self.environment.strip().lower() in _DEVELOPMENT_ENVS

    def validate_supported(self) -> None:
        """Validate all operational settings that can be checked without opening resources."""
        problems: list[str] = []
        env = self.environment.strip().lower()
        if env not in _VALID_ENVS:
            raise ConfigError("NOVA_ENV must be production, development, local, or test")
        self.validate_auth_configuration()
        if not self.db_path.strip():
            problems.append("NOVA_DB_PATH must not be empty")
        if not self.control_db_path.strip():
            problems.append("NOVA_CONTROL_DB_PATH must not be empty")
        if self.llm_provider.strip().lower() != "fake":
            problems.append("NOVA_LLM_PROVIDER is not supported; configure a provider per tenant")
        if self.log_level.strip().upper() not in _LOG_LEVELS:
            problems.append("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        if not 1 <= self.max_body_bytes <= 64 * 1024 * 1024:
            problems.append("NOVA_MAX_BODY_BYTES must be between 1 and 67108864")
        if self.audit_retention_days is not None and self.audit_retention_days < 1:
            problems.append("NOVA_AUDIT_RETENTION_DAYS must be positive or zero to disable")
        if env in {"production", "prod"}:
            if not self.encryption_key:
                problems.append("NOVA_ENCRYPTION_KEY is required in production")
            else:
                from nova.security.secrets import AesGcmCipher, SecretStoreUnavailableError

                try:
                    AesGcmCipher(self.encryption_key, self.encryption_keys_previous)
                except SecretStoreUnavailableError:
                    problems.append(
                        "NOVA_ENCRYPTION_KEY or NOVA_ENCRYPTION_KEYS_PREVIOUS is invalid"
                    )
        if env in {"production", "prod"} and self.allow_fake_llm:
            problems.append("NOVA_ALLOW_FAKE_LLM is not permitted in production")
        if problems:
            raise ConfigError("; ".join(problems))

    def validate_auth_configuration(self) -> None:
        """Require authentication in production and high-entropy configured API keys."""
        env = self.environment.strip().lower()
        if env not in _VALID_ENVS:
            raise ConfigError("NOVA_ENV must be production, development, local, or test")
        if self.api_key and self.tenant_api_keys:
            raise ConfigError("Configure either NOVA_API_KEY or NOVA_TENANT_API_KEYS, not both")
        keys = (
            list(self.tenant_api_keys)
            if self.tenant_api_keys
            else ([self.api_key] if self.api_key else [])
        )
        if not keys and env not in _DEVELOPMENT_ENVS:
            raise ConfigError(
                "API authentication is not configured; set NOVA_API_KEY or NOVA_TENANT_API_KEYS"
            )
        for tenant in self.tenant_api_keys.values():
            try:
                validate_tenant_id(tenant)
            except InvalidTenantError as exc:
                raise ConfigError("NOVA_TENANT_API_KEYS contains an invalid tenant id") from exc
        if env in {"production", "prod"}:
            for key in keys:
                if key is None or len(key) < 32:
                    raise ConfigError("Production API keys must contain at least 32 characters")


def get_settings() -> Settings:
    """Return settings initialized from the current process environment."""
    return Settings.from_env()


def configure_logging(settings: Settings) -> None:
    """Apply the configured log level to Nova's logger without adding duplicate handlers."""
    level_name = settings.log_level.strip().upper()
    if level_name not in _LOG_LEVELS:
        raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    logging.getLogger("nova").setLevel(getattr(logging, level_name))
