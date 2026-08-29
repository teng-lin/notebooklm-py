"""Direct-test Android implementation of the evidence-qualified artifact slice."""

from __future__ import annotations

import builtins
import logging
from typing import Any, NoReturn, Protocol, cast

import httpx
from google.protobuf import empty_pb2

from .._artifacts import ArtifactsAPI
from .._notebook_metadata import NotebookSourceIdProvider
from .._runtime.call_supervisor import CallSupervisor
from .._types.artifacts import _status_from_code
from .._types.enums import (
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    ArtifactStatus,
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    ExportType,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)
from .._types.research import MindMapResult
from ..exceptions import (
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    DecodingError,
    RPCError,
    ValidationError,
)
from ..types import Artifact, ArtifactType, GenerationStatus, ReportSuggestion
from .assets import AndroidAssetDownloadService
from .codecs.artifacts import decode_artifact, decode_artifacts, decode_report_suggestions
from .errors import sanitize_escaping_exception, unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import b1_read_pb2, b4_artifacts_pb2
from .proto.notebooklm.android.internal.v1 import b4_report_suggestions_pb2
from .session import AndroidSession

logger = logging.getLogger(__name__)
_PROTO = cast(Any, b4_artifacts_pb2)
_READ_PROTO = cast(Any, b1_read_pb2)
_REPORT_PROTO = cast(Any, b4_report_suggestions_pb2)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
LIST_ARTIFACTS_METHOD = f"/{_SERVICE}/ListArtifacts"
CREATE_ARTIFACT_METHOD = f"/{_SERVICE}/CreateArtifact"
DELETE_ARTIFACT_METHOD = f"/{_SERVICE}/DeleteArtifact"
UPDATE_ARTIFACT_METHOD = f"/{_SERVICE}/UpdateArtifact"
GENERATE_REPORT_SUGGESTIONS_METHOD = f"/{_SERVICE}/GenerateReportSuggestions"


class NoteBackedMindMapLister(Protocol):
    """The one notes-owned projection required by the aggregate catalog."""

    async def list_mind_map_artifacts(self, notebook_id: str) -> builtins.list[Artifact]: ...


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


def _matches_type(artifact: Artifact, requested: ArtifactType | None) -> bool:
    if requested is None:
        return True
    if requested == ArtifactType.MIND_MAP:
        return artifact._artifact_type == ArtifactTypeCode.MIND_MAP.value or (
            artifact._artifact_type == ArtifactTypeCode.QUIZ.value
            and artifact._variant == INTERACTIVE_MIND_MAP_VARIANT
        )
    return artifact.kind == requested


def _validate_quiz_option(value: Any, enum_type: type[Any], parameter: str) -> int:
    if value is None:
        return 0
    if not isinstance(value, enum_type):
        raise ValidationError(f"{parameter} must be a {enum_type.__name__} value")
    return int(value.value)


