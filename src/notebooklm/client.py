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

import asyncio
import logging
from collections.abc import Callable, Generator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .rpc import RPCMethod
    from .types import ClientMetricsSnapshot, ConnectionLimits, RpcTelemetryEvent

# The construction wiring lives in ``_client_assembly`` (the seam shared
# with the canonical test factory), but the names below stay runtime
# imports on purpose:
#
# - the feature-API / collaborator types annotate the class-level
#   attribute block, and keeping them importable at runtime keeps
#   ``typing.get_type_hints(NotebookLMClient)`` working for downstream
#   introspection;
# - this module's attribute surface (``notebooklm.client.SourcesAPI``
#   etc.) predates the assembly split and is kept byte-compatible so
#   external tooling/imports against it don't break. The F401-suppressed
#   names are exactly the previously-importable names the annotations no
#   longer reference.
from ._artifacts import ArtifactsAPI
from ._auth.session import refresh_auth_session
from ._chat import ChatAPI
from ._client_assembly import _assemble_client
from ._client_composed import ClientComposed
from ._client_seams import ClientSeams
from ._client_seams import resolve_client_seams as resolve_client_seams  # noqa: F401
from ._deprecation import warn_deprecated
from ._env import get_base_url as get_base_url
from ._labels import LabelsAPI
from ._mind_map import NoteBackedMindMapService as NoteBackedMindMapService  # noqa: F401
from ._mind_maps_api import MindMapsAPI
from ._note_service import NoteService as NoteService  # noqa: F401
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._research import ResearchAPI
from ._rpc_executor import RpcExecutor
from ._runtime.config import (
    DEFAULT_CHAT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from ._runtime.init import RuntimeCollaborators
from ._runtime.init import compose_client_internals as compose_client_internals  # noqa: F401
from ._runtime.lifecycle import CookieRotator, CookieSaver
from ._settings import SettingsAPI
from ._sharing import SharingAPI
from ._source.upload import SourceUploadPipeline
from ._sources import SourcesAPI
from ._url_utils import is_google_auth_redirect as is_google_auth_redirect
from .auth import AuthTokens
from .auth import authuser_query as authuser_query
from .auth import extract_wiz_field as extract_wiz_field
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
    - settings: Manage user settings (output language, etc.)
    - sharing: Manage notebook sharing and permissions
    - labels: AI-group sources into topic labels (auto-label / reorganize)

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
        settings: SettingsAPI for user settings
        sharing: SharingAPI for notebook sharing
        labels: LabelsAPI for source labels (topic grouping)
        auth: The AuthTokens used for authentication
    """

    # Constructor-set attribute surface. Declared here (annotation-only;
    # no runtime effect) because the assignments live in the shared
    # assembly seam :func:`notebooklm._client_assembly._assemble_client`,
    # not in ``__init__`` — see the delegation comment there. Keep this
    # block in sync with ``_assemble_client``; the parity gate
    # ``tests/_guardrails/test_client_factory_parity.py`` pins the
    # runtime attribute surface itself.
    _auth: AuthTokens
    _seams: ClientSeams
    _composed: ClientComposed
    _collaborators: RuntimeCollaborators
    _rpc_executor: RpcExecutor
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
        chat_timeout: float | None = DEFAULT_CHAT_TIMEOUT,
    ):
        """Initialize the NotebookLM client.

        Args:
            auth: Authentication tokens from browser login.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
            chat_timeout: Per-read HTTP timeout in seconds for
                ``client.chat.ask``. Defaults to 180 seconds because shared
                notebooks can be slow to send the first streamed byte. Pass
                ``None`` to inherit the normal client timeout for chat.
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
            cookie_saver: Optional injectable seam overriding
                the on-disk cookie writer used on close / refresh / keepalive.
                ``None`` (default) preserves the current behavior of resolving
                ``notebooklm._auth.storage.save_cookies_to_storage`` via a
                late-bound wrapper. Must be sync (``def``, not ``async def``)
                — it runs inside ``asyncio.to_thread``. Custom callables
                bypass the late-bind hop entirely.
            cookie_rotator: Optional injectable seam
                overriding the keepalive-loop cookie rotator. ``None``
                (default) preserves the current behavior of resolving
                ``notebooklm._auth.keepalive._rotate_cookies`` via a
                late-bound wrapper. Must be async — it is awaited from
                the keepalive loop.
        """
        # The full assembly lives in ``notebooklm._client_assembly`` —
        # one private seam shared with the canonical test factory
        # (``tests/_helpers/client_factory.build_client_shell_for_tests``)
        # so the two construction paths cannot drift (incidents #1196 /
        # #1225). Set EVERY constructor-time attribute inside
        # ``_assemble_client``, never here after the delegation call —
        # the parity gate
        # ``tests/_guardrails/test_client_factory_parity.py`` fails
        # otherwise. The test-only seam kwargs (``decode_response`` /
        # ``sleep`` / ``is_auth_error`` / ``async_client_factory``) stay
        # off this public constructor by design.
        _assemble_client(
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
        )

    @property
    def auth(self) -> AuthTokens:
        """Get the authentication tokens.

        ADR-0016's Auth Instance Invariant requires every reference across
        the live object graph to alias the same mutable
        :class:`AuthTokens` object set in :meth:`__init__`, so the public
        ``client.auth`` identity and behavior are unchanged.
        """
        return self._auth

    async def __aenter__(self) -> NotebookLMClient:
        """Open the client connection."""
        logger.debug("Opening NotebookLM client")
        # Preserve the historical fail-fast check that composition is complete.
        _ = self._composed.transport
        await self._collaborators.lifecycle.open(
            auth=self._auth,
            drain_tracker=self._collaborators.drain_tracker,
            auth_coord=self._collaborators.auth_coord,
            reqid=self._collaborators.reqid,
            cookie_persistence=self._collaborators.cookie_persistence,
            composed=self._composed,
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
        await self._collaborators.drain_tracker.drain(timeout=timeout)

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
        if drain:
            drain_timeout_exc: TimeoutError | None = None
            try:
                # Fire feature-owned cancel hooks BEFORE the drain wait (see
                # the "Drain-hook ordering" section of the docstring above for
                # why). Awaited inside this ``try`` so a *caller* CancelledError
                # arriving during the hook fire still routes through the I12
                # shielded-close path below; ``run_drain_hooks`` itself never
                # re-raises (it gathers with ``return_exceptions=True``).
                await self._collaborators.drain_tracker.run_drain_hooks()
                await self.drain(timeout=drain_timeout)
            except TimeoutError as exc:
                # Drain deadline missed. Hold onto the exception and
                # fall through to the shielded close below so callers
                # see both the timeout signal AND a torn-down transport.
                drain_timeout_exc = exc
            except asyncio.CancelledError:
                # Cancellation-safety contract (audit finding I12): if
                # the caller's task is cancelled while drain() is
                # waiting (e.g. ``asyncio.wait_for`` deadline, manual
                # ``task.cancel()``), we MUST still tear down the
                # transport before letting the cancel propagate. On a
                # single cancellation this shielded await runs to
                # completion synchronously (Python does not re-raise
                # CancelledError without an explicit re-cancel). If a
                # SECOND cancel arrives while we're parked here,
                # ``asyncio.shield`` isolates the inner lifecycle close
                # Task so it continues in the background; the second
                # cancel hits the awaiter and is swallowed below so the
                # original CancelledError surfaces unchanged.
                try:
                    await asyncio.shield(
                        self._collaborators.lifecycle.close(
                            auth_coord=self._collaborators.auth_coord,
                            drain_tracker=self._collaborators.drain_tracker,
                            cookie_persistence=self._collaborators.cookie_persistence,
                        )
                    )
                except (Exception, asyncio.CancelledError):
                    # Swallow regular close failures and any re-cancel
                    # propagated through the shield await so the
                    # original CancelledError below is the one that
                    # reaches the caller. The inner shielded Task
                    # continues to run regardless.
                    # NOTE: deliberately NOT catching ``BaseException`` —
                    # ``KeyboardInterrupt`` and ``SystemExit`` are
                    # process-exit signals that must propagate unchanged.
                    pass
                raise
            # Any other exception from drain (e.g. ``ValueError`` for a
            # caller-provided invalid deadline) propagates here without
            # an implicit close — matches pre-I12 caller-error semantics
            # asserted by
            # ``test_close_with_invalid_drain_does_not_close_transport``.

            try:
                await asyncio.shield(
                    self._collaborators.lifecycle.close(
                        auth_coord=self._collaborators.auth_coord,
                        drain_tracker=self._collaborators.drain_tracker,
                        cookie_persistence=self._collaborators.cookie_persistence,
                    )
                )
            except Exception as close_exc:
                if drain_timeout_exc is not None:
                    logger.warning(
                        "Suppressing close() error after drain timeout to "
                        "preserve timeout signal: %s",
                        close_exc,
                    )
                    raise drain_timeout_exc from close_exc
                raise
            if drain_timeout_exc is not None:
                raise drain_timeout_exc
            return
        await self._collaborators.lifecycle.close(
            auth_coord=self._collaborators.auth_coord,
            drain_tracker=self._collaborators.drain_tracker,
            cookie_persistence=self._collaborators.cookie_persistence,
        )

    def metrics_snapshot(self) -> ClientMetricsSnapshot:
        """Return cumulative observability counters for this client.

        Reads from the collaborator bundle stored by :meth:`__init__` from
        the composition root's :class:`ClientInternals`.
        """
        return self._collaborators.metrics.snapshot()

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        allow_null: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        """Make a raw NotebookLM RPC call.

        This is the public escape hatch for advanced callers who need an
        undocumented RPC before a typed API exists. Prefer the namespaced APIs
        (``client.notebooks``, ``client.sources``, etc.) when possible. Import
        ``RPCMethod`` from ``notebooklm.rpc``.

        The wrapper forwards to :meth:`RpcExecutor.rpc_call` on the
        executor that was bound during :meth:`__init__` (and that every
        feature API shares). Internal call sites that need to bind the
        underlying internal-only parameters do so against the executor
        surface directly, not via this public wrapper.

        .. versionchanged:: 0.6.0
            The deprecated keyword arguments previously documented here
            were removed (see :doc:`/deprecations`). The default-shape
            call (``client.rpc_call(method, params)``) is unchanged.
        """
        return await self._rpc_executor.rpc_call(
            method=method,
            params=params,
            allow_null=allow_null,
            disable_internal_retries=disable_internal_retries,
        )

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._collaborators.lifecycle.is_open()

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
        chat_timeout: float | None = DEFAULT_CHAT_TIMEOUT,
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
                ``client.chat.ask``. Defaults to 180 seconds. Pass ``None``
                to inherit ``timeout`` for chat.
            upload_timeout: Optional override for the ``httpx.Timeout`` used
                by the resumable-upload start handshake and the finalize
                POST. ``None`` (default) preserves the original hardcoded
                values for back-compat. See :class:`NotebookLMClient` for
                full semantics.
            on_rpc_event: Optional sync or async callback invoked after each
                logical RPC succeeds or fails.

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
            upload_timeout=upload_timeout,
            on_rpc_event=on_rpc_event,
        )

    async def refresh_auth(self) -> AuthTokens:
        """Refresh authentication tokens by fetching the NotebookLM homepage.

        This helps prevent 'Session Expired' errors by obtaining a fresh CSRF
        token (SNlM0e) and session ID (FdrFJe).

        This call site uses explicit collaborators sourced from
        ``self._auth`` and ``self._collaborators``. The five kwargs mirror
        the :func:`refresh_auth_session` signature: ``auth`` is the
        client-owned :class:`AuthTokens` instance (the Auth Instance
        Invariant guarantees this is the same object every auth consumer
        observes), and the remaining four come from the collaborator
        bundle the composition root produced
        (:func:`notebooklm._runtime.init.compose_client_internals`). The
        ``tests/_helpers/client_factory.build_client_shell_for_tests``
        helper wires ``_auth`` and ``_collaborators`` through the same
        :func:`notebooklm._client_assembly._assemble_client` seam this
        constructor delegates to, so test shells observe the same
        resolution path.

        Returns:
            Updated AuthTokens.

        Raises:
            ValueError: If token extraction fails (page structure may have changed).
        """
        return await refresh_auth_session(
            auth=self._auth,
            kernel=self._collaborators.kernel,
            auth_coord=self._collaborators.auth_coord,
            lifecycle=self._collaborators.lifecycle,
            cookie_persistence=self._collaborators.cookie_persistence,
        )


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
        """Load auth and instantiate the client (no session open).

        Idempotent on success: subsequent calls return the cached
        instance so awaiting the wrapper and then entering it as a
        context manager — or vice versa — never re-runs the auth load.

        Partial failure: if ``AuthTokens.from_storage(...)`` succeeds
        but the ``NotebookLMClient(...)`` constructor raises, the cache
        stays unset and a retry re-runs the auth load. That's
        intentional — the constructor only raises on programmer error
        (cross-validated kwargs) so the extra I/O on retry is
        acceptable.
        """
        if self._client is not None:
            return self._client

        kwargs = self._kwargs
        path = kwargs["path"]
        profile = kwargs["profile"]

        auth = await AuthTokens.from_storage(Path(path) if path else None, profile=profile)
        storage_path = auth.storage_path

        self._client = self._cls(
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
            upload_timeout=kwargs["upload_timeout"],
            on_rpc_event=kwargs["on_rpc_event"],
        )
        return self._client

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
