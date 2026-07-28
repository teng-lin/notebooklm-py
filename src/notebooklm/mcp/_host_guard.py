"""Host-header (DNS-rebinding) guard for the loopback-bound MCP HTTP transport.

The HTTP transport skips bearer auth on a loopback bind (like the REST server),
which leaves it open to DNS rebinding: a malicious page that resolves its own
domain to ``127.0.0.1`` can drive the local server with ``Host: evil.example`` and
reach every tool. This ASGI middleware rejects any request whose ``Host`` header is
not a loopback literal — mirroring the REST server's guard (``server/_auth`` /
issue #1869). It is skipped entirely when the operator has opted into an external
bind, where the REST-parity bearer/OAuth auth is mandatory instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .._serving import host_header_is_loopback

_Scope = dict[str, Any]
_Receive = Callable[[], Awaitable[dict[str, Any]]]
_Send = Callable[[dict[str, Any]], Awaitable[None]]


class LoopbackHostGuardMiddleware:
    """Reject HTTP requests whose ``Host`` header is not a loopback literal."""

    def __init__(self, app: Any, *, allow_external: bool) -> None:
        self.app = app
        self.allow_external = allow_external

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        # Only guard HTTP requests on a loopback-only bind; websockets/lifespan and
        # explicitly-external binds (which require auth) pass straight through.
        if self.allow_external or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        host = ""
        for name, value in scope.get("headers", ()):
            if name == b"host":
                host = value.decode("latin-1", "replace")
                break
        if not host_header_is_loopback(host):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"Host not allowed"})
            return
        await self.app(scope, receive, send)
