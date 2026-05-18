"""Private capability adapters for feature APIs."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import httpx

from ._core_polling import PollRegistry
from ._core_transport import _BuildRequest
from .auth import authuser_query, format_authuser_value
from .rpc.types import RPCMethod


class CoreRPCProvider(Protocol):
    """Provider for the core ``rpc_call`` entry point.

    Mirrors :meth:`ClientCore.rpc_call` exactly, including the kw-only
    ``disable_internal_retries`` flag used by mutating-create RPCs that
    must skip the inner 5xx/429 retry loop. Sub-clients that only need
    to issue RPC calls type their constructor on this provider rather
    than on the concrete ``ClientCore``.
    """

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any: ...


class SourceListProvider(Protocol):
    """Provider for the notebook→source-id enumeration helper."""

    async def get_source_ids(self, notebook_id: str) -> list[str]: ...


class CoreReqIdProvider(Protocol):
    """Provider for the shared request-id counter."""

    async def next_reqid(self, step: int = 100000) -> int: ...


class ChatStreamingProvider(Protocol):
    """Transitional chat-transport capability.

    Chat-aware error mapping still lives on ``ClientCore.query_post`` until
    that is extracted into a chat-owned transport.
    """

    async def query_post(
        self,
        *,
        build_request: _BuildRequest,
        parse_label: str,
    ) -> httpx.Response: ...


class PollRegistryProvider(Protocol):
    """Provider for the shared artifact polling registry."""

    @property
    def poll_registry(self) -> PollRegistry:
        """Return the existing per-core poll registry."""
        ...


class AuthRouteProvider(Protocol):
    """Provider for NotebookLM selected-account routing values."""

    @property
    def authuser(self) -> int:
        """Return the integer Google authuser index."""
        ...

    @property
    def account_email(self) -> str | None:
        """Return the stable selected-account email, when available."""
        ...

    def authuser_query(self) -> str:
        """Return the URL query value for NotebookLM auth routing."""
        ...

    def authuser_header(self) -> str:
        """Return the ``x-goog-authuser`` header value."""
        ...


class CookieJarProvider(Protocol):
    """Provider for the live HTTP client's cookie jar."""

    def live_cookies(self) -> httpx.Cookies:
        """Return the live HTTP-client cookies."""
        ...


class TransportOperationProvider(Protocol):
    """Provider for shared transport operation bookkeeping."""

    async def begin_transport_post(self, log_label: str) -> object: ...
    async def begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> object: ...
    async def finish_transport_post(self, token: object) -> None: ...


class UploadConcurrencyProvider(Protocol):
    """Provider for shared source-upload concurrency and queue metrics."""

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        """Return the existing per-core upload semaphore."""
        ...

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        """Record how long an upload waited for the semaphore."""
        ...


class LoopAffinityProvider(Protocol):
    """Provider for the open-time captured event-loop reference.

    Sub-clients that issue ``async`` calls touching loop-bound primitives
    (locks, semaphores, ``httpx.AsyncClient`` pools, condition variables)
    consult this property and forward it to
    :func:`notebooklm._loop_affinity.assert_bound_loop` so a cross-loop
    call surfaces an actionable ``RuntimeError`` at the call site rather
    than hanging on a lock bound to a dead loop.
    """

    @property
    def bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the loop ``ClientLifecycle.open`` captured, or ``None``
        if the client has not yet been opened.
        """
        ...


class ClientCoreCapabilities(
    CoreRPCProvider,
    SourceListProvider,
    CoreReqIdProvider,
    ChatStreamingProvider,
    PollRegistryProvider,
    AuthRouteProvider,
    CookieJarProvider,
    TransportOperationProvider,
    UploadConcurrencyProvider,
    LoopAffinityProvider,
):
    """Narrow capability adapter around a ``ClientCore``-shaped object.

    Construction is intentionally lazy: only store the core. Individual
    capability properties and methods read the underlying core when called.
    """

    def __init__(self, core: Any) -> None:
        self._core = core

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        return await self._core.rpc_call(
            method,
            params,
            source_path=source_path,
            allow_null=allow_null,
            _is_retry=_is_retry,
            disable_internal_retries=disable_internal_retries,
        )

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        return await self._core.get_source_ids(notebook_id)

    async def next_reqid(self, step: int = 100000) -> int:
        return await self._core.next_reqid(step)

    async def query_post(
        self,
        *,
        build_request: _BuildRequest,
        parse_label: str,
    ) -> httpx.Response:
        return await self._core.query_post(
            build_request=build_request,
            parse_label=parse_label,
        )

    @property
    def poll_registry(self) -> PollRegistry:
        return self._core.poll_registry

    @property
    def authuser(self) -> int:
        return self._core.auth.authuser

    @property
    def account_email(self) -> str | None:
        return self._core.auth.account_email

    def authuser_query(self) -> str:
        return authuser_query(self.authuser, self.account_email)

    def authuser_header(self) -> str:
        return format_authuser_value(self.authuser, self.account_email)

    def live_cookies(self) -> httpx.Cookies:
        return self._core.get_http_client().cookies

    async def begin_transport_post(self, log_label: str) -> object:
        return await self._core._begin_transport_post(log_label)

    async def begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> object:
        return await self._core._begin_transport_task(task, log_label)

    async def finish_transport_post(self, token: object) -> None:
        await self._core._finish_transport_post(token)

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        return self._core.get_upload_semaphore()

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        self._core.record_upload_queue_wait(wait_seconds)

    @property
    def bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the underlying core's open-time captured event loop.

        Reads through to ``ClientCore._lifecycle.get_bound_loop()`` so the
        capability adapter stays decoupled from the lifecycle helper's
        internal attribute layout. Returns ``None`` when the underlying
        core has no lifecycle (e.g. a ``MagicMock``-backed test fixture)
        or returns a non-loop value, so the affinity helper falls back to
        its silent no-op path rather than misclassifying a mock as a
        cross-loop call.
        """
        # ``ClientLifecycle.get_bound_loop()`` returns ``None`` if open()
        # has not been called; the affinity helper treats ``None`` as a
        # silent no-op, so this property is safe before open().
        lifecycle = getattr(self._core, "_lifecycle", None)
        if lifecycle is None:
            return None
        loop = lifecycle.get_bound_loop()
        # Defensive ``isinstance`` so a ``MagicMock``-shaped fixture
        # whose ``_lifecycle`` auto-vivifies into a mock doesn't
        # synthesize a fake loop object that the affinity helper would
        # otherwise treat as a real (mismatched) loop. Production paths
        # always store either ``None`` or a real ``AbstractEventLoop``.
        if isinstance(loop, asyncio.AbstractEventLoop):
            return loop
        return None
