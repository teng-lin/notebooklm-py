"""Android profile lifecycle and explicit, transport-neutral tool routing."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import replace
from functools import wraps
from pathlib import Path
from types import TracebackType
from typing import Any

from fastmcp import FastMCP

from .._app.android_profiles import android_profile_client
from .._app.profile_client import ProfileClientOwner
from ..client import NotebookLMClient
from ..exceptions import ServerError
from ._clientprovider import ClientFactory, ClientProvider
from ._context import AppState, ProfileRegistry, get_profile_state, selected_profile
from ._errors import mcp_errors
from ._filelink import FileTransferConfig

PROFILES_ENV = "NOTEBOOKLM_MCP_PROFILES"
PROFILE_STARTUP_TIMEOUT_ENV = "NOTEBOOKLM_MCP_PROFILE_STARTUP_TIMEOUT"
_RETRY_INTERVAL = 5.0
logger = logging.getLogger(__name__)


class ProfileClientProvider(ClientProvider):
    """Lazy single-flight client with a deadline and isolated, owned cleanup."""

    def __init__(self, factory: ClientFactory, timeout: float) -> None:
        super().__init__(factory)
        self._owner = ProfileClientOwner()
        self._timeout = timeout
        self._retry_not_before = 0.0

    def start(self) -> None:
        self._owner._assert_loop()
        super().start()

    async def get(self) -> NotebookLMClient:
        self._owner._assert_loop()
        return await super().get()

    async def _open(self) -> NotebookLMClient:
        if time.monotonic() < self._retry_not_before:
            raise ServerError("Selected Android profile is unavailable", status_code=503)
        try:
            client = await self._owner.open(self._factory, self._timeout)
        except Exception as exc:
            logger.warning("Android profile open failed (%s)", type(exc).__name__)
            self._retry_not_before = time.monotonic() + _RETRY_INTERVAL
            raise ServerError("Selected Android profile is unavailable", status_code=503) from None
        self._client = client
        return client

    async def aclose(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> bool | None:
        self._owner._assert_loop()
        try:
            await super().aclose(exc_type, exc, tb)
        finally:
            suppressed = await self._owner.close(exc_type, exc, tb)
        return suppressed


@asynccontextmanager
async def profile_lifespan(
    paths: dict[str, Path],
    factory: Callable[[str], AbstractAsyncContextManager[NotebookLMClient]] | None,
    timeout: float,
    file_transfer: FileTransferConfig | None,
) -> AsyncIterator[ProfileRegistry]:
    """Warm profiles independently without delaying MCP initialize."""
    registry = ProfileRegistry({})
    async with AsyncExitStack() as stack:
        for name, path in paths.items():
            client_factory = (
                (lambda name=name: factory(name))
                if factory is not None
                else (lambda path=path: android_profile_client(path))
            )
            provider = ProfileClientProvider(client_factory, timeout)
            state = AppState(
                client_provider=provider,
                profile=name,
                storage_path=path,
                file_transfer=(
                    replace(file_transfer, profile=name) if file_transfer is not None else None
                ),
            )
            registry.profiles[name] = state
            # LIFO: detached work stops before its client closes. Register every
            # owner before starting any task, including partial-startup failures.
            stack.push_async_exit(provider.aclose)
            stack.callback(state.chat_tasks.set_bound_loop, None)
            stack.push_async_callback(state.chat_tasks.aclose)
            state.chat_tasks.set_bound_loop(asyncio.get_running_loop())
            state.chat_tasks.reset_after_open()
            provider.start()
        yield registry


class ProfileTools:
    """Registration facade adding a required profile argument to every tool.

    The original signature (including Context injection) and tool annotations
    are preserved. A ContextVar routes only this invocation and its detached
    tasks; concurrent calls cannot change each other's selected account.
    """

    def __init__(self, server: FastMCP) -> None:
        self._server = server

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        bare_fn = args[0] if args and callable(args[0]) else None
        decorator_args = args[1:] if bare_fn is not None else args

        def decorate(fn: Any) -> Any:
            signature = inspect.signature(fn, eval_str=True)

            @wraps(fn)
            async def routed(*call_args: Any, **call_kwargs: Any) -> Any:
                profile = call_kwargs.pop("profile")
                token = selected_profile.set(profile)
                try:
                    with mcp_errors():
                        bound = signature.bind(*call_args, **call_kwargs)
                        get_profile_state(bound.arguments["ctx"])
                    return await fn(*call_args, **call_kwargs)
                finally:
                    selected_profile.reset(token)

            routed.__signature__ = signature.replace(  # type: ignore[attr-defined]
                parameters=[
                    *signature.parameters.values(),
                    inspect.Parameter("profile", inspect.Parameter.KEYWORD_ONLY, annotation=str),
                ]
            )
            routed.__annotations__ = {
                **{name: p.annotation for name, p in signature.parameters.items()},
                "profile": str,
                "return": signature.return_annotation,
            }
            return self._server.tool(*decorator_args, **kwargs)(routed)

        return decorate(bare_fn) if bare_fn is not None else decorate
