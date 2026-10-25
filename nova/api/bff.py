"""Same-origin backend-for-frontend: browser sessions in front of the tenant-scoped API.

Only explicitly allowlisted API routes are forwarded, each with its own permission and always with
a server-held tenant credential. Client-supplied credential and tenant headers are never forwarded.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request
from starlette.responses import RedirectResponse, Response

from nova.api.errors import PublicError
from nova.identity.dependencies import get_identity_service
from nova.identity.errors import RateLimited
from nova.identity.models import Principal, SessionRecord
from nova.identity.roles import Permission
from nova.identity.service import IdentityService, safe_next_path

logger = logging.getLogger("nova.api.bff")

INTERNAL_BASE_URL = "http://nova-internal.api"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class ProxyRoute:
    """One allowlisted internal API route exposed through the BFF."""

    method: str
    path: str
    permission: Permission

    @property
    def mutating(self) -> bool:
        """Return True when the route changes server state."""
        return self.method.upper() in MUTATING_METHODS


PROXY_ROUTES: tuple[ProxyRoute, ...] = (
    ProxyRoute("POST", "/ingest", Permission.INGEST),
    ProxyRoute("POST", "/ask", Permission.QUERY),
    ProxyRoute("POST", "/verify", Permission.QUERY),
    ProxyRoute("GET", "/audit/{audit_id}", Permission.AUDIT_READ),
    ProxyRoute("GET", "/provider", Permission.PROVIDER_MANAGE),
    ProxyRoute("PUT", "/provider", Permission.PROVIDER_MANAGE),
    ProxyRoute("DELETE", "/provider", Permission.PROVIDER_MANAGE),
    ProxyRoute("POST", "/provider/test", Permission.PROVIDER_MANAGE),
    ProxyRoute("GET", "/storage", Permission.STORAGE_MANAGE),
    ProxyRoute("PUT", "/storage", Permission.STORAGE_MANAGE),
    ProxyRoute("DELETE", "/storage", Permission.STORAGE_MANAGE),
    ProxyRoute("POST", "/storage/test", Permission.STORAGE_MANAGE),
    ProxyRoute("POST", "/storage/initialize", Permission.STORAGE_MANAGE),
    ProxyRoute("GET", "/data/export", Permission.DATA_EXPORT),
    ProxyRoute("DELETE", "/data", Permission.DATA_DELETE),
)


def _secure_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return conservative response headers for every browser-facing response."""
    headers = {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
    }
    if extra:
        headers.update(extra)
    return headers


def _json_error(status: int, error: str, detail: str) -> PublicError:
    """Build a public error with a stable code."""
    return PublicError(status, error, detail)


def _safe_path_value(value: Any) -> str:
    """Reject path parameters that could escape the allowlisted internal route."""
    text = str(value)
    if not text or len(text) > 200 or any(ch in text for ch in "/\\?#") or text.strip() != text:
        raise _json_error(400, "invalid_request", "The request path is invalid.")
    return text


def _whoami_payload(record: SessionRecord) -> dict[str, Any]:
    """Describe the authenticated principal; never secrets or session tokens."""
    principal: Principal = record.principal
    return {
        "authenticated": True,
        "subject": principal.subject,
        "email": principal.email,
        "tenant_id": principal.tenant_id,
        "role": principal.role.value,
        "permissions": sorted(permission.value for permission in principal.permissions),
        "session_expires_at": record.absolute_expires_at.isoformat(),
    }


