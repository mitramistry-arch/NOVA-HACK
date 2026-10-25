"""Tenant provider configuration models (no secrets: the API key lives in the secret store)."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, model_validator

from nova.providers.errors import ProviderConfigError

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
PROVIDER_KEY_SECRET = "provider-api-key"
MAX_OUTPUT_TOKENS_LIMIT = 8192
MAX_PROMPT_TOKENS_LIMIT = 200_000

ModelName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[^\s]+$")
]


class ProviderKind(StrEnum):
    """Supported provider adapters.

    ``openai_compatible`` speaks the OpenAI Chat Completions request/response shape
    (OpenAI and any server implementing it). No other agent/provider API is supported.
    """

    OPENAI_COMPATIBLE = "openai_compatible"


class ProviderSettings(BaseModel):
    """Non-secret provider settings for one tenant."""

    model_config = ConfigDict(extra="forbid")

    kind: ProviderKind = ProviderKind.OPENAI_COMPATIBLE
    model: ModelName
    base_url: str | None = Field(default=None, max_length=2048)
    agent_id: str | None = Field(default=None, max_length=128)
    timeout_s: float = Field(default=30.0, ge=1.0, le=120.0)
    max_output_tokens: int = Field(default=1024, ge=1, le=MAX_OUTPUT_TOKENS_LIMIT)
    max_prompt_tokens: int = Field(default=16_000, ge=1, le=MAX_PROMPT_TOKENS_LIMIT)
    max_attempts: int = Field(default=3, ge=1, le=5)
    retry_base_delay_s: float = Field(default=0.25, ge=0.0, le=5.0)
    retry_max_delay_s: float = Field(default=2.0, ge=0.0, le=30.0)
    total_timeout_s: float = Field(default=30.0, gt=0.0, le=300.0)
    input_cost_per_1k: float | None = Field(default=None, ge=0.0, le=1000.0)
    output_cost_per_1k: float | None = Field(default=None, ge=0.0, le=1000.0)

    @model_validator(mode="after")
    def _kind_rules(self) -> "ProviderSettings":
        if self.agent_id is not None:
            raise ValueError("agent_id is not supported by the openai_compatible adapter")
        if self.retry_max_delay_s < self.retry_base_delay_s:
            raise ValueError("retry_max_delay_s must be at least retry_base_delay_s")
        if (self.input_cost_per_1k is None) != (self.output_cost_per_1k is None):
            raise ValueError("configure both input_cost_per_1k and output_cost_per_1k, or neither")
        return self

    @property
    def effective_base_url(self) -> str:
        """Return the configured base URL or the OpenAI default."""
        return (self.base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")


class ProviderConfigRequest(ProviderSettings):
    """Payload for configuring a provider; ``api_key`` is write-only."""

    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=512)

    def settings_only(self) -> ProviderSettings:
        """Return the non-secret part."""
        return ProviderSettings(**self.model_dump(exclude={"api_key"}))


class ProviderStatus(BaseModel):
    """What the API reveals about a tenant's provider: never the key itself."""

    configured: bool
    kind: ProviderKind | None = None
    model: str | None = None
    base_url: str | None = None
    timeout_s: float | None = None
    max_output_tokens: int | None = None
    max_prompt_tokens: int | None = None
    has_api_key: bool = False
    api_key_mask: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class ConnectionTestResult(BaseModel):
    """Outcome of a provider or storage connection test (stable codes, no internals)."""

    ok: bool
    code: str
    message: str
    latency_ms: float | None = None


def parse_stored(body: dict[str, object]) -> ProviderSettings:
    """Rebuild settings from a stored document, raising ProviderConfigError if corrupt."""
    try:
        return ProviderSettings(**body)  # type: ignore[arg-type]
    except ValueError as e:
        raise ProviderConfigError("stored provider configuration is invalid") from e
