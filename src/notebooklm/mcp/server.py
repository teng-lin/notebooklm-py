"""FastMCP server construction for notebooklm-py.

Design highlights:

- **One client per process, opened lazily.** The FastMCP lifespan binds a
  :class:`~notebooklm.mcp._clientprovider.ClientProvider` over
  ``from_storage(profile=..., keepalive=600.0)``, starts the open in the
  background, and yields *immediately* — the MCP ``initialize`` handshake is
  never gated on Google's auth round-trip (#2330). The open runs on the server
  loop, so the client still satisfies the ADR-0004 loop-affinity contract, and
  is kept for the process lifetime; its keepalive task gives long sessions
  cookie rotation for free.
- **Transport-neutral.** Tools are thin adapters over the ``_app/`` cores; this
  package imports NO ``click`` / ``rich`` / ``cli`` (enforced by
  ``tests/_guardrails/test_mcp_boundary.py``).
- **Tools register through :func:`register_all`.** Phase 1 ships no tools yet —
  the registration seam is in place and tool modules plug in additively in
  Phase 2.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType
from typing import Literal, cast

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from .._runtime.config import DEFAULT_SERVER_KEEPALIVE_INTERVAL
from ..client import NotebookLMClient
from ..paths import get_active_profile, resolve_profile, set_active_profile
from ._clientprovider import ClientProvider
from ._context import AppState
from ._filelink import FileTransferConfig

__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "create_server", "register_all"]

SERVER_NAME = "notebooklm"

SERVER_INSTRUCTIONS = (
    "Drive Google NotebookLM: manage notebooks and sources, chat with a "
    "notebook's sources, generate and download studio artifacts (audio, video, "
    "reports, quizzes, …), and run deep research. Notebook- and source-scoped "
    "tools accept a name OR an id (full or unique prefix); use the matching "
    "*_list tool to discover them (set NOTEBOOKLM_MCP_STRICT_IDS=1 to require "
    "full canonical ids and reject names/prefixes, for deterministic automation). "
    "Long-running generation is split into a "
    "non-blocking generate step (returns a task_id) plus status polling. "
    "Destructive tools — and sharing-widening tools (making a notebook public, "
    "granting a user access) — require `confirm=true`; called without it they "
    "return a `needs_confirmation` preview. Errors arrive as `CODE: message "
    "(retriable=…)`."
)

#: A factory returns an async-context-manager that yields the client. The default
#: factory binds ``NotebookLMClient.from_storage(profile=..., keepalive=600.0)``;
#: tests inject a factory yielding a mock so no real auth/network is needed.
ClientFactory = Callable[[], AbstractAsyncContextManager[NotebookLMClient]]


def register_all(mcp: FastMCP) -> None:
    """Register every tool module on ``mcp``.

    Kept as a single chokepoint so the manifest guardrail has one place to reason
    about the full tool set. Phase 2a wired the notebooks/sources/chat/notes
    domains; Phase 2b added the artifacts/research/meta domains; the sharing
    domain followed.
    """
    from .tools import (
        chat,
        meta,
        notebooks,
        notes,
        research,
        sharing,
        sources,
        sources_drive,
        sources_playbooks,
        studio,
    )

    for module in (
        notebooks,
        sources,
        sources_drive,
        sources_playbooks,
        chat,
        notes,
        studio,
        research,
        sharing,
        meta,
    ):
        module.register(mcp)

    # ``await_upload`` (Phase 1 upload-completion signal) lives in the ``_fileupload``
    # sibling of the sources domain — registered here rather than from ``sources.register``
    # so that fat module (at its ADR-0008 size cap) does not absorb the wiring.
    from .tools._fileupload import register_file_tools

    register_file_tools(mcp)


async def _shutdown(
    state: AppState,
    provider: ClientProvider,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> bool | None:
    """Tear the lifespan down, forwarding the body's exception (if any) to the client.

    Detached chat asks are cancelled BEFORE the provider closes the client, so no
    server-owned task ever touches a closing client (see ``ChatTaskRegistry.aclose``).
    The client context manager's suppression result is returned to the lifespan.
    """
    await state.chat_tasks.aclose()
    state.chat_tasks.set_bound_loop(None)
    return await provider.aclose(exc_type, exc, tb)


def create_server(
    *,
    profile: str | None = None,
    backend: Literal["web", "android"] | None = None,
    client_factory: ClientFactory | None = None,
    auth: AuthProvider | None = None,
    file_transfer: FileTransferConfig | None = None,
) -> FastMCP:
    """Build the FastMCP server.

    Args:
        profile: Auth profile bound for the whole process. Defaults to the active
            profile when ``None``. Also drives process-wide profile resolution
            for diagnostics such as the ``server_info`` tool.
        backend: Preferred API backend for the default client factory. An explicit
            value takes precedence over ``NOTEBOOKLM_BACKEND``.
        client_factory: Test seam — a zero-arg callable returning an async context
            manager that yields a client. Defaults to
            ``NotebookLMClient.from_storage(profile=..., keepalive=600.0)``.
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
        if backend is None:
            return cast(
                "AbstractAsyncContextManager[NotebookLMClient]",
                NotebookLMClient.from_storage(
                    profile=profile,
                    keepalive=DEFAULT_SERVER_KEEPALIVE_INTERVAL,
                ),
            )
        return cast(
            "AbstractAsyncContextManager[NotebookLMClient]",
            NotebookLMClient.from_storage(
                profile=profile,
                keepalive=DEFAULT_SERVER_KEEPALIVE_INTERVAL,
                backend=backend,
            ),
        )

    factory = client_factory or _default_factory

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[AppState]:
        previous_profile = get_active_profile()
        set_active_profile(resolve_profile(profile))
        try:
            provider = ClientProvider(factory)
            state = AppState(client_provider=provider, file_transfer=file_transfer)
            state.chat_tasks.set_bound_loop(asyncio.get_running_loop())
            state.chat_tasks.reset_after_open()
            # Warm the client on the server loop WITHOUT awaiting it: the auth
            # round-trip can outlast the client's handshake deadline, and gating
            # ``initialize`` on it is what surfaced as CONNECT_TIMEOUT (#2330).
            provider.start()
            try:
                yield state
            except BaseException as exc:
                # Forward the exact exception triple, then honor the client context
                # manager's suppression result just as ``async with factory()`` did.
                # NotebookLMClient normally returns false, but injected/embedded
                # factories may deliberately suppress a lifespan exception.
                suppressed = await _shutdown(state, provider, type(exc), exc, exc.__traceback__)
                if not suppressed:
                    raise
            else:
                await _shutdown(state, provider, None, None, None)
        finally:
            set_active_profile(previous_profile)

    mcp = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS, lifespan=lifespan, auth=auth)
    register_all(mcp)
    if file_transfer is not None:
        # Import lazily so a build without file transfer never imports the route
        # module (and stdio stays untouched).
        from ._fileroutes import register_file_routes

        register_file_routes(mcp, file_transfer)
    # Dev-only in-app upload widget (Phase 3 experiment). No-op unless NOTEBOOKLM_MCP_UPLOAD_WIDGET=1,
    # so it never enters the prod manifest. Lazy import keeps the fastmcp.apps dependency off the
    # default path.
    from ._uploadwidget import register_upload_widget

    register_upload_widget(mcp, file_transfer)
    return mcp
