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
import os
from collections.abc import Callable, Generator, Mapping
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from .rpc import RPCMethod
    from .types import ClientMetricsSnapshot, ConnectionLimits, RpcTelemetryEvent

# Keep feature/collaborator types importable for runtime type-hint introspection.
from ._android.runtime import AndroidRuntime
from ._artifacts import ArtifactsAPI
from ._auth import tokens as _auth_tokens
from ._auth.account import _probe_authuser
from ._auth.account import authuser_query as authuser_query
from ._auth.account_email import AccountEmailCacheKey, resolve_account_email
from ._auth.extraction import extract_wiz_field as extract_wiz_field
from ._auth.session import refresh_auth_session
from ._chat import ChatAPI
from ._client_assembly import (
    BackendName,
    BackendPreference,
    _assemble_client,
    resolve_backend_preference,
)
from ._collections import CollectionsAPI
from ._deprecation import warn_deprecated, warn_registered_deprecation
from ._env import get_base_url as get_base_url
from ._labels import LabelsAPI
from ._mind_maps_api import MindMapsAPI
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._research import BaseResearchAPI
from ._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_CHAT_RESPONSE_MAX_BYTES,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from ._runtime.init import SharedRuntime
from ._settings import SettingsAPI
from ._sharing import SharingAPI
from ._sources import SourcesAPI
from ._url_utils import is_google_auth_redirect as is_google_auth_redirect
from ._web.mind_maps import NoteBackedMindMapService as NoteBackedMindMapService  # noqa: F401
from ._web.notes import NoteService as NoteService  # noqa: F401
from ._web.transport.composed import ClientComposed as ClientComposed  # noqa: F401
from ._web.transport.executor import RpcExecutor as RpcExecutor  # noqa: F401
from ._web.transport.init import WebRuntime
from ._web.transport.init import compose_client_internals as compose_client_internals  # noqa: F401
from ._web.transport.lifecycle import CookieRotator, CookieSaver
from ._web.transport.seams import ClientSeams
from ._web.transport.seams import resolve_client_seams as resolve_client_seams  # noqa: F401
from ._web.transport.sidecar import LazyWebSidecar
from .auth import AuthTokens
from .exceptions import AuthExtractionError as AuthExtractionError
from .raw import AndroidRawAPI, WebRawAPI

__all__ = ["NotebookLMClient"]

logger = logging.getLogger(__name__)


