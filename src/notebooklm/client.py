"""NotebookLM API Client - Main entry point.

This module provides the NotebookLMClient class, a modern async client
for interacting with Google NotebookLM using undocumented RPC APIs.

Example:
    async with await NotebookLMClient.from_storage() as client:
        # List notebooks
        notebooks = await client.notebooks.list()

        # Add sources
        source = await client.sources.add_url(notebook_id, "https://example.com")

        # Generate artifacts
        status = await client.artifacts.generate_audio(notebook_id)
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)

        # Chat with the notebook
        result = await client.chat.ask(notebook_id, "What is this about?")
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .rpc import RPCMethod
    from .types import ClientMetricsSnapshot, ConnectionLimits, RpcTelemetryEvent

from ._artifacts import ArtifactsAPI
from ._auth.session import refresh_auth_session
from ._chat import ChatAPI
from ._core import (
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
    ClientCore,
)
from ._env import get_base_url as get_base_url
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._research import ResearchAPI
from ._settings import SettingsAPI
from ._sharing import SharingAPI
from ._sources import SourcesAPI
from ._url_utils import is_google_auth_redirect as is_google_auth_redirect
from .auth import AuthTokens
from .auth import authuser_query as authuser_query
from .auth import extract_wiz_field as extract_wiz_field
from .exceptions import AuthExtractionError as AuthExtractionError

logger = logging.getLogger(__name__)


class NotebookLMClient:
    """Async client for NotebookLM API.

    Provides access to NotebookLM functionality through namespaced sub-clients:
    - notebooks: Create, list, delete, rename notebooks
    - sources: Add, list, delete sources (URLs, text, files, YouTube, Drive)
    - artifacts: Generate and manage AI content (audio, video, reports, etc.)
    - chat: Ask questions and manage conversations
    - research: Start research sessions and import sources
    - notes: Create and manage user notes
    - settings: Manage user settings (output language, etc.)
    - sharing: Manage notebook sharing and permissions

    Usage:
        # Create from saved authentication
        async with await NotebookLMClient.from_storage() as client:
            notebooks = await client.notebooks.list()

        # Create from AuthTokens directly
        auth = AuthTokens(cookies, csrf_token, session_id)
        async with NotebookLMClient(auth) as client:
            notebooks = await client.notebooks.list()

    Attributes:
        notebooks: NotebooksAPI for notebook operations
        sources: SourcesAPI for source management
        artifacts: ArtifactsAPI for AI-generated content
        chat: ChatAPI for conversations
        research: ResearchAPI for web/drive research
        notes: NotesAPI for user notes
        settings: SettingsAPI for user settings
        sharing: SharingAPI for notebook sharing
        auth: The AuthTokens used for authentication
    """

    def __init__(
        self,
        auth: AuthTokens,
        timeout: float = DEFAULT_TIMEOUT,
        storage_path: Path | None = None,
        keepalive: float | None = None,
        keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
        rate_limit_max_retries: int = 3,
        server_error_max_retries: int = 3,
        limits: ConnectionLimits | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
        upload_timeout: httpx.Timeout | None = None,
        on_rpc_event: Callable[[RpcTelemetryEvent], object] | None = None,
    ):
        """Initialize the NotebookLM client.

        Args:
            auth: Authentication tokens from browser login.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
            storage_path: Path to the storage state file for loading download cookies.
            keepalive: Optional interval in seconds for a background task that
                pokes ``accounts.google.com`` while the client is open, eliciting
                ``__Secure-1PSIDTS`` rotation so long-lived clients (e.g. agents,
                long-running workers) don't silently stale out. ``None`` (default)
                disables the task — preserving existing CLI semantics. Values
                below ``keepalive_min_interval`` are clamped up to that floor.
            keepalive_min_interval: Lower bound for ``keepalive`` (defaults to
                60 s) to avoid accidentally rate-limiting Google's identity
                surface.
            rate_limit_max_retries: Max automatic retries on HTTP 429.
                Defaults to ``3`` so programmatic users
                inherit "smart retry" behavior out of the box. Set to ``0``
                to raise ``RateLimitError`` immediately.
                Sleeps for ``Retry-After`` when the server provides a
                parseable header; otherwise falls back to capped exponential
                backoff ``min(2 ** attempt, 30)`` seconds with ±20% jitter.
                See :class:`ClientCore` for full sleep semantics.
            server_error_max_retries: Max automatic retries for retryable
                transient failures: HTTP 5xx and network-layer
                ``httpx.RequestError`` (timeouts, connect errors). Defaults to
                ``3``. Uses exponential backoff ``min(2 ** attempt, 30)``
                seconds. Set to ``0`` to disable.
            limits: HTTP connection-pool tuning (``ConnectionLimits``). ``None``
                (default) uses ``ConnectionLimits()`` defaults sized for typical
                batchexecute fan-out (max_connections=100,
                max_keepalive_connections=50, keepalive_expiry=30.0s). Widen
                for heavy batch workloads (FastAPI/Django services sharing one
                client across many concurrent requests).
            max_concurrent_uploads: Ceiling on simultaneous in-flight
                ``client.sources.add_file`` uploads. Defaults to ``4``. Each
                in-flight upload holds one open file descriptor for the
                duration of the upload, so the cap doubles as an
                FD-exhaustion guard against fan-out callers that would
                otherwise open dozens of files concurrently and exhaust
                the per-process FD limit. ``None``
                resolves to the default — unbounded uploads are
                intentionally rejected. Must be ``>= 1`` when supplied.
                Independent of the RPC pool sizing (uploads use their own
                ``httpx.AsyncClient`` against the Scotty endpoint and
                don't share the RPC connection pool).
            max_concurrent_rpcs: Ceiling on simultaneous in-flight RPC
                POSTs (``client.notebooks.list``, ``client.chat.ask``,
                etc.). Defaults to ``16`` — well below the default
                ``ConnectionLimits.max_connections=100`` so short-lived
                helper requests (auth refresh GETs, upload preflights)
                still have pool headroom. Pass ``None`` to disable the
                gate entirely; useful when an external rate-limiter is
                in front of the client or for single-shot CLI commands
                where the throttle is overhead. Must be ``>= 1`` when
                supplied, and must satisfy ``max_concurrent_rpcs <=
                limits.max_connections`` — the constructor raises
                ``ValueError`` otherwise (a semaphore that lets requests
                through that the pool can't fulfill would surface as
                opaque ``httpx.PoolTimeout`` rather than clean
                back-pressure). Before this gate was added, heavy
                fan-out workloads tripped pool timeouts before any
                upstream throttle could intervene.
            upload_timeout: Optional override for the ``httpx.Timeout`` used
                by the resumable-upload start handshake and the finalize
                POST in ``client.sources.add_file``. ``None`` (default)
                preserves the original hardcoded values (10.0s connect /
                60.0s read for start; 10.0s connect / 300.0s read for
                finalize). The supplied ``Timeout`` is used wholesale at
                both upload sites — specify all components explicitly
                (e.g. ``httpx.Timeout(10.0, read=600.0)``), or partial
                fields will fall back to httpx's own 5.0s defaults rather
                than the original 10.0s connect. Defaults are NOT changed
                silently for back-compat.
            on_rpc_event: Optional sync or async callback invoked after each
                logical RPC succeeds or fails. The callback receives a
                backend-agnostic ``RpcTelemetryEvent`` so applications can
                forward telemetry to logging, Prometheus, OpenTelemetry, or
                another metrics backend without this package depending on one.
        """
        # Normalize the effective storage path onto the auth object so every
        # downstream code path (refresh_auth, ClientCore.close on-close save,
        # the keepalive loop) writes to the same file. Without this, an
        # explicit ``storage_path=`` kwarg only reaches the keepalive loop
        # while ``auth.storage_path is None`` causes refresh and on-close
        # saves to silently skip persistence. ``dataclasses.replace`` instead
        # of in-place mutation so a caller reusing ``AuthTokens`` across
        # multiple clients (with different storage paths) doesn't see one
        # client's path leak into another.
        if storage_path is not None and auth.storage_path != storage_path:
            auth = dataclasses.replace(auth, storage_path=storage_path)

        # Canonicalize the keepalive storage path so different representations
        # of the same physical file (relative vs absolute, ``~`` shorthand,
        # symlink components) hash to the same key in the in-process rotation
        # dedupe (``_get_poke_lock`` / ``_try_claim_rotation`` /
        # ``_rotation_lock_path`` in auth.py). The auth refresh path already
        # canonicalizes at ``auth.py:_fetch_tokens_with_refresh`` via
        # ``Path(p).expanduser().resolve()``; this mirrors it so two clients
        # pointing at the same file via different path syntaxes share one
        # ``_LAST_POKE_ATTEMPT_MONOTONIC`` entry instead of bypassing dedupe
        # and firing duplicate ``RotateCookies`` POSTs.
        # NOTE: the public ``storage_path`` argument and ``auth.storage_path``
        # are intentionally left as the caller provided them — only the
        # internal-derived ``ClientCore._keepalive_storage_path`` is
        # canonicalized.
        keepalive_storage_path: Path | None = auth.storage_path
        if keepalive_storage_path is not None:
            keepalive_storage_path = Path(keepalive_storage_path).expanduser().resolve()

        # Cross-validate the RPC throttle against the underlying httpx pool
        # before ``ClientCore`` swallows the ``limits=None`` sentinel into
        # its own ``ConnectionLimits()`` synthesis.
        # Performed here so the constraint is enforced uniformly regardless
        # of whether the caller passed an explicit ``ConnectionLimits``
        # instance or relied on the default — ``ClientCore.__init__`` can't
        # see the caller's intent once the default has been substituted.
        # Skip when either side opts out (``max_concurrent_rpcs is None``
        # means "no gate"; we deliberately don't second-guess the caller's
        # external-throttle setup).
        if max_concurrent_rpcs is not None:
            from .types import ConnectionLimits

            effective_limits = limits if limits is not None else ConnectionLimits()
            if max_concurrent_rpcs > effective_limits.max_connections:
                raise ValueError(
                    "max_concurrent_rpcs must be <= limits.max_connections "
                    f"(got max_concurrent_rpcs={max_concurrent_rpcs}, "
                    f"max_connections={effective_limits.max_connections}). "
                    "A semaphore wider than the connection pool surfaces "
                    "saturation as opaque httpx.PoolTimeout instead of "
                    "clean back-pressure."
                )

        # Pass refresh_auth as callback for automatic retry on auth failures
        # Note: refresh_auth calls update_auth_headers internally
        self._core = ClientCore(
            auth,
            timeout=timeout,
            refresh_callback=self.refresh_auth,
            keepalive=keepalive,
            keepalive_min_interval=keepalive_min_interval,
            keepalive_storage_path=keepalive_storage_path,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            limits=limits,
            max_concurrent_uploads=max_concurrent_uploads,
            max_concurrent_rpcs=max_concurrent_rpcs,
            on_rpc_event=on_rpc_event,
        )

        # Initialize sub-client APIs.
        # ArtifactsAPI and NotesAPI both consume the shared ``_mind_map``
        # module for mind-map primitives, so their construction order is
        # not significant.
        self.notebooks = NotebooksAPI(self._core)
        self.sources = SourcesAPI(self._core, upload_timeout=upload_timeout)
        self.artifacts = ArtifactsAPI(self._core, storage_path=storage_path)
        self.notes = NotesAPI(self._core)
        self.chat = ChatAPI(self._core)
        self.research = ResearchAPI(self._core)
        self.settings = SettingsAPI(self._core)
        self.sharing = SharingAPI(self._core)

    @property
    def auth(self) -> AuthTokens:
        """Get the authentication tokens."""
        return self._core.auth

    async def __aenter__(self) -> NotebookLMClient:
        """Open the client connection."""
        logger.debug("Opening NotebookLM client")
        await self._core.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the client connection.

        Exception arbitration: if the ``async with``
        body raised, prefer that exception and demote any ``close()``
        failure to a WARNING log so the original cause isn't masked.
        If the body succeeded, propagate ``close()`` failures normally.
        ``BaseException`` is caught so ``CancelledError`` /
        ``KeyboardInterrupt`` mid-close also flow through arbitration.
        """
        logger.debug("Closing NotebookLM client")
        try:
            await self.close()
        except BaseException as close_exc:
            if exc_val is not None:
                logger.warning(
                    "Suppressing close() error to preserve original exception: %s",
                    close_exc,
                )
                return
            raise

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting new operations and wait for in-flight operations to finish."""
        await self._core.drain(timeout=timeout)

    async def close(
        self,
        *,
        drain: bool = False,
        drain_timeout: float | None = None,
    ) -> None:
        """Close the client.

        Pass ``drain=True`` to stop accepting new operations and wait for
        in-flight operations to finish before closing the transport. If the
        drain deadline is exceeded, the transport is still closed and the
        timeout is re-raised.
        """
        if drain:
            try:
                await self.drain(timeout=drain_timeout)
            except TimeoutError as drain_exc:
                try:
                    await self._core.close()
                except Exception as close_exc:
                    logger.warning(
                        "Suppressing close() error after drain timeout to preserve timeout "
                        "signal: %s",
                        close_exc,
                    )
                    raise drain_exc from close_exc
                raise
            else:
                await self._core.close()
                return
        await self._core.close()

    def metrics_snapshot(self) -> ClientMetricsSnapshot:
        """Return cumulative observability counters for this client."""
        return self._core.metrics_snapshot()

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
        """Make a raw NotebookLM RPC call.

        This is the public escape hatch for advanced callers who need an
        undocumented RPC before a typed API exists. Prefer the namespaced APIs
        (``client.notebooks``, ``client.sources``, etc.) when possible. Import
        ``RPCMethod`` from ``notebooklm.rpc``. The ``_is_retry`` parameter is
        exposed only to mirror ``ClientCore.rpc_call`` exactly; callers should
        leave it at the default unless they are intentionally reproducing core
        retry behavior.
        """
        return await self._core.rpc_call(
            method=method,
            params=params,
            source_path=source_path,
            allow_null=allow_null,
            _is_retry=_is_retry,
            disable_internal_retries=disable_internal_retries,
        )

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._core.is_open

    @classmethod
    async def from_storage(
        cls,
        path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        profile: str | None = None,
        keepalive: float | None = None,
        keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
        rate_limit_max_retries: int = 3,
        server_error_max_retries: int = 3,
        limits: ConnectionLimits | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
        upload_timeout: httpx.Timeout | None = None,
        on_rpc_event: Callable[[RpcTelemetryEvent], object] | None = None,
    ) -> NotebookLMClient:
        """Create a client from Playwright storage state file.

        This is the recommended way to create a client for programmatic use.
        Handles all authentication setup automatically.

        Args:
            path: Path to storage_state.json. If provided, takes precedence over profile.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
            profile: Profile name to load auth from (e.g., "work", "personal").
                If None, uses the active profile (from CLI flag, env var, or config).
            keepalive: Optional interval in seconds for the background SIDTS
                rotation poke. ``None`` disables it (default). See
                :class:`NotebookLMClient` for full semantics.
            keepalive_min_interval: Floor for ``keepalive`` (defaults to 60 s).
            rate_limit_max_retries: Max automatic retries on HTTP 429.
                Defaults to ``3``. Set to ``0`` to
                restore raise-immediately behavior. See
                :class:`NotebookLMClient` for full sleep semantics.
            server_error_max_retries: Max automatic retries for HTTP 5xx /
                network errors with exponential backoff. Defaults to ``3``.
            limits: HTTP connection-pool tuning (``ConnectionLimits``). ``None``
                (default) uses ``ConnectionLimits()`` defaults sized for
                typical batchexecute fan-out (max_connections=100,
                max_keepalive_connections=50, keepalive_expiry=30.0s). Widen
                for heavy batch workloads (FastAPI/Django services sharing one
                client across many concurrent requests).
            max_concurrent_uploads: Ceiling on simultaneous in-flight file
                uploads via ``client.sources.add_file``. Defaults to ``4``.
                ``None`` resolves to the default. See :class:`NotebookLMClient`
                for full semantics (FD-exhaustion guard, independence from
                the RPC pool).
            max_concurrent_rpcs: Ceiling on simultaneous in-flight RPC
                POSTs. Defaults to ``16``; ``None`` disables the gate.
                Must be ``>= 1`` and ``<= limits.max_connections``. See
                :class:`NotebookLMClient` for the cross-validation rule
                and the rationale (the gate sits below the connection
                pool so back-pressure surfaces cleanly instead of as
                opaque ``httpx.PoolTimeout``).
            upload_timeout: Optional override for the ``httpx.Timeout`` used
                by the resumable-upload start handshake and the finalize
                POST. ``None`` (default) preserves the original hardcoded
                values for back-compat. See :class:`NotebookLMClient` for
                full semantics.
            on_rpc_event: Optional sync or async callback invoked after each
                logical RPC succeeds or fails.

        Returns:
            NotebookLMClient instance (not yet connected).

        Example:
            async with await NotebookLMClient.from_storage() as client:
                notebooks = await client.notebooks.list()

            # Use a specific profile
            async with await NotebookLMClient.from_storage(profile="work") as client:
                notebooks = await client.notebooks.list()

            # Long-lived client with periodic keepalive (e.g. an agent worker)
            async with await NotebookLMClient.from_storage(keepalive=600) as client:
                ...
        """
        storage_path = Path(path) if path else None
        auth = await AuthTokens.from_storage(storage_path, profile=profile)
        # Always resolve the storage path so downstream cookie loading
        # (e.g. artifact downloads) uses the correct file, whether the
        # caller provided an explicit path, a named profile, or neither.
        if storage_path is None and not os.environ.get("NOTEBOOKLM_AUTH_JSON"):
            from .paths import get_storage_path

            storage_path = get_storage_path(profile)
        return cls(
            auth,
            timeout=timeout,
            storage_path=storage_path,
            keepalive=keepalive,
            keepalive_min_interval=keepalive_min_interval,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            limits=limits,
            max_concurrent_uploads=max_concurrent_uploads,
            max_concurrent_rpcs=max_concurrent_rpcs,
            upload_timeout=upload_timeout,
            on_rpc_event=on_rpc_event,
        )

    async def refresh_auth(self) -> AuthTokens:
        """Refresh authentication tokens by fetching the NotebookLM homepage.

        This helps prevent 'Session Expired' errors by obtaining a fresh CSRF
        token (SNlM0e) and session ID (FdrFJe).

        Returns:
            Updated AuthTokens.

        Raises:
            ValueError: If token extraction fails (page structure may have changed).
        """
        return await refresh_auth_session(self._core)