class AndroidArtifactsAPI(ArtifactsAPI):
    """Partial Android artifact adapter, intentionally absent from normal assembly."""

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

    async def _list_all_studio(self, notebook_id: str) -> builtins.list[Artifact]:
        # evidence: docs/android/proto-evidence-ledger.md#b4-service-ledger
        response = await self._transport.unary(
            LIST_ARTIFACTS_METHOD,
            _PROTO.ListArtifactsRequest(project_id=notebook_id),
            replay_safe=True,
            response_type=_PROTO.ListArtifactsResponse,
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
    ) -> tuple[builtins.list[Artifact], builtins.list[Artifact] | None]:
        """Return the aggregate plus ``None`` when note availability is unknown."""

        studio = [
            artifact
            for artifact in await self._list_all_studio(notebook_id)
            if _matches_type(artifact, artifact_type)
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
        filtered = [item for item in note_backed if _matches_type(item, ArtifactType.MIND_MAP)]
        return [*studio, *filtered], filtered

    async def list(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None = None,
    ) -> builtins.list[Artifact]:
        """Merge ordered Studio artifacts with the required notes-owned mind maps."""

        artifacts, _note_state = await self._list_with_note_state(notebook_id, artifact_type)
        return artifacts

    async def _list_studio(
        self,
        notebook_id: str,
        task_id: str,
    ) -> builtins.list[Artifact]:
        """Select the target raw proto before decoding one Studio poll row."""

        result: builtins.list[Artifact] = []
        failure: BaseException | None = None
        response: Any | None = None
        matches: builtins.list[Any] = []
        raw_target: Any | None = None
        try:
            response = await self._transport.unary(
                LIST_ARTIFACTS_METHOD,
                _PROTO.ListArtifactsRequest(project_id=notebook_id),
                replay_safe=True,
                response_type=_PROTO.ListArtifactsResponse,
            )
            assert response is not None
            matches = [
                raw_artifact
                for raw_artifact in response.artifacts
                if raw_artifact.artifact_id == task_id
            ]
            if len(matches) > 1:
                raise DecodingError(
                    "Android artifact polling returned a duplicate target id.",
                    method_id=LIST_ARTIFACTS_METHOD,
                )
            if matches:
                raw_target, *_unexpected = matches
                result = [decode_artifact(raw_target, method_id=LIST_ARTIFACTS_METHOD)]
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            matches.clear()
            del matches, raw_target, response, self
        if failure is not None:
            failure.__cause__ = None
            failure.__context__ = None
            raise failure from None
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
        if family != "quiz":
            _reject(f"artifacts.generate_{family}")
        if not source_ids:
            raise ValidationError("Quiz generation requires at least one source id")
        quantity = _validate_quiz_option(options.get("quantity"), QuizQuantity, "quantity")
        difficulty = _validate_quiz_option(
            options.get("difficulty"), QuizDifficulty, "difficulty"
        )
        instructions = options.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ValidationError("instructions must be a string or None")

        # evidence: docs/android/proto-evidence-ledger.md#b4-quiz-request
        request = _PROTO.CreateArtifactRequest(
            project_id=notebook_id,
            artifact=_PROTO.Artifact(
                type=_PROTO.ARTIFACT_TYPE_APP,
                sources=[
                    _PROTO.ArtifactSource(source_id=_READ_PROTO.SourceId(id=source_id))
                    for source_id in source_ids
                ],
                app=_PROTO.AppArtifact(
                    generation_options=_PROTO.AppArtifactGenerationOptions(
                        app_type=_PROTO.APP_TYPE_QUIZ,
                        free_text_steering_prompt=instructions or "",
                        quiz_generation_options=_PROTO.QuizGenerationOptions(
                            question_quantity=quantity,
                            quiz_difficulty=difficulty,
                        ),
                    )
                ),
            ),
        )
        response = await self._transport.unary(
            CREATE_ARTIFACT_METHOD,
            request,
            replay_safe=False,
            response_type=_PROTO.CreateArtifactResponse,
        )
        artifact = decode_artifact(response.artifact, method_id=CREATE_ARTIFACT_METHOD)
        if artifact._artifact_type != ArtifactTypeCode.QUIZ.value or artifact._variant not in (
            None,
            QUIZ_VARIANT,
        ):
            raise DecodingError(
                "Android quiz creation returned a different artifact family.",
                method_id=CREATE_ARTIFACT_METHOD,
            )
        return GenerationStatus(
            task_id=artifact.id,
            status=_status_from_code(artifact.status),
            url=artifact.url,
        )

    async def generate_audio(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        audio_format: AudioFormat | None = None,
        audio_length: AudioLength | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_audio")

    async def generate_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        video_format: VideoFormat | None = None,
        video_style: VideoStyle | None = None,
        style_prompt: str | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_video")

    async def generate_cinematic_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_cinematic_video")

    async def generate_report(
        self,
        notebook_id: str,
        report_format: ReportFormat = ReportFormat.BRIEFING_DOC,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        custom_prompt: str | None = None,
        extra_instructions: str | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_report")

    async def generate_study_guide(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        extra_instructions: str | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_study_guide")

    async def generate_flashcards(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_flashcards")

    async def generate_infographic(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        orientation: InfographicOrientation | None = None,
        detail_level: InfographicDetail | None = None,
        style: InfographicStyle | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_infographic")

    async def generate_slide_deck(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        slide_format: SlideDeckFormat | None = None,
        slide_length: SlideDeckLength | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_slide_deck")

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        _reject("artifacts.generate_data_table")

    async def revise_slide(
        self,
        notebook_id: str,
        artifact_id: str,
        slide_index: int,
        prompt: str,
    ) -> GenerationStatus:
        _reject("artifacts.revise_slide")

    async def retry_failed(self, notebook_id: str, artifact_id: str) -> GenerationStatus:
        _reject("artifacts.retry_failed")

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> MindMapResult:
        _reject("artifacts.generate_mind_map")

    async def download_audio(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        _reject("artifacts.download_audio")

    async def download_video(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        _reject("artifacts.download_video")

    async def download_infographic(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        if artifacts_data is not None:
            _reject("artifacts.download_infographic(artifacts_data=...)")
        candidates = [
            item
            for item in await self._list_all_studio(notebook_id)
            if item._artifact_type == ArtifactTypeCode.INFOGRAPHIC.value
            and item.status == ArtifactStatus.COMPLETED.value
        ]
        if artifact_id is not None:
            selected = next((item for item in candidates if item.id == artifact_id), None)
        else:
            selected = max(
                candidates,
                key=lambda item: (
                    item.last_modified_at.timestamp() if item.last_modified_at is not None else 0
                ),
                default=None,
            )
        if selected is None:
            raise ArtifactNotReadyError("infographic", artifact_id=artifact_id)
        if not selected.url:
            raise ArtifactParseError(
                "infographic",
                artifact_id=artifact_id,
                details="Could not find metadata",
            )

        transfer_failure: tuple[str | None, int | None] | None = None
        result: str | None = None
        try:
            result = await self._asset_downloads.download_url(selected.url, output_path)
        except ArtifactDownloadError as error:
            transfer_failure = (error.details, error.status_code)
        if transfer_failure is not None:
            details, status_code = transfer_failure
            selected_id = selected.id
            del candidates, selected, self
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
        _reject("artifacts.download_slide_deck")

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        _reject("artifacts.download_report")

    async def download_mind_map(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        mind_maps: builtins.list[Any] | None = None,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        _reject("artifacts.download_mind_map")

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        _reject("artifacts.download_data_table")

    async def download_quiz(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
        *,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str:
        _reject("artifacts.download_quiz")

    async def download_flashcards(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
        *,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str:
        _reject("artifacts.download_flashcards")

    async def delete(self, notebook_id: str, artifact_id: str) -> None:
        del notebook_id
        try:
            await self._transport.unary(
                DELETE_ARTIFACT_METHOD,
                _PROTO.DeleteArtifactRequest(artifact_id=artifact_id),
                replay_safe=False,
                response_type=empty_pb2.Empty,
            )
        except RPCError as error:
            if error.rpc_code != 5:
                raise

    async def rename(
        self,
        notebook_id: str,
        artifact_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Artifact | None:
        before = next(
            (
                artifact
                for artifact in await self._list_all_studio(notebook_id)
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
                for artifact in await self._list_all_studio(notebook_id)
                if artifact.id == artifact_id
            ),
            None,
        )
        if read_back is None:
            raise ArtifactNotFoundError(artifact_id, method_id=UPDATE_ARTIFACT_METHOD)
        return read_back if return_object else None

    async def export_report(
        self,
        notebook_id: str,
        artifact_id: str,
        title: str = "Export",
        export_type: ExportType = ExportType.DOCS,
    ) -> Any:
        _reject("artifacts.export_report")

    async def export_data_table(
        self,
        notebook_id: str,
        artifact_id: str,
        title: str = "Export",
    ) -> Any:
        _reject("artifacts.export_data_table")

    async def export(
        self,
        notebook_id: str,
        artifact_id: str | None = None,
        title: str = "Export",
        export_type: ExportType = ExportType.DOCS,
        *,
        content: str | None = None,
    ) -> Any:
        _reject("artifacts.export")

    async def suggest_reports(self, notebook_id: str) -> builtins.list[ReportSuggestion]:
        # APK-absent exact method path with repository-local wire-equivalent messages.
        response = await self._transport.unary(
            GENERATE_REPORT_SUGGESTIONS_METHOD,
            _REPORT_PROTO.GenerateReportSuggestionsRequestWire(project_id=notebook_id),
            replay_safe=True,
            response_type=_REPORT_PROTO.GenerateReportSuggestionsResponseWire,
        )
        return decode_report_suggestions(response.suggestions)


__all__ = [
    "AndroidArtifactsAPI",
    "CREATE_ARTIFACT_METHOD",
    "DELETE_ARTIFACT_METHOD",
    "GENERATE_REPORT_SUGGESTIONS_METHOD",
    "LIST_ARTIFACTS_METHOD",
    "NoteBackedMindMapLister",
    "UPDATE_ARTIFACT_METHOD",
]
