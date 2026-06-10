"""Canonical :class:`NotebookLMClient` shell construction helper for tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from notebooklm._client_assembly import _assemble_client
from notebooklm._runtime.config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from notebooklm._runtime.lifecycle import CookieRotator, CookieSaver
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.types import RpcTelemetryEvent

if TYPE_CHECKING:
    from notebooklm.types import ConnectionLimits


def build_client_shell_for_tests(
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
) -> NotebookLMClient:
    """Build a client shell through the production assembly seam.

    The helper preserves the historical test-only seam kwargs without adding
    them to :class:`NotebookLMClient`'s public constructor: it creates an
    uninitialized instance and runs the same
    :func:`notebooklm._client_assembly._assemble_client` function that
    ``NotebookLMClient.__init__`` delegates to, forwarding the seam kwargs
    that only exist on the assembly function.

    Because the wiring is the production function itself, a constructor
    refactor can no longer strand this factory the way issues #1196
    (open-time upload-semaphore loop reset needed ``_source_uploader``)
    and #1225 (open-time ChatAPI conversation-lock reset needed ``chat``)
    did when this helper still hand-wired private attributes against
    ``NotebookLMClient.__new__``. The shell therefore carries the full
    production attribute surface (feature APIs included) — pinned by
    ``tests/_guardrails/test_client_factory_parity.py``.

    Shell-specific defaults (unchanged from the historical helper):

    - ``refresh_callback=None`` — no auth-refresh coordination unless a
      test injects one (production wires ``client.refresh_auth``).
    - ``keepalive_storage_path`` — passed through verbatim, bypassing the
      production canonicalization (``expanduser().resolve()``) of
      ``auth.storage_path``; an explicit ``None`` still falls through to
      ``compose_client_internals``' own raw ``auth.storage_path``
      fallback, as it always did.
    - The client is returned **unopened**: loop binding still happens at
      ``open()`` time (via ``__aenter__``), exactly as in production.
    """
    client = NotebookLMClient.__new__(NotebookLMClient)
    _assemble_client(
        client,
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
    return client
