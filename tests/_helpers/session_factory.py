"""Canonical :class:`Session` construction helper for tests.

Stage B1 PR 2 of the post-refactoring plan inverted the composition root —
:meth:`Session.__init__` no longer takes the full bag of public/seam
kwargs; it now takes ``(*, collaborators, config, auth)`` and the
composition sequence lives in
:func:`notebooklm._session.compose_session_internals`. Tests that
previously called ``Session(auth, …)`` directly migrate to this helper,
which preserves the full historical kwarg surface (the union of
``NotebookLMClient.__init__`` kwargs + the four test-only seam kwargs
``decode_response`` / ``sleep`` / ``is_auth_error`` /
``async_client_factory``) and routes through
:func:`compose_session_internals` under the hood.

The helper returns the fully-bound :class:`Session` directly so the
common ``core = Session(auth)`` → ``core.<attribute>`` pattern keeps
working as a drop-in (``core = build_session_for_tests(auth)``). Tests
that need the composition extras (the ``transport`` / ``executor`` /
``collaborators`` fields of :class:`ComposedSession`) read them off the
returned ``Session`` (``session._transport`` / ``session._rpc_executor`` /
``session._collaborators``) or call
:func:`compose_session_internals` directly.

Seam kwargs live ONLY on this helper and on
:func:`compose_session_internals` — they are NOT on
:class:`NotebookLMClient`'s public constructor (which preserves the
production surface).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from notebooklm._session import Session, compose_session_internals
from notebooklm._session_config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from notebooklm._session_lifecycle import CookieRotator, CookieSaver
from notebooklm.auth import AuthTokens
from notebooklm.types import RpcTelemetryEvent

if TYPE_CHECKING:
    from notebooklm.types import ConnectionLimits


def build_session_for_tests(
    auth: AuthTokens,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    refresh_callback: Callable[[], Awaitable[AuthTokens]] | None = None,
    refresh_retry_delay: float = 0.2,
    keepalive: float | None = None,
    keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
    keepalive_storage_path: Path | None = None,
    rate_limit_max_retries: int = 3,
    server_error_max_retries: int = 3,
    limits: ConnectionLimits | None = None,
    max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
    max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
    on_rpc_event: Callable[[RpcTelemetryEvent], object] | None = None,
    cookie_saver: CookieSaver | None = None,
    cookie_rotator: CookieRotator | None = None,
    *,
    decode_response: Callable[..., Any] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    is_auth_error: Callable[[Exception], bool] | None = None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> Session:
    """Drop-in replacement for the historical ``Session(auth, …)`` test pattern.

    Accepts the full historical kwarg surface (``auth`` positional or
    keyword + every other knob ``Session.__init__`` used to accept,
    including the four seam kwargs). Routes through
    :func:`notebooklm._session.compose_session_internals`, which is the
    canonical composition root after Stage B1 PR 2 — so a test calling
    ``build_session_for_tests(auth)`` gets back a fully-composed
    :class:`Session` with ``_transport`` / ``_rpc_executor`` / chain
    pre-bound.

    The composition extras (``transport`` / ``executor`` /
    ``collaborators``) are not returned here because the vast majority
    of call sites only need the :class:`Session` instance; tests that
    want the full :class:`ComposedSession` tuple call
    :func:`compose_session_internals` directly.
    """
    composed = compose_session_internals(
        auth=auth,
        timeout=timeout,
        connect_timeout=connect_timeout,
        refresh_callback=refresh_callback,
        refresh_retry_delay=refresh_retry_delay,
        keepalive=keepalive,
        keepalive_min_interval=keepalive_min_interval,
        keepalive_storage_path=keepalive_storage_path,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        limits=limits,
        max_concurrent_uploads=max_concurrent_uploads,
        max_concurrent_rpcs=max_concurrent_rpcs,
        on_rpc_event=on_rpc_event,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
        decode_response=decode_response,
        sleep=sleep,
        is_auth_error=is_auth_error,
        async_client_factory=async_client_factory,
    )
    return composed.session
