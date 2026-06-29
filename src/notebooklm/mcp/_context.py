"""Per-request access to the lifespan-bound client.

The server binds exactly one :class:`~notebooklm.client.NotebookLMClient` for the
process lifetime via the FastMCP lifespan (one client, bound to the server's
event loop, satisfying the ADR-0004 loop-affinity contract). Tools reach it
through the request context. Keeping this in one place means the tool modules
never touch FastMCP internals directly.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from fastmcp import Context

if TYPE_CHECKING:
    from starlette.requests import Request

    from ..client import NotebookLMClient
    from ._filelink import FileTransferConfig

__all__ = ["AppState", "get_client", "get_client_from_app", "get_file_transfer"]


@dataclass
class AppState:
    """Lifespan state: the single long-lived client bound to the server loop.

    ``file_transfer`` is the optional remote file-transfer config (signer +
    validated public base URL); ``None`` on stdio and on an http deployment
    without a public URL (ADR-0024).
    """

    client: NotebookLMClient
    file_transfer: FileTransferConfig | None = None


def _app_state(ctx: Context) -> AppState:
    """Return the lifespan-bound :class:`AppState` for the current tool call.

    Raises:
        RuntimeError: If called outside an active MCP request context (the
            lifespan binding is always present during a real tool invocation).
    """
    request_context = ctx.request_context
    if request_context is None:  # pragma: no cover - always set during a tool call
        raise RuntimeError("no active MCP request context")
    return cast("AppState", request_context.lifespan_context)


def get_client(ctx: Context) -> NotebookLMClient:
    """Return the lifespan-bound client for the current tool call.

    Raises:
        RuntimeError: If called outside an active MCP request context (the
            lifespan binding is always present during a real tool invocation).
    """
    return _app_state(ctx).client


def get_file_transfer(ctx: Context) -> FileTransferConfig | None:
    """Return the file-transfer config bound at lifespan, or ``None`` if unset.

    ``None`` means the deployment has no signed-URL side-channel (stdio, or http
    without a public URL), so the file tools fall back to / reject the path-based
    behavior. Mirrors :func:`get_client`.
    """
    return _app_state(ctx).file_transfer


def get_client_from_app(request: Request) -> NotebookLMClient:
    """Return the lifespan-bound client from a bare Starlette ``Request``.

    The ``/files/*`` custom routes receive a Starlette :class:`Request`, not an
    MCP :class:`Context`, so they cannot use :func:`get_client`. FastMCP sets
    itself on ``request.app.state.fastmcp_server`` and stores the lifespan result
    (our :class:`AppState`) on ``._lifespan_result``, guarded by
    ``._lifespan_result_set``. Both are FastMCP **private** attributes — a
    regression test pins this access path so a FastMCP upgrade that changes either
    fails loudly.

    Raises:
        RuntimeError: the lifespan has not bound the client yet (the route then
            returns 500 rather than crashing).
    """
    server = request.app.state.fastmcp_server
    if not getattr(server, "_lifespan_result_set", False):
        raise RuntimeError("MCP lifespan client is not bound")
    state = cast("AppState", server._lifespan_result)
    return state.client
