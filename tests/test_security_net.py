"""Regression tests for DNS rebinding defenses in outbound network clients."""

import socket
from typing import Any

import httpcore
import pytest

from nova.providers.config import ProviderSettings
from nova.providers.openai_compat import OpenAICompatibleLLM
from nova.security.net import (
    PinnedHTTPTransport,
    UnsafeEndpointError,
    _PinnedNetworkBackend,
    resolve_public_addresses,
)


def _addr(ip: str) -> tuple[Any, ...]:
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))


def test_dns_resolution_is_single_snapshot_and_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only one DNS snapshot is used to create the vetted socket-address list."""
    calls: list[str] = []

    def lookup(host: str, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        calls.append(host)
        return [_addr("93.184.216.34"), _addr("93.184.216.34")]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    assert resolve_public_addresses("Example.com") == ["93.184.216.34"]
    assert calls == ["example.com"]


def test_dns_answer_with_any_private_address_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mixed public/private DNS response is blocked in full, not partially accepted."""
    calls = 0

    def lookup(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        nonlocal calls
        calls += 1
        return [_addr("93.184.216.34"), _addr("127.0.0.1")]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    with pytest.raises(UnsafeEndpointError, match="non-public"):
        resolve_public_addresses("rebinding.example")
    assert calls == 1


def test_pinned_backend_dials_the_vetted_ip_not_dns_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The socket backend receives only an IP while the HTTP layer keeps the host name."""
    dialed: list[tuple[str, int]] = []
    sentinel = object()

    def connect(
        _self: httpcore.SyncBackend,
        host: str,
        port: int,
        **_kwargs: Any,
    ) -> object:
        dialed.append((host, port))
        return sentinel

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    backend = _PinnedNetworkBackend({"example.com": ["93.184.216.34"]})
    assert backend.connect_tcp("example.com", 443) is sentinel
    assert dialed == [("93.184.216.34", 443)]
    with pytest.raises(OSError, match="not DNS-pinned"):
        backend.connect_tcp("other.example", 443)


def test_provider_builds_pinned_transport_from_validated_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider requests use a transport whose TCP connection cannot re-resolve DNS."""
    calls: list[str] = []

    def lookup(host: str, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        calls.append(host)
        return [_addr("93.184.216.34")]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    client = OpenAICompatibleLLM(
        ProviderSettings(model="test-model", base_url="https://example.com/v1"),
        "test-provider-key",
    )
    try:
        assert calls == ["example.com"]
        assert isinstance(client._client._transport, PinnedHTTPTransport)
        backend = client._client._transport._pool._network_backend
        assert isinstance(backend, _PinnedNetworkBackend)
    finally:
        client.close()


def test_provider_fails_closed_when_dns_contains_private_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider client is created if any resolved address is unsafe."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [_addr("93.184.216.34"), _addr("10.0.0.7")],
    )
    from nova.providers.errors import ProviderConfigError

    with pytest.raises(ProviderConfigError, match="unsafe provider endpoint"):
        OpenAICompatibleLLM(
            ProviderSettings(model="test-model", base_url="https://example.com/v1"),
            "test-provider-key",
        )
