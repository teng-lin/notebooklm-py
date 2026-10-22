"""Android implementation of the evidence-qualified public artifact API."""

from __future__ import annotations

import builtins
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import httpx

from .._artifact.creation import InteractiveMindMapCreationRequest
from .._artifact.creation_normalized import NormalizedArtifactCreationRequest, NormalizedAudio
from .._artifact.creation_policy import ANDROID_CREATION_POLICY
from .._artifact.download_selection import PreparedDownloadCache
from .._artifacts import ArtifactsAPI, _incomplete_lookup_error
from .._idempotency import (
    attach_journal_entry,
    bind_operation_journal_entries,
    call_unconfirmed_on_transport_loss,
    claim_generation_entry,
    mark_unconfirmed,
)
from .._notebook_metadata import NotebookSourceIdProvider
from .._runtime.call_supervisor import CallSupervisor, OperationLease
from .._types.artifact_download import (
    ArtifactDownloadListing,
    ArtifactDownloadRequest,
    ArtifactDownloadSelection,
)
from .._types.artifacts import ArtifactCreationCapability, _status_from_code
from .._types.enums import (
    ArtifactStatus,
    ArtifactTypeCode,
    ExportType,
)
from .._types.research import MindMapResult
from ..exceptions import (
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    AuthError,
    DecodingError,
    RPCError,
    ValidationError,
)
from ..outcomes import CommitState
from ..types import (
    Artifact,
    ArtifactListing,
    ArtifactListingComponent,
    ArtifactListingFailure,
    ArtifactLookupStatus,
    ArtifactType,
    GenerationStatus,
    MindMap,
    ReportSuggestion,
)
from .artifact_collaborators import NoteBackedMindMapLister
from .artifact_creation import (
    CREATE_ARTIFACT_METHOD,
    build_normalized_create_artifact_plan,
    create_artifact_once,
)
from .artifact_mutations import (
    DELETE_ARTIFACT_METHOD,
    EXPORT_TO_DRIVE_METHOD,
    GENERATE_ARTIFACT_METHOD,
    delete_artifact,
    export_to_drive,
    retry_failed_artifact,
)
from .artifact_note_mind_maps import (
    ACT_ON_SOURCES_METHOD as ACT_ON_SOURCES_METHOD,
)
from .artifact_note_mind_maps import generate_note_backed_mind_map
from .artifact_outputs import (
    data_table_csv,
    decode_interactive_app_data,
    decode_interactive_mind_map_tree,
    matches_artifact_type,
    report_doc_markdown,
    select_note_backed_mind_map,
    select_single_file_media_url,
    validate_echoed_source_ids,
    write_text_atomic,
)
from .artifact_outputs import validate_artifact_language as _validate_audio_language
from .artifact_proto import ARTIFACT_WIRE_PROTO as _WIRE_PROTO
from .artifact_proto import ARTIFACTS_PROTO as _PROTO
from .artifact_proto import READ_PROTO as _READ_PROTO
from .artifact_reads import (
    GET_ARTIFACT_METHOD,
    LIST_ARTIFACTS_METHOD,
    AndroidArtifactReadMixin,
)
from .artifact_transfers import (
    COPY_ARTIFACTS_ASYNC_METHOD,
    GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD,
    AndroidArtifactTransferMixin,
)
from .assets import AndroidAssetDownloadService, RepresentationKind
from .codecs.artifacts import decode_artifact, decode_artifacts, decode_report_suggestions
from .epoch import bind_workflow_epoch, reset_workflow_epoch
from .errors import sanitize_escaping_exception
from .session import AndroidSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedAndroidDownload:
    """Backend-local typed state for one prepared Android selection."""

    artifact: Artifact
    mind_map: MindMap | None


@dataclass(frozen=True)
class _NoteBackedMindMapState:
    """One aggregate note read in both artifact and hydrated mind-map forms."""

    artifacts: builtins.list[Artifact]
    mind_maps: builtins.list[MindMap]


def android_request_context() -> Any:
    from .upload import android_request_context as build_request_context

    return build_request_context()


_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
DERIVE_ARTIFACT_METHOD = f"/{_SERVICE}/DeriveArtifact"
UPDATE_ARTIFACT_METHOD = f"/{_SERVICE}/UpdateArtifact"
GENERATE_REPORT_SUGGESTIONS_METHOD = f"/{_SERVICE}/GenerateReportSuggestions"


