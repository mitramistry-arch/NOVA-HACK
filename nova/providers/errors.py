"""Typed provider errors. Messages are safe to show publicly; details stay in server logs."""


class ProviderError(Exception):
    """Base class for provider failures; ``code`` and ``public_message`` are stable and safe."""

    code = "provider_error"
    public_message = "The AI provider request failed."

    def __init__(self, message: str | None = None) -> None:
        """Create the error; ``message`` is internal detail (never include secrets)."""
        super().__init__(message or self.public_message)


class ProviderNotConfiguredError(ProviderError):
    """The tenant has no usable provider configuration or credential."""

    code = "provider_not_configured"
    public_message = "No AI provider is configured for this tenant."


class ProviderConfigError(ProviderError):
    """The supplied provider configuration is invalid or unsafe."""

    code = "provider_config_invalid"
    public_message = "The AI provider configuration is invalid."


class ProviderAuthError(ProviderError):
    """The provider rejected the credential."""

    code = "provider_auth_failed"
    public_message = "The AI provider rejected the configured credentials."


class ProviderRateLimitError(ProviderError):
    """The provider rate-limited the request."""

    code = "provider_rate_limited"
    public_message = "The AI provider is rate limiting requests. Try again later."


class ProviderTimeoutError(ProviderError):
    """The provider did not answer in time."""

    code = "provider_timeout"
    public_message = "The AI provider timed out."


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached or returned a server error."""

    code = "provider_unavailable"
    public_message = "The AI provider is unavailable."


class ProviderResponseError(ProviderError):
    """The provider reply did not match the expected shape."""

    code = "provider_bad_response"
    public_message = "The AI provider returned an unexpected response."


class ProviderBudgetError(ProviderError):
    """The request exceeds the configured token budget."""

    code = "provider_budget_exceeded"
    public_message = "The request exceeds the configured token budget."
