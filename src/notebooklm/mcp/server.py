"""FastMCP server construction for notebooklm-py.

Design highlights:

- **One client per configured profile, opened lazily.** The FastMCP lifespan binds a
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
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType
from typing import Literal, cast

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from .._adapter_support import DEFAULT_SERVER_KEEPALIVE_INTERVAL
from .._app.profiles import configured_profiles, profile_startup_timeout
from ..client import NotebookLMClient
from ..paths import get_active_profile, resolve_profile, set_active_profile
from ._clientprovider import ClientProvider
from ._context import AppState, ProfileRegistry
from ._filelink import FileTransferConfig
from ._profiles import PROFILE_STARTUP_TIMEOUT_ENV, ProfileTools, profile_lifespan

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
    profiles: Sequence[str] | None = None,
    profile_client_factory: Callable[[str], AbstractAsyncContextManager[NotebookLMClient]]
    | None = None,
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
        profiles: Static profile allowlist. Multiple entries require Android and
            a profile argument on every tool; one entry keeps single-profile behavior.
        profile_client_factory: Test seam for per-profile async client contexts.
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
        client per configured profile and which has every tool module registered.
    """

    if profiles is not None and profile is not None:
        raise ValueError("profile and profiles are mutually exclusive")
    paths = configured_profiles(profiles) if profiles is not None else {}
    multi_profile = len(paths) > 1
    if paths and not multi_profile:
        profile = next(iter(paths))
    if multi_profile and (backend or os.environ.get("NOTEBOOKLM_BACKEND", "web")) != "android":
        raise ValueError("Multi-profile MCP requires backend='android'")
    if multi_profile and client_factory is not None:
        raise ValueError("Use profile_client_factory for multi-profile clients")
    if not multi_profile and profile_client_factory is not None:
        raise ValueError("profile_client_factory requires multiple profiles")
    timeout = profile_startup_timeout(PROFILE_STARTUP_TIMEOUT_ENV) if multi_profile else 0.0

    def _default_factory() -> AbstractAsyncContextManager[NotebookLMClient]:
        # from_storage returns a dual awaitable/async-context-manager; we use only
        # the async-context-manager protocol.
        from .._app.client_config import adapter_client_config

        return cast(
            "AbstractAsyncContextManager[NotebookLMClient]",
            NotebookLMClient.from_storage(
                profile=profile,
                config=adapter_client_config(
                    backend=backend, keepalive=DEFAULT_SERVER_KEEPALIVE_INTERVAL
                ),
            ),
        )

    factory = client_factory or _default_factory

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[AppState | ProfileRegistry]:
        if multi_profile:
            async with profile_lifespan(
                paths, profile_client_factory, timeout, file_transfer
            ) as registry:
                yield registry
            return
        previous_profile = get_active_profile()
        set_active_profile(resolve_profile(profile))
        try:
            provider = ClientProvider(factory)
            state = AppState(
                client_provider=provider,
                file_transfer=file_transfer,
                profile=resolve_profile(profile),
            )
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

    instructions = SERVER_INSTRUCTIONS
    if multi_profile:
        instructions += (
            " Every tool call requires an explicit profile argument. Configured profiles: "
            + ", ".join(paths)
            + ". Profiles route accounts; the server credential grants access to all profiles."
        )
    mcp = FastMCP(name=SERVER_NAME, instructions=instructions, lifespan=lifespan, auth=auth)
    registrar = cast(FastMCP, ProfileTools(mcp)) if multi_profile else mcp
    register_all(registrar)
    if file_transfer is not None:
        # Import lazily so a build without file transfer never imports the route
        # module (and stdio stays untouched).
        from ._fileroutes import register_file_routes

        register_file_routes(mcp, file_transfer)
    # Dev-only in-app upload widget (Phase 3 experiment). No-op unless NOTEBOOKLM_MCP_UPLOAD_WIDGET=1,
    # so it never enters the prod manifest. Lazy import keeps the fastmcp.apps dependency off the
    # default path.
    from ._uploadwidget import register_upload_widget

    register_upload_widget(registrar, file_transfer)
    return mcp
