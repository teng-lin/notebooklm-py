"""Narrow capability Protocols consumed by feature APIs.

Sub-clients consume :class:`ClientCore` directly, typed against a
per-sub-client narrow Protocol (co-located in each sub-client file —
``_NotebooksCore``, ``_SourcesCore``, etc.). The base Protocols below
describe the discrete capability surfaces those narrow Protocols
compose; they live here so multiple sub-clients can share them without
dependency cycles. See ADR-002 for the design.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import httpx

from ._core_drain import _TransportOperationToken
from ._core_polling import PollRegistry
from .rpc.types import RPCMethod


class CoreRPCProvider(Protocol):
    """Provider for the core ``rpc_call`` entry point.

    Mirrors :meth:`ClientCore.rpc_call` exactly, including the kw-only
    ``disable_internal_retries`` flag used by mutating-create RPCs that
    must skip the inner 5xx/429 retry loop and the ``operation_variant``
    kwarg consulted by the mutating-RPC idempotency registry.
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
        operation_variant: str | None = None,
    ) -> Any: ...


class SourceListProvider(Protocol):
    """Provider for the notebook→source-id enumeration helper."""

    async def get_source_ids(self, notebook_id: str) -> list[str]: ...


class CoreReqIdProvider(Protocol):
    """Provider for the shared request-id counter."""

    async def next_reqid(self, step: int = 100000) -> int: ...


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
    """Provider for shared transport operation bookkeeping.

    Declares the underscore-private method names that
    :class:`ClientCore` exposes directly. After the D2 cutover,
    sub-clients call these on the live core without going through an
    adapter; the underscore prefix is preserved because the methods are
    package-private rather than part of any external public API.

    Method signatures mirror :class:`ClientCore` exactly (including the
    concrete :class:`_TransportOperationToken` return/parameter type) so
    a ``ClientCore`` instance structurally satisfies the Protocol under
    mypy's strict variance checks. Callers should treat the token as
    opaque.
    """

    async def _begin_transport_post(self, log_label: str) -> _TransportOperationToken: ...
    async def _begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> _TransportOperationToken: ...
    async def _finish_transport_post(self, token: _TransportOperationToken) -> None: ...


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
