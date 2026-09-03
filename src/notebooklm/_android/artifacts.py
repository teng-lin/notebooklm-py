"""Android implementation of the evidence-qualified public artifact API."""

from __future__ import annotations

import builtins
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx

from .._artifacts import ArtifactsAPI
from .._idempotency import call_unconfirmed_on_transport_loss, mark_unconfirmed
from .._notebook_metadata import NotebookSourceIdProvider
from .._runtime.call_supervisor import CallSupervisor, OperationLease
from .._types.artifacts import _status_from_code
from .._types.enums import (
    ArtifactStatus,
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
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
from ..types import Artifact, ArtifactType, GenerationStatus, ReportSuggestion
from .artifact_collaborators import NoteBackedMindMapLister
from .artifact_creation import (
    CREATE_ARTIFACT_METHOD,
    build_create_artifact_plan,
    create_artifact_once,
    normalize_creation_options,
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


def android_request_context() -> Any:
    from .upload import android_request_context as build_request_context

    return build_request_context()


_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
DERIVE_ARTIFACT_METHOD = f"/{_SERVICE}/DeriveArtifact"
UPDATE_ARTIFACT_METHOD = f"/{_SERVICE}/UpdateArtifact"
GENERATE_REPORT_SUGGESTIONS_METHOD = f"/{_SERVICE}/GenerateReportSuggestions"


def _audio_format_code(value: Any) -> int:
    if value is None:
        return AudioFormat.DEEP_DIVE.value
    if not isinstance(value, AudioFormat):
        raise ValidationError("audio_format must be an AudioFormat value")
    return int(value.value)


def _audio_length_code(value: Any) -> int:
    if value is None:
        return AudioLength.DEFAULT.value
    if not isinstance(value, AudioLength):
        raise ValidationError("audio_length must be an AudioLength value")
    return int(value.value)


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

        studio = [
            artifact
            for artifact in await self._list_all_studio(
                notebook_id,
                expected_epoch=expected_epoch,
            )
            if matches_artifact_type(artifact, artifact_type)
        ]
        if artifact_type is not None and artifact_type != ArtifactType.MIND_MAP:
            return studio, []
        try:
            note_backed = await self._mind_maps.list_mind_map_artifacts(notebook_id)
        except DecodingError:
            raise
        except (RPCError, httpx.HTTPError) as error:
            logger.warning(
                "Note-backed mind-map listing is temporarily unavailable (%s).",
                type(error).__name__,
            )
            return studio, None
        filtered = [
            item for item in note_backed if matches_artifact_type(item, ArtifactType.MIND_MAP)
        ]
        return [*studio, *filtered], filtered

    async def list(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None = None,
    ) -> builtins.list[Artifact]:
        """Merge ordered Studio artifacts with the required notes-owned mind maps."""

        async with self._transport.operation_scope("artifacts.list") as lease:
            artifacts, _note_state = await self._list_with_note_state(
                notebook_id,
                artifact_type,
                expected_epoch=lease.epoch,
            )
            return artifacts

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

    async def get_prompt(self, notebook_id: str, artifact_id: str) -> str | None:
        """Return the decoded Studio prompt or ``None`` for a note-backed mind map."""

        artifact = await self.get_or_none(notebook_id, artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id, method_id=LIST_ARTIFACTS_METHOD)
        return artifact.generation_prompt

    async def _send_create_artifact(
        self,
        notebook_id: str,
        family: str,
        source_ids: builtins.list[str],
        **options: Any,
    ) -> GenerationStatus:
        if family == "audio" and not source_ids:
            label = family.replace("_", " ").title()
            raise ValidationError(f"{label} generation requires at least one source id")
        if family == "audio":
            language_code = _validate_audio_language(options.get("language"))
            instructions = options.get("instructions")
            if instructions is not None and not isinstance(instructions, str):
                raise ValidationError("instructions must be a string or None")
            format_code = _audio_format_code(options.get("audio_format"))
            episode_length = _audio_length_code(options.get("audio_length"))

            # evidence: docs/android/proto-evidence-ledger.md#artifact-audio-overview-request
            generation_options = _PROTO.AudioOverviewGenerationOptions(
                episode_focus=instructions or "",
                episode_length=episode_length,
                source_ids=[_READ_PROTO.SourceId(id=source_id) for source_id in source_ids],
                language_code=language_code,
            )
            generation_options.MergeFromString(
                _WIRE_PROTO.WireAudioOverviewGenerationOptionsProjection(
                    format=format_code
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
            expected_type = ArtifactTypeCode.AUDIO.value
            expected_variant = None
            family_label = "audio"
        elif family in {
            "video",
            "cinematic_video",
            "report",
            "quiz",
            "flashcards",
            "interactive_mind_map",
            "infographic",
            "slide_deck",
            "data_table",
        }:
            plan = build_create_artifact_plan(
                notebook_id,
                family,
                source_ids,
                **options,
            )
            request = plan.request
            expected_type = plan.expected_type
            expected_variant = plan.expected_variant
            family_label = plan.family_label
        else:
            raise AssertionError(f"unreachable artifact family: {family}")

        response = await create_artifact_once(
            self._transport,
            request,
        )
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
            raise mark_unconfirmed(error) from None
        return GenerationStatus(
            task_id=artifact.id,
            status=_status_from_code(artifact.status),
            url=artifact.url,
        )

    async def _generate_supported_family(
        self,
        notebook_id: str,
        family: str,
        source_ids: builtins.list[str] | None,
        **options: Any,
    ) -> GenerationStatus:
        if source_ids == []:
            label = family.replace("_", " ").title()
            raise ValidationError(f"{label} generation requires at least one source id")
        async with self._operation_scope(f"artifacts.generate_{family}"):
            resolved_source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                family,
                resolved_source_ids,
                **options,
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
        language_code = _validate_audio_language(self._resolve_language(language))
        normalize_creation_options(
            "interactive_mind_map",
            language=language_code,
            instructions=instructions,
        )
        return await self._generate_supported_family(
            notebook_id,
            "interactive_mind_map",
            source_ids,
            language=language_code,
            instructions=instructions,
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

    async def download_audio(
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

    async def download_video(
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

    async def download_infographic(
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

    async def download_slide_deck(
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

    async def download_report(
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

    async def download_mind_map(
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

    async def download_data_table(
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

    async def download_quiz(
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

    async def download_flashcards(
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
