"""Artifacts API for NotebookLM studio content.

Provides operations for generating, listing, downloading, and managing
AI-generated artifacts including Audio Overviews, Video Overviews, Reports,
Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.

This module serves as the public facade, delegating to:
- _artifact_generate.py for generation operations
- _artifact_download.py for download operations
"""

import asyncio
import builtins
import logging
from typing import TYPE_CHECKING, Any

import httpx

from ._artifact_download import ArtifactDownloader
from ._artifact_generate import ArtifactGenerator
from ._core import ClientCore
from .rpc import (
    ArtifactStatus,
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    ExportType,
    InfographicDetail,
    InfographicOrientation,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    RPCError,
    RPCMethod,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
    artifact_status_to_str,
)
from .types import (
    Artifact,
    ArtifactType,
    GenerationStatus,
    ReportSuggestion,
)

logger = logging.getLogger(__name__)

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
    from ._notes import NotesAPI


class ArtifactsAPI:
    """Operations on NotebookLM artifacts (studio content).

    Artifacts are AI-generated content including Audio Overviews, Video Overviews,
    Reports, Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            # Generate
            status = await client.artifacts.generate_audio(notebook_id)
            await client.artifacts.wait_for_completion(notebook_id, status.task_id)

            # Download
            await client.artifacts.download_audio(notebook_id, "output.mp4")

            # List and manage
            artifacts = await client.artifacts.list(notebook_id)
            await client.artifacts.rename(notebook_id, artifact_id, "New Title")
    """

    def __init__(self, core: ClientCore, notes_api: "NotesAPI"):
        """Initialize the artifacts API.

        Args:
            core: The core client infrastructure.
            notes_api: The notes API for accessing notes/mind maps.
        """
        self._core = core
        self._notes = notes_api

        # Initialize sub-components with callback injection
        self._generator = ArtifactGenerator(core, notes_api)
        self._downloader = ArtifactDownloader(
            core=core,
            notes_api=notes_api,
            list_raw_fn=self._list_raw,
            list_quizzes_fn=self.list_quizzes,
            list_flashcards_fn=self.list_flashcards,
        )

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
        artifacts: list[Artifact] = []

        # Fetch studio artifacts (audio, video, reports, etc.)
        params = [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']
        result = await self._core.rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        artifacts_data: list[Any] = []
        if result and isinstance(result, list) and len(result) > 0:
            artifacts_data = result[0] if isinstance(result[0], list) else result

        for art_data in artifacts_data:
            if isinstance(art_data, list) and len(art_data) > 0:
                artifact = Artifact.from_api_response(art_data)
                if artifact_type is None or artifact.kind == artifact_type:
                    artifacts.append(artifact)

        # Fetch mind maps from notes system (if not filtering to non-mind-map type)
        if artifact_type is None or artifact_type == ArtifactType.MIND_MAP:
            try:
                mind_maps = await self._notes.list_mind_maps(notebook_id)
                for mm_data in mind_maps:
                    mind_map_artifact = Artifact.from_mind_map(mm_data)
                    if mind_map_artifact is not None:  # None means deleted (status=2)
                        if artifact_type is None or mind_map_artifact.kind == artifact_type:
                            artifacts.append(mind_map_artifact)
            except (RPCError, httpx.HTTPError) as e:
                # Network/API errors - log and continue with studio artifacts
                logger.warning("Failed to fetch mind maps: %s", e)

        return artifacts

    async def get(self, notebook_id: str, artifact_id: str) -> Artifact | None:
        """Get a specific artifact by ID.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID.

        Returns:
            Artifact object, or None if not found.
        """
        logger.debug("Getting artifact %s from notebook %s", artifact_id, notebook_id)
        artifacts = await self.list(notebook_id)
        for artifact in artifacts:
            if artifact.id == artifact_id:
                return artifact
        return None

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
    # Generate Operations (delegated to ArtifactGenerator)
    # =========================================================================

    async def generate_audio(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str = "en",
        instructions: str | None = None,
        audio_format: AudioFormat | None = None,
        audio_length: AudioLength | None = None,
    ) -> GenerationStatus:
        """Generate an Audio Overview (podcast).

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions for the podcast hosts.
            audio_format: DEEP_DIVE, BRIEF, CRITIQUE, or DEBATE.
            audio_length: SHORT, DEFAULT, or LONG.

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_audio(
            notebook_id, source_ids, language, instructions, audio_format, audio_length
        )

    async def generate_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str = "en",
        instructions: str | None = None,
        video_format: VideoFormat | None = None,
        video_style: VideoStyle | None = None,
    ) -> GenerationStatus:
        """Generate a Video Overview.

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions for video generation.
            video_format: EXPLAINER or BRIEF.
            video_style: AUTO_SELECT, CLASSIC, WHITEBOARD, etc.

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_video(
            notebook_id, source_ids, language, instructions, video_format, video_style
        )

    async def generate_report(
        self,
        notebook_id: str,
        report_format: ReportFormat = ReportFormat.BRIEFING_DOC,
        source_ids: builtins.list[str] | None = None,
        language: str = "en",
        custom_prompt: str | None = None,
    ) -> GenerationStatus:
        """Generate a report artifact.

        Args:
            notebook_id: The notebook ID.
            report_format: BRIEFING_DOC, STUDY_GUIDE, BLOG_POST, or CUSTOM.
            source_ids: Source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            custom_prompt: Required for CUSTOM format.

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_report(
            notebook_id, report_format, source_ids, language, custom_prompt
        )

    async def generate_study_guide(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str = "en",
    ) -> GenerationStatus:
        """Generate a study guide report.

        Convenience method wrapping generate_report with STUDY_GUIDE format.

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.
            language: Language code (default: "en").

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_study_guide(notebook_id, source_ids, language)

    async def generate_quiz(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        """Generate a quiz.

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.
            instructions: Custom instructions for quiz generation.
            quantity: FEWER, STANDARD, or MORE questions.
            difficulty: EASY, MEDIUM, or HARD.

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_quiz(
            notebook_id, source_ids, instructions, quantity, difficulty
        )

    async def generate_flashcards(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        """Generate flashcards.

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.
            instructions: Custom instructions for flashcard generation.
            quantity: FEWER, STANDARD, or MORE cards.
            difficulty: EASY, MEDIUM, or HARD.

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_flashcards(
            notebook_id, source_ids, instructions, quantity, difficulty
        )

    async def generate_infographic(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str = "en",
        instructions: str | None = None,
        orientation: InfographicOrientation | None = None,
        detail_level: InfographicDetail | None = None,
    ) -> GenerationStatus:
        """Generate an infographic.

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions for infographic generation.
            orientation: LANDSCAPE, PORTRAIT, or SQUARE.
            detail_level: CONCISE, STANDARD, or DETAILED.

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_infographic(
            notebook_id, source_ids, language, instructions, orientation, detail_level
        )

    async def generate_slide_deck(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str = "en",
        instructions: str | None = None,
        slide_format: SlideDeckFormat | None = None,
        slide_length: SlideDeckLength | None = None,
    ) -> GenerationStatus:
        """Generate a slide deck.

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions for slide deck generation.
            slide_format: DETAILED_DECK or PRESENTER_SLIDES.
            slide_length: DEFAULT or SHORT.

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_slide_deck(
            notebook_id, source_ids, language, instructions, slide_format, slide_length
        )

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str = "en",
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a data table.

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Description of desired table structure.

        Returns:
            GenerationStatus with task_id for polling.
        """
        return await self._generator.generate_data_table(
            notebook_id, source_ids, language, instructions
        )

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Generate an interactive mind map.

        The mind map is generated and saved as a note in the notebook.
        It will appear in artifact listings with type MIND_MAP (5).

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.

        Returns:
            Dictionary with 'mind_map' (JSON data) and 'note_id'.
        """
        return await self._generator.generate_mind_map(notebook_id, source_ids)

    # =========================================================================
    # Download Operations (delegated to ArtifactDownloader)
    # =========================================================================

    async def download_audio(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Audio Overview to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the audio file (MP4/MP3).
            artifact_id: Specific artifact ID, or uses first completed audio.

        Returns:
            The output path.
        """
        return await self._downloader.download_audio(notebook_id, output_path, artifact_id)

    async def download_video(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download a Video Overview to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the video file (MP4).
            artifact_id: Specific artifact ID, or uses first completed video.

        Returns:
            The output path.
        """
        return await self._downloader.download_video(notebook_id, output_path, artifact_id)

    async def download_infographic(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Infographic to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the image file (PNG).
            artifact_id: Specific artifact ID, or uses first completed infographic.

        Returns:
            The output path.
        """
        return await self._downloader.download_infographic(notebook_id, output_path, artifact_id)

    async def download_slide_deck(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download a slide deck as a PDF file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the PDF file.
            artifact_id: Specific artifact ID, or uses first completed slide deck.

        Returns:
            The output path.
        """
        return await self._downloader.download_slide_deck(notebook_id, output_path, artifact_id)

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a report artifact as markdown.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the markdown file.
            artifact_id: Specific artifact ID, or uses first completed report.

        Returns:
            The output path where the file was saved.
        """
        return await self._downloader.download_report(notebook_id, output_path, artifact_id)

    async def download_mind_map(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a mind map as JSON.

        Mind maps are stored in the notes system, not the regular artifacts list.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the JSON file.
            artifact_id: Specific mind map ID (note ID), or uses first available.

        Returns:
            The output path where the file was saved.
        """
        return await self._downloader.download_mind_map(notebook_id, output_path, artifact_id)

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a data table as CSV.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the CSV file.
            artifact_id: Specific artifact ID, or uses first completed data table.

        Returns:
            The output path where the file was saved.
        """
        return await self._downloader.download_data_table(notebook_id, output_path, artifact_id)

    async def download_quiz(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download quiz questions.

        Args:
            notebook_id: Notebook ID.
            output_path: Output file path.
            artifact_id: Specific quiz artifact ID (optional).
            output_format: Output format - json, markdown, or html.

        Returns:
            Path to downloaded file.

        Raises:
            ValueError: If no completed quiz artifact found.
        """
        return await self._downloader.download_quiz(
            notebook_id, output_path, artifact_id, output_format
        )

    async def download_flashcards(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download flashcard deck.

        Args:
            notebook_id: Notebook ID.
            output_path: Output file path.
            artifact_id: Specific flashcard artifact ID (optional).
            output_format: Output format - json, markdown, or html.

        Returns:
            Path to downloaded file.

        Raises:
            ValueError: If no completed flashcard artifact found.
        """
        return await self._downloader.download_flashcards(
            notebook_id, output_path, artifact_id, output_format
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

    # =========================================================================
    # Polling Operations
    # =========================================================================

    async def poll_status(self, notebook_id: str, task_id: str) -> GenerationStatus:
        """Poll the status of a generation task.

        Args:
            notebook_id: The notebook ID.
            task_id: The task/artifact ID to check.

        Returns:
            GenerationStatus with current status.
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
                return GenerationStatus(task_id=task_id, status=status)

        return GenerationStatus(task_id=task_id, status="pending")

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
    ) -> GenerationStatus:
        """Wait for a generation task to complete.

        Uses exponential backoff for polling to reduce API load.

        Args:
            notebook_id: The notebook ID.
            task_id: The task/artifact ID to wait for.
            initial_interval: Initial seconds between status checks.
            max_interval: Maximum seconds between status checks.
            timeout: Maximum seconds to wait.

        Returns:
            Final GenerationStatus.

        Raises:
            TimeoutError: If task doesn't complete within timeout.
        """
        start_time = asyncio.get_running_loop().time()
        current_interval = initial_interval

        while True:
            status = await self.poll_status(notebook_id, task_id)

            if status.is_complete or status.is_failed:
                return status

            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Task {task_id} timed out after {timeout}s")

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
        """Get AI-suggested report formats for a notebook.

        Args:
            notebook_id: The notebook ID.

        Returns:
            List of ReportSuggestion objects.
        """
        params = [[2], notebook_id]

        result = await self._core.rpc_call(
            RPCMethod.GET_SUGGESTED_REPORTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        suggestions = []
        # Response format: [[[title, description, null, null, prompt, audience_level], ...]]
        if result and isinstance(result, list) and len(result) > 0:
            items = result[0] if isinstance(result[0], list) else result
            for item in items:
                if isinstance(item, list) and len(item) >= 5:
                    suggestions.append(
                        ReportSuggestion(
                            title=item[0] if isinstance(item[0], str) else "",
                            description=item[1] if isinstance(item[1], str) else "",
                            prompt=item[4] if isinstance(item[4], str) else "",
                            audience_level=item[5] if len(item) > 5 else 2,
                        )
                    )

        return suggestions

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _list_raw(self, notebook_id: str) -> builtins.list[Any]:
        """Get raw artifact list data."""
        params = [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']
        result = await self._core.rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if result and isinstance(result, list) and len(result) > 0:
            return result[0] if isinstance(result[0], list) else result
        return []

    def _get_artifact_type_name(self, artifact_type: int) -> str:
        """Get human-readable name for an artifact type."""
        try:
            return ArtifactTypeCode(artifact_type).name
        except ValueError:
            return str(artifact_type)

    def _is_valid_media_url(self, value: Any) -> bool:
        """Check if value is a valid HTTP(S) URL."""
        return isinstance(value, str) and value.startswith(("http://", "https://"))

    def _find_infographic_url(self, art: builtins.list[Any]) -> str | None:
        """Extract infographic image URL from artifact data."""
        for item in reversed(art):
            if not isinstance(item, list) or len(item) <= 2:
                continue
            content = item[2]
            if not isinstance(content, list) or len(content) == 0:
                continue
            first_content = content[0]
            if not isinstance(first_content, list) or len(first_content) <= 1:
                continue
            img_data = first_content[1]
            if isinstance(img_data, list) and len(img_data) > 0:
                url = img_data[0]
                if self._is_valid_media_url(url):
                    return url
        return None

    def _is_media_ready(self, art: builtins.list[Any], artifact_type: int) -> bool:
        """Check if media artifact has URLs populated.

        For media artifacts (audio, video, infographic, slide deck), the API may
        set status=COMPLETED before the actual media URLs are populated. This
        method verifies that URLs are available for download.
        """
        try:
            if artifact_type == ArtifactTypeCode.AUDIO.value:
                # Audio URL is at art[6][5]
                if len(art) > 6 and isinstance(art[6], list) and len(art[6]) > 5:
                    media_list = art[6][5]
                    if isinstance(media_list, list) and len(media_list) > 0:
                        first_item = media_list[0]
                        if isinstance(first_item, list) and len(first_item) > 0:
                            return self._is_valid_media_url(first_item[0])
                return False

            elif artifact_type == ArtifactTypeCode.VIDEO.value:
                # Video URLs are in art[8]
                if len(art) > 8 and isinstance(art[8], list):
                    return any(
                        self._is_valid_media_url(item[0])
                        for item in art[8]
                        if isinstance(item, list) and len(item) > 0
                    )
                return False

            elif artifact_type == ArtifactTypeCode.INFOGRAPHIC.value:
                return self._find_infographic_url(art) is not None

            elif artifact_type == ArtifactTypeCode.SLIDE_DECK.value:
                # Slide deck PDF URL is at art[16][3]
                return (
                    len(art) > 16
                    and isinstance(art[16], list)
                    and len(art[16]) > 3
                    and self._is_valid_media_url(art[16][3])
                )

            # Non-media artifacts: status code alone is sufficient
            return True

        except (IndexError, TypeError) as e:
            # Defensive: if structure is unexpected, be conservative for media types
            is_media = artifact_type in _MEDIA_ARTIFACT_TYPES
            logger.debug(
                "Unexpected artifact structure for type %s (media=%s): %s",
                artifact_type,
                is_media,
                e,
            )
            return not is_media
