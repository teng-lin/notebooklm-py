"""Artifacts API for NotebookLM studio content.

Provides operations for generating, listing, downloading, and managing
AI-generated artifacts including Audio Overviews, Video Overviews, Reports,
Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import asyncio
import builtins
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _artifact_formatters, _mind_map
from ._artifact_downloads import ArtifactDownloadService, DownloadResult
from ._artifact_generation import ArtifactGenerationService
from ._artifact_listing import ArtifactListingService
from ._callbacks import maybe_await_callback
from ._capabilities import ClientCoreCapabilities
from ._core import ClientCore
from .auth import load_httpx_cookies
from .rpc import (
    ArtifactStatus,
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    ExportType,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    NetworkError,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    RPCMethod,
    RPCTimeoutError,
    ServerError,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
    artifact_status_to_str,
)
from .types import (
    Artifact,
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    ArtifactType,
    GenerationStatus,
    ReportSuggestion,
    _extract_artifact_url,
)

logger = logging.getLogger(__name__)

# Maximum number of retries for transient errors during artifact polling
POLL_MAX_RETRIES = 3

# Media artifact types that require URL availability before reporting completion
_MEDIA_ARTIFACT_TYPES = frozenset(
    {
        ArtifactTypeCode.AUDIO.value,
        ArtifactTypeCode.VIDEO.value,
        ArtifactTypeCode.INFOGRAPHIC.value,
        ArtifactTypeCode.SLIDE_DECK.value,
    }
)

if TYPE_CHECKING:
    from ._notes import NotesAPI  # retained for backward-compatible type hints

# Private compatibility exports. Tests and downstream code patch these names
# through ``notebooklm._artifacts`` even though download implementation now
# lives in ``_artifact_downloads``.
_DOWNLOAD_COMPAT_EXPORTS = (
    DownloadResult,
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    json,
    load_httpx_cookies,
)


# Backward-compatible private helper wrappers.
def _extract_app_data(html_content: str) -> dict:
    return _artifact_formatters._extract_app_data(html_content)


def _format_quiz_markdown(title: str, questions: list[dict]) -> str:
    return _artifact_formatters._format_quiz_markdown(title, questions)


def _format_flashcards_markdown(title: str, cards: list[dict]) -> str:
    return _artifact_formatters._format_flashcards_markdown(title, cards)


def _extract_cell_text(cell: Any) -> str:
    return _artifact_formatters._extract_cell_text(cell)


def _extract_data_table_rows(raw_data: Any) -> list[Any]:
    return _artifact_formatters._extract_data_table_rows(raw_data)


def _parse_data_table(raw_data: list) -> tuple[list[str], list[list[str]]]:
    return _artifact_formatters._parse_data_table(
        raw_data,
        rows_extractor=_extract_data_table_rows,
        cell_text_extractor=_extract_cell_text,
    )


def _format_interactive_content(
    app_data: dict,
    title: str,
    output_format: str,
    html_content: str,
    is_quiz: bool,
) -> str:
    return _artifact_formatters._format_interactive_content(
        app_data,
        title,
        output_format,
        html_content,
        is_quiz,
        quiz_markdown_formatter=_format_quiz_markdown,
        flashcards_markdown_formatter=_format_flashcards_markdown,
    )


class ArtifactsAPI:
    """Operations on NotebookLM artifacts (studio content).

    Artifacts are AI-generated content including Audio Overviews, Video Overviews,
    Reports, Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.

    Usage:
        async with await NotebookLMClient.from_storage() as client:
            # Generate
            status = await client.artifacts.generate_audio(notebook_id)
            await client.artifacts.wait_for_completion(notebook_id, status.task_id)

            # Download
            await client.artifacts.download_audio(notebook_id, "output.mp4")

            # List and manage
            artifacts = await client.artifacts.list(notebook_id)
            await client.artifacts.rename(notebook_id, artifact_id, "New Title")
    """

    def __init__(
        self,
        core: ClientCore,
        notes_api: "NotesAPI | None" = None,
        storage_path: Path | None = None,
    ):
        """Initialize the artifacts API.

        Args:
            core: The core client infrastructure.
            notes_api: Deprecated. Retained as an optional, ignored
                keyword for backward compatibility — ``ArtifactsAPI`` no
                longer depends on :class:`NotesAPI`. Mind-map RPC
                primitives are accessed directly through the
                :mod:`_mind_map` module, so the construction order of
                ``client.artifacts`` and ``client.notes`` is no longer
                significant.
            storage_path: Path to storage state file for loading download cookies.
        """
        self._core = core
        self._capabilities = ClientCoreCapabilities(core)
        # ``notes_api`` is intentionally not stored — it is accepted only
        # so that existing call sites (tests, third-party code) keep
        # working through the deprecation cycle.
        del notes_api
        self._storage_path = storage_path
        self._listing = ArtifactListingService()
        self._generation = ArtifactGenerationService(self)
        self._downloads = ArtifactDownloadService(self)

    # =========================================================================
    # List/Get Operations
    # =========================================================================

    async def list(
        self, notebook_id: str, artifact_type: ArtifactType | None = None
    ) -> list[Artifact]:
        """List all artifacts in a notebook, including mind maps.

        This returns all AI-generated content: Audio Overviews, Video Overviews,
        Reports, Quizzes, Flashcards, Infographics, Slide Decks, Data Tables,
        and Mind Maps.

        Note: Mind maps are stored in a separate system (notes) but are included
        here since they are AI-generated studio content.

        Args:
            notebook_id: The notebook ID.
            artifact_type: Optional ArtifactType to filter by.
                Use ArtifactType.MIND_MAP to get only mind maps.

        Returns:
            List of Artifact objects.
        """
        logger.debug("Listing artifacts in notebook %s", notebook_id)
        return await self._listing.list_artifacts(
            notebook_id,
            artifact_type,
            list_raw=self._list_raw,
            list_mind_maps=self._list_mind_maps,
        )

    async def get(self, notebook_id: str, artifact_id: str) -> Artifact | None:
        """Get a specific artifact by ID.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID.

        Returns:
            Artifact object, or None if not found.
        """
        logger.debug("Getting artifact %s from notebook %s", artifact_id, notebook_id)
        return await self._listing.get(notebook_id, artifact_id, list_artifacts=self.list)

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
        language: str | None = None,
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
        language: str | None = None,
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
        language: str | None = None,
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
        language: str | None = None,
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
        language: str | None = None,
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
        language: str | None = None,
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
        language: str | None = None,
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
        return await self._generation.revise_slide(
            notebook_id,
            artifact_id,
            slide_index,
            prompt,
        )

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = None,
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
        language: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        """Generate an interactive mind map."""
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
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Audio Overview to a file."""
        return await self._downloads.download_audio(notebook_id, output_path, artifact_id)

    async def download_video(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download a Video Overview to a file."""
        return await self._downloads.download_video(notebook_id, output_path, artifact_id)

    async def download_infographic(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Infographic to a file."""
        return await self._downloads.download_infographic(notebook_id, output_path, artifact_id)

    async def download_slide_deck(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "pdf",
    ) -> str:
        """Download a slide deck as PDF or PPTX."""
        return await self._downloads.download_slide_deck(
            notebook_id, output_path, artifact_id, output_format
        )

    async def _get_artifact_content(self, notebook_id: str, artifact_id: str) -> str | None:
        """Fetch artifact HTML content for quiz/flashcard types."""
        result = await self._core.rpc_call(
            RPCMethod.GET_INTERACTIVE_HTML,
            [artifact_id],
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # Response is wrapped: result[0] contains the artifact data
        if result and isinstance(result, list) and len(result) > 0:
            data = result[0]
            if isinstance(data, list) and len(data) > 9 and data[9]:
                return data[9][0]  # HTML content
        return None

    async def _download_interactive_artifact(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        output_format: str,
        artifact_type: str,
    ) -> str:
        """Download quiz or flashcard artifact."""
        return await self._downloads.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, artifact_type
        )

    def _format_interactive_content(
        self,
        app_data: dict,
        title: str,
        output_format: str,
        html_content: str,
        is_quiz: bool,
    ) -> str:
        """Format quiz or flashcard content for output.

        Args:
            app_data: Parsed data from HTML.
            title: Artifact title.
            output_format: Output format - json, markdown, or html.
            html_content: Original HTML content.
            is_quiz: True for quiz, False for flashcards.

        Returns:
            Formatted content string.
        """
        return _format_interactive_content(app_data, title, output_format, html_content, is_quiz)

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a report artifact as markdown."""
        return await self._downloads.download_report(notebook_id, output_path, artifact_id)

    async def download_mind_map(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a mind map as JSON."""
        return await self._downloads.download_mind_map(notebook_id, output_path, artifact_id)

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a data table as CSV."""
        return await self._downloads.download_data_table(notebook_id, output_path, artifact_id)

    async def download_quiz(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download quiz questions."""
        return await self._download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "quiz"
        )

    async def download_flashcards(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download flashcard deck."""
        return await self._download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "flashcards"
        )

    # =========================================================================
    # Management Operations
    # =========================================================================

    async def delete(self, notebook_id: str, artifact_id: str) -> bool:
        """Delete an artifact.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID to delete.

        Returns:
            True if deletion succeeded.
        """
        logger.debug("Deleting artifact %s from notebook %s", artifact_id, notebook_id)
        params = [[2], artifact_id]
        await self._core.rpc_call(
            RPCMethod.DELETE_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return True

    async def rename(self, notebook_id: str, artifact_id: str, new_title: str) -> None:
        """Rename an artifact.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID to rename.
            new_title: The new title.
        """
        params = [[artifact_id, new_title], [["title"]]]
        await self._core.rpc_call(
            RPCMethod.RENAME_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def poll_status(self, notebook_id: str, task_id: str) -> GenerationStatus:
        """Poll the status of a generation task.

        Args:
            notebook_id: The notebook ID.
            task_id: The task/artifact ID to check.

        Returns:
            GenerationStatus with current status.  When the artifact is not
            found in the list, ``status`` is set to ``"not_found"`` so that
            callers can distinguish "genuinely pending" from "removed by the
            server" (e.g. after a quota rejection).

        .. versionchanged:: 0.4.0
            **Breaking change:** Previously returned ``status="pending"``
            when an artifact was absent from the list.  Now returns
            ``status="not_found"`` to allow callers to distinguish a
            genuinely pending artifact from one that was removed.
        """
        # List all artifacts and find by ID (no poll-by-ID RPC exists)
        artifacts_data = await self._list_raw(notebook_id)
        for art in artifacts_data:
            if len(art) > 0 and art[0] == task_id:
                status_code = art[4] if len(art) > 4 else 0
                artifact_type = art[2] if len(art) > 2 else 0

                # For media artifacts, verify URL availability before reporting completion.
                # The API may set status=COMPLETED before media URLs are populated.
                if status_code == ArtifactStatus.COMPLETED:
                    if not self._is_media_ready(art, artifact_type):
                        type_name = self._get_artifact_type_name(artifact_type)
                        logger.debug(
                            "Artifact %s (type=%s) status=COMPLETED but media not ready, "
                            "continuing poll",
                            task_id,
                            type_name,
                        )
                        # Downgrade to PROCESSING to continue polling
                        status_code = ArtifactStatus.PROCESSING

                status = artifact_status_to_str(status_code)

                # Extract error details from failed artifacts.
                # The API may embed an error reason string at art[3] when
                # the artifact fails (e.g. daily quota exceeded).
                error_msg: str | None = None
                if status == "failed":
                    error_msg = self._extract_artifact_error(art)
                url = _extract_artifact_url(art, artifact_type)

                return GenerationStatus(
                    task_id=task_id,
                    status=status,
                    url=url,
                    error=error_msg,
                )

        # Artifact not found in the list.  Use a distinct status so
        # wait_for_completion can differentiate from genuine "pending".
        return GenerationStatus(task_id=task_id, status="not_found")

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
        poll_interval: float | None = None,  # Deprecated, use initial_interval
        max_not_found: int = 5,
        min_not_found_window: float = 10.0,
        on_status_change: Callable[[GenerationStatus], object] | None = None,
    ) -> GenerationStatus:
        """Wait for a generation task to complete.

        Uses exponential backoff for polling to reduce API load.

        Concurrent callers for the same ``(notebook_id, task_id)`` share a
        single underlying poll loop through the ``PollRegistry`` exposed by
        this API's capabilities. The first caller is the *leader* and drives
        the poll loop; subsequent *followers* attach to the leader's future
        without issuing their own ``LIST_ARTIFACTS`` requests. Cancellation is
        per-caller — only the cancelled caller's ``await`` raises
        ``CancelledError``; the underlying poll continues and remaining
        followers still receive the result.

        Because followers attach to the leader's already-running poll,
        only the *leader's* ``initial_interval`` / ``max_interval`` /
        ``timeout`` / ``max_not_found`` / ``min_not_found_window`` apply
        to the shared poll loop. Followers' values for these parameters
        are ignored once they attach. This is acceptable for the
        intended use case (deduping accidental fan-out from the same
        application) — distinct waiters that genuinely need distinct
        timeouts should serialize their calls instead.

        Args:
            notebook_id: The notebook ID.
            task_id: The task/artifact ID to wait for.
            initial_interval: Initial seconds between status checks
                (leader only — see note above).
            max_interval: Maximum seconds between status checks
                (leader only).
            timeout: Maximum seconds to wait (leader only).
            poll_interval: Deprecated. Use initial_interval instead.
            max_not_found: Consecutive "not found" polls before treating
                the task as failed.  When the API removes an artifact
                from the list (e.g. after a daily-quota rejection), the
                poller would otherwise spin until *timeout*.  Defaults
                to 5 to tolerate brief replication lag and slow networks.
                (Leader only.)
            min_not_found_window: Minimum seconds that must have elapsed
                since the *first* not-found response before a consecutive
                run triggers failure.  This avoids false positives on
                slow or unreliable networks.  Defaults to 10.0.
                (Leader only.)
            on_status_change: Optional sync or async callback invoked with a
                ``GenerationStatus`` when the leader observes a new status.
                Followers that attach to an existing poll receive only the
                final status through this callback.

        Returns:
            Final GenerationStatus.

        Raises:
            TimeoutError: If task doesn't complete within timeout.
        """
        # Backward compatibility: poll_interval overrides initial_interval
        if poll_interval is not None:
            import warnings

            warnings.warn(
                "poll_interval is deprecated, use initial_interval instead",
                DeprecationWarning,
                stacklevel=2,
            )
            initial_interval = poll_interval

        pending = self._capabilities.poll_registry.pending
        key = (notebook_id, task_id)

        existing = pending.get(key)
        if existing is not None:
            # Follower path. ``asyncio.shield`` ensures that *this* caller's
            # cancellation does not propagate into the shared future; the
            # leader's poll task continues on behalf of every other follower.
            result = await asyncio.shield(existing[0])
            if on_status_change is not None:
                await maybe_await_callback(on_status_change, result)
            return result

        # Leader path. Create the shared future, spawn the poll task,
        # register the pair so any follower can attach. Both the future
        # and the task live in the registry entry — the task reference
        # alone is what anchors the running poll against GC (Python's
        # task-GC contract is permissive; see asyncio C4 lesson /
        # Python 3.11+ task GC fix). The done-callback pops the entry
        # on every termination path (success / exception / cancel) so
        # the registry cannot leak.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[GenerationStatus] = loop.create_future()

        # Consume any exception set on the future if no caller ever
        # retrieves it (e.g. leader cancelled with no followers). Without
        # this, ``set_exception`` on an unawaited future logs
        # "Future exception was never retrieved" at GC time.
        def _consume_orphan_exception(fut: asyncio.Future[GenerationStatus]) -> None:
            if not fut.cancelled():
                # ``exception()`` clears the _log_traceback flag inside the
                # future. We intentionally drop the value.
                fut.exception()

        future.add_done_callback(_consume_orphan_exception)

        poll_task = asyncio.create_task(
            self._run_poll_loop(
                notebook_id,
                task_id,
                initial_interval=initial_interval,
                max_interval=max_interval,
                timeout=timeout,
                max_not_found=max_not_found,
                min_not_found_window=min_not_found_window,
                on_status_change=on_status_change,
            ),
            name=f"artifact-poll-{notebook_id}-{task_id}",
        )
        try:
            poll_operation_token = await self._capabilities.begin_transport_task(
                poll_task,
                f"artifact wait {task_id}",
            )
        except BaseException:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await poll_task
            raise

        pending[key] = (future, poll_task)

        async def _finish_poll_operation() -> None:
            try:
                await self._capabilities.finish_transport_post(poll_operation_token)
            except Exception as exc:  # noqa: BLE001 - cleanup should not mask poll result
                logger.warning("Artifact poll drain bookkeeping failed: %s", exc)

        def _on_poll_done(task: asyncio.Task[GenerationStatus]) -> None:
            asyncio.create_task(_finish_poll_operation())
            # Pop the registry entry first so a follower that arrives
            # concurrently with completion either (a) attaches to the
            # already-resolved future and gets the cached result, or
            # (b) misses the entry entirely and starts a fresh poll for
            # the *next* generation. Either is correct.
            pending.pop(key, None)
            # Inside this callback there are no ``await`` points, so
            # the single-threaded asyncio model guarantees ``future`` is
            # still pending — assert that invariant rather than guard
            # silently. A regression in callback ordering would surface
            # loudly instead of dropping a result on the floor.
            assert not future.done(), "future resolved before poll task done-callback"
            if task.cancelled():
                # The poll task itself was cancelled. Followers shield
                # the future and the leader's cancel doesn't propagate
                # to the task, so this is exceedingly rare — but if it
                # happens, surface ``CancelledError`` to attached waiters.
                future.cancel()
                return
            exc = task.exception()
            if exc is not None:
                future.set_exception(exc)
                return
            future.set_result(task.result())

        poll_task.add_done_callback(_on_poll_done)

        # Leader awaits via ``asyncio.shield`` so that the leader's
        # cancellation unwinds locally without taking down the shared
        # poll. The shielded poll task continues until the done-callback
        # fires; remaining followers still receive the result.
        return await asyncio.shield(future)

    async def _run_poll_loop(
        self,
        notebook_id: str,
        task_id: str,
        *,
        initial_interval: float,
        max_interval: float,
        timeout: float,
        max_not_found: int,
        min_not_found_window: float,
        on_status_change: Callable[[GenerationStatus], object] | None,
    ) -> GenerationStatus:
        """The actual polling loop. Driven by the leader's shielded task.

        This is intentionally private and parameter-keyword-only — direct
        callers should always go through ``wait_for_completion`` so the
        leader/follower dedupe is honored.
        """
        start_time = asyncio.get_running_loop().time()
        current_interval = initial_interval
        consecutive_not_found = 0
        total_not_found = 0
        poll_retry_count = 0
        first_not_found_time: float | None = None
        last_status: str | None = None
        last_emitted_status: str | None = None

        while True:
            try:
                status = await self.poll_status(notebook_id, task_id)
            except (NetworkError, RPCTimeoutError, ServerError) as e:
                # Transient — retry up to POLL_MAX_RETRIES times with exponential
                # backoff capped at 8s. Also clamp by remaining timeout budget so
                # the retry path never extends wall-clock past the caller's
                # `timeout` parameter; raise if there's no headroom left.
                if poll_retry_count >= POLL_MAX_RETRIES:
                    raise
                remaining = timeout - (asyncio.get_running_loop().time() - start_time)
                if remaining <= 0:
                    raise
                poll_retry_count += 1
                backoff = min(2**poll_retry_count, 8.0, remaining)
                logger.warning(
                    "wait_for_completion: transient %s on poll #%d, retrying in %.1fs",
                    e.__class__.__name__,
                    poll_retry_count,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue

            poll_retry_count = 0  # reset on success
            last_status = status.status
            if status.status != last_emitted_status:
                last_emitted_status = status.status
                if on_status_change is not None:
                    await maybe_await_callback(on_status_change, status)

            if status.is_complete or status.is_failed:
                return status

            # Track consecutive and total "not found" responses.  The API
            # may remove quota-rejected artifacts from the list entirely
            # instead of setting them to FAILED.  We track both a
            # consecutive run *and* a total count to handle "flickering"
            # artifacts that alternate between found/not-found due to API
            # replication lag.
            if status.status == "not_found":
                consecutive_not_found += 1
                total_not_found += 1
                now = asyncio.get_running_loop().time()
                if first_not_found_time is None:
                    first_not_found_time = now
                not_found_elapsed = now - first_not_found_time

                # Trigger failure when consecutive threshold is met AND
                # enough wall-clock time has passed (avoids false positives
                # on fast networks), OR when total not-found count is high
                # enough to indicate flickering artifacts.
                consecutive_trigger = (
                    consecutive_not_found >= max_not_found
                    and not_found_elapsed >= min_not_found_window
                )
                total_trigger = total_not_found >= max_not_found * 2

                if consecutive_trigger or total_trigger:
                    trigger = (
                        f"consecutive={consecutive_not_found}"
                        if consecutive_trigger
                        else f"total={total_not_found}"
                    )
                    logger.warning(
                        "Artifact %s disappeared from list (%s not-found polls, "
                        "%s) — treating as failed",
                        task_id,
                        trigger,
                        f"elapsed={not_found_elapsed:.1f}s",
                    )
                    failed_status = GenerationStatus(
                        task_id=task_id,
                        status="failed",
                        error=(
                            "Generation failed: artifact was removed by the server. "
                            "This may indicate a daily quota/rate limit was exceeded, "
                            "an invalid notebook ID, or a transient API issue. "
                            "Try again later."
                        ),
                    )
                    if on_status_change is not None and last_emitted_status != "failed":
                        await maybe_await_callback(on_status_change, failed_status)
                    return failed_status
            else:
                consecutive_not_found = 0

            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"Task {task_id} timed out after {timeout}s (last status: {last_status})"
                )

            # Clamp sleep duration to respect timeout
            remaining_time = timeout - elapsed
            sleep_duration = min(current_interval, remaining_time)
            if sleep_duration > 0:
                await asyncio.sleep(sleep_duration)

            # Exponential backoff: double the interval up to max_interval
            current_interval = min(current_interval * 2, max_interval)

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
        """Export a report to Google Docs.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The report artifact ID.
            title: Title for the exported document.
            export_type: ExportType.DOCS (default) or ExportType.SHEETS.

        Returns:
            Export result with document URL.
        """
        params = [None, artifact_id, None, title, int(export_type)]
        return await self._core.rpc_call(
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
        """Export a data table to Google Sheets.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The data table artifact ID.
            title: Title for the exported spreadsheet.

        Returns:
            Export result with spreadsheet URL.
        """
        params = [None, artifact_id, None, title, int(ExportType.SHEETS)]
        return await self._core.rpc_call(
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
        """Export an artifact to Google Docs/Sheets.

        Generic export method for any artifact type.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID (optional).
            content: Content to export (optional).
            title: Title for the exported document.
            export_type: ExportType.DOCS (default) or ExportType.SHEETS.

        Returns:
            Export result with document URL.
        """
        params = [None, artifact_id, content, title, int(export_type)]
        return await self._core.rpc_call(
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
        return await self._generation.suggest_reports(notebook_id)

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _call_generate(
        self, notebook_id: str, params: builtins.list[Any]
    ) -> GenerationStatus:
        """Make a generation RPC call with error handling."""
        return await self._generation._call_generate(notebook_id, params)

    async def _list_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """Get raw mind-map rows through the patchable module seam."""
        # Resolve the module seam at call time so tests patching
        # ``notebooklm._artifacts._mind_map`` affect public listing paths.
        return await _mind_map.list_mind_maps(self._core, notebook_id)

    async def _list_raw(self, notebook_id: str) -> builtins.list[Any]:
        """Get raw artifact list data."""
        # Keep this facade hop so callers/tests that patch ``api._list_raw``
        # still affect public listing paths that delegate into the service.
        return await self._listing.list_raw(notebook_id, rpc_call=self._core.rpc_call)

    def _select_artifact(
        self,
        candidates: builtins.list[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> Any:
        """Select an artifact from candidates by ID or return latest completed.

        This is the single point where completed-artifact selection happens.
        Callers pass the raw artifact list from ``_list_raw``; the helper
        filters it down to entries matching ``type_code`` with status
        ``COMPLETED`` before applying the explicit-ID or latest-timestamp
        rules.

        Note on the length guard: the filter only requires ``len(a) > 4`` —
        the minimum needed to read ``a[2]`` (type) and ``a[4]`` (status). The
        old inline filters in ``download_report`` and ``download_data_table``
        used stricter length checks (``> 7`` / ``> 18``). A completed-but-too-
        short artifact now passes this filter and surfaces as
        ``ArtifactParseError`` from the downstream extractor instead of
        ``ArtifactNotReadyError`` from the candidate filter. In practice the
        API returns consistent structures, and downstream paths already wrap
        ``IndexError``/``TypeError`` into ``ArtifactParseError``.

        Args:
            candidates: Raw artifact list (typically from ``_list_raw``).
            artifact_id: Specific artifact ID to select, or None for latest.
            type_name: Display name (e.g., "Audio", "Slide deck"). Used for
                the explicit-id-miss error key — lowercased with spaces turned
                into underscores (e.g., "Slide deck" -> "slide_deck").
            no_result_error_key: Error key used when no candidate survives
                filtering. Most callers pass ``type_name.lower()`` but some
                (e.g. ``download_video``) intentionally pass a distinct key
                (``"video_overview"``) to preserve historical exception keys.
                Named ``no_result_error_key`` (rather than something like
                ``type_name_lower``) because it is not in general the
                lowercase of ``type_name`` — see ``download_video``.
            type_code: ArtifactTypeCode used to filter candidates by type.

        Returns:
            Selected artifact data.

        Raises:
            ArtifactNotReadyError: If artifact not found or no candidates
                available after filtering.
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
        """Parse generation API result into GenerationStatus."""
        return self._generation._parse_generation_result(
            result,
            method_id=method_id,
            source=source,
        )

    @staticmethod
    def _extract_artifact_error(art: builtins.list[Any]) -> str | None:
        """Try to extract a human-readable error from a failed artifact.

        Google's batchexecute responses embed error information in varying
        positions depending on the artifact type.  This method walks through
        known locations and returns the first non-empty string it finds.

        Known error locations (reverse-engineered):
        - art[3]: Sometimes contains an error reason string.
        - art[5]: May contain a nested error payload similar to the
          UserDisplayableError structure in RPC responses.

        Args:
            art: Raw artifact data from ``_list_raw()``.

        Returns:
            A human-readable error string, or ``None`` if no error detail
            could be extracted.
        """
        try:
            # art[3] — simple string error reason
            if len(art) > 3 and isinstance(art[3], str) and art[3].strip():
                return art[3].strip()

            # art[5] — nested structure that may contain error text.
            # NOTE: This position is protocol-dependent and was
            # reverse-engineered; it may change without notice.
            if len(art) > 5 and isinstance(art[5], list):
                logger.debug(
                    "Falling back to art[5] for error extraction (art[3]=%r)",
                    art[3] if len(art) > 3 else "<missing>",
                )
                # Walk the list looking for the first non-empty string
                for item in art[5]:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
                    if isinstance(item, list):
                        for sub in item:
                            if isinstance(sub, str) and sub.strip():
                                return sub.strip()

            return None
        except Exception:
            logger.warning(
                "Failed to extract error from artifact data: %r",
                art[:6] if len(art) > 6 else art,
                exc_info=True,
            )
            return None

    def _get_artifact_type_name(self, artifact_type: int) -> str:
        """Get human-readable name for an artifact type.

        Args:
            artifact_type: The ArtifactTypeCode enum value.

        Returns:
            The enum name if valid, otherwise the raw integer as string.
        """
        try:
            return ArtifactTypeCode(artifact_type).name
        except ValueError:
            return str(artifact_type)

    def _is_media_ready(self, art: builtins.list[Any], artifact_type: int) -> bool:
        """Check if media artifact has URLs populated.

        For media artifacts (audio, video, infographic, slide deck), the API may
        set status=COMPLETED before the actual media URLs are populated. This
        method verifies that URLs are available for download.

        Artifact array structure (from BATCHEXECUTE responses):
        - art[0]: artifact_id
        - art[2]: artifact_type (ArtifactTypeCode enum value)
        - art[4]: status_code (ArtifactStatus enum value)
        - art[6][5]: audio media URL list
        - art[8][i][0][0]: video media URL string (within nested variants and entries)
        - art[16][3]: slide deck PDF URL

        Args:
            art: Raw artifact data from _list_raw().
            artifact_type: The ArtifactTypeCode enum value.

        Returns:
            True if media URLs are available, or if artifact is non-media type.
            Returns True on unexpected structure (defensive fallback).
        """
        try:
            if artifact_type in _MEDIA_ARTIFACT_TYPES:
                return _extract_artifact_url(art, artifact_type) is not None

            # Non-media artifacts (Report, Quiz, Flashcard, Data Table, Mind Map):
            # Status code alone is sufficient for these types
            return True

        except (IndexError, TypeError) as e:
            # Defensive: if structure is unexpected, be conservative for media types
            # Media types need URLs, so return False to continue polling
            # Non-media types only need status code, so return True
            is_media = artifact_type in _MEDIA_ARTIFACT_TYPES
            logger.debug(
                "Unexpected artifact structure for type %s (media=%s): %s",
                artifact_type,
                is_media,
                e,
            )
            return not is_media  # False for media (continue polling), True for non-media
