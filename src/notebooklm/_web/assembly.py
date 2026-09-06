"""Branch-local composition for the Web backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import httpx

from .._client_contracts import (
    FeatureNamespaces,
    WebAssembly,
    WebAssemblyConfig,
    WebCredentials,
    WebDependencies,
    installed_backend_map,
)
from .._runtime.config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    resolve_chat_read_timeout,
)
from .._runtime.init import SharedRuntime
from .artifacts import WebArtifactsAPI
from .chat import WebChatAPI
from .collections import WebCollectionsAPI
from .labels import WebLabelsAPI
from .mind_maps import NoteBackedMindMapService, WebMindMapsAPI
from .notebooks import WebNotebooksAPI
from .notes import NoteService, WebNotesAPI
from .raw import WebRawAPI
from .research import WebResearchAPI
from .settings import WebSettingsAPI
from .sharing import WebSharingAPI
from .sources import WebSourcesAPI
from .transport.config import validate_web_config
from .transport.init import (
    WebRuntime,
    _resolve_async_client_factory,
    build_web_runtime,
)
from .transport.seams import ClientSeams, resolve_client_seams

if TYPE_CHECKING:
    from .._client_compat import CompatibilityDependencies, CompatibilitySpec
    from ..options import TimeoutOptions


def _http_timeout(options: TimeoutOptions | None) -> httpx.Timeout | None:
    if options is None:
        return None
    return httpx.Timeout(
        connect=options.connect,
        read=options.read,
        write=options.write,
        pool=options.pool,
    )


def assemble_web_backend(
    *,
    shared: SharedRuntime,
    config: WebAssemblyConfig,
    credentials: WebCredentials,
    deps: WebDependencies,
) -> WebAssembly:
    """Return a complete Web graph without reading or mutating a client."""

    seams = deps.seams or resolve_client_seams(
        decode_response=deps.decode_response,
        sleep=deps.sleep,
        is_auth_error=deps.is_auth_error,
    )
    backend = config.backend
    transport = backend.transport
    session = backend.session
    retry = config.retry
    transfers = config.transfers
    features = config.features
    hooks = backend.hooks
    web_config, _ = validate_web_config(
        read_timeout=transport.read_timeout,
        write_timeout=transport.write_timeout,
        pool_timeout=transport.pool_timeout,
        connect_timeout=deps.connect_timeout,
        refresh_retry_delay=deps.refresh_retry_delay,
        rate_limit_max_retries=retry.rate_limit_max_retries,
        server_error_max_retries=retry.server_error_max_retries,
        keepalive=session.keepalive_interval,
        keepalive_min_interval=session.keepalive_min_interval,
        keepalive_storage_path=credentials.keepalive_storage_path,
        auth_storage_path=credentials.auth.storage_path,
        limits=transport.limits,
        max_concurrent_uploads=transfers.max_concurrent_uploads,
        max_concurrent_rpcs=config.shared_config.max_concurrent_rpcs,
        decode_response=seams.decode_response,
        sleep=seams.sleep,
        is_auth_error=seams.is_auth_error,
        async_client_factory=_resolve_async_client_factory(deps.async_client_factory),
        shared_config=config.shared_config,
    )
    web = build_web_runtime(
        config=web_config,
        auth=credentials.auth,
        refresh_callback=deps.refresh_callback,
        use_default_refresh_callback=deps.use_default_refresh_callback,
        shared=shared,
        start_timeout=_http_timeout(transfers.start_timeout),
        finalize_timeout=_http_timeout(transfers.finalize_timeout),
        drive_timeout=_http_timeout(transfers.drive_timeout),
        max_concurrent_uploads=transfers.max_concurrent_uploads,
        cookie_saver=hooks.cookie_saver if hooks is not None else None,
        cookie_rotator=hooks.cookie_rotator if hooks is not None else None,
        seams=seams,
        composed=deps.composed,
    )
    sources = WebSourcesAPI(
        web.executor,
        supervisor=shared.call_supervisor,
        uploader=web.source_uploader,
        # Preserve the legacy introspection identity without bypassing the
        # phase-specific TransferOptions consumed by SourceUploadPipeline.
        upload_timeout=deps.legacy_upload_timeout,
        max_concurrent_uploads=transfers.max_concurrent_uploads,
    )
    notebooks = WebNotebooksAPI(
        web.executor,
        sources_api=sources,
        supervisor=shared.call_supervisor,
    )
    note_service = NoteService(web.executor, supervisor=shared.call_supervisor)
    mind_maps = NoteBackedMindMapService(note_service)
    artifacts = WebArtifactsAPI(
        rpc=web.executor,
        supervisor=shared.call_supervisor,
        notebooks=notebooks,
        mind_maps=mind_maps,
        note_service=note_service,
        storage_path=credentials.storage_path,
    )
    chat = WebChatAPI(
        rpc=web.executor,
        transport=web.composed.transport,
        reqid=web.reqid,
        loop_guard=shared.call_supervisor,
        supervisor=shared.call_supervisor,
        chat_timeout=resolve_chat_read_timeout(features.chat_timeout, transport.read_timeout),
        chat_response_max_bytes=features.chat_response_max_bytes,
        notebooks=notebooks,
        created_chat_sessions=notebooks,
    )
    notes = WebNotesAPI(
        notes=note_service,
        mind_maps=mind_maps,
        supervisor=shared.call_supervisor,
    )
    mind_maps_api = WebMindMapsAPI(
        rpc=web.executor,
        mind_maps=mind_maps,
        artifacts=artifacts,
        notebooks=notebooks,
        notes=notes,
        supervisor=shared.call_supervisor,
    )
    research = WebResearchAPI(
        web.executor,
        supervisor=shared.call_supervisor,
        base_timeout=transport.read_timeout,
        import_research_timeout=cast(Any, features.import_research_timeout),
    )
    settings = WebSettingsAPI(web.executor, supervisor=shared.call_supervisor)
    sharing = WebSharingAPI(web.executor, supervisor=shared.call_supervisor)
    labels = WebLabelsAPI(
        web.executor,
        list_sources=sources.list,
        supervisor=shared.call_supervisor,
    )
    collections = WebCollectionsAPI(
        web.executor,
        list_notebooks=notebooks.list,
        supervisor=shared.call_supervisor,
    )

    namespaces = FeatureNamespaces(
        notebooks=notebooks,
        sources=sources,
        artifacts=artifacts,
        chat=chat,
        research=research,
        notes=notes,
        mind_maps=mind_maps_api,
        settings=settings,
        sharing=sharing,
        labels=labels,
        collections=collections,
    )
    return WebAssembly(
        backend="web",
        namespaces=namespaces,
        raw=WebRawAPI(web.executor),
        runtime=web,
        shared=shared,
        transports=(web.web_transport, web.source_uploader),
        loop_participants=(shared.call_supervisor, web.reqid, web.auth_coord, chat),
        backends=installed_backend_map("web"),
        seams=seams,
    )


def build_compatibility_runtime(
    *,
    shared: SharedRuntime,
    spec: CompatibilitySpec,
    deps: CompatibilityDependencies,
) -> WebRuntime:
    """Build the deprecated Android ``rpc_call`` Web runtime on first use."""

    resolved_seams = resolve_client_seams(
        decode_response=deps.seam_overrides.decode_response,
        sleep=deps.seam_overrides.sleep,
        is_auth_error=deps.seam_overrides.is_auth_error,
    )
    deps.seam_overrides.install(resolved_seams)
    seams = ClientSeams(
        decode_response=deps.seam_overrides.decode,
        sleep=deps.seam_overrides.delay,
        is_auth_error=deps.seam_overrides.classify_auth_error,
    )
    web_config, _ = validate_web_config(
        read_timeout=spec.read_timeout,
        write_timeout=spec.write_timeout,
        pool_timeout=spec.pool_timeout,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT,
        refresh_retry_delay=spec.refresh_retry_delay,
        rate_limit_max_retries=spec.rate_limit_max_retries,
        server_error_max_retries=spec.server_error_max_retries,
        keepalive=None,
        keepalive_min_interval=DEFAULT_KEEPALIVE_MIN_INTERVAL,
        keepalive_storage_path=None,
        auth_storage_path=spec.auth.storage_path,
        limits=None,
        max_concurrent_uploads=spec.max_concurrent_uploads,
        max_concurrent_rpcs=spec.shared_config.max_concurrent_rpcs,
        decode_response=seams.decode_response,
        sleep=seams.sleep,
        is_auth_error=seams.is_auth_error,
        async_client_factory=_resolve_async_client_factory(deps.async_client_factory),
        shared_config=spec.shared_config,
    )
    return build_web_runtime(
        config=web_config,
        auth=spec.auth,
        refresh_callback=deps.refresh_callback,
        use_default_refresh_callback=deps.use_default_refresh_callback,
        shared=shared,
        start_timeout=None,
        finalize_timeout=None,
        drive_timeout=None,
        max_concurrent_uploads=DEFAULT_MAX_CONCURRENT_UPLOADS,
        cookie_saver=None,
        cookie_rotator=None,
        seams=seams,
    )


__all__ = ["assemble_web_backend", "build_compatibility_runtime"]