def _make_proxy_handler(
    spec: ProxyRoute, service_getter: Callable[[], IdentityService]
) -> Callable[[Request], Any]:
    """Build the async handler for one allowlisted route."""

    async def handler(request: Request) -> Response:
        service: IdentityService = service_getter()
        record = service.authenticate(request)
        if spec.mutating:
            service.enforce_mutation(request, record)
        service.require(record.principal, spec.permission)
        target_path = spec.path
        if request.path_params:
            target_path = spec.path.format(
                **{name: _safe_path_value(value) for name, value in request.path_params.items()}
            )
        query = request.url.query
        url = f"{target_path}?{query}" if query else target_path
        body = await request.body() if spec.mutating else None
        headers = {
            "accept": "application/json",
            "x-api-key": service.tenant_credential(record.principal.tenant_id),
        }
        if body:
            headers["content-type"] = request.headers.get("content-type", "application/json")
        upstream = await _call_internal(request, spec.method, url, body, headers, service)
        response_headers = _secure_headers()
        content_type = upstream.headers.get("content-type")
        if content_type:
            response_headers["content-type"] = content_type
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    handler.__name__ = f"bff_{spec.method.lower()}_{spec.path.strip('/').replace('/', '_')}"
    handler.__doc__ = f"Proxy {spec.method} {spec.path} for a browser session."
    return handler


async def _call_internal(
    request: Request,
    method: str,
    url: str,
    body: bytes | None,
    headers: dict[str, str],
    service: IdentityService,
) -> httpx.Response:
    """Call the internal API in-process so all its middleware and auth still apply."""
    transport = httpx.ASGITransport(app=request.app)
    timeout = service.settings.proxy_timeout_seconds
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url=INTERNAL_BASE_URL, timeout=timeout
        ) as client:
            return await asyncio.wait_for(
                client.request(method, url, content=body, headers=headers), timeout=timeout
            )
    except TimeoutError as exc:
        raise _json_error(504, "bff_timeout", "The request took too long.") from exc
    except httpx.HTTPError as exc:
        raise _json_error(502, "bff_upstream_error", "The internal API is unavailable.") from exc


def build_bff_router() -> APIRouter:
    """Build the BFF router: login, callback, logout, whoami, and allowlisted proxies."""
    router = APIRouter(prefix="/bff", tags=["bff"])

    @router.get("/login")
    def login(request: Request, next: str | None = Query(default=None)) -> Response:
        """Start the authorization-code flow by redirecting to the configured provider."""
        service = get_identity_service()
        try:
            service.limit_login(request)
        except RateLimited as exc:
            raise PublicError(429, "rate_limited", exc.public_message) from exc
        url = service.start_login(safe_next_path(next))
        return RedirectResponse(url, status_code=302, headers=_secure_headers())

    @router.get("/callback")
    def callback(
        request: Request,
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
    ) -> Response:
        """Complete the flow, set hardened session cookies, and redirect within the app."""
        service = get_identity_service()
        if error or not code or not state:
            raise _json_error(401, "login_failed", "Sign-in was not completed.")
        issued, next_path = service.complete_login(code=code, state=state)
        response = RedirectResponse(next_path, status_code=303, headers=_secure_headers())
        service.set_session_cookies(response, issued)
        return response

    @router.get("/whoami")
    def whoami(request: Request) -> Response:
        """Describe the current session so the UI can render real identity and role state."""
        service = get_identity_service()
        record = service.authenticate(request)
        return Response(
            content=json.dumps(_whoami_payload(record), separators=(",", ":")),
            status_code=200,
            headers=_secure_headers({"content-type": "application/json"}),
        )

    @router.post("/logout")
    def logout(request: Request) -> Response:
        """Revoke the session and expire both cookies."""
        service = get_identity_service()
        record = service.authenticate(request)
        service.enforce_mutation(request, record)
        service.logout(request)
        response = Response(status_code=204, headers=_secure_headers())
        service.clear_session_cookies(response)
        return response

    for spec in PROXY_ROUTES:
        router.add_api_route(
            spec.path,
            _make_proxy_handler(spec, get_identity_service),
            methods=[spec.method],
            name=f"bff_{spec.method.lower()}_{spec.path.strip('/').replace('/', '_')}",
            tags=["bff"],
        )

    return router


__all__ = ["PROXY_ROUTES", "ProxyRoute", "build_bff_router"]
