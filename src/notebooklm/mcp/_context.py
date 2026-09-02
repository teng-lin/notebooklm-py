"""Per-request access to the lifespan-bound client.

The server binds exactly one :class:`~notebooklm.client.NotebookLMClient` for the
process lifetime via the FastMCP lifespan (one client, bound to the server's
event loop, satisfying the ADR-0004 loop-affinity contract). It is opened
**lazily** behind a :class:`~notebooklm.mcp._clientprovider.ClientProvider` so
the MCP handshake is not gated on Google's auth round-trip (#2330) — which makes
:func:`get_client` a coroutine: the first tool call to reach it awaits the open
(or joins the lifespan's background warm-up) and any auth failure surfaces there
as a categorized tool error. Tools reach it through the request context. Keeping
this in one place means the tool modules never touch FastMCP internals directly.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from fastmcp import Context

from ._chattasks import ChatTaskRegistry
from ._clientprovider import ClientProvider

if TYPE_CHECKING:
    from starlette.requests import Request

    from ..client import NotebookLMClient
    from ._filelink import FileTransferConfig

__all__ = [
    "AppState",
    "CancelledResearchTracker",
    "get_cancelled_research",
    "get_chat_tasks",
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

    ``chat_tasks`` is the bounded registry of detached chat asks backing
    ``chat_start`` / ``chat_status`` / ``chat_cancel`` — see
    :class:`~notebooklm.mcp._chattasks.ChatTaskRegistry`. Same process-scoped
    in-memory contract; the lifespan cancels its running tasks (``aclose``)
    before the client closes.

    ``client_provider`` owns the lazily-opened client (#2330); read it through
    :func:`get_client`, never by touching the provider from a tool.
    """

    client_provider: ClientProvider
    file_transfer: FileTransferConfig | None = None
    cancelled_research: CancelledResearchTracker = field(default_factory=CancelledResearchTracker)
    chat_tasks: ChatTaskRegistry = field(default_factory=ChatTaskRegistry)


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


async def get_client(ctx: Context) -> NotebookLMClient:
    """Return the lifespan-bound client for the current tool call, opening it first.

    A coroutine because the client is opened lazily (#2330): the first caller
    awaits the auth round-trip (or joins the lifespan's background warm-up)
    instead of the MCP handshake blocking on it. A failed open is re-tried by the
    next call rather than poisoning the server for its lifetime.

    Raises:
        RuntimeError: If called outside an active MCP request context (the
            lifespan binding is always present during a real tool invocation),
            or if the server is shutting down.
        Exception: Whatever the open path raised (auth, network) — tool bodies run
            inside ``mcp_errors``, which projects it onto a structured MCP error.
    """
    return await _app_state(ctx).client_provider.get()


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


def get_chat_tasks(ctx: Context) -> ChatTaskRegistry:
    """Return the detached-chat-task registry for the current tool call.

    The live :class:`~notebooklm.mcp._chattasks.ChatTaskRegistry` backing
    ``chat_start`` / ``chat_status`` / ``chat_cancel``: the start tool claims a
    slot and spawns the server-owned ask here; the status and cancel tools inspect
    or stop it. Returns the live registry so callers mutate it in place. Mirrors
    :func:`get_client`.
    """
    return _app_state(ctx).chat_tasks


def get_file_transfer(ctx: Context) -> FileTransferConfig | None:
    """Return the file-transfer config bound at lifespan, or ``None`` if unset.

    ``None`` means the deployment has no signed-URL side-channel (stdio, or http
    without a public URL), so the file tools fall back to / reject the path-based
    behavior. Mirrors :func:`get_client`.
    """
    return _app_state(ctx).file_transfer


async def get_client_from_app(request: Request) -> NotebookLMClient:
    """Return the lifespan-bound client from a bare Starlette ``Request``.

    The ``/files/*`` custom routes receive a Starlette :class:`Request`, not an
    MCP :class:`Context`, so they cannot use :func:`get_client`. FastMCP sets
    itself on ``request.app.state.fastmcp_server`` and stores the lifespan result
    (our :class:`AppState`) on ``._lifespan_result``, guarded by
    ``._lifespan_result_set``. Both are FastMCP **private** attributes — a
    regression test pins this access path so a FastMCP upgrade that changes either
    fails loudly.

    Awaits the lazy open exactly like :func:`get_client` does, so a ``/files/*``
    request that lands before the background warm-up finished still gets a live
    client instead of a 500.

    Raises:
        RuntimeError: the lifespan has not bound the provider yet (the route then
            returns 500 rather than crashing), or the server is shutting down.
    """
    server = request.app.state.fastmcp_server
    if not getattr(server, "_lifespan_result_set", False):
        raise RuntimeError("MCP lifespan client is not bound")
    state = cast("AppState", server._lifespan_result)
    return await state.client_provider.get()