class AndroidArtifactsAPI(AndroidArtifactTransferMixin, AndroidArtifactReadMixin, ArtifactsAPI):
    """Evidence-qualified Android implementation of the public artifact API."""

    @asynccontextmanager
    async def _operation_scope(self, label: str) -> AsyncIterator[OperationLease]:
        async with self._transport.operation_scope(label) as lease:
            token = bind_workflow_epoch(self._transport, lease.epoch)
            try:
                yield lease
            finally:
                reset_workflow_epoch(token)

    def __init__(
        self,
        *,
        session: AndroidSession,
        supervisor: CallSupervisor,
        notebooks: NotebookSourceIdProvider,
        mind_maps: NoteBackedMindMapLister,
        asset_downloads: AndroidAssetDownloadService,
    ) -> None:
        if mind_maps is None:
            raise TypeError("mind_maps must be a NoteBackedMindMapLister")
        self._transport = session
        self._mind_maps = mind_maps
        super().__init__(
            supervisor=supervisor,
            notebooks=notebooks,
            asset_downloads=asset_downloads,
        )
        self._prepared_downloads: PreparedDownloadCache[_PreparedAndroidDownload] = (
            PreparedDownloadCache()
        )

    async def _list_all_studio(
        self,
        notebook_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> builtins.list[Artifact]:
        # evidence: docs/android/proto-evidence-ledger.md#artifact-service-ledger
        epoch_kwargs: dict[str, Any] = (
            {} if expected_epoch is None else {"expected_epoch": expected_epoch}
        )
        response = await self._transport.unary(
            LIST_ARTIFACTS_METHOD,
            _PROTO.ListArtifactsRequest(project_id=notebook_id),
            replay_safe=True,
            response_type=_PROTO.ListArtifactsResponse,
            **epoch_kwargs,
        )
        return [
            artifact
            for artifact in decode_artifacts(response.artifacts, method_id=LIST_ARTIFACTS_METHOD)
            if artifact.status != ArtifactStatus.SUGGESTED.value
        ]

    async def _list_with_note_state(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None,
        *,
        expected_epoch: int | None = None,
    ) -> tuple[builtins.list[Artifact], builtins.list[Artifact] | None]:
        """Return the aggregate plus ``None`` when note availability is unknown."""

        listing, note_state = await self._list_with_status_and_note_state(
            notebook_id,
            artifact_type,
            expected_epoch=expected_epoch,
        )
        return list(listing.items), None if note_state is None else note_state.artifacts

    async def _list_with_status_and_note_state(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None,
        *,
        expected_epoch: int | None = None,
    ) -> tuple[ArtifactListing, _NoteBackedMindMapState | None]:
        """Build the aggregate result before secondary failure evidence is lost."""

        studio = [
            artifact
            for artifact in await self._list_all_studio(
                notebook_id,
                expected_epoch=expected_epoch,
            )
            if matches_artifact_type(artifact, artifact_type)
        ]
        if artifact_type is not None and artifact_type != ArtifactType.MIND_MAP:
            return ArtifactListing(tuple(studio), is_complete=True), _NoteBackedMindMapState([], [])
        try:
            note_backed, mind_maps = await self._mind_maps.list_mind_map_artifacts_with_content(
                notebook_id
            )
        except DecodingError:
            raise
        except (RPCError, httpx.HTTPError) as error:
            logger.warning(
                "Note-backed mind-map listing is temporarily unavailable (%s).",
                type(error).__name__,
            )
            failure = ArtifactListingFailure(
                component=ArtifactListingComponent.NOTE_BACKED_MIND_MAPS,
                error_type=type(error).__name__[:80],
                message="The note-backed mind-map listing is unavailable.",
            )
            return (
                ArtifactListing(
                    tuple(studio),
                    is_complete=False,
                    failures=(failure,),
                ),
                None,
            )
        filtered = [
            item for item in note_backed if matches_artifact_type(item, ArtifactType.MIND_MAP)
        ]
        return (
            ArtifactListing((*studio, *filtered), is_complete=True),
            _NoteBackedMindMapState(filtered, mind_maps),
        )

    async def list(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None = None,
    ) -> builtins.list[Artifact]:
        """Merge ordered Studio artifacts with the required notes-owned mind maps."""

        listing = await self.list_with_status(notebook_id, artifact_type)
        return list(listing.items)

    async def list_with_status(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None = None,
    ) -> ArtifactListing:
        """Merge artifacts while retaining bounded secondary-read evidence."""
        async with self._transport.operation_scope("artifacts.list") as lease:
            listing, _note_state = await self._list_with_status_and_note_state(
                notebook_id,
                artifact_type,
                expected_epoch=lease.epoch,
            )
            return listing

    async def _list_studio(
        self,
        notebook_id: str,
        task_id: str,
    ) -> builtins.list[Artifact]:
        """Read one exact Studio polling target without querying note-backed rows."""
        async with self._transport.operation_scope("artifacts.poll") as lease:
            try:
                artifact = await self._get_studio_artifact(
                    notebook_id,
                    task_id,
                    expected_epoch=lease.epoch,
                )
            except ArtifactNotFoundError:
                return []
            return [] if artifact is None else [artifact]

    async def _transfer_representation(
        self,
        *,
        url: str,
        output_path: str,
        representation: RepresentationKind,
        artifact_type: str,
        artifact_id: str,
    ) -> str:
        failure: tuple[str | None, int | None] | None = None
        auth_failure: BaseException | None = None
        result: str | None = None
        asset_downloads = cast(AndroidAssetDownloadService, self._asset_downloads)
        try:
            result = await asset_downloads.download_representation(
                url,
                output_path,
                representation=representation,
            )
        except AuthError as error:
            auth_failure = sanitize_escaping_exception(error)
        except ArtifactDownloadError as error:
            failure = (error.details, error.status_code)
        finally:
            del asset_downloads, self, url
        if auth_failure is not None:
            raise auth_failure from None
        if failure is not None:
            details, status_code = failure
            raise ArtifactDownloadError(
                artifact_type,
                artifact_id=artifact_id,
                details=details,
                status_code=status_code,
                cause=None,
            ) from None
        assert result is not None
        return result

    async def get_prompt(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        require_complete: bool = False,
    ) -> str | None:
        """Return the decoded Studio prompt or ``None`` for a note-backed mind map."""

        if require_complete:
            result = await self.lookup(notebook_id, artifact_id)
            if result.status is ArtifactLookupStatus.UNKNOWN:
                raise _incomplete_lookup_error(result.failures)
            if result.status is ArtifactLookupStatus.MISSING:
                raise ArtifactNotFoundError(artifact_id, method_id=LIST_ARTIFACTS_METHOD)
            assert result.artifact is not None
            return result.artifact.generation_prompt

        artifact = await self.get_or_none(notebook_id, artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id, method_id=LIST_ARTIFACTS_METHOD)
        return artifact.generation_prompt

    _creation_policy = ANDROID_CREATION_POLICY

    @property
    def creation_capabilities(self) -> tuple[ArtifactCreationCapability, ...]:
        return super().creation_capabilities + (
            ArtifactCreationCapability("interactive_mind_map", ("language", "instructions")),
        )

    async def _send_create_artifact(
        self,
        creation: NormalizedArtifactCreationRequest,
    ) -> GenerationStatus:
        notebook_id = creation.notebook_id
        source_ids = list(creation.source_ids)
        if isinstance(creation, NormalizedAudio):
            # evidence: docs/android/proto-evidence-ledger.md#artifact-audio-overview-request
            generation_options = _PROTO.AudioOverviewGenerationOptions(
                episode_focus=creation.instructions or "",
                episode_length=creation.length_code,
                source_ids=[_READ_PROTO.SourceId(id=source_id) for source_id in source_ids],
                language_code=creation.language,
            )
            generation_options.MergeFromString(
                _WIRE_PROTO.WireAudioOverviewGenerationOptionsProjection(
                    format=creation.format_code
                ).SerializeToString()
            )
            request = _PROTO.CreateArtifactRequest(
                project_id=notebook_id,
                artifact=_PROTO.Artifact(
                    type=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW,
                    sources=[
                        _PROTO.ArtifactSource(source_id=_READ_PROTO.SourceId(id=source_id))
                        for source_id in source_ids
                    ],
                    audio_overview=_PROTO.AudioOverviewArtifact(
                        generation_options=generation_options
                    ),
                ),
            )
            if creation.later:
                request.MergeFromString(
                    _WIRE_PROTO.WireCreateArtifactDeferredProjection(
                        deferred_generation=_WIRE_PROTO.WireDeferredGenerationOptions(enabled=True)
                    ).SerializeToString()
                )
            expected_type = ArtifactTypeCode.AUDIO.value
            expected_variant = None
            family_label = "audio"
        else:
            plan = build_normalized_create_artifact_plan(creation)
            request = plan.request
            expected_type = plan.expected_type
            expected_variant = plan.expected_variant
            family_label = plan.family_label

        fingerprint = hashlib.sha256(
            b"\0".join(
                (
                    str(id(self._transport)).encode(),
                    request.SerializeToString(),
                )
            )
        ).hexdigest()
        journal_entry = claim_generation_entry(
            method=CREATE_ARTIFACT_METHOD,
            semantic_key=fingerprint,
        )
        with bind_operation_journal_entries(journal_entry):
            response = await create_artifact_once(self._transport, request)
        try:
            artifact = decode_artifact(response.artifact, method_id=CREATE_ARTIFACT_METHOD)
            if artifact._artifact_type != expected_type or (
                expected_variant is not None and artifact._variant not in (None, expected_variant)
            ):
                raise DecodingError(
                    f"Android {family_label} creation returned a different artifact family.",
                    method_id=CREATE_ARTIFACT_METHOD,
                )
            validate_echoed_source_ids(artifact, source_ids, family_label, CREATE_ARTIFACT_METHOD)
        except DecodingError as error:
            attach_journal_entry(error, journal_entry)
            raise error from None
        journal_entry.record(
            CommitState.CONFIRMED,
            "decoded artifact generation",
            known_resource_ids=((artifact.id,) if artifact.id else ()),
        )
        return GenerationStatus(
            task_id=artifact.id,
            status=_status_from_code(artifact.status),
            url=artifact.url,
        )

    async def revise_slide(
        self,
        notebook_id: str,
        artifact_id: str,
        slide_index: int,
        prompt: str,
    ) -> GenerationStatus:
        if slide_index < 0:
            raise ValidationError(f"slide_index must be >= 0, got {slide_index}")

        # The official APK's TailwindRpcService.deriveSlidesArtifact constructs
        # this exact request closure and invokes the generated DeriveArtifact
        # client method. A derivation is a mutation and must never be replayed.
        async with self._transport.operation_scope("artifacts.revise_slide") as lease:
            await self._require_studio_artifact_owned(
                notebook_id,
                artifact_id,
                expected_epoch=lease.epoch,
                method_id=DERIVE_ARTIFACT_METHOD,
            )
            response = await call_unconfirmed_on_transport_loss(
                lambda: self._transport.unary(
                    DERIVE_ARTIFACT_METHOD,
                    _PROTO.DeriveArtifactRequest(
                        request_context=android_request_context(),
                        original_artifact_id=artifact_id,
                        slides_derivation_options=_PROTO.SlidesDerivationOptions(
                            slide_edit_instructions=[
                                _PROTO.SlideEditInstruction(
                                    slide_index=slide_index,
                                    edit_instruction=prompt,
                                )
                            ]
                        ),
                    ),
                    replay_safe=False,
                    response_type=_PROTO.DeriveArtifactResponse,
                    expected_epoch=lease.epoch,
                ),
                method=DERIVE_ARTIFACT_METHOD,
                what="DeriveArtifact",
                chain=None,
            )
        try:
            if not response.HasField("artifact"):
                raise DecodingError(
                    "Android DeriveArtifact response omitted its artifact.",
                    method_id=DERIVE_ARTIFACT_METHOD,
                )
            artifact = decode_artifact(response.artifact, method_id=DERIVE_ARTIFACT_METHOD)
            if artifact._artifact_type != ArtifactTypeCode.SLIDE_DECK.value:
                raise DecodingError(
                    "Android slide revision returned a different artifact family.",
                    method_id=DERIVE_ARTIFACT_METHOD,
                )
            if artifact.id == artifact_id:
                raise DecodingError(
                    "Android slide revision reused the original artifact id.",
                    method_id=DERIVE_ARTIFACT_METHOD,
                )
        except DecodingError as error:
            raise mark_unconfirmed(error) from None
        return GenerationStatus(
            task_id=artifact.id,
            status=_status_from_code(artifact.status),
            url=artifact.url,
        )

    async def retry_failed(self, notebook_id: str, artifact_id: str) -> GenerationStatus:
        return await retry_failed_artifact(
            self._transport,
            self._require_studio_artifact_owned,
            notebook_id,
            artifact_id,
        )

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> MindMapResult:
        if instructions is not None and not isinstance(instructions, str):
            raise ValidationError("instructions must be a string or None")
        language_code = _validate_audio_language(self._resolve_language(language))
        async with self._transport.operation_scope("artifacts.generate_mind_map") as lease:
            selected_sources = await self._resolve_source_ids(notebook_id, source_ids)
            return await generate_note_backed_mind_map(
                self._transport,
                notebook_id,
                selected_sources,
                language=language_code,
                instructions=instructions,
                expected_epoch=lease.epoch,
            )

    async def _generate_interactive_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None,
        *,
        language: str | None,
        instructions: str | None,
    ) -> GenerationStatus:
        async with self._operation_scope("artifacts.generate_interactive_mind_map"):
            selected = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._create_artifact(
                InteractiveMindMapCreationRequest(
                    notebook_id,
                    tuple(selected),
                    self._resolve_language(language),
                    instructions,
                )
            )

    async def _get_interactive_mind_map_tree(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> dict[str, Any] | None:
        if expected_epoch is None:
            async with self._transport.operation_scope(
                "artifacts.get_interactive_mind_map_tree"
            ) as lease:
                return await self._get_interactive_mind_map_tree(
                    notebook_id,
                    artifact_id,
                    expected_epoch=lease.epoch,
                )
        raw = await self._get_raw_studio_artifact(
            notebook_id,
            artifact_id,
            expected_epoch=expected_epoch,
        )
        content = raw.app.mind_map_json if raw.HasField("app") else ""
        del raw
        if not content:
            return None
        return decode_interactive_mind_map_tree(content, artifact_id=artifact_id)

    async def prepare_downloads(self, request: ArtifactDownloadRequest) -> ArtifactDownloadListing:
        """Prepare completed candidates without exposing backend caches.

        Validate the representation before I/O. Every returned selection is
        bound to this backend instance, notebook, and current client generation.
        Partial results retain typed failure evidence; they do not prove absence.
        """
        # Do this before either aggregate read.  The cache also resolves the
        # format, but an empty list must still reject unsupported formats.
        self._prepared_downloads.validate_request(request)
        async with self._operation_scope("artifacts.prepare_downloads") as lease:
            listing, note_state = await self._list_with_status_and_note_state(
                request.notebook_id,
                request.kind,
                expected_epoch=lease.epoch,
            )
            mind_maps_by_id = {
                mind_map.id: mind_map for mind_map in (note_state.mind_maps if note_state else ())
            }
            selections: list[ArtifactDownloadSelection] = []
            for artifact in listing.items:
                if artifact.kind is not request.kind or not artifact.is_completed:
                    continue
                selections.append(
                    self._prepared_downloads.prepare(
                        request,
                        artifact,
                        _PreparedAndroidDownload(
                            artifact=artifact,
                            mind_map=mind_maps_by_id.get(artifact.id),
                        ),
                        epoch=lease.epoch,
                    )
                )
            return ArtifactDownloadListing(
                selections=tuple(selections),
                is_complete=listing.is_complete,
                failures=listing.failures,
            )

    async def download(self, selection: ArtifactDownloadSelection, output_path: str) -> str:
        """Download an owned prepared identity within its admitted generation."""
        snapshot: _PreparedAndroidDownload | None = None
        request: ArtifactDownloadRequest | None = None
        try:
            async with self._operation_scope("artifacts.download") as lease:
                snapshot = self._prepared_downloads.require(selection, epoch=lease.epoch)
                request = ArtifactDownloadRequest(
                    selection.notebook_id,
                    selection.kind,
                    selection.representation,
                )
                if snapshot.mind_map is not None:
                    # The aggregate read already hydrated the note-backed tree;
                    # preserve that exact snapshot rather than issuing another
                    # notes read between selection and publication.
                    return await self._download_with_legacy_prefetch(
                        request,
                        output_path,
                        selection.artifact_id,
                        mind_maps=[snapshot.mind_map],
                    )
                return await self._download_with_legacy_prefetch(
                    request,
                    output_path,
                    selection.artifact_id,
                    artifacts_data=[snapshot.artifact],
                    # A prepared Studio interactive mind map must not retry a
                    # failed notes aggregate merely to prove its already-known id.
                    mind_maps=[] if selection.kind is ArtifactType.MIND_MAP else None,
                    artifacts=[snapshot.artifact],
                )
        finally:
            del self, snapshot, request

    async def _download_with_legacy_prefetch(
        self,
        request: ArtifactDownloadRequest,
        output_path: str,
        artifact_id: str | None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
        mind_maps: builtins.list[Any] | None = None,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str:
        """Use explicit per-kind dispatch for old raw-prefetch callers."""
        try:
            if request.kind is ArtifactType.AUDIO:
                return await self._download_audio_legacy(
                    request.notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
                )
            if request.kind is ArtifactType.VIDEO:
                return await self._download_video_legacy(
                    request.notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
                )
            if request.kind is ArtifactType.INFOGRAPHIC:
                return await self._download_infographic_legacy(
                    request.notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
                )
            if request.kind is ArtifactType.SLIDE_DECK:
                return await self._download_slide_deck_legacy(
                    request.notebook_id,
                    output_path,
                    artifact_id,
                    "pdf" if request.output_format is None else request.output_format,
                    artifacts_data=artifacts_data,
                )
            if request.kind is ArtifactType.REPORT:
                return await self._download_report_legacy(
                    request.notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
                )
            if request.kind is ArtifactType.MIND_MAP:
                return await self._download_mind_map_legacy(
                    request.notebook_id,
                    output_path,
                    artifact_id,
                    mind_maps=mind_maps,
                    artifacts_data=artifacts_data,
                )
            if request.kind is ArtifactType.DATA_TABLE:
                return await self._download_data_table_legacy(
                    request.notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
                )
            if request.kind is ArtifactType.QUIZ:
                return await self._download_quiz_legacy(
                    request.notebook_id,
                    output_path,
                    artifact_id,
                    "json" if request.output_format is None else request.output_format,
                    artifacts=artifacts,
                )
            if request.kind is ArtifactType.FLASHCARDS:
                return await self._download_flashcards_legacy(
                    request.notebook_id,
                    output_path,
                    artifact_id,
                    "json" if request.output_format is None else request.output_format,
                    artifacts=artifacts,
                )
            raise AssertionError(f"unsupported prepared artifact kind: {request.kind!r}")
        finally:
            del self, request, artifacts_data, mind_maps, artifacts

    async def _download_audio_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        async with self._transport.operation_scope("artifacts.download_audio") as lease:
            selected = await self._select_completed_studio_at_epoch(
                notebook_id,
                artifact_id,
                type_code=ArtifactTypeCode.AUDIO,
                artifact_type="audio",
                expected_epoch=lease.epoch,
                prefetched=artifacts_data,
            )
            media_url = select_single_file_media_url(selected)
            if media_url is None:
                raise ArtifactParseError(
                    "audio",
                    artifact_id=selected.id,
                    details="Could not extract a downloadable media URL from artifact metadata",
                )
            return await self._transfer_representation(
                url=media_url,
                output_path=output_path,
                representation="audio",
                artifact_type="audio",
                artifact_id=selected.id,
            )

    async def _download_video_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        async with self._transport.operation_scope("artifacts.download_video") as lease:
            selected = await self._select_completed_studio_at_epoch(
                notebook_id,
                artifact_id,
                type_code=ArtifactTypeCode.VIDEO,
                artifact_type="video",
                expected_epoch=lease.epoch,
                prefetched=artifacts_data,
            )
            media_url = select_single_file_media_url(selected)
            if media_url is None:
                raise ArtifactParseError(
                    "video",
                    artifact_id=selected.id,
                    details="Could not extract a downloadable media URL from artifact metadata",
                )
            return await self._transfer_representation(
                url=media_url,
                output_path=output_path,
                representation="video",
                artifact_type="video",
                artifact_id=selected.id,
            )

    async def _download_infographic_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        adapter = self
        result: str | None = None
        failure: BaseException | None = None
        try:
            async with adapter._transport.operation_scope(
                "artifacts.download_infographic"
            ) as lease:
                result = await adapter._download_infographic_at_epoch(
                    notebook_id,
                    output_path,
                    artifact_id,
                    expected_epoch=lease.epoch,
                    prefetched=artifacts_data,
                )
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, adapter
        if failure is not None:
            failure.__cause__ = None
            failure.__context__ = None
            raise failure from None
        return cast(str, result)

    async def _download_infographic_at_epoch(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        *,
        expected_epoch: int,
        prefetched: builtins.list[Any] | None,
    ) -> str:
        selected = await self._select_completed_studio_at_epoch(
            notebook_id,
            artifact_id,
            type_code=ArtifactTypeCode.INFOGRAPHIC,
            artifact_type="infographic",
            expected_epoch=expected_epoch,
            prefetched=prefetched,
        )
        if not selected.url:
            raise ArtifactParseError(
                "infographic",
                artifact_id=artifact_id,
                details="Could not find metadata",
            )

        transfer_failure: tuple[str | None, int | None] | None = None
        auth_failure: BaseException | None = None
        result: str | None = None
        try:
            result = await self._asset_downloads.download_url(selected.url, output_path)
        except AuthError as error:
            auth_failure = sanitize_escaping_exception(error)
        except ArtifactDownloadError as error:
            transfer_failure = (error.details, error.status_code)
        if auth_failure is not None:
            del selected, self
            raise auth_failure from None
        if transfer_failure is not None:
            details, status_code = transfer_failure
            selected_id = selected.id
            del selected, self
            public_error = ArtifactDownloadError(
                "infographic",
                details=details,
                artifact_id=selected_id,
                cause=None,
                status_code=status_code,
            )
            public_error.__cause__ = None
            public_error.__context__ = None
            raise public_error from None
        assert result is not None
        return result

    async def _download_slide_deck_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "pdf",
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        if output_format not in ("pdf", "pptx"):
            raise ValidationError(f"Invalid format '{output_format}'. Must be 'pdf' or 'pptx'.")
        async with self._transport.operation_scope("artifacts.download_slide_deck") as lease:
            selected = await self._select_completed_studio_at_epoch(
                notebook_id,
                artifact_id,
                type_code=ArtifactTypeCode.SLIDE_DECK,
                artifact_type="slide_deck",
                expected_epoch=lease.epoch,
                prefetched=artifacts_data,
            )
            raw = await self._get_raw_studio_artifact(
                notebook_id,
                selected.id,
                expected_epoch=lease.epoch,
            )
            url = ""
            if raw.HasField("slides"):
                url = (
                    raw.slides.pptx_download_url
                    if output_format == "pptx"
                    else raw.slides.pdf_download_url
                )
            del raw
            if not url:
                raise ArtifactDownloadError(
                    "slide_deck",
                    artifact_id=selected.id,
                    details=f"{output_format.upper()} URL not available in artifact data",
                    cause=None,
                )
            return await self._transfer_representation(
                url=url,
                output_path=output_path,
                representation="slide_pptx" if output_format == "pptx" else "slide_pdf",
                artifact_type="slide_deck",
                artifact_id=selected.id,
            )

    async def _download_report_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        async with self._transport.operation_scope("artifacts.download_report") as lease:
            selected = await self._select_completed_studio_at_epoch(
                notebook_id,
                artifact_id,
                type_code=ArtifactTypeCode.REPORT,
                artifact_type="report",
                expected_epoch=lease.epoch,
                prefetched=artifacts_data,
            )
            raw = await self._get_raw_studio_artifact(
                notebook_id,
                selected.id,
                expected_epoch=lease.epoch,
            )
            content = ""
            if raw.HasField("tailored_report") and raw.tailored_report.HasField("report_doc"):
                content = report_doc_markdown(raw.tailored_report.report_doc)
            del raw
            if not content:
                raise ArtifactParseError(
                    "report",
                    artifact_id=selected.id,
                    details="Could not decode report document content",
                )
            return await write_text_atomic(
                output_path,
                content,
                artifact_type="report",
                artifact_id=selected.id,
            )

    async def _download_mind_map_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        mind_maps: builtins.list[Any] | None = None,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        async with self._transport.operation_scope("artifacts.download_mind_map") as lease:
            if mind_maps is None:
                mind_maps = await self._mind_maps.list_note_backed_mind_maps(notebook_id)
            note_backed = select_note_backed_mind_map(mind_maps, mind_map_id=artifact_id)
            if note_backed is not None:
                if note_backed.tree is None:
                    raise ArtifactNotReadyError("mind_map", artifact_id=note_backed.id)
                return await write_text_atomic(
                    output_path,
                    json.dumps(note_backed.tree, indent=2, ensure_ascii=False),
                    artifact_type="mind_map",
                    artifact_id=note_backed.id,
                )
            selected = await self._select_completed_studio_at_epoch(
                notebook_id,
                artifact_id,
                type_code=ArtifactTypeCode.QUIZ,
                artifact_type="mind_map",
                kind=ArtifactType.MIND_MAP,
                expected_epoch=lease.epoch,
                prefetched=artifacts_data,
            )
            tree = await self._get_interactive_mind_map_tree(
                notebook_id,
                selected.id,
                expected_epoch=lease.epoch,
            )
            if tree is None:
                raise ArtifactNotReadyError("mind_map", artifact_id=selected.id)
            content = json.dumps(tree, indent=2, ensure_ascii=False)
            del tree
            return await write_text_atomic(
                output_path,
                content,
                artifact_type="mind_map",
                artifact_id=selected.id,
            )

    async def _download_data_table_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        async with self._transport.operation_scope("artifacts.download_data_table") as lease:
            selected = await self._select_completed_studio_at_epoch(
                notebook_id,
                artifact_id,
                type_code=ArtifactTypeCode.DATA_TABLE,
                artifact_type="data_table",
                expected_epoch=lease.epoch,
                prefetched=artifacts_data,
            )
            raw = await self._get_raw_studio_artifact(
                notebook_id,
                selected.id,
                expected_epoch=lease.epoch,
            )
            content = data_table_csv(raw, artifact_id=selected.id)
            del raw
            return await write_text_atomic(
                output_path,
                content,
                artifact_type="data_table",
                artifact_id=selected.id,
            )

    async def _download_interactive_app(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        *,
        output_format: str,
        artifact_type: str,
        kind: ArtifactType,
        prefetched: builtins.list[Artifact] | None,
    ) -> str:
        valid_formats = ("json", "markdown", "html")
        if output_format not in valid_formats:
            raise ValidationError(
                f"Invalid output_format: {output_format!r}. Use one of: {', '.join(valid_formats)}"
            )

        async with self._transport.operation_scope(f"artifacts.download_{artifact_type}") as lease:
            selected = await self._select_completed_studio_at_epoch(
                notebook_id,
                artifact_id,
                type_code=ArtifactTypeCode.QUIZ,
                artifact_type=artifact_type,
                kind=kind,
                expected_epoch=lease.epoch,
                prefetched=cast(builtins.list[Any] | None, prefetched),
            )
            raw = await self._get_raw_studio_artifact(
                notebook_id,
                selected.id,
                expected_epoch=lease.epoch,
            )
            html_content = ""
            app_data_json = ""
            if raw.HasField("app"):
                html_content = raw.app.app_html
                if raw.app.HasField("templatized_app"):
                    app_data_json = raw.app.templatized_app.app_data
            del raw

            if output_format == "html" and not html_content:
                raise ArtifactDownloadError(
                    artifact_type,
                    artifact_id=selected.id,
                    details="HTML content is not available in artifact data",
                    cause=None,
                )
            if output_format == "html":
                del app_data_json
                return await write_text_atomic(
                    output_path,
                    html_content,
                    artifact_type=artifact_type,
                    artifact_id=selected.id,
                )
            if not html_content and not app_data_json:
                raise ArtifactDownloadError(
                    artifact_type,
                    artifact_id=selected.id,
                    details="Interactive content is not available in artifact data",
                    cause=None,
                )

            app_data = decode_interactive_app_data(
                html_content,
                app_data_json,
                artifact_type=artifact_type,
                artifact_id=selected.id,
            )

            title = selected.title or (
                "Untitled Quiz" if artifact_type == "quiz" else "Untitled Flashcards"
            )
            content = self._format_interactive_content(
                app_data,
                title,
                output_format,
                html_content,
                artifact_type == "quiz",
            )
            del app_data, html_content, app_data_json
            return await write_text_atomic(
                output_path,
                content,
                artifact_type=artifact_type,
                artifact_id=selected.id,
            )

    async def _download_quiz_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
        *,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str:
        return await self._download_interactive_app(
            notebook_id,
            output_path,
            artifact_id,
            output_format=output_format,
            artifact_type="quiz",
            kind=ArtifactType.QUIZ,
            prefetched=artifacts,
        )

    async def _download_flashcards_legacy(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
        *,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str:
        return await self._download_interactive_app(
            notebook_id,
            output_path,
            artifact_id,
            output_format=output_format,
            artifact_type="flashcards",
            kind=ArtifactType.FLASHCARDS,
            prefetched=artifacts,
        )

    async def delete(self, notebook_id: str, artifact_id: str) -> None:
        await delete_artifact(
            self._transport,
            self._list_all_studio,
            notebook_id,
            artifact_id,
        )

    async def rename(
        self,
        notebook_id: str,
        artifact_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Artifact | None:
        async with self._transport.operation_scope("artifacts.rename") as lease:
            return await self._rename_at_epoch(
                notebook_id,
                artifact_id,
                new_title,
                return_object=return_object,
                expected_epoch=lease.epoch,
            )

    async def _rename_at_epoch(
        self,
        notebook_id: str,
        artifact_id: str,
        new_title: str,
        *,
        return_object: bool,
        expected_epoch: int,
    ) -> Artifact | None:
        before = next(
            (
                artifact
                for artifact in await self._list_all_studio(
                    notebook_id,
                    expected_epoch=expected_epoch,
                )
                if artifact.id == artifact_id
            ),
            None,
        )
        if before is None:
            raise ArtifactNotFoundError(artifact_id, method_id=UPDATE_ARTIFACT_METHOD)
        if before.etag is None:
            raise DecodingError(
                "Android artifact rename requires the listed artifact etag.",
                method_id=UPDATE_ARTIFACT_METHOD,
            )
        response = await self._transport.unary(
            UPDATE_ARTIFACT_METHOD,
            _PROTO.UpdateArtifactRequest(
                artifact=_PROTO.Artifact(artifact_id=artifact_id, title=new_title),
                update_mask={"paths": ["title"]},
                etag=before.etag,
            ),
            replay_safe=False,
            response_type=_PROTO.Artifact,
            expected_epoch=expected_epoch,
        )
        updated = decode_artifact(response, method_id=UPDATE_ARTIFACT_METHOD)
        if updated.id != artifact_id:
            raise DecodingError(
                "Android artifact rename returned a different artifact id.",
                method_id=UPDATE_ARTIFACT_METHOD,
            )
        read_back = next(
            (
                artifact
                for artifact in await self._list_all_studio(
                    notebook_id,
                    expected_epoch=expected_epoch,
                )
                if artifact.id == artifact_id
            ),
            None,
        )
        if read_back is None:
            raise ArtifactNotFoundError(artifact_id, method_id=UPDATE_ARTIFACT_METHOD)
        return read_back if return_object else None

    async def _send_export(
        self,
        notebook_id: str,
        artifact_id: str | None,
        title: str,
        export_type: ExportType,
        *,
        content: str | None,
    ) -> Any:
        return await export_to_drive(
            self._transport,
            self._require_studio_artifact_owned,
            notebook_id,
            artifact_id=artifact_id,
            content=content,
            title=title,
            export_type=export_type,
        )

    async def suggest_reports(self, notebook_id: str) -> builtins.list[ReportSuggestion]:
        response = await self._transport.unary(
            GENERATE_REPORT_SUGGESTIONS_METHOD,
            _PROTO.GenerateReportSuggestionsRequest(
                request_context=android_request_context(),
                project_id=notebook_id,
            ),
            replay_safe=True,
            response_type=_PROTO.GenerateReportSuggestionsResponse,
        )
        return decode_report_suggestions(response.suggestions)


__all__ = [
    "AndroidArtifactsAPI",
    "COPY_ARTIFACTS_ASYNC_METHOD",
    "CREATE_ARTIFACT_METHOD",
    "DERIVE_ARTIFACT_METHOD",
    "DELETE_ARTIFACT_METHOD",
    "EXPORT_TO_DRIVE_METHOD",
    "GENERATE_ARTIFACT_METHOD",
    "GENERATE_REPORT_SUGGESTIONS_METHOD",
    "GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD",
    "GET_ARTIFACT_METHOD",
    "LIST_ARTIFACTS_METHOD",
    "UPDATE_ARTIFACT_METHOD",
]
