"""Artifacts API for NotebookLM studio content.

Provides operations for generating, listing, downloading, and managing
AI-generated artifacts including Audio Overviews, Video Overviews, Reports,
Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import builtins
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ._artifact import downloads as _artifact_downloads
from ._artifact import formatters as _artifact_formatters
from ._artifact import polling as _artifact_polling
from ._artifact import validation as _artifact_validation
from ._artifact.downloads import DownloadResult
from ._artifact.generation_workflow import ArtifactGenerationWorkflow
from ._artifact.listing import ArtifactListingService
from ._backend import BackendAdapter, BackendContractError, BackendError, BackendErrorReason
from ._deadline import RuntimeDeadlineFactory
from ._lookup import unwrap_or_raise
from ._notebook_metadata import NotebookSourceIdProvider
from ._polling_registry import PollRegistry
from ._semantic.compat import project_backend_call, project_backend_error
from ._semantic.projectors import (
    project_artifact,
    project_generation_status,
    project_report_suggestion,
)
from ._semantic.records import (
    ArtifactDeleteInput,
    ArtifactRecord,
    ArtifactRenameInput,
    ArtifactRepresentationRecord,
    ArtifactRetryInput,
    ArtifactSuggestReportsInput,
    DriveExportInput,
    MindMapGenerateInput,
    MindMapRepresentationRecord,
)
from ._semantic.services.read import NotebookReadService
from ._studio import (
    ArtifactLifecycleService,
    ArtifactRepresentationService,
    AudioFamilyService,
    DataTableFamilyService,
    DriveExportService,
    InteractiveFamilyService,
    NoteBackedMindMapFamilyService,
    ReportFamilyService,
    ReportSuggestionService,
    StudioCatalog,
    StudioGenerationInputs,
    StudioManagementService,
    VideoFamilyService,
    VisualFamilyService,
)
from ._studio.downloads import StudioDownloadClient
from ._types.research import MindMapResult
from .artifacts import RateLimitRetryEvent
from .exceptions import (
    ArtifactFeatureUnavailableError,
    ArtifactNotFoundError,
)

if TYPE_CHECKING:
    from ._runtime.lifecycle import ClientLifecycle
    from ._transport_drain import TransportDrainTracker
from .rpc import (
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
from .types import (
    Artifact,
    ArtifactType,
    GenerationStatus,
    ReportSuggestion,
)

logger = logging.getLogger(__name__)

_PARTIAL_MIND_MAP_FAILURE_REASONS = frozenset(
    {
        BackendErrorReason.AUTH,
        BackendErrorReason.CLIENT,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.RESPONSE_TOO_LARGE,
        BackendErrorReason.RPC,
        BackendErrorReason.SERVER,
        BackendErrorReason.TIMEOUT,
    }
)


class ArtifactsAPI:
    """Operations on NotebookLM artifacts (studio content).

    Artifacts are AI-generated content: Audio/Video Overviews, Reports,
    Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.

    Usage::

        async with NotebookLMClient.from_storage() as client:
            status = await client.artifacts.generate_audio(notebook_id)
            await client.artifacts.wait_for_completion(notebook_id, status.task_id)
            await client.artifacts.download_audio(notebook_id, "output.mp4")
            artifacts = await client.artifacts.list(notebook_id)
            await client.artifacts.rename(notebook_id, artifact_id, "New Title")
    """

    def __init__(
        self,
        *,
        drain: "TransportDrainTracker",
        lifecycle: "ClientLifecycle",
        notebooks: NotebookSourceIdProvider,
        storage_path: Path | None = None,
        _backend: BackendAdapter | None = None,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        """Initialize the artifacts API.

        Args:
            drain: Transport drain coordinator — owns ``operation_scope`` (used
                by the polling service) and ``register_drain_hook`` (used here
                to register the polling-service close-time cleanup hook).
            lifecycle: Client lifecycle seam — owns ``assert_bound_loop`` used
                by the polling service before it touches loop-bound state.
            notebooks: Source-id resolver. Required — wire from
                ``NotebookLMClient`` (no implicit fallback). Threaded into the
                generation service.
            storage_path: Path to storage state file for loading download cookies.
            _backend: Private semantic backend used by Studio catalog reads.
            deadline_factory: Client-scoped factory for service-owned workflow deadlines.
        """
        self._drain = drain
        self._lifecycle = lifecycle
        self._notebooks = notebooks
        self._backend = _backend
        self._catalog = (
            StudioCatalog(_backend, deadline_factory=deadline_factory)
            if _backend is not None
            else None
        )
        # R5.1a: the generate families take pre-resolved inputs, so the source-set
        # and language defaults are resolved here, above the port.
        self._generation_inputs = (
            StudioGenerationInputs(NotebookReadService(_backend), deadline_factory=deadline_factory)
            if _backend is not None
            else None
        )
        self._data_tables = (
            DataTableFamilyService(_backend, self._catalog, self._generation_inputs)
            if _backend is not None
            and self._catalog is not None
            and self._generation_inputs is not None
            else None
        )
        self._mind_map_family = (
            NoteBackedMindMapFamilyService(_backend, self._catalog, self._generation_inputs)
            if _backend is not None
            and self._catalog is not None
            and self._generation_inputs is not None
            else None
        )
        self._drive_exports = DriveExportService(_backend) if _backend is not None else None
        self._audio = (
            AudioFamilyService(_backend, self._catalog, self._generation_inputs)
            if _backend is not None
            and self._catalog is not None
            and self._generation_inputs is not None
            else None
        )
        self._interactive = (
            InteractiveFamilyService(_backend, self._catalog, self._generation_inputs)
            if _backend is not None
            and self._catalog is not None
            and self._generation_inputs is not None
            else None
        )
        self._video = (
            VideoFamilyService(_backend, self._catalog, self._generation_inputs)
            if _backend is not None
            and self._catalog is not None
            and self._generation_inputs is not None
            else None
        )
        self._reports = (
            ReportFamilyService(_backend, self._catalog, self._generation_inputs)
            if _backend is not None
            and self._catalog is not None
            and self._generation_inputs is not None
            else None
        )
        self._visuals = (
            VisualFamilyService(_backend, self._catalog, self._generation_inputs)
            if _backend is not None
            and self._catalog is not None
            and self._generation_inputs is not None
            else None
        )
        self._poll_registry = PollRegistry()
        self._listing = ArtifactListingService()
        self._management = (
            StudioManagementService(_backend, deadline_factory=deadline_factory)
            if _backend is not None
            else None
        )
        self._suggestions = ReportSuggestionService(_backend) if _backend is not None else None
        self._lifecycle_service = ArtifactLifecycleService(
            _backend,
            loop_guard=self._lifecycle,
            op_scope=self._drain,
            poll_registry=self._poll_registry,
        )
        self._generation_workflow = ArtifactGenerationWorkflow(
            audio=self._audio,
            video=self._video,
            reports=self._reports,
            interactive=self._interactive,
            visuals=self._visuals,
            data_tables=self._data_tables,
            management=self._management,
            lifecycle=self._lifecycle_service,
        )
        self._representations = ArtifactRepresentationService(
            _backend,
            remote=StudioDownloadClient(
                storage_path=storage_path,
                cookie_loader=_artifact_downloads._load_httpx_cookies,
            ),
        )
        # Retain the historical private monkeypatch seam while its implementation
        # is now the backend-neutral Studio representation service.
        self._downloads = self._representations
        self._drain.register_drain_hook(
            "artifacts.polls",
            self._lifecycle_service.drain,
        )

    # =========================================================================
    # List/Get Operations
    # =========================================================================

    async def list(
        self, notebook_id: str, artifact_type: ArtifactType | None = None
    ) -> list[Artifact]:
        """List all artifacts in a notebook, including mind maps.

        Returns all AI-generated content. Note-backed mind maps live in the
        notes collection while interactive mind maps are studio artifacts
        (type 4 / variant 4); this listing merges both backings under
        ``ArtifactType.MIND_MAP``. Pass ``artifact_type`` to filter (e.g.
        ``ArtifactType.MIND_MAP`` for mind maps only).
        """
        logger.debug("Listing artifacts in notebook %s", notebook_id)
        if self._catalog is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        public_error: Exception | None = None
        try:
            family = (
                None if artifact_type is None else getattr(artifact_type, "value", artifact_type)
            )
            records = await self._catalog.list_records(notebook_id, family)
            return [project_artifact(record) for record in records]
        except BackendError as error:
            public_error = project_backend_error(error)
        assert public_error is not None
        raise public_error

    async def _list_for_download(
        self, notebook_id: str, artifact_type: ArtifactType | None = None
    ) -> tuple[
        builtins.list[Artifact],
        builtins.list[ArtifactRepresentationRecord],
        builtins.list[MindMapRepresentationRecord] | None,
    ]:
        """List once and retain neutral representation records for downloads."""
        if self._representations is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        studio = await project_backend_call(
            self._representations._list_representations(notebook_id)
        )

        mind_maps: tuple[MindMapRepresentationRecord, ...] | None = ()
        if artifact_type is None or artifact_type is ArtifactType.MIND_MAP:
            public_error = None
            try:
                mind_maps = await self._representations._list_mind_maps(notebook_id)
            except BackendError as error:
                if error.reason not in _PARTIAL_MIND_MAP_FAILURE_REASONS:
                    if isinstance(error, BackendContractError):
                        raise
                    public_error = project_backend_error(error)
                else:
                    logger.warning("Failed to fetch mind maps: %s", error)
                    mind_maps = None
            if public_error is not None:
                raise public_error

        records = [item.artifact for item in studio]
        if mind_maps is not None:
            records.extend(
                ArtifactRecord(
                    id=item.id,
                    title=item.title,
                    family="mind_map",
                    status="completed",
                    created_at=item.created_at,
                )
                for item in mind_maps
            )
        artifacts = [project_artifact(record) for record in records]
        if artifact_type is not None:
            artifacts = [artifact for artifact in artifacts if artifact.kind is artifact_type]
        return artifacts, list(studio), None if mind_maps is None else list(mind_maps)

    async def get(self, notebook_id: str, artifact_id: str) -> Artifact:
        """Get a specific artifact by ID.

        Raises:
            ArtifactNotFoundError: If no artifact with ``artifact_id`` exists
                (matches ``notebooks.get``; issue #1247). Use :meth:`get_or_none`
                for the sanctioned ``None``-on-miss lookup.
        """
        # ``unwrap_or_raise`` single-sources the raise-on-miss decision (#1247);
        # internal callers needing the silent lookup use get_or_none.
        return unwrap_or_raise(
            await self.get_or_none(notebook_id, artifact_id),
            ArtifactNotFoundError(artifact_id),
        )

    async def get_or_none(self, notebook_id: str, artifact_id: str) -> Artifact | None:
        """Get an artifact by ID, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019): unlike :meth:`get`
        — which raises ``ArtifactNotFoundError`` on a miss (#1247) — this
        returns ``None`` for a genuine absence with no deprecation warning. It
        lists once and id-matches, inheriting :meth:`list`'s behavior. (Per
        ADR-0019 Rule 3, ``list`` keeps its deliberate *partial-availability*
        policy: a mind-map sub-fetch transport failure logs a warning and
        yields the studio artifacts that loaded, so a note-backed mind-map id
        can read absent while that sub-fetch is down.) Faults from the primary
        studio-artifact listing propagate unchanged.
        """
        logger.debug("Getting artifact %s from notebook %s", artifact_id, notebook_id)
        if self._catalog is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        public_error: Exception | None = None
        try:
            record = await self._catalog.get_record(notebook_id, artifact_id)
            return None if record is None else project_artifact(record)
        except BackendError as error:
            public_error = project_backend_error(error)
        assert public_error is not None
        raise public_error

    # Internal optional-lookup alias: stable private name for the ``None``-on-miss lookup (vs. raising ``get()``).
    _get_or_none = get_or_none

    async def get_prompt(self, notebook_id: str, artifact_id: str) -> str | None:
        """Get the free-text prompt an artifact was generated from (any studio type).

        Returns ``None`` when the artifact stores no prompt (e.g. a note-backed
        mind map); raises :class:`ArtifactNotFoundError` for an unknown id.

        .. versionadded:: 0.8.0
        """
        if self._catalog is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        record = await project_backend_call(self._catalog.get_record(notebook_id, artifact_id))
        if record is None:
            raise ArtifactNotFoundError(artifact_id)
        return record.generation_prompt

    async def list_audio(self, notebook_id: str) -> builtins.list[Artifact]:
        """List audio overview artifacts."""
        if self._audio is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        public_error: Exception | None = None
        try:
            return [project_artifact(record) for record in await self._audio.list(notebook_id)]
        except BackendError as error:
            public_error = project_backend_error(error)
        assert public_error is not None
        raise public_error

    async def list_video(self, notebook_id: str) -> builtins.list[Artifact]:
        """List video overview artifacts."""
        if self._video is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return [
            project_artifact(record)
            for record in await project_backend_call(self._video.list(notebook_id))
        ]

    async def list_reports(self, notebook_id: str) -> builtins.list[Artifact]:
        """List report artifacts (Briefing Doc, Study Guide, Blog Post)."""
        if self._reports is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return [
            project_artifact(record)
            for record in await project_backend_call(self._reports.list(notebook_id))
        ]

    async def list_quizzes(self, notebook_id: str) -> builtins.list[Artifact]:
        """List quiz artifacts."""
        if self._interactive is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return [
            project_artifact(record)
            for record in await project_backend_call(self._interactive.list_quizzes(notebook_id))
        ]

    async def list_flashcards(self, notebook_id: str) -> builtins.list[Artifact]:
        """List flashcard artifacts."""
        if self._interactive is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return [
            project_artifact(record)
            for record in await project_backend_call(self._interactive.list_flashcards(notebook_id))
        ]

    async def list_infographics(self, notebook_id: str) -> builtins.list[Artifact]:
        """List infographic artifacts."""
        if self._visuals is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return [
            project_artifact(record)
            for record in await project_backend_call(self._visuals.list_infographics(notebook_id))
        ]

    async def list_slide_decks(self, notebook_id: str) -> builtins.list[Artifact]:
        """List slide deck artifacts."""
        if self._visuals is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return [
            project_artifact(record)
            for record in await project_backend_call(self._visuals.list_slide_decks(notebook_id))
        ]

    async def list_data_tables(self, notebook_id: str) -> builtins.list[Artifact]:
        """List data table artifacts."""
        if self._data_tables is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return [
            project_artifact(record)
            for record in await project_backend_call(self._data_tables.list(notebook_id))
        ]

    # =========================================================================
    # Generate Operations
    # =========================================================================

    async def _generate_with_retry_and_wait(
        self,
        method_name: str,
        notebook_id: str,
        call_kwargs: dict[str, Any],
        *,
        timeout: float,
        max_retries: int,
        wait: bool,
        artifact_type: str,
        wait_message: str,
        initial_interval: float | None = None,
        on_retry: Callable[[RateLimitRetryEvent], object | Awaitable[object]] | None = None,
        on_wait_start: Callable[[str], None] | None = None,
        wait_context: Callable[[str, str], AbstractAsyncContextManager[None]] | None = None,
    ) -> GenerationStatus | None:
        """Run one private application workflow under one caller budget."""
        return await self._generation_workflow.run(
            method_name,
            notebook_id,
            call_kwargs,
            timeout=timeout,
            max_retries=max_retries,
            wait=wait,
            artifact_type=artifact_type,
            wait_message=wait_message,
            initial_interval=initial_interval,
            on_retry=on_retry,
            on_wait_start=on_wait_start,
            wait_context=wait_context,
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
        """Generate an Audio Overview (podcast)."""
        return await self._generation_workflow.generate_once(
            "generate_audio",
            notebook_id,
            {
                "source_ids": source_ids,
                "language": language,
                "instructions": instructions,
                "audio_format": audio_format,
                "audio_length": audio_length,
            },
        )

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
        """Generate a Video Overview."""
        return await self._generation_workflow.generate_once(
            "generate_video",
            notebook_id,
            {
                "source_ids": source_ids,
                "language": language,
                "instructions": instructions,
                "video_format": video_format,
                "video_style": video_style,
                "style_prompt": style_prompt,
            },
        )

    async def generate_cinematic_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a Cinematic Video Overview."""
        return await self._generation_workflow.generate_once(
            "generate_cinematic_video",
            notebook_id,
            {
                "source_ids": source_ids,
                "language": language,
                "instructions": instructions,
            },
        )

    async def generate_report(
        self,
        notebook_id: str,
        report_format: ReportFormat = ReportFormat.BRIEFING_DOC,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        custom_prompt: str | None = None,
        extra_instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a report artifact."""
        return await self._generation_workflow.generate_once(
            "generate_report",
            notebook_id,
            {
                "report_format": report_format,
                "source_ids": source_ids,
                "language": language,
                "custom_prompt": custom_prompt,
                "extra_instructions": extra_instructions,
            },
        )

    async def generate_study_guide(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        extra_instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a study guide report."""
        return await self.generate_report(
            notebook_id,
            report_format=ReportFormat.STUDY_GUIDE,
            source_ids=source_ids,
            language=language,
            extra_instructions=extra_instructions,
        )

    async def generate_quiz(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        """Generate a quiz."""
        return await self._generation_workflow.generate_once(
            "generate_quiz",
            notebook_id,
            {
                "source_ids": source_ids,
                "instructions": instructions,
                "quantity": quantity,
                "difficulty": difficulty,
            },
        )

    async def generate_flashcards(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        """Generate flashcards."""
        return await self._generation_workflow.generate_once(
            "generate_flashcards",
            notebook_id,
            {
                "source_ids": source_ids,
                "instructions": instructions,
                "quantity": quantity,
                "difficulty": difficulty,
            },
        )

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
        """Generate an infographic."""
        return await self._generation_workflow.generate_once(
            "generate_infographic",
            notebook_id,
            {
                "source_ids": source_ids,
                "language": language,
                "instructions": instructions,
                "orientation": orientation,
                "detail_level": detail_level,
                "style": style,
            },
        )

    async def generate_slide_deck(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        slide_format: SlideDeckFormat | None = None,
        slide_length: SlideDeckLength | None = None,
    ) -> GenerationStatus:
        """Generate a slide deck."""
        return await self._generation_workflow.generate_once(
            "generate_slide_deck",
            notebook_id,
            {
                "source_ids": source_ids,
                "language": language,
                "instructions": instructions,
                "slide_format": slide_format,
                "slide_length": slide_length,
            },
        )

    async def revise_slide(
        self,
        notebook_id: str,
        artifact_id: str,
        slide_index: int,
        prompt: str,
    ) -> GenerationStatus:
        """Revise an individual slide in a completed slide deck using a prompt."""
        return await self._generation_workflow.generate_once(
            "revise_slide",
            notebook_id,
            {
                "artifact_id": artifact_id,
                "slide_index": slide_index,
                "prompt": prompt,
            },
        )

    async def retry_failed(self, notebook_id: str, artifact_id: str) -> GenerationStatus:
        """Retry a failed Studio artifact in place (the UI "Retry" action).

        Re-runs generation for an already-failed artifact without deleting it
        first; the same ``artifact_id`` is preserved as the task id, so existing
        :meth:`poll_status` / :meth:`wait_for_completion` flows keep working. An
        accepted retry returns ``GenerationStatus(status="pending")`` (#2127).

        Follows the ADR-0019 "async kickoff" contract: a synchronous
        ``USER_DISPLAYABLE_ERROR`` refusal (rate limit, quota, non-retryable
        artifact) **raises** ``RateLimitError`` / ``RPCError`` rather than
        returning ``status="failed"``, matching the sibling ``generate_*`` /
        :meth:`revise_slide` methods after v0.8.0 (#1342). A null / missing-id
        result raises :class:`ArtifactFeatureUnavailableError`. ``notebook_id``
        is routing-only (sets the ``source_path`` header); the artifact is
        identified solely by ``artifact_id``.
        """
        if self._management is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        result = await project_backend_call(
            self._management.retry(ArtifactRetryInput(notebook_id, artifact_id))
        )
        return project_generation_status(result.status)

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a data table."""
        return await self._generation_workflow.generate_once(
            "generate_data_table",
            notebook_id,
            {
                "source_ids": source_ids,
                "language": language,
                "instructions": instructions,
            },
        )

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> MindMapResult:
        """Generate a note-backed mind map and persist it as a note.

        Returns a :class:`~notebooklm._types.research.MindMapResult` with
        ``mind_map`` (parsed structure, or ``None`` on an empty response) and
        ``note_id`` (the persisted note id, or ``None``).
        """
        if self._mind_map_family is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        result = await project_backend_call(
            self._mind_map_family.generate(
                MindMapGenerateInput(
                    notebook_id,
                    None if source_ids is None else tuple(source_ids),
                    language,
                    instructions,
                )
            )
        )
        return MindMapResult(result.mind_map, result.note_id, result.created_at)

    # =========================================================================
    # Download Operations
    # =========================================================================

    async def download_audio(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download an Audio Overview to a file."""
        service = self._require_representations()
        return await project_backend_call(
            service.download_audio(
                notebook_id,
                output_path,
                artifact_id,
                representations=self._representation_records(artifacts_data),
            )
        )

    async def download_video(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a Video Overview to a file."""
        service = self._require_representations()
        return await project_backend_call(
            service.download_video(
                notebook_id,
                output_path,
                artifact_id,
                representations=self._representation_records(artifacts_data),
            )
        )

    async def download_infographic(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download an Infographic to a file."""
        service = self._require_representations()
        return await project_backend_call(
            service.download_infographic(
                notebook_id,
                output_path,
                artifact_id,
                representations=self._representation_records(artifacts_data),
            )
        )

    async def download_slide_deck(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "pdf",
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a slide deck as PDF or PPTX."""
        service = self._require_representations()
        return await project_backend_call(
            service.download_slide_deck(
                notebook_id,
                output_path,
                artifact_id,
                output_format,
                representations=self._representation_records(artifacts_data),
            )
        )

    async def _download_interactive_artifact(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        output_format: str,
        artifact_type: str,
        *,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str:
        """Download quiz or flashcard artifact."""
        service = self._require_representations()
        return await project_backend_call(
            service.download_interactive(
                notebook_id,
                output_path,
                artifact_id,
                output_format,
                artifact_type,
                artifacts=self._artifact_records(artifacts),
            )
        )

    def _format_interactive_content(
        self,
        app_data: dict,
        title: str,
        output_format: str,
        html_content: str,
        is_quiz: bool,
    ) -> str:
        """Format quiz (``is_quiz=True``) or flashcard content as json/markdown/html."""
        return _artifact_formatters._format_interactive_content(
            app_data,
            title,
            output_format,
            html_content,
            is_quiz,
        )

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a report artifact as markdown."""
        service = self._require_representations()
        return await project_backend_call(
            service.download_report(
                notebook_id,
                output_path,
                artifact_id,
                representations=self._representation_records(artifacts_data),
            )
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
        """Download a mind map as JSON."""
        service = self._require_representations()
        return await project_backend_call(
            service.download_mind_map(
                notebook_id,
                output_path,
                artifact_id,
                mind_maps=self._mind_map_records(mind_maps),
                representations=self._representation_records(artifacts_data),
            )
        )

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a data table as CSV."""
        service = self._require_representations()
        return await project_backend_call(
            service.download_data_table(
                notebook_id,
                output_path,
                artifact_id,
                representations=self._representation_records(artifacts_data),
            )
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
        """Download quiz questions."""
        return await self._download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "quiz", artifacts=artifacts
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
        """Download flashcard deck."""
        return await self._download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "flashcards", artifacts=artifacts
        )

    # =========================================================================
    # Management Operations
    # =========================================================================

    async def delete(self, notebook_id: str, artifact_id: str) -> None:
        """Delete an artifact.

        Idempotent: deleting an already-absent artifact succeeds (returns
        ``None``) and never raises ``ArtifactNotFoundError``. Real failures
        (``403``/``5xx``/auth/transport) still propagate.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211). ``if await artifacts.delete(...):``
            no longer enters its block.
        """
        logger.debug("Deleting artifact %s from notebook %s", artifact_id, notebook_id)
        if self._management is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        await project_backend_call(
            self._management.delete(ArtifactDeleteInput(notebook_id, artifact_id))
        )

    async def rename(
        self,
        notebook_id: str,
        artifact_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Artifact | None:
        """Rename an artifact.

        ``return_object=True`` (default) re-fetches (a full ``LIST_ARTIFACTS``
        call) and returns the renamed :class:`~notebooklm.types.Artifact`;
        ``False`` returns ``None`` on success. Miss-detection runs in both
        modes.

        Raises:
            ArtifactNotFoundError: if the artifact does not exist (detected via
                a list fetch, not a 404), in both ``return_object`` modes.
                Note-backed mind-map ids are *not* renameable here — use
                ``mind_maps.rename``.

        .. versionchanged:: 0.7.0
            **Breaking change:** no longer returns ``None`` on success; it
            re-fetches and raises :class:`ArtifactNotFoundError` for a missing
            target (#1255), plus the ``return_object`` opt-out.

        .. versionchanged:: 0.8.0
            **Breaking change:** ``return_object=False`` now runs the existence
            preflight too, so a missing target raises
            :class:`ArtifactNotFoundError` instead of silently returning
            ``None`` (#1362).
        """
        if self._management is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        result = await project_backend_call(
            self._management.rename(ArtifactRenameInput(notebook_id, artifact_id, new_title))
        )
        if result.artifact is None:
            raise RuntimeError("artifact.rename backend returned no post-mutation artifact")
        return None if not return_object else project_artifact(result.artifact)

    async def poll_status(self, notebook_id: str, task_id: str) -> GenerationStatus:
        """Poll the status of a generation task.

        Returns a ``GenerationStatus``; when the artifact is absent from the
        list, ``status`` is ``"not_found"`` so callers can distinguish
        "genuinely pending" from "removed by the server" (e.g. after a quota
        rejection).

        .. versionchanged:: 0.4.0
            **Breaking change:** Previously returned ``status="pending"`` when
            an artifact was absent from the list; now returns
            ``status="not_found"``.
        """
        if self._lifecycle_service is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return project_generation_status(
            await project_backend_call(self._lifecycle_service.observe(notebook_id, task_id))
        )

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
        max_not_found: int = 5,
        min_not_found_window: float = 10.0,
        on_status_change: Callable[[GenerationStatus], object] | None = None,
    ) -> GenerationStatus:
        """Wait for a generation task to complete (exponential-backoff polling).

        Concurrent callers for the same ``(notebook_id, task_id)`` share a
        single poll loop via this API's feature-owned ``PollRegistry``. The
        first caller is the *leader* and drives the loop; *followers* attach to
        the leader's future without issuing their own ``LIST_ARTIFACTS``
        requests. Cancellation is per-caller — only the cancelled caller's
        ``await`` raises ``CancelledError``; the poll continues and remaining
        followers still receive the result. Only the *leader's* interval /
        timeout / not-found knobs apply to the shared loop; followers' values
        are ignored once they attach. Distinct waiters that genuinely need
        distinct timeouts should serialize their calls instead.

        ``max_not_found`` (default 5) is the consecutive "not found" poll count
        before the task is treated as *removed* — the returned status is
        ``"removed"`` (see :attr:`GenerationStatus.is_removed`), kept distinct
        from ``"failed"`` so a delisted artifact (e.g. after a daily-quota
        rejection) is not conflated with a server terminal-FAILED.
        ``min_not_found_window`` (default 10.0) is the minimum elapsed seconds
        since the *first* not-found before a consecutive run triggers failure,
        avoiding false positives on slow networks. ``on_status_change`` is an
        optional sync/async callback invoked for each status observed by the
        shared leader. Late followers first receive its retained transition
        history, then receive live transitions in their own waiter task.

        Raises:
            TimeoutError: If task doesn't complete within ``timeout``.
        """
        if self._lifecycle_service is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        # ``_wait_for_completion`` is private: the service stays I1-neutral by
        # not naming the public ``GenerationStatus`` its callbacks carry, so
        # the facade — which already owns that type — restores it here.
        return cast(
            GenerationStatus,
            await self._lifecycle_service._wait_for_completion(
                notebook_id,
                task_id,
                initial_interval=initial_interval,
                max_interval=max_interval,
                timeout=timeout,
                max_not_found=max_not_found,
                min_not_found_window=min_not_found_window,
                poll_status=self.poll_status,
                on_status_change=on_status_change,
            ),
        )

    # =========================================================================
    # Export Operations
    # =========================================================================

    async def export_report(
        self,
        notebook_id: str,
        artifact_id: str,
        title: str = "Export",
        export_type: ExportType = ExportType.DOCS,
    ) -> Any:
        """Export a report to Google Docs (``export_type`` selects DOCS/SHEETS)."""
        return await self._export_to_drive(
            notebook_id,
            artifact_id=artifact_id,
            title=title,
            export_type=export_type,
        )

    async def export_data_table(
        self,
        notebook_id: str,
        artifact_id: str,
        title: str = "Export",
    ) -> Any:
        """Export a data table to Google Sheets."""
        return await self._export_to_drive(
            notebook_id,
            artifact_id=artifact_id,
            title=title,
            export_type=ExportType.SHEETS,
        )

    async def export(
        self,
        notebook_id: str,
        artifact_id: str | None = None,
        title: str = "Export",
        export_type: ExportType = ExportType.DOCS,
        *,
        content: str | None = None,
    ) -> Any:
        """Export any artifact to Drive; exactly one of ``artifact_id=``/``content=`` (``export_type`` picks Docs/Sheets)."""
        _artifact_validation.check_exactly_one_export_target(artifact_id, content)
        return await self._export_to_drive(
            notebook_id,
            artifact_id=artifact_id,
            content=content,
            title=title,
            export_type=export_type,
        )

    async def _export_to_drive(
        self,
        notebook_id: str,
        *,
        artifact_id: str | None,
        title: str,
        export_type: ExportType,
        content: str | None = None,
    ) -> Any:
        if self._drive_exports is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        destination = "sheets" if int(export_type) == int(ExportType.SHEETS) else "docs"
        result = await project_backend_call(
            self._drive_exports.export(
                DriveExportInput(
                    notebook_id=notebook_id,
                    artifact_id=artifact_id,
                    content=content,
                    title=title,
                    destination=destination,
                )
            )
        )
        return result.value

    # =========================================================================
    # Suggestions
    # =========================================================================

    async def suggest_reports(
        self,
        notebook_id: str,
    ) -> builtins.list[ReportSuggestion]:
        """Get AI-suggested report formats for a notebook."""
        if self._suggestions is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        result = await project_backend_call(
            self._suggestions.suggest(ArtifactSuggestReportsInput(notebook_id))
        )
        return [project_report_suggestion(item) for item in result.suggestions]

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _select_artifact(
        self,
        candidates: builtins.list[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> Any:
        """Compatibility shim over the pure artifact-row selector."""

        return self._listing.select_artifact(
            candidates,
            artifact_id,
            type_name,
            no_result_error_key,
            type_code=type_code,
        )

    async def _download_urls_batch(
        self, urls_and_paths: builtins.list[tuple[str, str]]
    ) -> "DownloadResult":
        """Download multiple files using httpx with proper cookie handling."""
        return await self._require_representations().download_batch(urls_and_paths)

    async def _download_url(self, url: str, output_path: str) -> str:
        """Download a file from URL using streaming with proper cookie handling."""
        return await self._require_representations().download_url(url, output_path)

    def _parse_generation_result(
        self,
        result: Any,
        *,
        method_id: str,
        source: str = "_parse_generation_result",
    ) -> GenerationStatus:
        """Compatibility parser retained for downstream private callers."""
        from ._web.codec.studio_documents import decode_generation_status

        record = decode_generation_status(result, method_id=method_id, source=source)
        if record is None:
            raise ArtifactFeatureUnavailableError("artifact", method_id=method_id)
        return project_generation_status(record)

    def _require_representations(self) -> ArtifactRepresentationService:
        if self._representations is None:
            raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
        return self._representations

    @staticmethod
    def _representation_records(
        rows: builtins.list[Any] | None,
    ) -> tuple[ArtifactRepresentationRecord, ...] | None:
        """Freeze the download prefetch handoff, decoding any raw wire rows.

        The shared CLI/MCP/REST prefetch producer (``_list_for_download``)
        always hands over already-decoded records, so this is a no-op tuple
        freeze on that path. It stays permissive for direct public callers of
        ``download_<x>(..., artifacts_data=...)``, who may still pass raw
        ``LIST_ARTIFACTS`` rows as they always could (public surface — see
        P10 PR1 review finding 1).
        """
        if rows is None:
            return None
        from ._web.codec.artifacts import decode_artifact_representation

        return tuple(
            row
            if isinstance(row, ArtifactRepresentationRecord)
            else decode_artifact_representation(row)
            for row in rows
        )

    @staticmethod
    def _artifact_records(
        artifacts: builtins.list[Artifact] | None,
    ) -> tuple[ArtifactRecord, ...] | None:
        if artifacts is None:
            return None
        return tuple(
            ArtifactRecord(
                id=item.id,
                title=item.title,
                family=item.kind.value,
                status=item.status_str,
                created_at=item.created_at,
            )
            for item in artifacts
        )

    @staticmethod
    def _mind_map_records(
        rows: builtins.list[Any] | None,
    ) -> tuple[MindMapRepresentationRecord, ...] | None:
        """Freeze the mind-map prefetch handoff, decoding any raw wire rows.

        See ``_representation_records`` above — permissive for direct public
        callers of ``download_mind_map(..., mind_maps=...)`` passing raw note
        rows. Rows that decode to ``None`` (deleted / non-mind-map notes) are
        filtered out, matching ``decode_mind_map_representations``.
        """
        if rows is None:
            return None
        from ._web.codec.artifacts import decode_mind_map_representation

        return tuple(
            record
            for row in rows
            if (
                record := (
                    row
                    if isinstance(row, MindMapRepresentationRecord)
                    else decode_mind_map_representation(row)
                )
            )
            is not None
        )

    def _get_artifact_type_name(self, artifact_type: int) -> str:
        """Human-readable name for an ``ArtifactTypeCode``, else the raw int as str."""
        return _artifact_polling._get_artifact_type_name(artifact_type)

    def _is_media_ready(self, art: builtins.list[Any], artifact_type: int) -> bool:
        """Check if a media artifact's download URLs are populated.

        For media artifacts (audio, video, infographic, slide deck) the API may
        set status=COMPLETED before the URLs are populated; this verifies they
        are available. Returns ``True`` for non-media types and (defensively)
        on unexpected structure.

        Positional URL locations (BATCHEXECUTE rows): ``art[6][5]`` audio URL
        list, ``art[8][i][0][0]`` video URL string (nested variants/entries),
        ``art[16][3]`` slide-deck PDF URL.
        """
        return _artifact_polling._is_media_ready(art, artifact_type)
