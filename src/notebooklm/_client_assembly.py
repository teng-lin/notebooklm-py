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
public constructor (see the seam policy in ``_web/transport/seams.py``).
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx

from ._client_composed import ClientComposed
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
from ._runtime.init import compose_client_internals
from ._runtime.lifecycle import ClientLifecycle, LoopParticipant, TransportLifecycle
from ._web.artifacts import WebArtifactsAPI
from ._web.chat import WebChatAPI
from ._web.collections import WebCollectionsAPI
from ._web.labels import WebLabelsAPI
from ._web.mind_maps import NoteBackedMindMapService, WebMindMapsAPI
from ._web.notebooks import WebNotebooksAPI
from ._web.notes import NoteService, WebNotesAPI
from ._web.research import WebResearchAPI
from ._web.settings import WebSettingsAPI
from ._web.sharing import WebSharingAPI
from ._web.sources import WebSourcesAPI
from ._web.sources.upload import SourceUploadPipeline
from ._web.transport.lifecycle import CookieRotator, CookieSaver
from ._web.transport.seams import resolve_client_seams
from .auth import AuthTokens

if TYPE_CHECKING:
    from .client import NotebookLMClient
    from .types import ConnectionLimits, RpcTelemetryEvent


BackendName = Literal["web", "android"]
logger = logging.getLogger("notebooklm.backend")


@dataclass(frozen=True)
class BackendPreference:
    """One construction-time backend preference and how it was selected."""

    preferred: BackendName
    reason: Literal["explicit", "env", "default"]


def resolve_backend_preference(*, explicit: str | None, env: str | None) -> BackendPreference:
    """Resolve and validate the backend preference without performing I/O."""
    value: str
    reason: Literal["explicit", "env", "default"]
    if explicit is not None:
        value = explicit
        reason = "explicit"
    elif env is not None:
        value = env
        reason = "env"
    else:
        value = "web"
        reason = "default"
    if value not in ("web", "android"):
        raise ValueError(
            f"Invalid NotebookLM backend {value!r}: expected 'web' or 'android'. "
            "The aliases 'mobile' and 'auto' are not supported."
        )
    return BackendPreference(preferred=cast(BackendName, value), reason=reason)


_NAMESPACE_NAMES = (
    "notebooks",
    "sources",
    "artifacts",
    "chat",
    "research",
    "notes",
    "mind_maps",
    "settings",
    "sharing",
    "labels",
    "collections",
)


