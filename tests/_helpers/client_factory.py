"""Canonical :class:`NotebookLMClient` shell construction helper for tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from notebooklm._client_composed import ClientComposed
from notebooklm._client_seams import resolve_client_seams
from notebooklm._session_config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from notebooklm._session_init import compose_client_internals
from notebooklm._session_lifecycle import CookieRotator, CookieSaver
from notebooklm._source_upload import SourceUploadPipeline
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
    """Build a minimal client shell with composed runtime attributes populated.

    The helper preserves the historical test-only seam kwargs without adding
    them to :class:`NotebookLMClient`'s public constructor. It intentionally
    does not construct feature API attributes; tests that need the public
    feature surface should instantiate :class:`NotebookLMClient` directly.
    """
    seams = resolve_client_seams(
        decode_response=decode_response,
        sleep=sleep,
        is_auth_error=is_auth_error,
    )
    composed = ClientComposed(max_concurrent_rpcs=max_concurrent_rpcs)
    internals = compose_client_internals(
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

    client = NotebookLMClient.__new__(NotebookLMClient)
    client._auth = auth
    client._seams = seams
    client._composed = composed
    client._collaborators = internals.collaborators
    client._rpc_executor = internals.executor
    # The shell skips feature-API construction, but ``ClientLifecycle.open``
    # (driven via ``client.__aenter__``) now resets the upload semaphore's
    # loop binding through ``client._source_uploader`` (issue #1196 upload
    # variant), so the shell must wire a real uploader the same way
    # ``NotebookLMClient.__init__`` does.
    client._source_uploader = SourceUploadPipeline(
        rpc=internals.executor,
        drain=internals.collaborators.drain_tracker,
        lifecycle=internals.collaborators.lifecycle,
        kernel=internals.collaborators.kernel,
        auth=auth,
        max_concurrent_uploads=max_concurrent_uploads,
        record_upload_queue_wait=internals.collaborators.metrics.record_upload_queue_wait,
    )
    return client
