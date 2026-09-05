"""Branch-local composition for the Web backend."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .._client_contracts import (
    BackendAssembly,
    CookieRotator,
    CookieSaver,
    installed_backend_map,
)
from .._runtime.config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    resolve_chat_read_timeout,
)
from .._runtime.init import SharedRuntime, SharedRuntimeConfig
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
    _resolve_async_client_factory,
    build_web_runtime,
    compose_client_internals,
)
from .transport.seams import ClientSeams, resolve_client_seams

if TYPE_CHECKING:
    from .._client_compat import WebSeamOverrides
    from ..auth import AuthTokens
    from ..client import NotebookLMClient
    from ..types import ConnectionLimits, RpcTelemetryEvent


def assemble_web_backend(
    client: NotebookLMClient,
    *,
    auth: AuthTokens,
    timeout: float,
    storage_path: Path | None,
    keepalive: float | None,
    keepalive_min_interval: float,
    rate_limit_max_retries: int,
    server_error_max_retries: int,
    limits: ConnectionLimits | None,
    max_concurrent_uploads: int | None,
    max_concurrent_rpcs: int | None,
    upload_timeout: httpx.Timeout | None,
    on_rpc_event: Callable[[RpcTelemetryEvent], object] | None,
    cookie_saver: CookieSaver | None,
    cookie_rotator: CookieRotator | None,
    chat_timeout: float | None,
    import_research_timeout: float | None,
    chat_response_max_bytes: int | None,
    refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None,
    use_default_refresh_callback: bool,
    refresh_retry_delay: float,
    connect_timeout: float,
    keepalive_storage_path: Path | None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None,
    decode_response: Callable[..., Any] | None,
    sleep: Callable[[float], Awaitable[Any]] | None,
    is_auth_error: Callable[[Exception], bool] | None,
    shared_config: SharedRuntimeConfig,
) -> BackendAssembly:
    """Install the Web graph and return its neutral lifecycle parts."""

    internals = compose_client_internals(
        auth=auth,
        timeout=timeout,
        connect_timeout=connect_timeout,
        refresh_callback=refresh_callback,
        use_default_refresh_callback=use_default_refresh_callback,
        refresh_retry_delay=refresh_retry_delay,
        keepalive=keepalive,
        keepalive_min_interval=keepalive_min_interval,
        keepalive_storage_path=keepalive_storage_path,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        limits=limits,
        max_concurrent_uploads=max_concurrent_uploads,
        max_concurrent_rpcs=max_concurrent_rpcs,
        upload_timeout=upload_timeout,
        on_rpc_event=on_rpc_event,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
        async_client_factory=async_client_factory,
        decode_response=decode_response,
        sleep=sleep,
        is_auth_error=is_auth_error,
        shared_config=shared_config,
    )
    web = internals.web_runtime
    shared = internals.collaborators
    client._web_runtime = web
    client._web_sidecar = None
    client._android_runtime = None
    client._seams = internals.seams
    client._raw = WebRawAPI(web.executor)
    client.sources = WebSourcesAPI(
        web.executor,
        supervisor=shared.call_supervisor,
        uploader=web.source_uploader,
        upload_timeout=upload_timeout,
        max_concurrent_uploads=max_concurrent_uploads,
    )
    client.notebooks = WebNotebooksAPI(
        web.executor,
        sources_api=client.sources,
        supervisor=shared.call_supervisor,
    )
    note_service = NoteService(web.executor, supervisor=shared.call_supervisor)
    mind_maps = NoteBackedMindMapService(note_service)
    client.artifacts = WebArtifactsAPI(
        rpc=web.executor,
        supervisor=shared.call_supervisor,
        notebooks=client.notebooks,
        mind_maps=mind_maps,
        note_service=note_service,
        storage_path=storage_path,
    )
    client.chat = WebChatAPI(
        rpc=web.executor,
        transport=web.composed.transport,
        reqid=web.reqid,
        loop_guard=shared.call_supervisor,
        supervisor=shared.call_supervisor,
        chat_timeout=resolve_chat_read_timeout(chat_timeout, timeout),
        chat_response_max_bytes=chat_response_max_bytes,
        notebooks=client.notebooks,
        created_chat_sessions=client.notebooks,
    )
    client.notes = WebNotesAPI(
        notes=note_service,
        mind_maps=mind_maps,
        supervisor=shared.call_supervisor,
    )
    client.mind_maps = WebMindMapsAPI(
        rpc=web.executor,
        mind_maps=mind_maps,
        artifacts=client.artifacts,
        notebooks=client.notebooks,
        notes=client.notes,
        supervisor=shared.call_supervisor,
    )
    client.research = WebResearchAPI(
        web.executor,
        supervisor=shared.call_supervisor,
        base_timeout=timeout,
        import_research_timeout=import_research_timeout,
    )
    client.settings = WebSettingsAPI(web.executor, supervisor=shared.call_supervisor)
    client.sharing = WebSharingAPI(web.executor, supervisor=shared.call_supervisor)
    client.labels = WebLabelsAPI(
        web.executor,
        list_sources=client.sources.list,
        supervisor=shared.call_supervisor,
    )
    client.collections = WebCollectionsAPI(
        web.executor,
        list_notebooks=client.notebooks.list,
        supervisor=shared.call_supervisor,
    )

    return BackendAssembly(
        backend="web",
        runtime=web,
        collaborators=shared,
        transports=(web.web_transport, web.source_uploader),
        loop_participants=(shared.call_supervisor, web.reqid, web.auth_coord, client.chat),
        backends=installed_backend_map("web"),
        bind_collaborators=web.composed.bind_runtime_collaborators,
    )


def build_compatibility_runtime(
    *,
    auth: AuthTokens,
    refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None,
    use_default_refresh_callback: bool,
    shared: SharedRuntime,
    shared_config: SharedRuntimeConfig,
    seam_overrides: WebSeamOverrides,
    timeout: float,
    refresh_retry_delay: float,
    rate_limit_max_retries: int,
    server_error_max_retries: int,
    max_concurrent_uploads: int | None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None,
) -> tuple[Any, ClientSeams]:
    """Build the deprecated Android ``rpc_call`` Web runtime on first use."""

    seams = resolve_client_seams(
        decode_response=seam_overrides.decode_response,
        sleep=seam_overrides.sleep,
        is_auth_error=seam_overrides.is_auth_error,
    )
    web_config, _ = validate_web_config(
        timeout=timeout,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT,
        refresh_retry_delay=refresh_retry_delay,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        keepalive=None,
        keepalive_min_interval=DEFAULT_KEEPALIVE_MIN_INTERVAL,
        keepalive_storage_path=None,
        auth_storage_path=auth.storage_path,
        limits=None,
        max_concurrent_uploads=max_concurrent_uploads,
        max_concurrent_rpcs=shared_config.max_concurrent_rpcs,
        decode_response=seams.decode_response,
        sleep=seams.sleep,
        is_auth_error=seams.is_auth_error,
        async_client_factory=_resolve_async_client_factory(async_client_factory),
        shared_config=shared_config,
    )
    return (
        build_web_runtime(
            config=web_config,
            auth=auth,
            refresh_callback=refresh_callback,
            use_default_refresh_callback=use_default_refresh_callback,
            shared=shared,
            upload_timeout=None,
            max_concurrent_uploads=DEFAULT_MAX_CONCURRENT_UPLOADS,
            cookie_saver=None,
            cookie_rotator=None,
            seams=seams,
        ),
        seams,
    )


__all__ = ["assemble_web_backend", "build_compatibility_runtime"]
