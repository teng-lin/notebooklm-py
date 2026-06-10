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
from typing import TYPE_CHECKING

from fastmcp import Context

if TYPE_CHECKING:
    from ..client import NotebookLMClient

__all__ = ["AppState", "get_client"]


@dataclass
class AppState:
    """Lifespan state: the single long-lived client bound to the server loop."""

    client: NotebookLMClient


def get_client(ctx: Context) -> NotebookLMClient:
    """Return the lifespan-bound client for the current tool call.

    Raises:
        RuntimeError: If called outside an active MCP request context (the
            lifespan binding is always present during a real tool invocation).
    """
    request_context = ctx.request_context
    if request_context is None:  # pragma: no cover - always set during a tool call
        raise RuntimeError("no active MCP request context")
    state: AppState = request_context.lifespan_context
    return state.client
