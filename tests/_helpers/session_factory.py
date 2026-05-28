"""Canonical :class:`Session` construction helper for tests.

Stage B1 PR 2 of the post-refactoring plan inverted the composition root —
:meth:`Session.__init__` no longer takes the full bag of public/seam
kwargs; it now takes ``(*, collaborators, config, auth)`` and the
composition sequence lives in
:func:`notebooklm._session_init.compose_session_internals`. Tests that
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

from notebooklm._client_composed import ClientComposed
from notebooklm._client_seams import resolve_client_seams
from notebooklm._session import Session
from notebooklm._session_config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from notebooklm._session_init import ComposedSession, compose_session_internals
from notebooklm._session_lifecycle import CookieRotator, CookieSaver
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
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
    :func:`notebooklm._session_init.compose_session_internals`, which is the
    canonical composition root after Stage B1 PR 2 — so a test calling
    ``build_session_for_tests(auth)`` gets back a fully-composed
    :class:`Session` with ``_transport`` / ``_rpc_executor`` / chain
    pre-bound.

    The composition extras (``transport`` / ``executor`` /
    ``collaborators``) are not returned here because the vast majority
    of call sites only need the :class:`Session` instance; tests that
    want the full :class:`ComposedSession` bundle call
    :func:`build_composed_session_for_tests` (the same kwarg surface,
    returns the full bundle) — addressed to keep the kwarg-default
    layer this helper applies, rather than reaching directly to
    :func:`notebooklm._session_init.compose_session_internals`.
    """
    return build_composed_session_for_tests(
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
    ).session


def build_composed_session_for_tests(
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
) -> ComposedSession:
    """Same kwarg surface as :func:`build_session_for_tests` but returns the full bundle.

    Tests that need to construct a ``NotebookLMClient``-shaped shell via
    :func:`build_refresh_client_shell` need access to the full
    :class:`ComposedSession` (``session`` + ``executor`` +
    ``collaborators``), not just the :class:`Session` instance. This
    helper is a thin forwarder to
    :func:`notebooklm._session_init.compose_session_internals` that preserves
    the documented monkeypatch contract (seam resolution happens against
    ``notebooklm._session``'s module bindings, not this helper's).

    Wave 0 of the host-protocol-removal plan (see
    ``.sisyphus/phases/host-protocol-removal/phase-1.md``) introduced this
    helper as the canonical construction site for shell-client test
    fixtures so that the later deletion of ``Session.lifecycle`` (Wave 2)
    is mechanical — no shell-test code has to reach back through
    ``session.lifecycle`` because the helper hands the runtime fields to
    :func:`build_refresh_client_shell` directly.
    """
    seams = resolve_client_seams(
        decode_response=decode_response,
        sleep=sleep,
        is_auth_error=is_auth_error,
    )
    composed = ClientComposed(max_concurrent_rpcs=max_concurrent_rpcs)
    return compose_session_internals(
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
        async_client_factory=async_client_factory,
        seams=seams,
        composed=composed,
    )


def build_refresh_client_shell(composed: ComposedSession) -> NotebookLMClient:
    """Build a minimal :class:`NotebookLMClient` shell for refresh-path tests.

    Uses :meth:`NotebookLMClient.__new__` to bypass the heavy
    ``__init__`` side effects (feature-API construction, cross-validation,
    storage-path canonicalization) while still wiring the runtime attributes
    the refresh code path and holder assertions read off the client.

    The assignments below MUST stay in lock-step with
    :meth:`NotebookLMClient.__init__` so test shells and production
    aliases observe the same ``AuthTokens`` instance for the Auth
    Instance Invariant (see
    ``.sisyphus/phases/host-protocol-removal/phase-1.md``):
    ``composed.session.auth`` aliases the same object that flowed into
    :func:`notebooklm._session_init.compose_session_internals` and that the
    snapshot-provider lambdas captured, so setting
    ``client._auth = composed.session.auth`` here mirrors what
    ``self._auth = auth`` sets in production.

    The helper sources its runtime fields exclusively from
    :class:`ComposedSession` — there is no read-back through
    ``session.lifecycle`` — so the upcoming Wave 2 deletion of
    ``Session.lifecycle`` does not ripple into shell-test code.
    """
    client = NotebookLMClient.__new__(NotebookLMClient)
    client._session = composed.session
    client._auth = composed.session.auth
    client._collaborators = composed.collaborators
    client._rpc_executor = composed.executor
    client._seams = composed.seams
    client._composed = composed.composed
    return client
