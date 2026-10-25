"""Outbound endpoint validation to defend against SSRF and credential leakage."""

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpcore
import httpx

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata"}


class UnsafeEndpointError(ValueError):
    """Raised when a user-supplied host or URL is not allowed as an outbound target."""


def _check_ip(ip: ipaddress._BaseAddress) -> None:
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (
            isinstance(ip, ipaddress.IPv6Address)
            and ip.ipv4_mapped is not None
            and not ip.ipv4_mapped.is_global
        )
    ):
        raise UnsafeEndpointError("endpoint resolves to a non-public address")


def validate_host(host: str, *, allow_private: bool = False, resolve: bool = True) -> None:
    """Reject empty, blocked, or non-public hosts (checks every resolved address)."""
    host = host.strip().strip("[]").lower()
    if not host or len(host) > 253 or any(c.isspace() or c in "/\\@?#" for c in host):
        raise UnsafeEndpointError("invalid host")
    if allow_private:
        return
    if host in _BLOCKED_HOSTNAMES or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeEndpointError("endpoint host is not allowed")
    try:
        _check_ip(ipaddress.ip_address(host))
        return
    except ValueError as e:
        if isinstance(e, UnsafeEndpointError):
            raise
    if not resolve:
        return
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as e:
        raise UnsafeEndpointError("endpoint host could not be resolved") from e
    for info in infos:
        _check_ip(ipaddress.ip_address(info[4][0]))


def validate_https_url(url: str, *, allow_private: bool = False, resolve: bool = True) -> str:
    """Validate a base URL: https only (http for allowed private hosts), no credentials."""
    if len(url) > 2048:
        raise UnsafeEndpointError("URL is too long")
    parts = urlsplit(url.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise UnsafeEndpointError("URL must be an http(s) URL with a host")
    if parts.scheme == "http" and not allow_private:
        raise UnsafeEndpointError("URL must use https")
    if parts.username or parts.password:
        raise UnsafeEndpointError("URL must not contain credentials")
    if parts.query or parts.fragment:
        raise UnsafeEndpointError("URL must not contain a query or fragment")
    try:
        _ = parts.port
    except ValueError as e:
        raise UnsafeEndpointError("URL has an invalid port") from e
    validate_host(parts.hostname, allow_private=allow_private, resolve=resolve)
    return url.strip().rstrip("/")


def resolve_public_addresses(host: str, *, allow_private: bool = False) -> list[str] | None:
    """Resolve a host once, reject any non-public answer, and return the vetted IPs."""
    clean = host.strip().strip("[]").lower()
    if not clean or len(clean) > 253 or any(c.isspace() or c in "/\\@?#" for c in clean):
        raise UnsafeEndpointError("invalid host")
    if clean in _BLOCKED_HOSTNAMES or clean.endswith((".local", ".internal", ".localhost")):
        if not allow_private:
            raise UnsafeEndpointError("endpoint host is not allowed")
    if allow_private:
        return None

    try:
        address = ipaddress.ip_address(clean)
    except ValueError:
        address = None
    if address is not None:
        _check_ip(address)
        return [str(address)]

    try:
        infos = socket.getaddrinfo(clean, None, type=socket.SOCK_STREAM)
    except OSError as e:
        raise UnsafeEndpointError("endpoint host could not be resolved") from e
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise UnsafeEndpointError("endpoint host could not be resolved")
    for resolved in addresses:
        try:
            _check_ip(ipaddress.ip_address(resolved))
        except ValueError as e:
            if isinstance(e, UnsafeEndpointError):
                raise
            raise UnsafeEndpointError("endpoint resolved to an invalid address") from e
    return addresses


def resolve_public_address(host: str, *, allow_private: bool = False) -> str | None:
    """Return one vetted address after a single DNS lookup (for libpq hostaddr pinning)."""
    addresses = resolve_public_addresses(host, allow_private=allow_private)
    return addresses[0] if addresses else None


class _PinnedNetworkBackend(httpcore.SyncBackend):
    """Connect only to IP addresses already vetted for each outbound hostname."""

    def __init__(self, addresses: dict[str, list[str]]) -> None:
        self._addresses = {
            host.strip("[]").lower(): list(values) for host, values in addresses.items()
        }

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.NetworkStream:
        """Dial only vetted IPs while keeping the original hostname for TLS verification."""
        key = host.strip("[]").lower()
        addresses = self._addresses.get(key)
        if not addresses:
            raise OSError("outbound host was not DNS-pinned")
        last_error: BaseException | None = None
        for address in addresses:
            try:
                return super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last_error = e
        if last_error is not None:
            raise last_error
        raise OSError("no vetted address is available")


class PinnedHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport that connects only to an explicit map of DNS-vetted IP addresses."""

    def __init__(self, addresses: dict[str, list[str]]) -> None:
        """Build a transport whose pool preserves TLS hostname verification but pins TCP IPs."""
        super().__init__(trust_env=False, retries=0)
        self._pool._network_backend = _PinnedNetworkBackend(addresses)
