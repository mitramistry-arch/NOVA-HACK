"""Declared capabilities per provider kind.

Generation and embedding are separate capabilities. A provider that speaks a generation API is
not assumed to provide an embedding endpoint: the adapter must implement it and declare it.
"""

from enum import StrEnum

from nova.providers.config import ProviderKind
from nova.providers.errors import ProviderConfigError


class ProviderCapability(StrEnum):
    """A capability a provider adapter genuinely implements."""

    GENERATION = "generation"
    EMBEDDINGS = "embeddings"


CAPABILITIES: dict[ProviderKind, frozenset[ProviderCapability]] = {
    # Chat Completions only. OpenAI's embeddings endpoint is deliberately not declared: this
    # adapter does not implement it, and production embeddings are a separate (Phase 5) concern.
    ProviderKind.OPENAI_COMPATIBLE: frozenset({ProviderCapability.GENERATION}),
}


def capabilities_for(kind: ProviderKind) -> frozenset[ProviderCapability]:
    """Return the declared capabilities for a provider kind."""
    return CAPABILITIES.get(kind, frozenset())


def supports(kind: ProviderKind, capability: ProviderCapability) -> bool:
    """Return True when the adapter declares the capability."""
    return capability in capabilities_for(kind)


def require(kind: ProviderKind, capability: ProviderCapability) -> None:
    """Raise a typed configuration error when the adapter lacks the capability."""
    if not supports(kind, capability):
        raise ProviderConfigError(
            f"{kind.value} does not support {capability.value}; configure a compatible adapter"
        )


__all__ = ["CAPABILITIES", "ProviderCapability", "capabilities_for", "require", "supports"]
