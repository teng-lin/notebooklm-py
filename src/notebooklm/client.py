"""NotebookLM API Client - Main entry point.

This module provides the NotebookLMClient class, a modern async client
for interacting with Google NotebookLM using undocumented RPC APIs.

Example:
    async with NotebookLMClient.from_storage() as client:
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

import logging
from collections.abc import Callable, Generator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .rpc import RPCMethod
    from .types import ClientMetricsSnapshot, ConnectionLimits, RpcTelemetryEvent

# Keep feature/collaborator types importable for runtime type-hint introspection.
from ._artifacts import ArtifactsAPI
from ._auth.account import authuser_query as authuser_query
from ._auth.extraction import extract_wiz_field as extract_wiz_field
from ._auth.web_provider_storage import load_web_provider_bootstrap
from ._chat import ChatAPI
from ._client_composition import compose_client
from ._collections import CollectionsAPI
from ._deprecation import warn_deprecated
from ._env import get_base_url as get_base_url
from ._labels import LabelsAPI
from ._mind_map import NoteBackedMindMapService as NoteBackedMindMapService  # noqa: F401
from ._mind_maps_api import MindMapsAPI
from ._note_service import NoteService as NoteService  # noqa: F401
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._research import ResearchAPI
from ._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_CHAT_RESPONSE_MAX_BYTES,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from ._runtime.lifecycle import CookieRotator, CookieSaver
from ._settings import SettingsAPI
from ._sharing import SharingAPI
from ._source.upload import SourceUploadPipeline
from ._sources import SourcesAPI
from ._url_utils import is_google_auth_redirect as is_google_auth_redirect
from ._web.backend import WebRpcBackend
from ._web_cookie_provider import WebCookieProvider
from .auth import AuthTokens
from .exceptions import AuthExtractionError as AuthExtractionError

__all__ = ["NotebookLMClient"]

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
    - mind_maps: Generate and manage note-backed and interactive mind maps
    - settings: Manage user settings (output language, etc.)
    - sharing: Manage notebook sharing and permissions
    - labels: AI-group sources into topic labels (auto-label / reorganize)
    - collections: Group notebooks into account-level collections

    Usage:
        # Create from saved authentication (canonical idiom)
        async with NotebookLMClient.from_storage() as client:
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
        mind_maps: MindMapsAPI for note-backed and interactive mind maps
        settings: SettingsAPI for user settings
        sharing: SharingAPI for notebook sharing
        labels: LabelsAPI for source labels (topic grouping)
        collections: CollectionsAPI for account-level notebook collections
        auth: The AuthTokens used for authentication
    """

    # Constructor-set attribute surface. Declared here (annotation-only;
    # no runtime effect) because the assignments live in the shared
    # production composition root :func:`notebooklm._client_composition.compose_client`,
    # not in ``__init__`` — see the delegation comment there. Keep this
    # block in sync with ``compose_client``; the composition guard
    # ``tests/_guardrails/test_client_composition.py`` pins the runtime
    # attribute surface itself.
    _backend: WebRpcBackend
    _provider: WebCookieProvider
    _source_uploader: SourceUploadPipeline
    sources: SourcesAPI
    notebooks: NotebooksAPI
    artifacts: ArtifactsAPI
    chat: ChatAPI
    notes: NotesAPI
    mind_maps: MindMapsAPI
    research: ResearchAPI
    settings: SettingsAPI
    sharing: SharingAPI
    labels: LabelsAPI
    collections: CollectionsAPI

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
        cookie_saver: CookieSaver | None = None,
        cookie_rotator: CookieRotator | None = None,
        chat_timeout: float | None = AUTO_READ_TIMEOUT,
        chat_response_max_bytes: int | None = DEFAULT_CHAT_RESPONSE_MAX_BYTES,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
    ):
        """Initialize the NotebookLM client.

        Args:
            auth: Authentication tokens from browser login.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
                It is the *base* read budget for every RPC: the built-in
                per-RPC windows below only ever lengthen it, never shorten it,
                so ``timeout=600`` really does buy 600 s everywhere (#2205).
            chat_timeout: Per-read HTTP timeout in seconds for
                ``client.chat.ask``. Left unset it is ``max(180, timeout)`` —
                180 s because shared notebooks can be slow to send the first
                streamed byte, floored at ``timeout`` so a larger configured
                budget still applies to chat. Pass an explicit value to fix
                the chat window outright (including *below* ``timeout``, for
                deliberately fast failure), or ``None`` to inherit ``timeout``.
            import_research_timeout: Per-attempt read window in seconds for
                ``client.research.import_sources``' IMPORT_RESEARCH RPC, read
                exactly like ``chat_timeout``: left unset it is the batch-scaled
                window (60 s + 3 s per requested source, capped at 240 s)
                floored at ``timeout``; a value replaces both the scaling and
                the floor; ``None`` inherits ``timeout`` verbatim. Either way an
                attempt made by ``import_sources_with_verification`` is
                additionally clamped to what remains of that call's
                ``max_elapsed`` budget, and that loop stops rather than sending
                an attempt too short to observe its own result.

                A non-positive or non-finite ``chat_timeout`` /
                ``import_research_timeout`` raises rather than silently
                producing a window that times out instantly.
            chat_response_max_bytes: Maximum buffered response size for
                ``client.chat.ask``. Defaults to 256 MiB because the
                streamed chat endpoint can include notebook-state sync
                bytes in addition to the answer text. Pass ``None`` to
                inherit the shared RPC response cap. Must be ``>= 1``
                when supplied.
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
                See the retry middleware docs for full sleep semantics.
            server_error_max_retries: Max automatic retries for retryable
                transient failures: HTTP 5xx and network-layer
                ``httpx.RequestError`` (timeouts, connect errors). Defaults to
                ``3``. Uses capped exponential backoff
                ``min(2 ** attempt, 30)`` seconds with ±20% jitter and a 0.1s
                floor. Set to ``0`` to disable.
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
            cookie_saver: Optional injectable seam overriding
                the on-disk cookie writer used on close / refresh / keepalive.
                ``None`` (default) uses the canonical typed ``ProfileStore``
                path. Must be sync (``def``, not ``async def``) — an explicit
                callback runs inside ``asyncio.to_thread`` through the v0.x
                compatibility adapter and receives ``jar``, ``path``,
                ``original_snapshot=...``, and ``return_result=True``.
            cookie_rotator: Optional injectable seam
                overriding the keepalive-loop cookie rotator. ``None``
                (default) preserves the current behavior of resolving
                ``notebooklm._auth.keepalive._rotate_cookies`` via a
                late-bound wrapper. Must be async — it is awaited from
                the keepalive loop.
        """
        # The production-only composition root builds the complete graph
        # before publication and exposes no test-only construction path.
        compose_client(
            self,
            auth=auth,
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
            cookie_saver=cookie_saver,
            cookie_rotator=cookie_rotator,
            chat_timeout=chat_timeout,
            import_research_timeout=import_research_timeout,
            chat_response_max_bytes=chat_response_max_bytes,
        )

    #: Per-client memo for the signed-in account email so a *successful* live probe
    #: (used only when neither the in-memory ``AuthTokens`` nor persisted storage
    #: carries one) runs at most once per account route. A failed/undiscoverable probe
    @property
    def auth(self) -> AuthTokens:
        """Get the authentication tokens.

        ADR-0016's Auth Instance Invariant requires every reference across
        the live object graph to alias the same mutable
        :class:`AuthTokens` object set in :meth:`__init__`, so the public
        ``client.auth`` identity and behavior are unchanged.
        """
        return self._provider.auth

    async def __aenter__(self) -> NotebookLMClient:
        """Open the client connection."""
        logger.debug("Opening NotebookLM client")
        await self._backend.open_client(
            uploader=self._source_uploader,
            chat=self.chat,
        )
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
        """Stop accepting new operations and wait for in-flight operations to finish.

        Delegates directly to the :class:`TransportDrainTracker` that
        owns the in-flight counter; the public client-side behavior
        (drain semantics and timeout propagation) is unchanged.
        """
        await self._backend.drain_client(timeout=timeout)

    async def close(
        self,
        *,
        drain: bool = True,
        drain_timeout: float | None = None,
    ) -> None:
        """Close the client.

        By default (``drain=True``), ``close()`` first stops accepting new
        operations and waits for in-flight operations to finish before tearing
        down the transport. If the drain deadline (``drain_timeout``) is
        exceeded, the transport is still closed and the timeout is re-raised.

        Pass ``drain=False`` to skip the drain step and tear the transport
        down immediately (fire-and-forget semantics).

        BREAKING CHANGE: prior versions defaulted to ``drain=False``. Callers
        relying on fire-and-forget close semantics (e.g. via
        ``__aexit__``) will now block briefly on the drain step; pass
        ``drain=False`` explicitly to restore the old behavior.

        Cancellation-safety contract (audit finding I12):

        If the caller's task is cancelled while ``close(drain=True)`` is
        still waiting on ``drain()`` (e.g. ``asyncio.wait_for`` deadline,
        manual ``task.cancel()``), the underlying transport is STILL torn
        down before the cancellation propagates. The drain await
        explicitly catches ``CancelledError`` and schedules
        lifecycle close through ``asyncio.shield`` — the shield wraps
        the inner close in a ``Task`` that survives the outer
        cancellation, so the ``Kernel.aclose()`` it drives runs to
        completion in the background. On the normal-success and
        ``TimeoutError`` paths the same shielded close call runs inline.
        ``ValueError`` (and any other unexpected exception) from
        ``drain()`` propagates without an implicit close, matching the
        pre-I12 caller-error semantics asserted by
        ``test_close_with_invalid_drain_does_not_close_transport``.

        Practical guarantee:

        - **Normal-success and drain-timeout paths**: on return,
          ``is_connected is False`` and the underlying
          ``httpx.AsyncClient`` is closed synchronously.
        - **Cancel-during-drain path** (single cancellation): the
          shielded lifecycle close runs to completion synchronously
          before ``CancelledError`` is re-raised — Python does not
          re-raise ``CancelledError`` to the same task without an
          explicit re-cancel, so the await on the shielded Task
          blocks normally. On return, ``is_connected is False`` and
          the transport is closed.
        - **Cancel-during-drain path** (re-cancellation while awaiting
          the shielded close): the shielded lifecycle close Task is
          isolated from the second cancel by ``asyncio.shield`` and
          continues running in the background; the second cancel
          surfaces in the awaiter, is suppressed, and the *original*
          ``CancelledError`` is re-raised. ``is_connected`` settles to
          ``False`` once the background Task lands (callers can
          ``await asyncio.sleep(0)`` or poll to observe it).

        There is no path that leaves a live transport behind.

        Drain-hook ordering (issue #1161): feature-owned cancel hooks
        (e.g. ``artifacts.polls``) run BEFORE the drain wait, not just in
        the shielded lifecycle close below. In-flight artifact polls wrap
        themselves in ``TransportDrainTracker.operation_scope`` (see
        :meth:`notebooklm._artifact.polling.ArtifactPollingService._run_poll_loop_in_scope`),
        which increments the same in-flight counter ``drain()`` waits on.
        Without firing the cancel hooks first, ``drain()`` would block on a
        poll that the cancel hook is supposed to short-circuit — up to the
        poll's own 300s timeout. Running the hooks first lets ``drain()``
        observe a cancelled-then-settled count instead of parking on it. The
        lifecycle close below still re-runs the hooks; for the only
        production hook (``artifacts.polls``) that re-run is a cheap no-op
        because already-settled poll tasks are filtered out of
        :meth:`notebooklm._polling_registry.PollRegistry.active_tasks`.

        Note: the cancel-hook fire is NOT bounded by ``drain_timeout`` — that
        deadline budgets the drain *wait*. The production poll-cancel hook
        settles near-instantly (it cancels its tasks and awaits the
        cancellation gather), so this is a non-issue in practice; a custom
        feature hook that blocks indefinitely could still extend shutdown,
        and such hooks should bound their own work.
        """
        await self._backend.close_client(drain=drain, drain_timeout=drain_timeout)

    def metrics_snapshot(self) -> ClientMetricsSnapshot:
        """Return cumulative observability counters for this client.

        The backend owns the one metrics instance used by the runtime chain.
        """
        return self._backend.metrics_snapshot()

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        allow_null: bool = False,
        *,
        disable_internal_retries: bool = False,
        read_timeout: float | None = None,
        raise_on_null_status: bool = False,
    ) -> Any:
        """Make a raw NotebookLM RPC call.

        This is the public escape hatch for advanced callers who need an
        undocumented RPC before a typed API exists. Prefer the namespaced APIs
        (``client.notebooks``, ``client.sources``, etc.) when possible. Import
        ``RPCMethod`` from ``notebooklm.rpc``.

        The wrapper forwards through ``WebRpcBackend`` to the
        :class:`WebExecutionRuntime` bound during construction (and shared by
        every feature API). Internal call sites bind internal-only parameters
        against that backend runtime, not via this public wrapper.

        ``read_timeout`` (default ``None``) overrides the client-wide read
        timeout for this one call — useful for RPCs known to run long (e.g.
        bulk imports) without lowering the default for every other call.

        ``raise_on_null_status`` (default ``False``) pairs with
        ``allow_null=True``: it turns a null result that the server tagged with
        a non-OK ``google.rpc.Status`` into a raised error instead of a silent
        ``None``, so the server's own rejection is reported rather than
        swallowed (#2188).

        .. versionchanged:: 0.6.0
            The deprecated keyword arguments previously documented here
            were removed (see :doc:`/deprecations`). The default-shape
            call (``client.rpc_call(method, params)``) is unchanged.
        """
        return await self._backend.public_rpc_call(
            method=method,
            params=params,
            allow_null=allow_null,
            disable_internal_retries=disable_internal_retries,
            read_timeout=read_timeout,
            raise_on_null_status=raise_on_null_status,
        )

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._backend.is_connected

    @classmethod
    def from_storage(
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
        chat_timeout: float | None = AUTO_READ_TIMEOUT,
        chat_response_max_bytes: int | None = DEFAULT_CHAT_RESPONSE_MAX_BYTES,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
        *,
        allow_headless: bool = False,
    ) -> _FromStorageContext:
        """Create a client from Playwright storage state file.

        This is the recommended way to create a client for programmatic use.
        Handles all authentication setup automatically.

        The returned object supports two usage patterns:

        - **Canonical (recommended):** use as an async context manager — no
          ``await`` on ``from_storage`` itself. The auth load and session open
          happen on ``__aenter__``.
        - **Legacy (deprecated, removed in v1.0):** await the call to obtain a
          built-but-unentered ``NotebookLMClient``. Awaiting emits a
          ``DeprecationWarning`` pointing at the v1.0 removal.

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
            chat_timeout: Per-read HTTP timeout in seconds for
                ``client.chat.ask``. Left unset it is ``max(180, timeout)``;
                pass a value to fix it outright, or ``None`` to inherit
                ``timeout``. See :class:`NotebookLMClient`.
            import_research_timeout: Per-attempt read window for
                IMPORT_RESEARCH. Unset keeps the batch-scaled window floored at
                ``timeout``; a value replaces both; ``None`` inherits
                ``timeout``. See :class:`NotebookLMClient`.
            chat_response_max_bytes: Maximum buffered response size for
                ``client.chat.ask``. Defaults to 256 MiB. Pass ``None`` to
                inherit the shared RPC response cap. Must be ``>= 1``
                when supplied.
            upload_timeout: Optional override for the ``httpx.Timeout`` used
                by the resumable-upload start handshake and the finalize
                POST. ``None`` (default) preserves the original hardcoded
                values for back-compat. See :class:`NotebookLMClient` for
                full semantics.
            on_rpc_event: Optional sync or async callback invoked after each
                logical RPC succeeds or fails.
            allow_headless: Permit one cold-start layer-3 browser recovery when
                stored cookies are fully expired. A sibling master token can
                recover automatically without enabling browser recovery.

        Returns:
            ``_FromStorageContext`` — an awaitable async-context-manager
            wrapper. ``await``-ing it (legacy path) returns a
            ``NotebookLMClient`` instance. ``async with``-ing it (canonical
            path) yields a ``NotebookLMClient`` that is already connected.

        Example:
            # Canonical idiom — no `await` on `from_storage`.
            async with NotebookLMClient.from_storage() as client:
                notebooks = await client.notebooks.list()

            # Use a specific profile
            async with NotebookLMClient.from_storage(profile="work") as client:
                notebooks = await client.notebooks.list()

            # Long-lived client with periodic keepalive (e.g. an agent worker)
            async with NotebookLMClient.from_storage(keepalive=600) as client:
                ...

            # Legacy form (deprecated, removed in v1.0):
            # async with await NotebookLMClient.from_storage() as client: ...
        """
        return _FromStorageContext(
            cls,
            path=path,
            timeout=timeout,
            profile=profile,
            keepalive=keepalive,
            keepalive_min_interval=keepalive_min_interval,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            limits=limits,
            max_concurrent_uploads=max_concurrent_uploads,
            max_concurrent_rpcs=max_concurrent_rpcs,
            chat_timeout=chat_timeout,
            chat_response_max_bytes=chat_response_max_bytes,
            import_research_timeout=import_research_timeout,
            upload_timeout=upload_timeout,
            on_rpc_event=on_rpc_event,
            allow_headless=allow_headless,
        )

    async def refresh_auth(self, *, allow_headless: bool = False) -> AuthTokens:
        """Refresh authentication tokens by fetching the NotebookLM homepage.

        This helps prevent 'Session Expired' errors by obtaining a fresh CSRF
        token (SNlM0e) and session ID (FdrFJe).

        This compatibility method delegates to WebRpcBackend, which owns the
        authoritative AuthTokens and refresh collaborators. The public
        constructor and canonical test factory share the same atomic assembly
        path.

        Args:
            allow_headless: Opt in to **layer-3 headless re-auth** when the
                first-party NotebookLM cookies are fully dead (the homepage GET
                302s to the Google login page) and neither L1 token refresh nor
                L2 ``RotateCookies`` rotation can help. When ``True``, an
                unattended **headless** browser is driven against the persistent
                login profile to silently re-mint cookies from a still-live
                Google session, then this refresh retries once. Defaults to
                ``False`` — the locked design decision is that L3 NEVER fires by
                default; with no opt-in and no profile the behavior is
                byte-identical to before. (A *mid-RPC* auto-fire is separately
                gated on ``NOTEBOOKLM_HEADLESS_REAUTH=1``.)

                SECURITY: the persistent profile is an account-equivalent
                credential (a live Google session). L3 is local-unattended-only
                and must NOT be the auth path for a remote / hosted MCP server.

        Coordinator single-flight + join-then-rerun (caller-side):

            The base-policy refresh (``allow_headless=False``) is BOTH the
            provider's single-flight callback (the mid-RPC 401 path joins it
            through the provider boundary) and what a default ``refresh_auth()``
            performs directly, without recursive coordinator entry.

            A wider-policy caller (``allow_headless=True``) instead JOINS
            whatever base-policy flight the coordinator has in progress (or
            starts one) via ``await_refresh``. If that shared flight SUCCEEDS
            the tokens are already re-minted and it returns. If it FAILS, the
            base flight lacked the L3 rung, so this caller RE-RUNS its own
            refresh with the full rung policy (``allow_headless=True``) — it
            never silently loses its L3 rung to a narrower flight it joined.
            The coordinator's internals (its single unkeyed task slot) are not
            modified; the join-then-rerun is entirely caller-side.

        Returns:
            Updated AuthTokens.

        Raises:
            ValueError: If token extraction fails (page structure may have
                changed), or if cookies are dead and L3 is unavailable / also
                fails (the persisted profile's Google session is expired too).
        """
        return await self._provider.refresh(allow_headless=allow_headless)

    def get_account_authuser(self) -> int:
        """Return the ``authuser`` index of the signed-in account (0 = default).

        Read from the in-memory :class:`AuthTokens` (populated at construction from
        the profile's persisted metadata or inline ``NOTEBOOKLM_AUTH_JSON``);
        network-free. Falls back to ``0`` for pre-account-binding profiles.
        """
        return self.auth.authuser

    async def get_account_email(self, *, live_fallback: bool = True) -> str | None:
        """Return the signed-in Google account email, or ``None`` if undiscoverable.

        Resolution order (first two are network-free):

        1. The in-memory :class:`AuthTokens` (``account_email``) — set at
           construction from persisted metadata OR inline ``NOTEBOOKLM_AUTH_JSON``.
        2. The persisted profile metadata (belt-and-braces for a profile whose
           in-memory value wasn't populated).
        3. When ``live_fallback`` is true, a single probe of the active
           ``authuser`` page (``WIZ_global_data``) on the open session; on success
           it is persisted back so the next call is network-free.

        ``GET_USER_SETTINGS`` carries no identity, hence this separate source.
        Never raises for network or on-disk faults — a probe transport error or a
        self-heal write failure degrades to ``None`` / a no-op. A closed client
        (calling outside ``async with``) is the only surfaced error, from
        :meth:`Kernel.get_http_client`, and only on the live-fallback path.
        """
        return await self._provider.get_account_email(live_fallback=live_fallback)


