"""Backend-neutral artifact operations API."""

from __future__ import annotations

import builtins
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._artifact import formatters as _artifact_formatters  # noqa: F401
from ._artifact import polling as _artifact_polling  # noqa: F401
from ._artifact import validation as _artifact_validation  # noqa: F401
from ._artifact.downloads import AssetDownloadService, DownloadResult
from ._artifact.polling import ArtifactPollingService
from ._env import get_default_language
from ._notebook_metadata import NotebookSourceIdProvider, reconcile_copy_mapping
from ._polling_registry import PollRegistry
from ._runtime.call_supervisor import OperationLease
from ._types.enums import (
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
from ._types.research import MindMapResult
from .exceptions import ArtifactNotFoundError, ValidationError
from .types import (
    Artifact,
    ArtifactCustomizationChoices,
    ArtifactType,
    CopiedArtifact,
    GenerationStatus,
    ReportSuggestion,
)

if TYPE_CHECKING:
    from ._runtime.call_supervisor import CallSupervisor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ArtifactCopyResult:
    """Decoded copy mappings plus backend-specific failure diagnostics."""

    items: builtins.list[CopiedArtifact]
    method_id: str
    malformed_count: int = 0
    raw_response: str | None = None


def __getattr__(name: str) -> Any:
    """Resolve the legacy private ``_mind_map`` module alias lazily."""
    if name == "_mind_map":
        from ._web import mind_maps

        return mind_maps
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ArtifactsAPI(ABC):
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

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

    def __init__(
        self,
        *,
        supervisor: CallSupervisor,
        notebooks: NotebookSourceIdProvider,
        asset_downloads: AssetDownloadService,
    ) -> None:
        """Initialize the backend-neutral artifacts API.

        Args:
            supervisor: The single logical-call admission authority. Owns the
                polling caller scope, leader-child task, loop-affinity guard,
                and close-time drain-hook registration.
            notebooks: Base-typed source-id resolver used by shared generation
                workflows.
            asset_downloads: Required backend-supplied neutral asset-transfer
                service, configured with that backend's per-hop credential policy.
        """
        self._supervisor = supervisor
        self._notebooks = notebooks
        self._asset_downloads = asset_downloads
        self._poll_registry = PollRegistry()
        self._polling = ArtifactPollingService(
            supervisor=self._supervisor,
            poll_registry=self._poll_registry,
        )

    @abstractmethod
    async def _list_studio(
        self,
        notebook_id: str,
        task_id: str,
    ) -> builtins.list[Artifact]:
        """List the decoded studio artifact matching a polling task, if present."""

    @abstractmethod
    async def list(
        self, notebook_id: str, artifact_type: ArtifactType | None = None
    ) -> builtins.list[Artifact]:
        """List all artifacts in a notebook, including mind maps.

        Returns all AI-generated content. Note-backed mind maps live in the
        notes collection while interactive mind maps are studio artifacts
        (type 4 / variant 4); this listing merges both backings under
        ``ArtifactType.MIND_MAP``. Pass ``artifact_type`` to filter (e.g.
        ``ArtifactType.MIND_MAP`` for mind maps only).
        """

    async def get(self, notebook_id: str, artifact_id: str) -> Artifact:
        """Get a specific artifact by ID.

        Raises:
            ArtifactNotFoundError: If no artifact with ``artifact_id`` exists
                (matches ``notebooks.get``; issue #1247). Use :meth:`get_or_none`
                for the sanctioned ``None``-on-miss lookup.
        """
        artifact = await self.get_or_none(notebook_id, artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id)
        return artifact

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
        return next(
            (artifact for artifact in await self.list(notebook_id) if artifact.id == artifact_id),
            None,
        )

    _get_or_none = get_or_none

    @abstractmethod
    async def get_prompt(self, notebook_id: str, artifact_id: str) -> str | None:
        """Get the free-text prompt an artifact was generated from (any studio type).

        Returns ``None`` when the artifact stores no prompt (e.g. a note-backed
        mind map); raises :class:`ArtifactNotFoundError` for an unknown id.

        .. versionadded:: 0.8.0
        """

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

    @abstractmethod
    async def _send_create_artifact(
        self,
        notebook_id: str,
        family: str,
        source_ids: builtins.list[str],
        **options: Any,
    ) -> GenerationStatus:
        """Send one backend-specific artifact creation request."""

    async def _resolve_source_ids(
        self, notebook_id: str, source_ids: builtins.list[str] | None
    ) -> builtins.list[str]:
        return (
            await self._notebooks.get_source_ids(notebook_id) if source_ids is None else source_ids
        )

    @staticmethod
    def _resolve_language(language: str | None) -> str:
        return get_default_language() if language is None else language

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
        async with self._operation_scope("artifacts.generate_audio"):
            language = self._resolve_language(language)
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "audio",
                source_ids,
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
        language = self._resolve_language(language)
        if style_prompt is not None and not isinstance(style_prompt, str):
            raise ValidationError("style_prompt must be a string or None")
        normalized_style_prompt = style_prompt.strip() if style_prompt is not None else None
        if video_format == VideoFormat.CINEMATIC and normalized_style_prompt:
            raise ValidationError("style_prompt is not supported for cinematic videos")
        if video_format == VideoFormat.SHORT and (
            (video_style is not None and video_style != VideoStyle.AUTO_SELECT)
            or normalized_style_prompt
        ):
            raise ValidationError(
                "video_style and style_prompt are not supported for short videos "
                "(short has a fixed visual style)"
            )
        if video_style == VideoStyle.CUSTOM and not normalized_style_prompt:
            raise ValidationError("style_prompt is required when video_style is CUSTOM")
        if normalized_style_prompt and video_style != VideoStyle.CUSTOM:
            raise ValidationError("style_prompt requires video_style=VideoStyle.CUSTOM")
        async with self._operation_scope("artifacts.generate_video"):
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "video",
                source_ids,
                language=language,
                instructions=instructions,
                video_format=video_format,
                video_style=video_style,
                style_prompt=normalized_style_prompt,
            )

    async def generate_cinematic_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a Cinematic Video Overview."""
        async with self._operation_scope("artifacts.generate_cinematic_video"):
            language = self._resolve_language(language)
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "cinematic_video",
                source_ids,
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
        async with self._operation_scope("artifacts.generate_report"):
            report_format = _artifact_validation.coerce_report_format(report_format)
            language = self._resolve_language(language)
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "report",
                source_ids,
                report_format=report_format,
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
        async with self._operation_scope("artifacts.generate_study_guide"):
            language = self._resolve_language(language)
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "report",
                source_ids,
                report_format=ReportFormat.STUDY_GUIDE,
                language=language,
                custom_prompt=None,
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
        async with self._operation_scope("artifacts.generate_quiz"):
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "quiz",
                source_ids,
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
        async with self._operation_scope("artifacts.generate_flashcards"):
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "flashcards",
                source_ids,
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
        async with self._operation_scope("artifacts.generate_infographic"):
            language = self._resolve_language(language)
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "infographic",
                source_ids,
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
        async with self._operation_scope("artifacts.generate_slide_deck"):
            language = self._resolve_language(language)
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "slide_deck",
                source_ids,
                language=language,
                instructions=instructions,
                slide_format=slide_format,
                slide_length=slide_length,
            )

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a data table."""
        async with self._operation_scope("artifacts.generate_data_table"):
            language = self._resolve_language(language)
            source_ids = await self._resolve_source_ids(notebook_id, source_ids)
            return await self._send_create_artifact(
                notebook_id,
                "data_table",
                source_ids,
                language=language,
                instructions=instructions,
            )

    @abstractmethod
    async def revise_slide(
        self, notebook_id: str, artifact_id: str, slide_index: int, prompt: str
    ) -> GenerationStatus:
        """Revise an individual slide in a completed slide deck using a prompt."""

    @abstractmethod
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

    @abstractmethod
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

    @abstractmethod
    async def download_audio(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download an Audio Overview to a file."""

    @abstractmethod
    async def download_video(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a Video Overview to a file."""

    @abstractmethod
    async def download_infographic(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download an Infographic to a file."""

    @abstractmethod
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

    @abstractmethod
    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a report artifact as markdown."""

    @abstractmethod
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

    @abstractmethod
    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a data table as CSV."""

    @abstractmethod
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

    @abstractmethod
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

    @abstractmethod
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

    @abstractmethod
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
            list_studio=self._list_studio,
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

    async def _download_to_path(self, url: str, output_path: str) -> str:
        """Transfer a resolved byte URL through the shared asset plane."""
        return await self._asset_downloads.download_url(url, output_path)

    async def _download_url(self, url: str, output_path: str) -> str:
        """Compatibility alias for the historical private transfer helper."""
        return await self._download_to_path(url, output_path)

    async def _download_urls_batch(
        self, urls_and_paths: builtins.list[tuple[str, str]]
    ) -> DownloadResult:
        """Transfer multiple resolved byte URLs through the shared asset plane."""
        return await self._asset_downloads.download_urls_batch(urls_and_paths)

    def _format_interactive_content(
        self,
        app_data: dict,
        title: str,
        output_format: str,
        html_content: str,
        is_quiz: bool,
    ) -> str:
        """Format quiz or flashcard content as JSON, Markdown, or HTML."""
        return _artifact_formatters._format_interactive_content(
            app_data,
            title,
            output_format,
            html_content,
            is_quiz,
        )

    async def export_report(
        self,
        notebook_id: str,
        artifact_id: str,
        title: str = "Export",
        export_type: ExportType = ExportType.DOCS,
    ) -> Any:
        """Export a report to Google Docs (``export_type`` selects DOCS/SHEETS)."""
        return await self.export(notebook_id, artifact_id, title, export_type)

    async def export_data_table(
        self, notebook_id: str, artifact_id: str, title: str = "Export"
    ) -> Any:
        """Export a data table to Google Sheets."""
        return await self.export(notebook_id, artifact_id, title, ExportType.SHEETS)

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
        return await self._send_export(
            notebook_id,
            artifact_id,
            title,
            export_type,
            content=content,
        )

    @abstractmethod
    async def _send_export(
        self,
        notebook_id: str,
        artifact_id: str | None,
        title: str,
        export_type: ExportType,
        *,
        content: str | None,
    ) -> Any:
        """Send one backend-specific Drive export request."""

    @abstractmethod
    async def suggest_reports(self, notebook_id: str) -> builtins.list[ReportSuggestion]:
        """Get AI-suggested report formats for a notebook."""

    @abstractmethod
    async def _send_copy(
        self,
        notebook_id: str,
        artifact_ids: builtins.list[str],
        target_notebook_id: str,
    ) -> _ArtifactCopyResult:
        """Copy artifacts and return decoded mappings plus wire diagnostics."""

    async def copy(
        self,
        notebook_id: str,
        artifact_ids: builtins.list[str],
        target_notebook_id: str,
    ) -> builtins.list[CopiedArtifact]:
        """Copy Studio artifacts into another notebook (``CopyArtifactsAsync``).

        Returns one :class:`~notebooklm.types.CopiedArtifact` per copied
        artifact, pairing the original id with the full new row (verified live
        by re-listing the target). Raises
        ``ArtifactNotFoundError`` when none of the requested ids were copied —
        the server answers unknown ids with an empty mapping rather than
        ``NOT_FOUND``. A partial result is returned with a warning because the
        copies it names have already committed.

        The sync twin ``CopyArtifacts`` (``zVGIdd``) accepts any ids, copies
        nothing and reports success; it is deliberately not modelled (#2283).

        .. versionadded:: 0.9.0
        """
        if not artifact_ids:
            raise ValidationError("artifact_ids must not be empty")
        if any(not artifact_id for artifact_id in artifact_ids):
            raise ValidationError("artifact_ids must not contain empty entries")
        if not target_notebook_id:
            raise ValidationError("target_notebook_id must not be empty")

        transfer = await self._send_copy(notebook_id, artifact_ids, target_notebook_id)
        return reconcile_copy_mapping(
            artifact_ids,
            transfer.items,
            original_id=lambda item: item.original_id,
            operation="CopyArtifactsAsync",
            item_label="artifact",
            target_notebook_id=target_notebook_id,
            method_id=transfer.method_id,
            malformed_count=transfer.malformed_count,
            raw_response=transfer.raw_response,
            empty_error=ArtifactNotFoundError(
                ", ".join(artifact_ids), method_id=transfer.method_id
            ),
            warning_logger=logger,
        )

    async def get_customization_choices(
        self, notebook_id: str | None = None
    ) -> ArtifactCustomizationChoices:
        """Return the Studio "Customize" option tables (``GetArtifactCustomizationChoices``).

        Account-level: the server returns the same ~3.3 KB table for an empty
        request, a bogus notebook id and every artifact type (live, both front
        doors, 2026-09-01), so ``notebook_id`` is optional and only fills the
        request's ``project_id`` slot. Audio / video / slide-deck rows carry the wire codes
        of :class:`~notebooklm.types.AudioFormat`,
        :class:`~notebooklm.types.VideoFormat` and
        :class:`~notebooklm.types.SlideDeckFormat`; report presets carry the
        full generation directive each preset expands to. This is an
        availability table rather than an exhaustive enum manifest; dedicated
        options such as cinematic video may be omitted.

        .. versionadded:: 0.9.0
        """
        return await self._read_customization_choices(notebook_id)

    @abstractmethod
    async def _read_customization_choices(
        self, notebook_id: str | None = None
    ) -> ArtifactCustomizationChoices:
        """Read and decode the selected backend's customization table."""
        raise NotImplementedError


__all__ = ["ArtifactsAPI"]
