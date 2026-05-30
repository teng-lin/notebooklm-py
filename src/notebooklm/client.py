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
import dataclasses
import logging
import warnings
from collections.abc import Callable, Generator
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
from ._client_composed import ClientComposed
from ._client_seams import resolve_client_seams
from ._env import get_base_url as get_base_url
from ._mind_map import NoteBackedMindMapService
from ._note_service import NoteService
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._research import ResearchAPI
from ._runtime_config import (
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from ._runtime_init import compose_client_internals
from ._runtime_lifecycle import CookieRotator, CookieSaver
from ._settings import SettingsAPI
from ._sharing import SharingAPI
from ._source_upload import SourceUploadPipeline
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
        cookie_saver: CookieSaver | None = None,
        cookie_rotator: CookieRotator | None = None,
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
            cookie_saver: Optional injectable seam (Phase 2 PR 3) overriding
                the on-disk cookie writer used on close / refresh / keepalive.
                ``None`` (default) preserves the current behavior of resolving
                ``notebooklm._auth.storage.save_cookies_to_storage`` via a
                late-bound wrapper. Must be sync (``def``, not ``async def``)
                — it runs inside ``asyncio.to_thread``. Custom callables
                bypass the late-bind hop entirely.
            cookie_rotator: Optional injectable seam (Phase 2 PR 3)
                overriding the keepalive-loop cookie rotator. ``None``
                (default) preserves the current behavior of resolving
                ``notebooklm._auth.keepalive._rotate_cookies`` via a
                late-bound wrapper. Must be async — it is awaited from
                the keepalive loop.
        """
        # Normalize the effective storage path onto the auth object so every
        # downstream code path (refresh_auth, lifecycle on-close save,
        # the keepalive loop) writes to the same file. Without this, an
        # explicit ``storage_path=`` kwarg only reaches the keepalive loop
        # while ``auth.storage_path is None`` causes refresh and on-close
        # saves to silently skip persistence. ``dataclasses.replace`` instead
        # of in-place mutation so a caller reusing ``AuthTokens`` across
        # multiple clients (with different storage paths) doesn't see one
        # client's path leak into another.
        if storage_path is not None and auth.storage_path != storage_path:
            auth = dataclasses.replace(auth, storage_path=storage_path)

        # Direct client-owned reference to the authoritative ``AuthTokens``
        # instance. Set AFTER the ``storage_path`` normalization above so it
        # captures the same (possibly rebound) instance that
        # :func:`compose_client_internals` then propagates into
        # :class:`CookiePersistence`, the snapshot-provider lambdas,
        # and :class:`SourceUploadPipeline`. ADR-016's Auth Instance
        # Invariant requires every reference across the live object graph
        # to alias this exact same mutable object so
        # :meth:`AuthRefreshCoordinator.update_auth_tokens` in-place
        # mutations are observed everywhere.
        #
        # ``refresh_auth()``, the public ``auth`` property, and the
        # ``SourceUploadPipeline(auth=...)`` constructor argument all back
        # off this field instead of any former Session-owned auth
        # reference. The client shell helper
        # (``tests/_helpers/client_factory.build_client_shell_for_tests``)
        # mirrors the production attribute shape so tests exercise the
        # same code path as production.
        self._auth = auth

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
        # internal-derived keepalive storage path is
        # canonicalized.
        keepalive_storage_path: Path | None = auth.storage_path
        if keepalive_storage_path is not None:
            keepalive_storage_path = Path(keepalive_storage_path).expanduser().resolve()

        # Cross-validate the RPC throttle against the underlying httpx pool
        # before the collaborator builder swallows the ``limits=None``
        # sentinel into its own ``ConnectionLimits()`` synthesis.
        # Performed here so the constraint is enforced uniformly regardless
        # of whether the caller passed an explicit ``ConnectionLimits``
        # instance or relied on the default — scalar config validation
        # can't see the caller's intent once the default has been substituted.
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

        # Stage B1 PR 2 of the post-refactoring plan inverted the
        # composition root. Session-elimination Phase 3 finishes the
        # ownership move: :func:`compose_client_internals` binds
        # composition state onto ``self._composed`` and returns only the
        # collaborators + executor that feature adapters need.
        #
        # The public NotebookLMClient kwarg surface is unchanged — the
        # four seam kwargs (``decode_response`` / ``sleep`` /
        # ``is_auth_error`` / ``async_client_factory``) live on
        # ``compose_client_internals`` and the client-shell test helper
        # only.
        self._seams = resolve_client_seams(
            decode_response=None,
            sleep=None,
            is_auth_error=None,
        )
        self._composed = ClientComposed(max_concurrent_rpcs=max_concurrent_rpcs)

        internals = compose_client_internals(
            auth=auth,
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
            # Phase 2 PR 3 injectable seams — pass-through to the
            # lifecycle. ``None`` (default) preserves the legacy late-
            # binding contract via ``_default_cookie_saver`` /
            # ``_default_cookie_rotator``.
            cookie_saver=cookie_saver,
            cookie_rotator=cookie_rotator,
            seams=self._seams,
            composed=self._composed,
        )
        # Owned reference to the collaborator bundle so
        # :meth:`metrics_snapshot` (and any future
        # NotebookLMClient-side collaborator consumers) read from the
        # same bundle feature internals use.
        self._collaborators = internals.collaborators
        # Owned reference to the RPC executor so ``client.rpc_call``
        # dispatches through it directly rather than through a
        # compatibility wrapper. The executor satisfies the
        # ``RpcCaller`` Protocol and is the same instance the feature
        # APIs receive (``internals.executor`` is shared with
        # ``SourcesAPI`` / ``NotebooksAPI`` / ``ArtifactsRuntimeAdapter``
        # / ``ChatAPI`` / etc., so a test that swaps the executor's
        # ``rpc_call`` sees the swap on every feature consumer).
        self._rpc_executor = internals.executor

        # ADR-014 Rule 2: the upload pipeline takes its three runtime
        # collaborators (``rpc`` + ``drain`` + ``lifecycle``) directly
        # instead of via a composite-runtime adapter. ``Kernel`` and
        # ``AuthMetadata`` continue to flow as separate parameters per
        # the ADR-014 Rule 6 example. ``NotebookLMClient.__init__`` is
        # the composition root that knows these internals;
        # ``SourcesAPI`` no longer reads them back off a broad host.
        source_uploader = SourceUploadPipeline(
            rpc=internals.executor,
            drain=internals.collaborators.drain_tracker,
            lifecycle=internals.collaborators.lifecycle,
            kernel=internals.collaborators.kernel,
            # ADR-016's Auth Instance Invariant: the upload pipeline
            # reads the client-owned ``self._auth`` reference set above
            # instead of a detached auth copy. Production refresh-time
            # mutation is therefore observed by the uploader unchanged.
            auth=self._auth,
            upload_timeout=upload_timeout,
            max_concurrent_uploads=max_concurrent_uploads,
            record_upload_queue_wait=internals.collaborators.metrics.record_upload_queue_wait,
        )
        # Hold the uploader as a first-class client attribute so the
        # open-time loop-affinity reset (issue #1196 upload variant) can
        # reach it independently of the ``self.sources`` feature surface:
        # the upload semaphore is a lazily-built loop-bound
        # ``asyncio.Semaphore`` that must be discarded on close→reopen, the
        # same as the RPC semaphore. ``__aenter__`` threads this into
        # ``ClientLifecycle.open`` which calls
        # ``set_bound_loop`` / ``reset_after_open`` on it.
        self._source_uploader = source_uploader
        # ADR-014 Rule 3 Stage B (Stage B1 PR 2 of the post-refactoring
        # plan): simple features take their RpcCaller dependency directly
        # from the composition root's executor, not from a Stage A
        # accessor on the deleted Session surface.
        self.sources = SourcesAPI(
            internals.executor,
            uploader=source_uploader,
            upload_timeout=upload_timeout,
            max_concurrent_uploads=max_concurrent_uploads,
        )
        self.notebooks = NotebooksAPI(internals.executor, sources_api=self.sources)
        # Phase 5 wiring per docs/refactor-history.md Migration Plan steps 6-7:
        # the legacy single-service handoff passed as ``mind_map_service=``
        # is replaced with the explicit
        # NoteService + NoteBackedMindMapService split. NoteService owns the
        # raw row primitives; NoteBackedMindMapService is the mind-map-only
        # adapter the download path uses; the artifact-generation path uses
        # NoteService.create_note directly to persist a generated mind map.
        note_service = NoteService(internals.executor)
        mind_maps = NoteBackedMindMapService(note_service)
        # ADR-014 Rule 2: the artifacts API takes its three runtime
        # collaborators (``rpc`` + ``drain`` + ``lifecycle``) directly
        # instead of via a composite-runtime adapter. ``rpc`` covers
        # RPC dispatch; ``drain`` covers ``operation_scope`` and the
        # close-time ``register_drain_hook`` used by the polling
        # service; ``lifecycle`` covers ``assert_bound_loop``.
        self.artifacts = ArtifactsAPI(
            rpc=internals.executor,
            drain=internals.collaborators.drain_tracker,
            lifecycle=internals.collaborators.lifecycle,
            notebooks=self.notebooks,
            mind_maps=mind_maps,
            note_service=note_service,
            storage_path=storage_path,
        )
        # ``chat`` MUST be constructed before ``notes`` so the
        # ``save_chat_answer`` callback (= ``chat.save_answer_as_note``)
        # exists at NotesAPI construction time. Phase 6 (refactor-history.md
        # Step 8, ADR-013) moves saved-chat ownership to ChatAPI and
        # has NotesAPI delegate via constructor injection.
        #
        # Wave 8 of session-decoupling (ADR-014 Rule 2 Corollary): ChatAPI
        # takes its four direct collaborators (RpcCaller, RuntimeTransport,
        # ReqidCounter, LoopGuard) by keyword argument. The transport is
        # sourced from ``self._composed``; other runtime fields come from
        # the :class:`ClientInternals` returned by the composition root.
        self.chat = ChatAPI(
            rpc=internals.executor,
            transport=self._composed.transport,
            reqid=internals.collaborators.reqid,
            loop_guard=internals.collaborators.lifecycle,
            notebooks=self.notebooks,
        )
        self.notes = NotesAPI(
            notes=note_service,
            mind_maps=mind_maps,
            save_chat_answer=self.chat.save_answer_as_note,
        )
        # Pure-RPC features (typed as ``rpc: RpcCaller``). Wave 7 of
        # session-decoupling: pass the ``RpcExecutor`` collaborator
        # directly. Stage B1 PR 2 updated the source from
        # the deleted Session executor accessor to the
        # composed executor.
        self.research = ResearchAPI(internals.executor)
        self.settings = SettingsAPI(internals.executor)
        self.sharing = SharingAPI(internals.executor)

    @property
    def auth(self) -> AuthTokens:
        """Get the authentication tokens.

        ADR-016's Auth Instance Invariant requires every reference across
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
        :meth:`notebooklm._artifact_polling.ArtifactPollingService._run_poll_loop_in_scope`),
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

        Stage B1 PR 2 of the post-refactoring plan migrated the read off
        the deleted Stage A collaborator accessor onto the
        bundle stored by :meth:`__init__` from the composition root's
        :class:`ClientInternals`.
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
        (:func:`compose_client_internals`). The
        ``tests/_helpers/client_factory.build_client_shell_for_tests``
        helper wires ``_auth`` and ``_collaborators`` from the composed
        runtime directly, so test shells observe the same resolution path.

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
            upload_timeout=kwargs["upload_timeout"],
            on_rpc_event=kwargs["on_rpc_event"],
        )
        return self._client

    def __await__(self) -> Generator[Any, None, NotebookLMClient]:
        """Legacy await path — returns a built-but-unentered client.

        Emits ``DeprecationWarning`` (removed in v1.0). Prefer the
        ``async with NotebookLMClient.from_storage(...) as client:`` idiom.
        """
        warnings.warn(
            "Awaiting NotebookLMClient.from_storage(...) is deprecated; use "
            "`async with NotebookLMClient.from_storage(...) as client:` "
            "instead. The await form will be removed in v1.0.",
            DeprecationWarning,
            stacklevel=2,
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
