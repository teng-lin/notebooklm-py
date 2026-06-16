"""Artifacts API for NotebookLM studio content.

Provides operations for generating, listing, downloading, and managing
AI-generated artifacts including Audio Overviews, Video Overviews, Reports,
Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import builtins
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

# ``_mind_map`` is re-exported as ``_artifacts._mind_map`` so legacy patch
# seams can still resolve the module via the artifacts facade (monkeypatch
# convenience only). Runtime code talks to the injected
# ``NoteBackedMindMapService`` / ``NoteService`` instances.
from . import _mind_map  # noqa: F401 — re-exported as facade attribute
from ._artifact import formatters as _artifact_formatters
from ._artifact import polling as _artifact_polling
from ._artifact.downloads import ArtifactDownloadService, DownloadResult
from ._artifact.generation import ArtifactGenerationService
from ._artifact.listing import ArtifactListingService
from ._artifact.payloads import build_suggest_reports_params
from ._lookup import unwrap_or_raise
from ._mind_map import NoteBackedMindMapService
from ._note_service import NoteService
from ._notebook_metadata import NotebookSourceIdProvider
from ._polling_registry import PollRegistry
from ._row_adapters import artifacts as _artifact_rows
from ._runtime.contracts import RpcCaller
from ._types.research import MindMapResult
from .exceptions import ArtifactNotFoundError

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
    RPCMethod,
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
        rpc: RpcCaller,
        drain: "TransportDrainTracker",
        lifecycle: "ClientLifecycle",
        notebooks: NotebookSourceIdProvider,
        mind_maps: NoteBackedMindMapService,
        note_service: NoteService,
        storage_path: Path | None = None,
    ) -> None:
        """Initialize the artifacts API.

        Args:
            rpc: RPC dispatch surface (:class:`RpcCaller`) — used for direct
                artifact RPCs (delete, rename, export, list_raw) and threaded
                into the generation and download services.
            drain: Transport drain coordinator — owns ``operation_scope`` (used
                by the polling service) and ``register_drain_hook`` (used here
                to register the polling-service close-time cleanup hook).
            lifecycle: Client lifecycle seam — owns ``assert_bound_loop`` used
                by the polling service before it touches loop-bound state.
            notebooks: Source-id resolver. Required — wire from
                ``NotebookLMClient`` (no implicit fallback). Threaded into the
                generation service.
            mind_maps: Note-backed mind-map facade (:class:`NoteBackedMindMapService`)
                — owns the ``list_mind_maps`` / ``extract_content`` paths
                consumed by ``_artifact.downloads.download_mind_map``.
            note_service: Backend note-row primitives — owns the ``create_note``
                call site that the generation service's ``generate_mind_map``
                uses to persist generated mind maps.
            storage_path: Path to storage state file for loading download cookies.
        """
        self._rpc = rpc
        self._drain = drain
        self._lifecycle = lifecycle
        self._notebooks = notebooks
        self._mind_maps = mind_maps
        self._note_service = note_service
        self._poll_registry = PollRegistry()
        self._listing = ArtifactListingService()
        self._downloads = ArtifactDownloadService(
            rpc=self._rpc,
            listing=self._listing,
            mind_maps=self._mind_maps,
            storage_path=storage_path,
        )
        self._generation = ArtifactGenerationService(
            rpc=self._rpc,
            notebooks=self._notebooks,
            note_service=self._note_service,
        )
        self._polling = _artifact_polling.ArtifactPollingService(
            loop_guard=self._lifecycle,
            op_scope=self._drain,
            poll_registry=self._poll_registry,
        )
        self._drain.register_drain_hook("artifacts.polls", self._polling.drain)

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
        return await self._listing.list_artifacts(
            notebook_id,
            artifact_type,
            list_raw=self._list_raw,
            list_mind_maps=self._list_mind_maps,
        )

    async def _list_for_download(
        self, notebook_id: str, artifact_type: ArtifactType | None = None
    ) -> tuple[builtins.list[Artifact], builtins.list[Any], builtins.list[Any] | None]:
        """List artifacts + the raw rows fetched to build them — same RPC set as
        :meth:`list`. Internal seam for the ``_app`` download executor (#1488)."""
        return await self._listing.list_artifacts_with_raw(
            notebook_id,
            artifact_type,
            list_raw=self._list_raw,
            list_mind_maps=self._list_mind_maps,
        )

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
        return await self._listing.get(notebook_id, artifact_id, list_artifacts=self.list)

    # Internal optional-lookup alias: a stable private name so internal call
    # sites and tests use the ``None``-on-miss lookup rather than the raising get().
    _get_or_none = get_or_none

    async def get_prompt(self, notebook_id: str, artifact_id: str) -> str | None:
        """Get the free-text prompt an artifact was generated from (any studio type).

        Returns ``None`` when the artifact stores no prompt (e.g. a note-backed
        mind map); raises :class:`ArtifactNotFoundError` for an unknown id.

        .. versionadded:: 0.8.0
        """
        return await self._listing.get_prompt(notebook_id, artifact_id, list_raw=self._list_raw, list_mind_maps=self._list_mind_maps)  # fmt: skip

    async def list_audio(self, notebook_id: str) -> builtins.list[Artifact]:
        """List audio overview artifacts."""
        return await self.list(notebook_id, ArtifactType.AUDIO)

    async def list_video(self, notebook_id: str) -> builtins.list[Artifact]:
        """List video overview artifacts."""
        return await self.list(notebook_id, ArtifactType.VIDEO)

    async def list_reports(self, notebook_id: str) -> builtins.list[Artifact]:
        """List report artifacts (Briefing Doc, Study Guide, Blog Post)."""
        return await self.list(notebook_id, ArtifactType.REPORT)

    async def list_quizzes(self, notebook_id: str) -> builtins.list[Artifact]:
        """List quiz artifacts."""
        return await self.list(notebook_id, ArtifactType.QUIZ)

    async def list_flashcards(self, notebook_id: str) -> builtins.list[Artifact]:
        """List flashcard artifacts."""
        return await self.list(notebook_id, ArtifactType.FLASHCARDS)

    async def list_infographics(self, notebook_id: str) -> builtins.list[Artifact]:
        """List infographic artifacts."""
        return await self.list(notebook_id, ArtifactType.INFOGRAPHIC)

    async def list_slide_decks(self, notebook_id: str) -> builtins.list[Artifact]:
        """List slide deck artifacts."""
        return await self.list(notebook_id, ArtifactType.SLIDE_DECK)

    async def list_data_tables(self, notebook_id: str) -> builtins.list[Artifact]:
        """List data table artifacts."""
        return await self.list(notebook_id, ArtifactType.DATA_TABLE)

    # =========================================================================
    # Generate Operations
    # =========================================================================

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
        return await self._generation.generate_audio(
            notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            audio_format=audio_format,
            audio_length=audio_length,
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
        return await self._generation.generate_video(
            notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            video_format=video_format,
            video_style=video_style,
            style_prompt=style_prompt,
        )

    async def generate_cinematic_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a Cinematic Video Overview."""
        return await self._generation.generate_cinematic_video(
            notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
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
        return await self._generation.generate_report(
            notebook_id,
            report_format=report_format,
            source_ids=source_ids,
            language=language,
            custom_prompt=custom_prompt,
            extra_instructions=extra_instructions,
        )

    async def generate_study_guide(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        extra_instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a study guide report."""
        return await self._generation.generate_study_guide(
            notebook_id,
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
        return await self._generation.generate_quiz(
            notebook_id,
            source_ids=source_ids,
            instructions=instructions,
            quantity=quantity,
            difficulty=difficulty,
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
        return await self._generation.generate_flashcards(
            notebook_id,
            source_ids=source_ids,
            instructions=instructions,
            quantity=quantity,
            difficulty=difficulty,
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
        return await self._generation.generate_infographic(
            notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            orientation=orientation,
            detail_level=detail_level,
            style=style,
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
        return await self._generation.generate_slide_deck(
            notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            slide_format=slide_format,
            slide_length=slide_length,
        )

    async def revise_slide(
        self,
        notebook_id: str,
        artifact_id: str,
        slide_index: int,
        prompt: str,
    ) -> GenerationStatus:
        """Revise an individual slide in a completed slide deck using a prompt."""
        return await self._generation.revise_slide(notebook_id, artifact_id, slide_index, prompt)

    async def retry_failed(self, notebook_id: str, artifact_id: str) -> GenerationStatus:
        """Retry a failed Studio artifact in place (the UI "Retry" action).

        Re-runs generation for an already-failed artifact without deleting it
        first; the same ``artifact_id`` is preserved as the task id, so existing
        :meth:`poll_status` / :meth:`wait_for_completion` flows keep working. An
        accepted retry returns ``GenerationStatus(status="in_progress")``.

        Follows the ADR-0019 "async kickoff" contract: a synchronous
        ``USER_DISPLAYABLE_ERROR`` refusal (rate limit, quota, non-retryable
        artifact) **raises** ``RateLimitError`` / ``RPCError`` rather than
        returning ``status="failed"``, matching the sibling ``generate_*`` /
        :meth:`revise_slide` methods after v0.8.0 (#1342). A null / missing-id
        result raises :class:`ArtifactFeatureUnavailableError`. ``notebook_id``
        is routing-only (sets the ``source_path`` header); the artifact is
        identified solely by ``artifact_id``.
        """
        return await self._generation.retry_failed(notebook_id, artifact_id)

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a data table."""
        return await self._generation.generate_data_table(
            notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
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
        return await self._generation.generate_mind_map(
            notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
        )

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
        return await self._downloads.download_audio(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
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
        return await self._downloads.download_video(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
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
        return await self._downloads.download_infographic(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
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
        return await self._downloads.download_slide_deck(
            notebook_id, output_path, artifact_id, output_format, artifacts_data=artifacts_data
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
        return await self._downloads.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, artifact_type, artifacts=artifacts
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
        return await self._downloads.download_report(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
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
        return await self._downloads.download_mind_map(
            notebook_id,
            output_path,
            artifact_id,
            mind_maps=mind_maps,
            artifacts_data=artifacts_data,
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
        return await self._downloads.download_data_table(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
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
        params = [[2], artifact_id]
        await self._rpc.rpc_call(
            RPCMethod.DELETE_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
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
        params = [[artifact_id, new_title], [["title"]]]
        await self._rpc.rpc_call(
            RPCMethod.RENAME_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # Resolve via studio artifacts only — never public ``get()`` (#1247) nor
        # the merged listing (a note-backed mind-map id no-ops on RENAME_ARTIFACT
        # — use ``mind_maps.rename``). v0.8.0 (#1362): the lookup runs on
        # ``False`` too so a missing target is detected, but ``False`` still
        # returns ``None`` on success.
        artifact = await self._listing.get_studio_only(
            notebook_id, artifact_id, list_raw=self._list_raw
        )
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id, method_id=RPCMethod.RENAME_ARTIFACT.value)
        return None if not return_object else artifact

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
        return await self._polling.poll_status(
            notebook_id,
            task_id,
            list_raw=self._list_raw,
            is_media_ready=self._is_media_ready,
            get_artifact_type_name=self._get_artifact_type_name,
            extract_artifact_error=self._extract_artifact_error,
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
        optional sync/async callback invoked when the leader observes a new
        status (followers receive only the final status).

        Raises:
            TimeoutError: If task doesn't complete within ``timeout``.
        """
        return await self._polling.wait_for_completion(
            notebook_id,
            task_id,
            initial_interval=initial_interval,
            max_interval=max_interval,
            timeout=timeout,
            max_not_found=max_not_found,
            min_not_found_window=min_not_found_window,
            poll_status=self.poll_status,
            on_status_change=on_status_change,
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
        params = [None, artifact_id, None, title, int(export_type)]
        return await self._rpc.rpc_call(
            RPCMethod.EXPORT_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def export_data_table(
        self,
        notebook_id: str,
        artifact_id: str,
        title: str = "Export",
    ) -> Any:
        """Export a data table to Google Sheets."""
        params = [None, artifact_id, None, title, int(ExportType.SHEETS)]
        return await self._rpc.rpc_call(
            RPCMethod.EXPORT_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def export(
        self,
        notebook_id: str,
        artifact_id: str | None = None,
        content: str | None = None,
        title: str = "Export",
        export_type: ExportType = ExportType.DOCS,
    ) -> Any:
        """Export any artifact to Google Drive (live ``ExportToDrive``; ``export_type`` picks Docs/Sheets)."""
        params = [None, artifact_id, content, title, int(export_type)]
        return await self._rpc.rpc_call(
            RPCMethod.EXPORT_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    # =========================================================================
    # Suggestions
    # =========================================================================

    async def suggest_reports(
        self,
        notebook_id: str,
    ) -> builtins.list[ReportSuggestion]:
        """Get AI-suggested report formats for a notebook."""
        params = build_suggest_reports_params(notebook_id)

        result = await self._rpc.rpc_call(
            RPCMethod.GET_SUGGESTED_REPORTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if not (result and isinstance(result, list)):
            return []

        # GET_SUGGESTED_REPORTS returns a wrapped ``[[row1, ...]]`` envelope or a
        # flat list; the wrap probe + per-row decode are centralised behind
        # ``unwrap_artifact_rows`` / ``ReportSuggestionRow`` (#1491).
        items = _artifact_rows.unwrap_artifact_rows(
            result, method_id=RPCMethod.GET_SUGGESTED_REPORTS.value, source="suggest_reports"
        )
        return [
            ReportSuggestion(
                title=row.title,
                description=row.description,
                prompt=row.prompt,
                audience_level=row.audience_level,
            )
            for row in map(_artifact_rows.ReportSuggestionRow, items)
            if row.is_well_formed
        ]

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _call_generate(
        self,
        notebook_id: str,
        params: builtins.list[Any],
        *,
        null_result_artifact_type: str | None = None,
    ) -> GenerationStatus:
        """Make a generation RPC call with error handling.

        Facade hop: tests call ``api._call_generate(...)`` directly; the
        implementation lives on :class:`ArtifactGenerationService`.
        """
        return await self._generation._call_generate(
            notebook_id,
            params,
            null_result_artifact_type=null_result_artifact_type,
        )

    async def _list_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """Get raw mind-map rows via the injected mind-map facade."""
        return await self._mind_maps.list_mind_maps(notebook_id)

    async def _list_raw(self, notebook_id: str) -> builtins.list[Any]:
        """Get raw artifact list data."""
        # Keep this facade hop so callers/tests that patch ``api._list_raw``
        # still affect public listing paths that delegate into the service.
        return await self._listing.list_raw(notebook_id, rpc=self._rpc)

    def _select_artifact(
        self,
        candidates: builtins.list[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> Any:
        """Select an artifact from candidates by ID, or return latest completed.

        Single point of completed-artifact selection: filters the raw
        ``_list_raw`` list to entries matching ``type_code`` with status
        ``COMPLETED``, then applies the explicit-ID or latest-timestamp rule.

        The length guard requires only ``len(a) > 4`` — the minimum to read
        ``a[2]`` (type) and ``a[4]`` (status). A completed-but-too-short
        artifact passes here and surfaces as ``ArtifactParseError`` from the
        downstream extractor rather than ``ArtifactNotReadyError`` from this
        filter (downstream wraps ``IndexError``/``TypeError`` into
        ``ArtifactParseError``). ``no_result_error_key`` is *not* in general
        ``type_name.lower()`` — ``download_video`` passes ``"video_overview"``
        to preserve historical exception keys.

        Raises:
            ArtifactNotReadyError: If no candidate is found after filtering.
        """
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
        return await self._downloads.download_urls_batch(urls_and_paths)

    async def _download_url(self, url: str, output_path: str) -> str:
        """Download a file from URL using streaming with proper cookie handling."""
        return await self._downloads.download_url(url, output_path)

    def _parse_generation_result(
        self,
        result: Any,
        *,
        method_id: str,
        source: str = "_parse_generation_result",
    ) -> GenerationStatus:
        """Parse a generation result into GenerationStatus.

        Facade hop: tests call ``api._parse_generation_result(...)`` directly;
        the implementation lives on :class:`ArtifactGenerationService`.
        """
        return self._generation._parse_generation_result(result, method_id=method_id, source=source)

    @staticmethod
    def _extract_artifact_error(art: builtins.list[Any]) -> str | None:
        """Extract a human-readable error from a failed artifact, or ``None``.

        Google's batchexecute responses embed error info in varying positions;
        this walks known (reverse-engineered) locations and returns the first
        non-empty string. ``art[3]`` sometimes holds an error reason string;
        ``art[5]`` may hold a nested UserDisplayableError-style payload.
        """
        return _artifact_polling._extract_artifact_error(art)

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
