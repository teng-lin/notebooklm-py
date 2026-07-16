"""Per-request access to the lifespan-bound client.

The server binds exactly one :class:`~notebooklm.client.NotebookLMClient` for the
process lifetime via the FastMCP lifespan (one client, bound to the server's
event loop, satisfying the ADR-0004 loop-affinity contract). Tools reach it
through the request context. Keeping this in one place means the tool modules
never touch FastMCP internals directly.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from fastmcp import Context

if TYPE_CHECKING:
    from starlette.requests import Request

    from ..client import NotebookLMClient
    from ._filelink import FileTransferConfig

__all__ = [
    "AppState",
    "CancelledResearchTracker",
    "get_cancelled_research",
    "get_client",
    "get_client_from_app",
    "get_file_transfer",
]

# Hard ceiling on retained cancel intents (issue #1922, F9). Cancels are rare
# and user-driven, so this is generous; it only guards against a pathological
# long-lived server that cancels many runs which are never polled to a terminal
# state (the usual eviction path). Oldest intents are dropped first (FIFO).
_CANCEL_INTENT_CAP = 1024


class CancelledResearchTracker:
    """Bounded, insertion-ordered set of cancelled ``(notebook_id, task_id)`` runs.

    Backs the client-side cancel-intent tracking for issue #1922 (F9):
    ``research_cancel`` records a run here so a later ``research_status`` poll
    can annotate the resulting generic ``failed`` as ``cancelled`` (the backend
    surfaces a user-cancelled run as ``FAILED`` with no distinct wire code).

    Bounded two ways so a long-running MCP server cannot leak memory:
    ``research_status`` evicts an intent (:meth:`discard`) once its run reaches a
    terminal poll, and a hard FIFO cap (:data:`_CANCEL_INTENT_CAP`) drops the
    oldest intents even if a cancelled run is never polled to a terminal state.
    """

    def __init__(self, cap: int = _CANCEL_INTENT_CAP) -> None:
        self._cap = cap
        # ``OrderedDict`` as an ordered set (values unused) — preserves insertion
        # order for FIFO eviction and gives O(1) membership / discard.
        self._items: OrderedDict[tuple[str, str], None] = OrderedDict()

    def record(self, key: tuple[str, str]) -> None:
        """Record a cancel intent, evicting the oldest entries past the cap."""
        self._items.pop(key, None)
        self._items[key] = None
        while len(self._items) > self._cap:
            self._items.popitem(last=False)

    def discard(self, key: tuple[str, str]) -> None:
        """Drop a cancel intent if present (no-op otherwise)."""
        self._items.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class AppState:
    """Lifespan state: the single long-lived client bound to the server loop.

    ``file_transfer`` is the optional remote file-transfer config (signer +
    validated public base URL); ``None`` on stdio and on an http deployment
    without a public URL (ADR-0024).

    ``cancelled_research`` is the bounded cancel-intent tracker for issue #1922
    (F9) — see :class:`CancelledResearchTracker`. Process-scoped in-memory state
    (no persistence, consistent with the loop-bound lifespan client).
    """

    client: NotebookLMClient
    file_transfer: FileTransferConfig | None = None
    cancelled_research: CancelledResearchTracker = field(default_factory=CancelledResearchTracker)


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


def get_cancelled_research(ctx: Context) -> CancelledResearchTracker:
    """Return the bounded cancel-intent tracker for the current tool call.

    The live :class:`CancelledResearchTracker` backing the client-side
    cancel-intent tracking (issue #1922, F9): ``research_cancel`` records an
    entry on a successful cancel and ``research_status`` reads it to annotate a
    later ``failed`` poll as ``cancelled`` (evicting it on the terminal poll).
    Returns the live tracker so callers mutate it in place. Mirrors
    :func:`get_client`.
    """
    return _app_state(ctx).cancelled_research


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