def _derive_installed_backends(client: NotebookLMClient) -> MappingProxyType[str, BackendName]:
    """Report each immutable namespace selection from its installed class."""
    installed: dict[str, BackendName] = {}
    for name in _NAMESPACE_NAMES:
        module = type(getattr(client, name)).__module__
        if module.startswith("notebooklm._android."):
            installed[name] = "android"
        elif module.startswith("notebooklm._web."):
            installed[name] = "web"
        else:
            raise RuntimeError(
                f"Cannot determine backend for client.{name}: installed class module is {module!r}"
            )
    return MappingProxyType(installed)


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
    chat_timeout: float | None = AUTO_READ_TIMEOUT,
    import_research_timeout: float | None = AUTO_READ_TIMEOUT,
    chat_response_max_bytes: int | None = DEFAULT_CHAT_RESPONSE_MAX_BYTES,
    backend: BackendName | None = None,
    # --- Production-default overrides (test factory only) -----------------
    # ``NotebookLMClient.__init__`` never passes these; the sentinels
    # resolve to the exact behavior the constructor had when this logic
    # lived inline. The test factory forwards its caller's values
    # explicitly to preserve the historical shell semantics (e.g.
    # ``refresh_callback=None`` → no auth refresh coordination).
    refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None | _UnsetType = _UNSET,
    refresh_retry_delay: float = 0.2,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    keepalive_storage_path: Path | None | _UnsetType = _UNSET,
    # --- Test-only injection seams (see ``_web/transport/seams.py``) ------
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
    client._backend_preference = resolve_backend_preference(
        explicit=backend,
        env=None if backend is not None else os.environ.get("NOTEBOOKLM_BACKEND"),
    )
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
    # Per-client, route-keyed memo for ``get_account_email``. Set here — not in
    # ``__init__`` — so the factory-built shell has it too
    # (test_client_factory_parity, incidents #1196/#1225).
    client._account_email_cache = None
    client._account_email_cache_route = None

    # Production default: the client's own ``refresh_auth`` bound method.
    # The test factory overrides this (typically with ``None`` or a fake)
    # to keep shells network-free.
    if isinstance(refresh_callback, _UnsetType):

        async def refresh_callback(expected_epoch: int) -> AuthTokens:
            return await client._refresh_auth_for_epoch(expected_epoch=expected_epoch)

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
    # for deterministic test injection — see ``_web/transport/seams.py``
    # docstring. Do not promote any of them to a public kwarg without
    # a production caller that varies them.
    client._seams = resolve_client_seams(
        decode_response=decode_response,
        sleep=sleep,
        is_auth_error=is_auth_error,
    )
    # ``ClientComposed`` owned this validation before semaphore ownership moved
    # into ``CallSupervisor``. Keep the check at the same assembly position
    # so combinations of invalid public kwargs preserve the deterministic
    # first error instead of whichever collaborator happens to validate first.
    if max_concurrent_rpcs is not None and max_concurrent_rpcs < 1:
        raise ValueError(f"max_concurrent_rpcs must be >= 1, got {max_concurrent_rpcs!r}")
    client._composed = ClientComposed()

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
        # Injectable seams — pass-through to the lifecycle. A ``None`` cookie
        # saver selects the canonical typed store path; a ``None`` rotator
        # preserves its historical late-bound default.
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
        async_client_factory=async_client_factory,
        seams=client._seams,
        composed=client._composed,
    )
    # Owned reference to the RPC executor so ``client.rpc_call``
    # dispatches through it directly rather than through a
    # compatibility wrapper. The executor satisfies the
    # ``RpcCaller`` Protocol and is the same instance the feature
    # APIs receive (``internals.executor`` is shared with
    # ``SourcesAPI`` / ``NotebooksAPI`` / ``ArtifactsAPI``
    # / ``ChatAPI`` / etc., so a test that swaps the executor's
    # ``rpc_call`` sees the swap on every feature consumer).
    client._rpc_executor = internals.executor

    # ADR-0014 Rule 2: the upload pipeline takes its direct runtime
    # collaborators (``rpc`` + ``supervisor`` + ``kernel`` + ``auth``)
    # instead of reaching through a composite-runtime adapter. This
    # assembly function is
    # the composition root that knows these internals;
    # ``SourcesAPI`` no longer reads them back off a broad host.
    source_uploader = SourceUploadPipeline(
        rpc=internals.executor,
        supervisor=internals.collaborators.call_supervisor,
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
    client.sources = WebSourcesAPI(
        internals.executor,
        supervisor=internals.collaborators.call_supervisor,
        uploader=source_uploader,
        upload_timeout=upload_timeout,
        max_concurrent_uploads=max_concurrent_uploads,
    )
    client.notebooks = WebNotebooksAPI(internals.executor, sources_api=client.sources)
    # Note wiring (see docs/refactor-history.md): an explicit
    # NoteService + NoteBackedMindMapService split. NoteService owns the
    # raw row primitives; NoteBackedMindMapService is the mind-map-only
    # adapter the download path uses; the artifact-generation path uses
    # NoteService.create_note directly to persist a generated mind map.
    note_service = NoteService(
        internals.executor,
        supervisor=internals.collaborators.call_supervisor,
    )
    mind_maps = NoteBackedMindMapService(note_service)
    # The artifacts API takes RPC dispatch plus the single call supervisor.
    # That supervisor is the one authority for polling operation scopes,
    # same-generation leader tasks, loop affinity, and drain-hook registration.
    client.artifacts = WebArtifactsAPI(
        rpc=internals.executor,
        supervisor=internals.collaborators.call_supervisor,
        notebooks=client.notebooks,
        mind_maps=mind_maps,
        note_service=note_service,
        storage_path=storage_path,
    )
    # WebChatAPI (per ADR-0014) takes its
    # five direct collaborators (RpcCaller, RuntimeTransport, ReqidCounter,
    # LoopGuard, NotebookSourceIdProvider) by keyword argument. The transport is
    # sourced from ``client._composed``; other runtime fields come from
    # the :class:`ClientInternals` returned by the composition root.
    client.chat = WebChatAPI(
        rpc=internals.executor,
        transport=client._composed.transport,
        reqid=internals.collaborators.reqid,
        loop_guard=internals.collaborators.call_supervisor,
        chat_timeout=resolve_chat_read_timeout(chat_timeout, timeout),
        chat_response_max_bytes=chat_response_max_bytes,
        notebooks=client.notebooks,
        created_chat_sessions=client.notebooks,
    )
    client.notes = WebNotesAPI(
        notes=note_service,
        mind_maps=mind_maps,
    )
    # Unified mind-map surface over both backends (note-backed + interactive
    # studio artifact); dispatches each op to the correct RPC family (#1256).
    web_mind_maps = WebMindMapsAPI(
        rpc=internals.executor,
        mind_maps=mind_maps,
        artifacts=client.artifacts,
        notebooks=client.notebooks,
        notes=client.notes,
    )
    client.mind_maps = web_mind_maps
    # Pure-RPC features (typed as ``rpc: RpcCaller``). Pass the
    # ``RpcExecutor`` collaborator directly, sourced from the composed
    # executor.
    client.research = WebResearchAPI(
        internals.executor,
        base_timeout=timeout,
        import_research_timeout=import_research_timeout,
    )
    client.settings = WebSettingsAPI(internals.executor)
    client.sharing = WebSharingAPI(internals.executor)
    # Source labels. Takes a narrow ``list_sources`` callable (not the whole
    # SourcesAPI) for the membership->Source join in ``labels.sources()``;
    # wired after ``client.sources`` exists. Same client/bound loop (ADR-0004).
    client.labels = WebLabelsAPI(internals.executor, list_sources=client.sources.list)
    # Android selection replaces the complete public namespace graph. Cross-namespace
    # joins receive selected Android capabilities instead of manufacturing a second
    # frontend, and no operation collaborator routes back through Web. Android
    # dependency/token validation remains deferred to async open, while the gRPC
    # channel remains lazy until the first Android RPC.
    client._android_bearer_provider = None
    client._android_session = None
    android_transports: tuple[TransportLifecycle, ...] = ()
    android_loop_participants: tuple[LoopParticipant, ...] = ()
    if client._backend_preference.preferred == "android":
        from ._android.artifacts import AndroidArtifactsAPI
        from ._android.assets import AndroidAssetDownloadService
        from ._android.auth import _make_bearer_provider
        from ._android.chat import AndroidChatAPI
        from ._android.collections import AndroidCollectionsAPI
        from ._android.labels import AndroidLabelsAPI
        from ._android.mind_maps import AndroidMindMapsAPI
        from ._android.note_backed import NoteBackedMindMapArtifactAdapter
        from ._android.notebooks import AndroidNotebooksAPI
        from ._android.notes import AndroidNotesAPI
        from ._android.phenotype import PhenotypeTokenProvider
        from ._android.research import AndroidResearchAPI
        from ._android.session import AndroidSession
        from ._android.settings import AndroidSettingsAPI
        from ._android.sharing import AndroidSharingAPI
        from ._android.sources import AndroidSourcesAPI
        from ._android.upload import AndroidUploadPipeline

        android_bearer_provider = _make_bearer_provider(
            Path(auth.storage_path) if auth.storage_path is not None else None
        )
        android_session = AndroidSession(
            android_bearer_provider,
            internals.collaborators.call_supervisor,
            timeout=timeout,
        )
        client._android_bearer_provider = android_bearer_provider
        client._android_session = android_session
        android_asset_downloads = AndroidAssetDownloadService(
            bearer_provider=android_bearer_provider,
            supervisor=internals.collaborators.call_supervisor,
        )
        android_upload_pipeline = AndroidUploadPipeline(
            session=android_session,
            bearer_provider=android_bearer_provider,
            upload_timeout=upload_timeout,
            max_concurrent_uploads=max_concurrent_uploads,
            record_upload_queue_wait=internals.collaborators.metrics.record_upload_queue_wait,
        )
        android_phenotype = PhenotypeTokenProvider()
        client.sources = AndroidSourcesAPI(
            android_session,
            android_upload_pipeline,
            drive_download=android_upload_pipeline.drive_download_scope,
            phenotype=android_phenotype,
        )
        client.notebooks = AndroidNotebooksAPI(android_session, client.sources)
        client.notes = AndroidNotesAPI(android_session)
        note_backed_artifacts = NoteBackedMindMapArtifactAdapter(
            client.notes._list_note_backed_mind_maps,
        )
        client.artifacts = AndroidArtifactsAPI(
            session=android_session,
            supervisor=internals.collaborators.call_supervisor,
            notebooks=client.notebooks,
            mind_maps=note_backed_artifacts,
            asset_downloads=android_asset_downloads,
        )
        client.mind_maps = AndroidMindMapsAPI(
            supervisor=internals.collaborators.call_supervisor,
            artifacts=client.artifacts,
            notes=client.notes,
        )
        client.chat = AndroidChatAPI(
            session=android_session,
            loop_guard=internals.collaborators.call_supervisor,
            chat_timeout=resolve_chat_read_timeout(chat_timeout, timeout),
            chat_response_max_bytes=chat_response_max_bytes,
            notebooks=client.notebooks,
            created_chat_sessions=client.notebooks,
        )
        client.research = AndroidResearchAPI(
            android_session,
            client.sources,
            base_timeout=timeout,
            import_research_timeout=import_research_timeout,
        )
        client.settings = AndroidSettingsAPI(android_session)
        client.sharing = AndroidSharingAPI(android_session)
        client.labels = AndroidLabelsAPI(
            android_session,
            list_sources=client.sources.list,
        )
        client.collections = AndroidCollectionsAPI(
            android_session,
            list_notebooks=client.notebooks.list,
        )
        android_transports = (
            android_session,
            android_asset_downloads,
            android_upload_pipeline,
            android_phenotype,
        )
        android_loop_participants = (
            android_bearer_provider,
            android_session,
            android_upload_pipeline,
        )
    else:
        client.collections = WebCollectionsAPI(
            internals.executor,
            list_notebooks=client.notebooks.list,
        )

    client._backends = _derive_installed_backends(client)
    if client._backend_preference.preferred == "android":
        unqualified = [name for name, selected in client._backends.items() if selected == "web"]
        if unqualified:
            logger.info(
                "Android backend preference selected; unqualified namespaces remain web: %s",
                ", ".join(unqualified),
            )

    # The protocol-neutral root is constructed last, after every concrete
    # transport and loop participant exists. Its tuples never mutate after
    # publication, so open/close waves cannot observe a partially assembled
    # graph or silently omit a later-added owner.
    transports: tuple[TransportLifecycle, ...] = (
        internals.collaborators.web_transport,
        source_uploader,
        *android_transports,
    )
    loop_participants: tuple[LoopParticipant, ...] = (
        internals.collaborators.call_supervisor,
        internals.collaborators.reqid,
        internals.collaborators.auth_coord,
        client.chat,
        *android_loop_participants,
    )

    lifecycle = ClientLifecycle(
        supervisor=internals.collaborators.call_supervisor,
        transports=transports,
        loop_participants=loop_participants,
    )
    client._collaborators = dataclasses.replace(
        internals.collaborators,
        _lifecycle=lifecycle,
    )
    client._composed.bind_runtime_collaborators(client._collaborators)
