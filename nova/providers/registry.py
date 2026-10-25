"""Adapter registry: the one place a provider kind is mapped to its implementation.

Provider-specific imports live here so the service layer, API routes, and tests depend on a kind
rather than a concrete class. Adding an adapter means adding a declaration, not editing callers.
"""

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from nova.core.llm import LLMClient
from nova.providers.capabilities import ProviderCapability, capabilities_for
from nova.providers.config import ProviderKind, ProviderSettings
from nova.providers.errors import ProviderConfigError
from nova.providers.openai_compat import OpenAICompatibleLLM

AdapterFactory = Callable[..., LLMClient]


@dataclass(frozen=True)
class AdapterSpec:
    """A registered adapter: which kind it implements and how to construct it."""

    kind: ProviderKind
    requires_api_key: bool
    build: AdapterFactory

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        """Return the capabilities this adapter declares."""
        return capabilities_for(self.kind)


def _build_openai_compatible(
    settings: ProviderSettings,
    api_key: str,
    *,
    allow_private: bool = False,
    transport: httpx.BaseTransport | None = None,
    max_output_tokens: int | None = None,
) -> LLMClient:
    """Construct the OpenAI-compatible Chat Completions adapter."""
    return OpenAICompatibleLLM(
        settings,
        api_key,
        allow_private=allow_private,
        transport=transport,
        max_output_tokens=max_output_tokens,
    )


ADAPTERS: dict[ProviderKind, AdapterSpec] = {
    ProviderKind.OPENAI_COMPATIBLE: AdapterSpec(
        kind=ProviderKind.OPENAI_COMPATIBLE,
        requires_api_key=True,
        build=_build_openai_compatible,
    ),
}


def spec_for(kind: ProviderKind) -> AdapterSpec:
    """Return the registered adapter for a kind, rejecting unregistered kinds."""
    spec = ADAPTERS.get(kind)
    if spec is None:
        raise ProviderConfigError(f"no adapter is registered for provider kind {kind.value!r}")
    return spec


def build_adapter(
    settings: ProviderSettings,
    api_key: str | None,
    *,
    allow_private: bool = False,
    transport: httpx.BaseTransport | None = None,
    max_output_tokens: int | None = None,
) -> LLMClient:
    """Build the adapter for the settings' kind, requiring a key when the adapter needs one."""
    spec = spec_for(settings.kind)
    if spec.requires_api_key and not api_key:
        raise ProviderConfigError("this adapter requires an API key")
    return spec.build(
        settings,
        api_key or "",
        allow_private=allow_private,
        transport=transport,
        max_output_tokens=max_output_tokens,
    )


def supported_kinds() -> tuple[ProviderKind, ...]:
    """Return every kind that has a registered adapter."""
    return tuple(ADAPTERS)


__all__ = ["ADAPTERS", "AdapterSpec", "build_adapter", "spec_for", "supported_kinds"]
