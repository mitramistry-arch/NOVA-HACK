"""Pure ASGI middleware: request IDs, body-size limits (works for chunked uploads too)."""

import contextvars
import json
import re
import uuid
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def current_request_id() -> str:
    """Return the correlation id of the request being handled."""
    return request_id_var.get()


async def _send_json(send: Any, status: int, body: dict[str, Any], rid: str) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"x-request-id", rid.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RequestContextMiddleware:
    """Assign a correlation id (accepting a safe inbound X-Request-ID) and echo it back."""

    def __init__(self, app: Any) -> None:
        """Wrap an ASGI app."""
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = inbound if _SAFE_ID.fullmatch(inbound) else uuid.uuid4().hex
        token = request_id_var.set(rid)

        async def send_with_id(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", []) if h[0].lower() != b"x-request-id"]
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


class BodyLimitMiddleware:
    """Reject request bodies above ``max_bytes`` with 413, even without a Content-Length."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        """Wrap an ASGI app with a byte limit."""
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = current_request_id()
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = True
            if too_big:
                await _send_json(send, 413, _too_large(rid), rid)
                return

        seen = 0
        responded = False

        async def limited_receive() -> Any:
            nonlocal seen, responded
            message = await receive()
            if message["type"] == "http.request" and not responded:
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    responded = True
                    await _send_json(send, 413, _too_large(rid), rid)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Any) -> None:
            if not responded:
                await send(message)

        await self.app(scope, limited_receive, guarded_send)


def _too_large(rid: str) -> dict[str, Any]:
    return {"error": "request_too_large", "detail": "Request body is too large.", "request_id": rid}
