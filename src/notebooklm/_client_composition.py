"""Production-only composition root for :class:`NotebookLMClient`.

:func:`compose_client` is the one production place that wires a
:class:`~notebooklm.client.NotebookLMClient` instance: auth normalization,
collaborator composition (via
:func:`notebooklm._runtime.init.build_web_runtime`), the upload
pipeline, and every feature API. ``NotebookLMClient.__init__`` is its sole
caller and forwards only the documented public constructor options. Tests
construct the smallest runtime owner they exercise; none call this root.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ._artifacts import ArtifactsAPI
from ._chat import ChatAPI
from ._collections import CollectionsAPI
from ._deadline import RuntimeDeadlineFactory
from ._labels import LabelsAPI
from ._mind_map import NoteBackedMindMapService
from ._mind_maps_api import MindMapsAPI
from ._note_service import LegacyNoteBackedService, NoteService
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._read_services import SourceReadService
from ._research import ResearchAPI
from ._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_CHAT_RESPONSE_MAX_BYTES,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
    resolve_chat_read_timeout,
    validate_read_timeout_kwarg,
)
from ._runtime.init import build_web_runtime
from ._runtime.lifecycle import CookieRotator, CookieSaver
from ._settings import SettingsAPI
from ._sharing import SharingAPI
from ._sharing_manager import ShareManager
from ._source.upload import SourceUploadPipeline
from ._sources import SourcesAPI
from ._studio import MindMapFamilyService, StudioCatalog
from ._web.backend import WebRpcBackend
from .auth import AuthTokens

if TYPE_CHECKING:
    from .client import NotebookLMClient
    from .types import ConnectionLimits, RpcTelemetryEvent


def compose_client(
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
    chat_timeout: float | None = AUTO_READ_TIMEOUT,
    import_research_timeout: float | None = AUTO_READ_TIMEOUT,
    chat_response_max_bytes: int | None = DEFAULT_CHAT_RESPONSE_MAX_BYTES,
) -> None:
    """Wire every constructor-set attribute onto ``client``.

    ``NotebookLMClient.__init__`` is the sole caller. Any new
    constructor-time attribute must be set here before the graph is
    published; this function intentionally exposes no test-only seams.
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

    # ``auth`` is handed once to the backend-owned runtime and every
    # auth-sensitive leaf captures that identical mutable object. The public
    # client no longer publishes a second protocol-runtime owner.

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
    # ``build_web_runtime``' own raw ``auth.storage_path``
    # fallback downstream.
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
    if chat_response_max_bytes is not None and chat_response_max_bytes < 1:
        raise ValueError(
            f"chat_response_max_bytes must be >= 1 when supplied (got {chat_response_max_bytes!r})"
        )
    # Both per-RPC read windows are validated here, at the one seam every
    # construction path funnels through (constructor, ``from_storage``, the
    # canonical test factory). A zero/negative window is accepted verbatim by
    # ``httpx.Timeout`` and would otherwise surface only as an instant,
    # unexplained transport timeout on every affected RPC (#2205).
    chat_timeout = validate_read_timeout_kwarg(chat_timeout, name="chat_timeout")
    import_research_timeout = validate_read_timeout_kwarg(
        import_research_timeout, name="import_research_timeout"
    )

    # The public constructor is the sole client construction path. Runtime
    # tests vary explicit backend/provider/transport leaves after using that
    # path; production assembly therefore has no test-only callable seams.
    internals = build_web_runtime(
        auth=auth,
        timeout=timeout,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT,
        refresh_callback=refresh_callback,
        refresh_retry_delay=0.2,
        keepalive=keepalive,
        keepalive_min_interval=keepalive_min_interval,
        keepalive_storage_path=keepalive_storage_path,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        limits=limits,
        max_concurrent_uploads=max_concurrent_uploads,
        max_concurrent_rpcs=max_concurrent_rpcs,
        on_rpc_event=on_rpc_event,
        # Injectable seams — pass-through to the lifecycle. A ``None`` cookie
        # saver selects the canonical typed store path; a ``None`` rotator
        # preserves its historical late-bound default.
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
    )
    # ADR-0014 Rule 2: the upload pipeline takes its three runtime
    # collaborators (``rpc`` + ``drain`` + ``lifecycle``) directly
    # instead of via a composite-runtime adapter. ``Kernel`` and
    # ``AuthMetadata`` continue to flow as separate parameters per
    # the ADR-0014 Rule 6 example. This assembly function is
    # the composition root that knows these internals;
    # ``SourcesAPI`` no longer reads them back off a broad host.
    source_uploader = SourceUploadPipeline(
        rpc=internals.executor,
        drain=internals.drain_tracker,
        lifecycle=internals.lifecycle,
        kernel=internals.backend_kernel,
        # Direct upload/Drive HTTP legs use the provider's reconciled-generation
        # transaction so a matching registration-RPC Set-Cookie is published
        # before their one immutable cookie/route value is cloned. Ordinary RPC
        # transport keeps its separate cached, lock-free generation read.
        generation_provider=internals.provider.reconciled_generation,
        generation_installer=internals.backend_kernel.install_generation,
        upload_timeout=upload_timeout,
        max_concurrent_uploads=max_concurrent_uploads,
        record_upload_queue_wait=internals.metrics.record_upload_queue_wait,
    )
    # The provider is a first-class compatibility owner outside ``_web``.
    # ``WebRpcBackend`` receives the same object only for close ownership; all
    # auth/account facade methods delegate here without teaching the backend
    # package about credential material.
    client._provider = internals.provider
    # Assemble the private semantic port once every backend-owned collaborator
    # is available.
    # One factory serves the backend's CLIENT_TIMEOUT composites and the
    # service-owned workflows (P9.2 contract 3), so both mint identical budgets.
    deadline_factory = RuntimeDeadlineFactory(lambda: internals.lifecycle._timeout)
    client._backend = WebRpcBackend(
        internals.executor,
        source_uploader=source_uploader,
        chat_transport=internals.transport,
        chat_reqid=internals.reqid,
        chat_timeout=resolve_chat_read_timeout(chat_timeout, timeout),
        chat_response_max_bytes=chat_response_max_bytes,
        # Match WebExecutionRuntime's established live timeout-provider
        # contract. Each semantic call captures the current client timeout
        # once; an already-started RuntimeDeadline remains immutable even if a
        # later test/internal reconfiguration changes the lifecycle scalar.
        deadline_factory=deadline_factory,
        metrics=internals.metrics,
        drain_tracker=internals.drain_tracker,
        reqid=internals.reqid,
        pipeline=internals.pipeline,
        provider=internals.provider,
        session=internals.backend_session,
        owns_provider=True,
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
        deadline_factory=deadline_factory,
        _backend=client._backend,
    )
    client.notebooks = NotebooksAPI(
        sources_api=client.sources,
        share_manager=ShareManager(backend=client._backend),
        _backend=client._backend,
        _deadline_factory=deadline_factory,
    )
    # P6.3 note wiring keeps semantic NOTE_* ownership disjoint from the
    # deferred MIND_MAP_* slice. NotesAPI receives the backend-neutral
    # NoteService; note-backed artifact/mind-map callers retain the explicitly
    # named legacy RPC service until MindMapsAPI migrates.
    note_service = NoteService(backend=client._backend)
    legacy_note_backed = LegacyNoteBackedService(internals.executor)
    mind_maps = NoteBackedMindMapService(legacy_note_backed)
    # P5.8: the artifacts compatibility facade owns no native RPC authority.
    # It receives the semantic backend plus the drain/lifecycle collaborators
    # used by its lifecycle-terminal polling service.
    client.artifacts = ArtifactsAPI(
        drain=internals.drain_tracker,
        lifecycle=internals.lifecycle,
        notebooks=client.notebooks,
        mind_maps=mind_maps,
        storage_path=storage_path,
        _backend=client._backend,
        deadline_factory=deadline_factory,
    )
    # P6.1: ChatAPI keeps loop-bound orchestration and client-local state, but
    # delegates all six semantic operations to the client-owned backend.
    client.chat = ChatAPI(
        backend=client._backend,
        loop_guard=internals.lifecycle,
        notebooks=client.notebooks,
        created_chat_sessions=client.notebooks,
    )
    client.notes = NotesAPI(
        notes=note_service,
        mind_maps=mind_maps,
    )
    # Unified mind-map surface over two semantic services. Note-backed flows
    # share the client-scoped NoteService; interactive flows use the Studio
    # family and its typed MIND_MAP_* bindings. The legacy adapter above remains
    # only for artifact/download compatibility outside MindMapsAPI.
    mind_map_catalog = StudioCatalog(backend=client._backend)
    mind_map_studio = MindMapFamilyService(
        backend=client._backend,
        catalog=mind_map_catalog,
        wait_for_completion=client.artifacts.wait_for_completion,
    )
    client.mind_maps = MindMapsAPI(
        notes=note_service,
        studio=mind_map_studio,
    )
    # Research runs entirely on the semantic backend, source reconciliation
    # included: the import/verify loop probes the semantic SourceReadService for
    # neutral records rather than calling back up through the public
    # ``sources.list`` facade it used to receive (P10 R6.4, defect S7). Same
    # SOURCE_LIST operation on the same backend — one fewer layer crossed.
    client.research = ResearchAPI(
        source_lister=SourceReadService(client._backend),
        base_timeout=timeout,
        import_research_timeout=import_research_timeout,
        _backend=client._backend,
    )
    client.settings = SettingsAPI(_backend=client._backend)
    # Sharing is fully migrated to the semantic backend: it takes the
    # client-owned adapter and no RpcCaller at all (P6.5).
    client.sharing = SharingAPI(
        _backend=client._backend,
        _deadline_factory=deadline_factory,
    )
    # Source labels. Takes a narrow ``list_sources`` callable (not the whole
    # SourcesAPI) for the membership->Source join in ``labels.sources()``;
    # wired after ``client.sources`` exists. Same client/bound loop (ADR-0004).
    client.labels = LabelsAPI(
        client._backend, list_sources=client.sources.list, deadline_factory=deadline_factory
    )
    # Collections (account-level notebook groups). Takes a narrow ``list_notebooks``
    # callable for the membership->Notebook join in ``collections.notebooks()``;
    # wired after ``client.notebooks`` exists. Same client/bound loop (ADR-0004).
    client.collections = CollectionsAPI(
        client._backend,
        list_notebooks=client.notebooks.list,
        deadline_factory=deadline_factory,
    )
