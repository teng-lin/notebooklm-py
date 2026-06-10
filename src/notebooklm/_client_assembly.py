"""Single client-assembly seam shared by production and the test factory.

:func:`_assemble_client` is the ONE place that wires a
:class:`~notebooklm.client.NotebookLMClient` instance: auth normalization,
seam resolution, collaborator composition (via
:func:`notebooklm._runtime.init.compose_client_internals`), the upload
pipeline, and every feature API. Two callers exist:

1. ``NotebookLMClient.__init__`` (production) — delegates its whole body
   here, passing only its public kwargs.
2. ``tests/_helpers/client_factory.build_client_shell_for_tests`` — calls
   ``NotebookLMClient.__new__`` and then this function with the
   test-only injection seams (``decode_response`` / ``sleep`` /
   ``is_auth_error`` / ``async_client_factory`` plus ``refresh_callback`` /
   ``refresh_retry_delay`` / ``connect_timeout`` /
   ``keepalive_storage_path``).

History: the test factory previously duplicated this wiring by hand
against ``NotebookLMClient.__new__``. That drifted twice — issue #1196
(the open-time upload-semaphore loop reset needed ``_source_uploader``)
and issue #1225 (the open-time ChatAPI conversation-lock reset needed
``chat``) — each time silently stranding the shell until a test happened
to exercise the missing attribute. Sharing one assembly function makes
that whole drift class structurally impossible;
``tests/_guardrails/test_client_factory_parity.py`` pins the remaining
edges (attributes added *outside* this function).

This module is private: it is not exported from ``notebooklm`` and the
test-only parameters MUST NOT be promoted to ``NotebookLMClient``'s
public constructor (see the seam policy in ``_client_seams``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._artifacts import ArtifactsAPI
from ._chat import ChatAPI
from ._client_composed import ClientComposed
from ._client_seams import resolve_client_seams
from ._labels import LabelsAPI
from ._mind_map import NoteBackedMindMapService
from ._mind_maps_api import MindMapsAPI
from ._note_service import NoteService
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._research import ResearchAPI
from ._runtime.config import (
    DEFAULT_CHAT_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from ._runtime.init import compose_client_internals
from ._runtime.lifecycle import CookieRotator, CookieSaver
from ._settings import SettingsAPI
from ._sharing import SharingAPI
from ._source.upload import SourceUploadPipeline
from ._sources import SourcesAPI
from .auth import AuthTokens

if TYPE_CHECKING:
    from .client import NotebookLMClient
    from .types import ConnectionLimits, RpcTelemetryEvent


class _UnsetType:
    """Sentinel type: resolve the production default inside ``_assemble_client``.

    Used where ``None`` is itself a meaningful caller value
    (``refresh_callback=None`` means "no refresh callback";
    ``keepalive_storage_path=None`` skips the constructor-level
    canonicalization and lets ``compose_client_internals`` apply its own
    raw ``auth.storage_path`` fallback — the historical test-shell
    behavior), so the production default ("use ``client.refresh_auth``" /
    "derive the canonicalized path from ``auth.storage_path``") needs a
    distinct marker.
    """


_UNSET = _UnsetType()


def _assemble_client(
    client: NotebookLMClient,
    *,
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
    # --- Production-default overrides (test factory only) -----------------
    # ``NotebookLMClient.__init__`` never passes these; the sentinels
    # resolve to the exact behavior the constructor had when this logic
    # lived inline. The test factory forwards its caller's values
    # explicitly to preserve the historical shell semantics (e.g.
    # ``refresh_callback=None`` → no auth refresh coordination).
    refresh_callback: Callable[[], Awaitable[AuthTokens]] | None | _UnsetType = _UNSET,
    refresh_retry_delay: float = 0.2,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    keepalive_storage_path: Path | None | _UnsetType = _UNSET,
    # --- Test-only injection seams (see ``_client_seams`` docstring) ------
    decode_response: Callable[..., Any] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    is_auth_error: Callable[[Exception], bool] | None = None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> None:
    """Wire every constructor-set attribute onto ``client``.

    This is the production assembly path — ``NotebookLMClient.__init__``
    is a thin delegate to this function — and simultaneously the seam the
    canonical test factory builds on, so the two can never drift apart
    (incidents #1196 / #1225). Any new constructor-time attribute MUST be
    set here, not in ``__init__`` after the delegation call; the parity
    gate ``tests/_guardrails/test_client_factory_parity.py`` fails
    otherwise.
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
    # Type-coerce only (``Path(...)``) — deliberately NOT
    # ``expanduser().resolve()``: the caller-provided ``storage_path`` and
    # ``auth.storage_path`` stay as supplied (see the keepalive NOTE
    # below); without the coercion a ``str`` argument would compare
    # unequal to an identical ``Path`` and bind a raw ``str`` onto
    # ``auth.storage_path``.
    if storage_path is not None:
        storage_path = Path(storage_path)
        if auth.storage_path != storage_path:
            auth = dataclasses.replace(auth, storage_path=storage_path)

    # Direct client-owned reference to the authoritative ``AuthTokens``
    # instance. Set AFTER the ``storage_path`` normalization above so it
    # captures the same (possibly rebound) instance that
    # :func:`compose_client_internals` then propagates into
    # :class:`CookiePersistence`, the snapshot-provider lambdas,
    # and :class:`SourceUploadPipeline`. ADR-0016's Auth Instance
    # Invariant requires every reference across the live object graph
    # to alias this exact same mutable object so
    # :meth:`AuthRefreshCoordinator.update_auth_tokens` in-place
    # mutations are observed everywhere.
    #
    # ``refresh_auth()``, the public ``auth`` property, and the
    # ``SourceUploadPipeline(auth=...)`` constructor argument all back
    # off this field. The client shell helper
    # (``tests/_helpers/client_factory.build_client_shell_for_tests``)
    # runs this exact function, so tests exercise the same code path as
    # production.
    client._auth = auth

    # Production default: the client's own ``refresh_auth`` bound method.
    # The test factory overrides this (typically with ``None`` or a fake)
    # to keep shells network-free.
    if isinstance(refresh_callback, _UnsetType):
        refresh_callback = client.refresh_auth

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
    # internal-derived keepalive storage path is canonicalized. The test
    # factory passes its own ``keepalive_storage_path`` explicitly, which
    # bypasses THIS canonicalizing derivation (preserving the historical
    # shell semantics); an explicit ``None`` still falls through to
    # ``compose_client_internals``' own raw ``auth.storage_path``
    # fallback downstream.
    if isinstance(keepalive_storage_path, _UnsetType):
        derived_keepalive_path: Path | None = auth.storage_path
        if derived_keepalive_path is not None:
            derived_keepalive_path = Path(derived_keepalive_path).expanduser().resolve()
        keepalive_storage_path = derived_keepalive_path

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

    # The client is the composition root: :func:`compose_client_internals`
    # binds composition state onto ``client._composed`` and returns only the
    # collaborators + executor that feature adapters need.
    #
    # The public NotebookLMClient kwarg surface is unchanged — the
    # four seam kwargs (``decode_response`` / ``sleep`` /
    # ``is_auth_error`` / ``async_client_factory``) live on
    # ``compose_client_internals`` and this private assembly function
    # only.
    #
    # TEST-ONLY injection points: production passes ``None`` for all
    # three runtime seams here (and never supplies an
    # ``async_client_factory``), so they always resolve to the
    # canonical module bindings. The non-``None`` paths exist solely
    # for deterministic test injection — see ``_client_seams`` module
    # docstring. Do not promote any of them to a public kwarg without
    # a production caller that varies them.
    client._seams = resolve_client_seams(
        decode_response=decode_response,
        sleep=sleep,
        is_auth_error=is_auth_error,
    )
    client._composed = ClientComposed(max_concurrent_rpcs=max_concurrent_rpcs)

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
        # Injectable seams — pass-through to the lifecycle. ``None``
        # (default) preserves the late-binding contract via
        # ``_default_cookie_saver`` / ``_default_cookie_rotator``.
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
        async_client_factory=async_client_factory,
        seams=client._seams,
        composed=client._composed,
    )
    # Owned reference to the collaborator bundle so
    # :meth:`metrics_snapshot` (and any future
    # NotebookLMClient-side collaborator consumers) read from the
    # same bundle feature internals use.
    client._collaborators = internals.collaborators
    # Owned reference to the RPC executor so ``client.rpc_call``
    # dispatches through it directly rather than through a
    # compatibility wrapper. The executor satisfies the
    # ``RpcCaller`` Protocol and is the same instance the feature
    # APIs receive (``internals.executor`` is shared with
    # ``SourcesAPI`` / ``NotebooksAPI`` / ``ArtifactsRuntimeAdapter``
    # / ``ChatAPI`` / etc., so a test that swaps the executor's
    # ``rpc_call`` sees the swap on every feature consumer).
    client._rpc_executor = internals.executor

    # ADR-0014 Rule 2: the upload pipeline takes its three runtime
    # collaborators (``rpc`` + ``drain`` + ``lifecycle``) directly
    # instead of via a composite-runtime adapter. ``Kernel`` and
    # ``AuthMetadata`` continue to flow as separate parameters per
    # the ADR-0014 Rule 6 example. This assembly function is
    # the composition root that knows these internals;
    # ``SourcesAPI`` no longer reads them back off a broad host.
    source_uploader = SourceUploadPipeline(
        rpc=internals.executor,
        drain=internals.collaborators.drain_tracker,
        lifecycle=internals.collaborators.lifecycle,
        kernel=internals.collaborators.kernel,
        # ADR-0016's Auth Instance Invariant: the upload pipeline
        # reads the client-owned ``client._auth`` reference set above
        # instead of a detached auth copy. Production refresh-time
        # mutation is therefore observed by the uploader unchanged.
        auth=client._auth,
        upload_timeout=upload_timeout,
        max_concurrent_uploads=max_concurrent_uploads,
        record_upload_queue_wait=internals.collaborators.metrics.record_upload_queue_wait,
    )
    # Hold the uploader as a first-class client attribute so the
    # open-time loop-affinity reset (issue #1196 upload variant) can
    # reach it independently of the ``client.sources`` feature surface:
    # the upload semaphore is a lazily-built loop-bound
    # ``asyncio.Semaphore`` that must be discarded on close→reopen, the
    # same as the RPC semaphore. ``__aenter__`` threads this into
    # ``ClientLifecycle.open`` which calls
    # ``set_bound_loop`` / ``reset_after_open`` on it.
    client._source_uploader = source_uploader
    # Per ADR-0014 Rule 3: simple features take their RpcCaller dependency
    # directly from the composition root's executor.
    client.sources = SourcesAPI(
        internals.executor,
        uploader=source_uploader,
        upload_timeout=upload_timeout,
        max_concurrent_uploads=max_concurrent_uploads,
    )
    client.notebooks = NotebooksAPI(internals.executor, sources_api=client.sources)
    # Note wiring (see docs/refactor-history.md): an explicit
    # NoteService + NoteBackedMindMapService split. NoteService owns the
    # raw row primitives; NoteBackedMindMapService is the mind-map-only
    # adapter the download path uses; the artifact-generation path uses
    # NoteService.create_note directly to persist a generated mind map.
    note_service = NoteService(internals.executor)
    mind_maps = NoteBackedMindMapService(note_service)
    # ADR-0014 Rule 2: the artifacts API takes its three runtime
    # collaborators (``rpc`` + ``drain`` + ``lifecycle``) directly
    # instead of via a composite-runtime adapter. ``rpc`` covers
    # RPC dispatch; ``drain`` covers ``operation_scope`` and the
    # close-time ``register_drain_hook`` used by the polling
    # service; ``lifecycle`` covers ``assert_bound_loop``.
    client.artifacts = ArtifactsAPI(
        rpc=internals.executor,
        drain=internals.collaborators.drain_tracker,
        lifecycle=internals.collaborators.lifecycle,
        notebooks=client.notebooks,
        mind_maps=mind_maps,
        note_service=note_service,
        storage_path=storage_path,
    )
    # ChatAPI (per ADR-0014) takes its
    # four direct collaborators (RpcCaller, RuntimeTransport,
    # ReqidCounter, LoopGuard) by keyword argument. The transport is
    # sourced from ``client._composed``; other runtime fields come from
    # the :class:`ClientInternals` returned by the composition root.
    client.chat = ChatAPI(
        rpc=internals.executor,
        transport=client._composed.transport,
        reqid=internals.collaborators.reqid,
        loop_guard=internals.collaborators.lifecycle,
        chat_timeout=chat_timeout,
        notebooks=client.notebooks,
    )
    client.notes = NotesAPI(
        notes=note_service,
        mind_maps=mind_maps,
    )
    # Unified mind-map surface over both backends (note-backed + interactive
    # studio artifact); dispatches each op to the correct RPC family (#1256).
    client.mind_maps = MindMapsAPI(
        rpc=internals.executor,
        mind_maps=mind_maps,
        artifacts=client.artifacts,
        notebooks=client.notebooks,
    )
    # Pure-RPC features (typed as ``rpc: RpcCaller``). Pass the
    # ``RpcExecutor`` collaborator directly, sourced from the composed
    # executor.
    client.research = ResearchAPI(internals.executor)
    client.settings = SettingsAPI(internals.executor)
    client.sharing = SharingAPI(internals.executor)
    # Source labels. Takes a narrow ``list_sources`` callable (not the whole
    # SourcesAPI) for the membership->Source join in ``labels.sources()``;
    # wired after ``client.sources`` exists. Same client/bound loop (ADR-0004).
    client.labels = LabelsAPI(internals.executor, list_sources=client.sources.list)