class NotebookLMClient:
    """Async client for NotebookLM API.

    Provides access to NotebookLM functionality through namespaced sub-clients:
    - notebooks: Create, copy, list, delete, and rename notebooks
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
    - raw: Advanced backend-selected Web or Android wire access

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
        raw: Backend-selected advanced wire access
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
    _collaborators: SharedRuntime
    _web_runtime: WebRuntime | None
    _web_sidecar: LazyWebSidecar | None
    _android_runtime: AndroidRuntime | None
    _backend_preference: BackendPreference
    _backends: Mapping[str, BackendName]
    _rpc_call_deprecation_warned: bool
    sources: SourcesAPI
    notebooks: NotebooksAPI
    artifacts: ArtifactsAPI
    chat: ChatAPI
    notes: NotesAPI
    mind_maps: MindMapsAPI
    research: BaseResearchAPI
    settings: SettingsAPI
    sharing: SharingAPI
    labels: LabelsAPI
    collections: CollectionsAPI
    _raw: WebRawAPI | AndroidRawAPI

    def _require_web_runtime(self) -> WebRuntime:
        """Return the web bundle or fail before a web-only operation."""
        runtime = self._web_runtime
        if runtime is None:
            raise RuntimeError("The web runtime is not available for this client.")
        return runtime

    @property
    def raw(self) -> WebRawAPI | AndroidRawAPI:
        """Return the advanced wire adapter selected for this client backend."""

        return self._raw

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
        *,
        backend: Literal["web", "android"] | None = None,
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
            backend: Preferred namespace backend. ``"web"`` preserves the
                established implementation; ``"android"`` installs the Android
                adapter for every public namespace, with typed namespace
                operations staying on the Android transport. Android validates
                the selected profile's durable master token when the client is
                opened. When omitted, ``NOTEBOOKLM_BACKEND`` is consulted, then
                the default is web.
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
            import_research_timeout=import_research_timeout,
            chat_response_max_bytes=chat_response_max_bytes,
            backend=backend,
        )

    #: Per-client memo for the signed-in account email so a *successful* live probe
    #: (used only when neither the in-memory ``AuthTokens`` nor persisted storage
    #: carries one) runs at most once per account route. A failed/undiscoverable probe
    #: is NOT memoized, so a genuinely account-less profile re-probes on each call —
    #: acceptable for the rare ``include_account`` path. The route key invalidates a
    #: cached email after mid-session profile reload switches or clears the account.
    #: Assigned in ``_assemble_client`` (factory-shell parity).
    _account_email_cache: str | None
    _account_email_cache_route: AccountEmailCacheKey | None

    @property
    def auth(self) -> AuthTokens:
        """Get the authentication tokens.

        ADR-0016's Auth Instance Invariant requires every reference across
        the live object graph to alias the same mutable
        :class:`AuthTokens` object set in :meth:`__init__`, so the public
        ``client.auth`` identity and behavior are unchanged.
        """
        return self._auth

    @property
    def backends(self) -> Mapping[str, Literal["web", "android"]]:
        """Read-only mapping of namespaces to their installed adapter backend.

        Explicit Android selection installs Android adapters for all public
        namespaces, so every mapping value is ``"android"``. The deprecated
        Web-shaped root :meth:`rpc_call` wrapper and its lazy compatibility
        sidecar are outside this namespace mapping.
        """
        return self._backends

    async def __aenter__(self) -> NotebookLMClient:
        """Open the client connection."""
        logger.debug("Opening NotebookLM client")
        # Preserve the historical fail-fast check that primary transport
        # composition is complete, without requiring a Web bundle on Android.
        if self._backend_preference.preferred == "web":
            _ = self._require_web_runtime().composed.transport
        elif self._android_runtime is None:  # pragma: no cover - assembly invariant
            raise RuntimeError("The Android runtime is not available for this client.")
        await self._collaborators.lifecycle.open()
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
        except (KeyboardInterrupt, SystemExit):
            raise
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

        Resource ownership and admission are separate: a successfully drained
        client remains connected, but rejects new top-level work until closed.
        """
        await self._collaborators.lifecycle.drain(timeout=timeout)

    async def close(
        self,
        *,
        drain: bool = True,
        drain_timeout: float | None = None,
    ) -> None:
        """Close every client transport through one root-owned lifecycle wave.

        With ``drain=True`` (the default), admission stops, feature hooks run,
        and the generation waits for supervised work up to ``drain_timeout``
        before teardown. A timeout is retained and re-raised after every
        transport is prepared and closed. ``drain=False`` skips the graceful
        prephase and fences the generation immediately.

        Concurrent callers join the same close wave. A first caller
        cancellation aborts a hung graceful prephase but continues shielding
        teardown; re-cancellation may detach the caller while the strongly
        retained wave finishes in the background.
        """
        await self._collaborators.lifecycle.close(
            drain=drain,
            drain_timeout=drain_timeout,
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
        read_timeout: float | None = None,
        raise_on_null_status: bool = False,
    ) -> Any:
        """Make a deprecated raw Web NotebookLM RPC call.

        Deprecated since v0.9 and removed in v1.0. Web callers should migrate
        to :meth:`notebooklm.raw.WebRawAPI.call`. Android callers should use
        :meth:`notebooklm.raw.AndroidRawAPI.unary` or ``unary_stream`` for
        Android wire methods, or create a Web-selected client and use its
        ``raw.call`` method when Web ``RPCMethod`` access is still required.

        During the 0.x warning window this method retains its historical Web
        behavior under both backend selections. On Android it materialises a
        lazy Web compatibility sidecar on first use. The sidecar never starts a
        Web keepalive task.

        The wrapper forwards to :meth:`RpcExecutor.rpc_call` on the
        executor that was bound during :meth:`__init__` (and that every
        feature API shares). Internal call sites that need to bind the
        underlying internal-only parameters do so against the executor
        surface directly, not via this public wrapper.

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
        if self._backend_preference.preferred == "web":
            if not self._rpc_call_deprecation_warned:
                warn_registered_deprecation("client_rpc_call_web")
                self._rpc_call_deprecation_warned = True
            executor = self._require_web_runtime().executor
            return await executor.rpc_call(
                method=method,
                params=params,
                allow_null=allow_null,
                disable_internal_retries=disable_internal_retries,
                read_timeout=read_timeout,
                raise_on_null_status=raise_on_null_status,
            )

        if not self._rpc_call_deprecation_warned:
            warn_registered_deprecation("client_rpc_call_android")
            self._rpc_call_deprecation_warned = True
        sidecar = self._web_sidecar
        if sidecar is None:  # pragma: no cover - assembly invariant
            raise RuntimeError("The deprecated Web compatibility sidecar is unavailable.")
        async with self._collaborators.call_supervisor.operation_scope("rpc_call.sidecar") as lease:
            runtime = await sidecar.materialize(lease.epoch)
            return await runtime.executor.rpc_call(
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
        chat_timeout: float | None = AUTO_READ_TIMEOUT,
        chat_response_max_bytes: int | None = DEFAULT_CHAT_RESPONSE_MAX_BYTES,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
        *,
        allow_headless: bool = False,
        backend: Literal["web", "android"] | None = None,
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
            backend: Preferred namespace backend. An explicit value takes
                precedence over ``NOTEBOOKLM_BACKEND``; Android installs the
                complete Android namespace graph and the default is web.

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
        backend_preference = resolve_backend_preference(
            explicit=backend,
            env=None if backend is not None else os.environ.get("NOTEBOOKLM_BACKEND"),
        )
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
            backend_preference=backend_preference,
        )

    async def refresh_auth(self, *, allow_headless: bool = False) -> AuthTokens:
        """Refresh the selected backend's authentication state.

        Web refreshes the NotebookLM homepage to obtain a fresh CSRF token
        (SNlM0e) and session ID (FdrFJe). Android re-mints its bearer token;
        when the deprecated Web compatibility sidecar has been materialised,
        its cookies are refreshed best-effort through that sidecar's own Web
        ladder as well.

        The Web path uses explicit collaborators sourced from ``self._auth``
        and the applicable :class:`WebRuntime`. The five kwargs mirror the
        :func:`refresh_auth_session` signature: ``auth`` is the client-owned
        :class:`AuthTokens` instance (the Auth Instance Invariant guarantees
        every auth consumer observes it), and the remaining four come from
        the bundle the composition root produced. The canonical test helper
        wires ``_auth`` and both runtime variants through the same
        :func:`notebooklm._client_assembly._assemble_client` seam, so test
        shells observe the production resolution path.

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

                On Android this applies only to an already-materialised Web
                compatibility sidecar; bearer re-minting does not use a browser.

                SECURITY: the persistent profile is an account-equivalent
                credential (a live Google session). L3 is local-unattended-only
                and must NOT be the auth path for a remote / hosted MCP server.

        Web coordinator single-flight + join-then-rerun (caller-side):

            The base-policy refresh (``allow_headless=False``) is BOTH the
            coordinator's single-flight callback (the mid-RPC 401 path runs it
            via :meth:`AuthRefreshCoordinator.await_refresh`) and what a default
            ``refresh_auth()`` performs directly — so the callback path never
            re-routes into the coordinator and there is no recursion.

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
        async with self._collaborators.call_supervisor.operation_scope("auth.refresh") as lease:
            return await self._refresh_auth_for_epoch(
                allow_headless=allow_headless,
                expected_epoch=lease.epoch,
            )

    async def _refresh_auth_for_epoch(
        self,
        *,
        allow_headless: bool = False,
        expected_epoch: int,
    ) -> AuthTokens:
        """Run refresh against the resource generation admitted by the caller."""

        if self._backend_preference.preferred != "android":
            return await self._refresh_web_auth_for_epoch(
                allow_headless=allow_headless,
                expected_epoch=expected_epoch,
            )

        android = self._android_runtime
        if android is None:  # pragma: no cover - assembly invariant
            raise RuntimeError("Android bearer provider is not configured.")
        await android.bearer_provider.refresh(expected_epoch)

        sidecar = self._web_sidecar
        if sidecar is None or not sidecar.is_materialized:
            return self._auth
        try:
            return await self._refresh_sidecar_auth_for_epoch(
                allow_headless=allow_headless,
                expected_epoch=expected_epoch,
            )
        except Exception as error:
            # A compatibility-cookie failure must not turn a successful bearer
            # refresh into a public failure on master-token-only profiles.
            logger.warning(
                "Android bearer refreshed; compatibility web refresh failed (%s)",
                type(error).__name__,
            )
            return self._auth

    async def _refresh_web_auth_for_epoch(
        self,
        *,
        allow_headless: bool = False,
        expected_epoch: int,
    ) -> AuthTokens:
        """Run only the compatibility web recovery ladder for one epoch."""

        return await self._refresh_web_runtime_auth_for_epoch(
            self._require_web_runtime(),
            allow_headless=allow_headless,
            expected_epoch=expected_epoch,
        )

    async def _refresh_sidecar_auth_for_epoch(
        self,
        *,
        allow_headless: bool = False,
        expected_epoch: int,
    ) -> AuthTokens:
        """Refresh an already-materialised compatibility Web bundle."""

        sidecar = self._web_sidecar
        web = None if sidecar is None else sidecar.runtime
        if web is None:
            raise RuntimeError("The deprecated Web compatibility sidecar is not materialised.")
        return await self._refresh_web_runtime_auth_for_epoch(
            web,
            allow_headless=allow_headless,
            expected_epoch=expected_epoch,
        )

    async def _refresh_web_runtime_auth_for_epoch(
        self,
        web: WebRuntime,
        *,
        allow_headless: bool = False,
        expected_epoch: int,
    ) -> AuthTokens:
        """Run the Web recovery ladder for an explicit Web bundle."""

        coord = web.auth_coord
        if not allow_headless or not coord.has_refresh_callback:
            # Base policy — also the coordinator's single-flight callback body,
            # so this branch must NOT re-enter await_refresh (that would recurse
            # through the callback). No coordinator wired ⇒ same direct path.
            return await refresh_auth_session(
                auth=self._auth,
                kernel=web.kernel,
                auth_coord=coord,
                web_transport=web.web_transport,
                cookie_persistence=web.cookie_persistence,
                allow_headless=allow_headless,
                expected_epoch=expected_epoch,
            )
        # Wider policy: join the in-flight base refresh (join-then-rerun).
        try:
            await coord.await_refresh(expected_epoch)
        except ValueError:
            # Narrow by design: the L3-remediable base-flight failure surfaces as
            # ValueError (dead-cookie 302 / token extraction). refresh-cmd swallows
            # its RuntimeError internally (returns bool), so a RuntimeError here is
            # incidental and must propagate rather than trigger a second refresh.
            return await refresh_auth_session(
                auth=self._auth,
                kernel=web.kernel,
                auth_coord=coord,
                web_transport=web.web_transport,
                cookie_persistence=web.cookie_persistence,
                allow_headless=True,
                expected_epoch=expected_epoch,
            )
        return self._auth

    def get_account_authuser(self) -> int:
        """Return the ``authuser`` index of the signed-in account (0 = default).

        Read from the in-memory :class:`AuthTokens` (populated at construction from
        the profile's persisted metadata or inline ``NOTEBOOKLM_AUTH_JSON``);
        network-free. Falls back to ``0`` for pre-account-binding profiles.
        """
        return self._auth.authuser

    async def get_account_email(self, *, live_fallback: bool = True) -> str | None:
        """Return the signed-in Google account email, or ``None`` if undiscoverable.

        On Android this returns ``AuthTokens.account_email`` only and never
        constructs or probes a Web transport. The resolution ladder below is
        the Web-backend contract.

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
        self-heal write failure degrades to ``None`` / a no-op. The live-fallback
        path requires lifecycle admission, so a closed or draining client raises
        the same operation-admission error as other network work.
        """
        # Android profiles carry no Web identity probe. The durable account
        # route from AuthTokens is the complete network-free answer.
        if self._backend_preference.preferred == "android":
            return self._auth.account_email

        # Resolve every network-free source first.  This preserves the public
        # pre-open/post-close diagnostic behavior without granting a live probe
        # a path around client-wide admission.
        web = self._require_web_runtime()
        email, cached_email, cached_key = await resolve_account_email(
            auth=self._auth,
            cached_email=self._account_email_cache,
            cached_key=self._account_email_cache_route,
            live_fallback=False,
            get_cookies=web.kernel.get_cookies,
            get_http_client=web.kernel.get_http_client,
            probe=_probe_authuser,
            to_thread=asyncio.to_thread,
        )
        self._account_email_cache = cached_email
        self._account_email_cache_route = cached_key
        if email is not None or not live_fallback:
            return email

        # The probe and its optional persistence await while client teardown
        # may race.  Hold one generation-bearing operation lease for their
        # complete lifetime, fence both resource reads, then verify the epoch
        # once more before publishing/caching the result.
        supervisor = self._collaborators.call_supervisor
        async with supervisor.operation_scope("auth.account_email") as lease:
            email, cached_email, cached_key = await resolve_account_email(
                auth=self._auth,
                cached_email=self._account_email_cache,
                cached_key=self._account_email_cache_route,
                live_fallback=True,
                get_cookies=lambda: web.kernel.get_cookies(expected_epoch=lease.epoch),
                get_http_client=lambda: web.kernel.get_http_client(expected_epoch=lease.epoch),
                probe=_probe_authuser,
                to_thread=asyncio.to_thread,
            )
            web.kernel.assert_epoch(lease.epoch)
            self._account_email_cache = cached_email
            self._account_email_cache_route = cached_key
            return email


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

        loaded = await _auth_tokens._load_stored_auth(
            path=Path(path) if path else None,
            profile=profile,
            policy=_auth_tokens.LoadPolicy(
                allow_headless=kwargs["allow_headless"],
                heal_psidts=kwargs["backend_preference"].preferred == "web",
            ),
            auth_type=AuthTokens,
        )
        match loaded:
            case _auth_tokens.InlineLoadedAuth(auth=auth):
                pass
            case _auth_tokens.FileLoadedAuth(auth=auth):
                pass
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
            backend=kwargs["backend_preference"].preferred,
        )
        client._backend_preference = kwargs["backend_preference"]
        if (
            isinstance(loaded, _auth_tokens.FileLoadedAuth)
            and hasattr(client, "_web_runtime")
            and client._web_runtime is not None
        ):
            client._web_runtime.cookie_persistence.register_open_baseline(
                loaded.store, loaded.persistence_baseline
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