class _FromStorageContext:
    """Awaitable async-context-manager wrapper for ``NotebookLMClient.from_storage``.

    Supports two usage patterns so users get a friendly fix-it path off the
    historical ``async with await`` double-keyword trap:

    Canonical (recommended):
        async with NotebookLMClient.from_storage(...) as client:
            ...

    Legacy (deprecated, removed in v1.0):
        async with await NotebookLMClient.from_storage(...) as client:
            ...
        # or:
        client = await NotebookLMClient.from_storage(...)

    The legacy ``__await__`` path emits a ``DeprecationWarning`` naming the
    v1.0 removal so existing call sites have a clear migration target. The
    new ``__aenter__`` path emits no warning.

    Auth load and storage-path resolution are deferred until the first use
    (``__aenter__`` or ``__await__``) — constructing the wrapper itself does
    no I/O.
    """

    __slots__ = ("_cls", "_kwargs", "_client", "_owns_close")

    def __init__(
        self,
        cls: type[NotebookLMClient],
        **kwargs: Any,
    ) -> None:
        self._cls = cls
        self._kwargs = kwargs
        self._client: NotebookLMClient | None = None
        self._owns_close = False

    async def _build(self) -> NotebookLMClient:
        """Load auth and instantiate a cached, not-yet-open client.

        Constructor failure leaves the cache empty, so retry reloads auth.
        """
        if self._client is not None:
            return self._client

        kwargs = self._kwargs
        path = kwargs["path"]
        profile = kwargs["profile"]

        bootstrap = await load_web_provider_bootstrap(
            path=Path(path) if path else None,
            profile=profile,
            allow_headless=kwargs["allow_headless"],
        )
        auth = bootstrap.auth
        storage_path = auth.storage_path

        client = self._cls(
            auth,
            timeout=kwargs["timeout"],
            storage_path=storage_path,
            keepalive=kwargs["keepalive"],
            keepalive_min_interval=kwargs["keepalive_min_interval"],
            rate_limit_max_retries=kwargs["rate_limit_max_retries"],
            server_error_max_retries=kwargs["server_error_max_retries"],
            limits=kwargs["limits"],
            max_concurrent_uploads=kwargs["max_concurrent_uploads"],
            max_concurrent_rpcs=kwargs["max_concurrent_rpcs"],
            chat_timeout=kwargs["chat_timeout"],
            chat_response_max_bytes=kwargs["chat_response_max_bytes"],
            import_research_timeout=kwargs["import_research_timeout"],
            upload_timeout=kwargs["upload_timeout"],
            on_rpc_event=kwargs["on_rpc_event"],
        )
        if (
            bootstrap.store is not None
            and bootstrap.persistence_baseline is not None
            and hasattr(client, "_provider")
        ):
            client._provider.register_open_baseline(
                bootstrap.store,
                bootstrap.persistence_baseline,
            )
        self._client = client
        return client

    def __await__(self) -> Generator[Any, None, NotebookLMClient]:
        """Legacy await path — returns a built-but-unentered client.

        Emits ``DeprecationWarning`` (removed in v1.0). Prefer the
        ``async with NotebookLMClient.from_storage(...) as client:`` idiom.
        """
        warn_deprecated(
            "Awaiting NotebookLMClient.from_storage(...) is deprecated; use "
            "`async with NotebookLMClient.from_storage(...) as client:` "
            "instead. The await form will be removed in v1.0.",
            removal="1.0",
            stacklevel=3,
        )
        return self._build().__await__()

    async def __aenter__(self) -> NotebookLMClient:
        """Canonical path — build the client and enter its session."""
        client = await self._build()
        await client.__aenter__()
        self._owns_close = True
        return client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Tear down the client we opened in ``__aenter__``.

        Only closes when ``__aenter__`` ran successfully — re-entering via the
        legacy ``async with await ...`` path opens the client through
        ``NotebookLMClient.__aenter__`` directly, so ``_FromStorageContext``
        is not in that chain and never tries to close someone else's client.
        """
        if self._owns_close and self._client is not None:
            await self._client.__aexit__(exc_type, exc_val, exc_tb)
