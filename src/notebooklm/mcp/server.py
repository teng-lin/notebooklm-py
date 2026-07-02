"""FastMCP server construction for notebooklm-py.

Design highlights:

- **One client per process, bound at lifespan.** The FastMCP lifespan opens a
  single :class:`~notebooklm.client.NotebookLMClient` via
  ``from_storage(profile=...)`` inside the server loop (satisfies the ADR-0004
  loop-affinity contract) and keeps it for the process lifetime. Its keepalive
  task gives long sessions cookie rotation for free.
- **Transport-neutral.** Tools are thin adapters over the ``_app/`` cores; this
  package imports NO ``click`` / ``rich`` / ``cli`` (enforced by
  ``tests/_guardrails/test_mcp_boundary.py``).
- **Tools register through :func:`register_all`.** Phase 1 ships no tools yet —
  the registration seam is in place and tool modules plug in additively in
  Phase 2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from ..client import NotebookLMClient
from ._context import AppState
from ._filelink import FileTransferConfig

__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "create_server", "register_all"]

SERVER_NAME = "notebooklm"

SERVER_INSTRUCTIONS = (
    "Drive Google NotebookLM: manage notebooks and sources, chat with a "
    "notebook's sources, generate and download studio artifacts (audio, video, "
    "reports, quizzes, …), and run deep research. Notebook- and source-scoped "
    "tools accept a name OR an id (full or unique prefix); use the matching "
    "*_list tool to discover them. Long-running generation is split into a "
    "non-blocking generate step (returns a task_id) plus status polling. "
    "Destructive tools require `confirm=true`; called without it they return a "
    "`needs_confirmation` preview. Errors arrive as `CODE: message "
    "(retriable=…)`."
)

#: A factory returns an async-context-manager that yields the client. The default
#: factory binds ``NotebookLMClient.from_storage(profile=...)``; tests inject a
#: factory yielding a mock so no real auth/network is needed.
ClientFactory = Callable[[], AbstractAsyncContextManager[NotebookLMClient]]


def register_all(mcp: FastMCP) -> None:
    """Register every tool module on ``mcp``.

    Kept as a single chokepoint so the manifest guardrail has one place to reason
    about the full tool set. Phase 2a wired the notebooks/sources/chat/notes
    domains; Phase 2b added the artifacts/research/meta domains; the sharing
    domain followed.
    """
    from .tools import chat, meta, notebooks, notes, research, sharing, sources, studio

    for module in (notebooks, sources, chat, notes, studio, research, sharing, meta):
        module.register(mcp)


def create_server(
    *,
    profile: str | None = None,
    client_factory: ClientFactory | None = None,
    auth: AuthProvider | None = None,
    file_transfer: FileTransferConfig | None = None,
) -> FastMCP:
    """Build the FastMCP server.

    Args:
        profile: Auth profile bound for the whole process. Defaults to the active
            profile when ``None``.
        client_factory: Test seam — a zero-arg callable returning an async context
            manager that yields a client. Defaults to
            ``NotebookLMClient.from_storage(profile=...)``.
        auth: Optional FastMCP auth provider gating the HTTP transport. Passed
            **explicitly** by the caller — this function never reads
            ``NOTEBOOKLM_MCP_TOKEN`` itself, so stdio runs and the unit suite
            never silently attach auth (the token check + provider build live in
            :mod:`.__main__`, only on the network-bound http path).
        file_transfer: Optional remote file-transfer config (signer + validated
            public base URL). When set, the two file tools emit signed URLs and the
            ``/files/*`` routes are mounted on the http app; when ``None`` (stdio,
            or http without a public URL) the tools keep / reject the path-based
            behavior and no routes are mounted (ADR-0024). Built only on the
            network-bound http path in :mod:`.__main__`.

    Returns:
        A configured :class:`~fastmcp.FastMCP` server whose lifespan binds one
        client and which has every tool module registered.
    """

    def _default_factory() -> AbstractAsyncContextManager[NotebookLMClient]:
        # from_storage returns a dual awaitable/async-context-manager; we use only
        # the async-context-manager protocol.
        return cast(
            "AbstractAsyncContextManager[NotebookLMClient]",
            NotebookLMClient.from_storage(profile=profile),
        )

    factory = client_factory or _default_factory

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[AppState]:
        async with factory() as client:
            yield AppState(client=client, file_transfer=file_transfer)

    mcp = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS, lifespan=lifespan, auth=auth)
    register_all(mcp)
    if file_transfer is not None:
        # Import lazily so a build without file transfer never imports the route
        # module (and stdio stays untouched).
        from ._fileroutes import register_file_routes

        register_file_routes(mcp, file_transfer)
    return mcp
