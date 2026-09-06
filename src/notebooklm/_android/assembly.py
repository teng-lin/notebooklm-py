"""Branch-local composition for the Android backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import httpx

from .._auth.mint_service import MintService
from .._auth.profile_store import ProfileStore
from .._client_contracts import (
    AndroidAssembly,
    AndroidAssemblyConfig,
    AndroidCredentials,
    AndroidDependencies,
    FeatureNamespaces,
    installed_backend_map,
)
from .._runtime.config import normalize_max_concurrent_uploads, resolve_chat_read_timeout
from .._runtime.init import SharedRuntime
from .artifacts import AndroidArtifactsAPI
from .assets import AndroidAssetDownloadService
from .auth import _make_bearer_provider, _NoMasterTokenReader
from .chat import AndroidChatAPI
from .collections import AndroidCollectionsAPI
from .labels import AndroidLabelsAPI
from .mind_maps import AndroidMindMapsAPI
from .note_backed import NoteBackedMindMapArtifactAdapter
from .notebooks import AndroidNotebooksAPI
from .notes import AndroidNotesAPI
from .phenotype import PhenotypeTokenProvider
from .raw import AndroidRawAPI
from .research import AndroidResearchAPI
from .runtime import AndroidRuntime
from .session import AndroidSession
from .settings import AndroidSettingsAPI
from .sharing import AndroidSharingAPI
from .sources import AndroidSourcesAPI
from .upload import AndroidUploadPipeline

if TYPE_CHECKING:
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


def _validate_android_settings(
    *,
    rate_limit_max_retries: int,
    server_error_max_retries: int,
    max_concurrent_uploads: int | None,
) -> None:
    """Validate Android-owned values in the historical constructor order."""

    if rate_limit_max_retries < 0:
        raise ValueError(f"rate_limit_max_retries must be >= 0, got {rate_limit_max_retries}")
    if server_error_max_retries < 0:
        raise ValueError(f"server_error_max_retries must be >= 0, got {server_error_max_retries}")
    normalize_max_concurrent_uploads(max_concurrent_uploads)


def assemble_android_backend(
    *,
    shared: SharedRuntime,
    config: AndroidAssemblyConfig,
    credentials: AndroidCredentials,
    deps: AndroidDependencies,
) -> AndroidAssembly:
    """Return a complete Android graph without reading or mutating a client."""

    backend = config.backend
    retry = config.retry
    transfers = config.transfers
    features = config.features
    _validate_android_settings(
        rate_limit_max_retries=retry.rate_limit_max_retries,
        server_error_max_retries=retry.server_error_max_retries,
        max_concurrent_uploads=transfers.max_concurrent_uploads,
    )
    master_token_reader = deps.master_token_reader
    if master_token_reader is None:
        master_token_reader = (
            ProfileStore(credentials.profile_path)
            if credentials.profile_path is not None
            else _NoMasterTokenReader()
        )
    oauth_minter = deps.oauth_minter
    if oauth_minter is None:
        oauth_minter = MintService()
    bearer_provider = _make_bearer_provider(master_token_reader, oauth_minter)
    session = AndroidSession(
        bearer_provider,
        shared.call_supervisor,
        timeout=backend.rpc_timeout,
        rate_limit_max_retries=retry.rate_limit_max_retries,
        server_error_max_retries=retry.server_error_max_retries,
        refresh_retry_delay=deps.refresh_retry_delay,
        metrics=shared.metrics,
        sleep=deps.sleep,
    )
    asset_downloads = AndroidAssetDownloadService(
        bearer_provider=bearer_provider,
        supervisor=shared.call_supervisor,
    )
    upload_pipeline = AndroidUploadPipeline(
        session=session,
        bearer_provider=bearer_provider,
        start_timeout=_http_timeout(transfers.start_timeout),
        finalize_timeout=_http_timeout(transfers.finalize_timeout),
        drive_timeout=_http_timeout(transfers.drive_timeout),
        max_concurrent_uploads=transfers.max_concurrent_uploads,
        record_upload_queue_wait=shared.metrics.record_upload_queue_wait,
    )
    phenotype = PhenotypeTokenProvider()
    android = AndroidRuntime(
        bearer_provider=bearer_provider,
        session=session,
        upload_pipeline=upload_pipeline,
        asset_downloads=asset_downloads,
        phenotype=phenotype,
    )
    sources = AndroidSourcesAPI(
        session,
        upload_pipeline,
        drive_download=upload_pipeline.drive_download_scope,
        phenotype=phenotype,
    )
    notebooks = AndroidNotebooksAPI(session, sources)
    notes = AndroidNotesAPI(session)
    note_backed_artifacts = NoteBackedMindMapArtifactAdapter(
        notes._list_note_backed_mind_maps,
    )
    artifacts = AndroidArtifactsAPI(
        session=session,
        supervisor=shared.call_supervisor,
        notebooks=notebooks,
        mind_maps=note_backed_artifacts,
        asset_downloads=asset_downloads,
    )
    mind_maps = AndroidMindMapsAPI(
        session=session,
        artifacts=artifacts,
        notes=notes,
    )
    chat = AndroidChatAPI(
        session=session,
        loop_guard=shared.call_supervisor,
        chat_timeout=resolve_chat_read_timeout(features.chat_timeout, backend.rpc_timeout),
        chat_response_max_bytes=features.chat_response_max_bytes,
        notebooks=notebooks,
        created_chat_sessions=notebooks,
    )
    research = AndroidResearchAPI(
        session,
        sources,
        base_timeout=backend.rpc_timeout,
        import_research_timeout=cast(Any, features.import_research_timeout),
    )
    settings = AndroidSettingsAPI(session)
    sharing = AndroidSharingAPI(session)
    labels = AndroidLabelsAPI(session, list_sources=sources.list)
    collections = AndroidCollectionsAPI(
        session,
        list_notebooks=notebooks.list,
    )

    namespaces = FeatureNamespaces(
        notebooks=notebooks,
        sources=sources,
        artifacts=artifacts,
        chat=chat,
        research=research,
        notes=notes,
        mind_maps=mind_maps,
        settings=settings,
        sharing=sharing,
        labels=labels,
        collections=collections,
    )
    return AndroidAssembly(
        backend="android",
        namespaces=namespaces,
        raw=AndroidRawAPI(session),
        runtime=android,
        shared=shared,
        transports=(session, asset_downloads, upload_pipeline, phenotype),
        loop_participants=(
            shared.call_supervisor,
            chat,
            bearer_provider,
            session,
            upload_pipeline,
        ),
        backends=installed_backend_map("android"),
    )


__all__ = ["assemble_android_backend"]
