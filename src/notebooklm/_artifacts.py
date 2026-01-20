"""Artifacts API for NotebookLM studio content.

Provides operations for generating, listing, downloading, and managing
AI-generated artifacts including Audio Overviews, Video Overviews, Reports,
Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import asyncio
import builtins
import csv
import html
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._core import ClientCore
from .auth import load_httpx_cookies
from .rpc import (
    ArtifactStatus,
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
    StudioContentType,
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
    GenerationStatus,
    ReportSuggestion,
)

logger = logging.getLogger(__name__)

# Media artifact types that require URL availability before reporting completion
_MEDIA_ARTIFACT_TYPES = frozenset(
    {
        StudioContentType.AUDIO.value,
        StudioContentType.VIDEO.value,
        StudioContentType.INFOGRAPHIC.value,
        StudioContentType.SLIDE_DECK.value,
    }
)

if TYPE_CHECKING:
    from ._notes import NotesAPI


def _extract_app_data(html_content: str) -> dict:
    """Extract JSON from data-app-data HTML attribute.

    The quiz/flashcard HTML embeds JSON in a data-app-data attribute
    with HTML-encoded content (e.g., &quot; for quotes).
    """
    match = re.search(r'data-app-data="([^"]+)"', html_content)
    if not match:
        raise ArtifactParseError(
            "quiz/flashcard",
            details="No data-app-data attribute found in HTML",
        )

    encoded_json = match.group(1)
    decoded_json = html.unescape(encoded_json)
    return json.loads(decoded_json)


def _format_quiz_markdown(title: str, questions: list[dict]) -> str:
    """Format quiz as markdown."""
    lines = [f"# {title}", ""]
    for i, q in enumerate(questions, 1):
        lines.append(f"## Question {i}")
        lines.append(q.get("question", ""))
        lines.append("")
        for opt in q.get("answerOptions", []):
            marker = "[x]" if opt.get("isCorrect") else "[ ]"
            lines.append(f"- {marker} {opt.get('text', '')}")
        if q.get("hint"):
            lines.append("")
            lines.append(f"**Hint:** {q['hint']}")
        lines.append("")
    return "\n".join(lines)


def _format_flashcards_markdown(title: str, cards: list[dict]) -> str:
    """Format flashcards as markdown."""
    lines = [f"# {title}", ""]
    for i, card in enumerate(cards, 1):
        front = card.get("f", "")
        back = card.get("b", "")
        lines.extend(
            [
                f"## Card {i}",
                "",
                f"**Q:** {front}",
                "",
                f"**A:** {back}",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines)


def _extract_cell_text(cell: Any) -> str:
    """Recursively extract text from a nested cell structure.

    Data table cells have deeply nested arrays with position markers (integers)
    and text content (strings). This function traverses the structure and
    concatenates all text fragments found.
    """
    if isinstance(cell, str):
        return cell
    if isinstance(cell, int):
        return ""
    if isinstance(cell, list):
        return "".join(text for item in cell if (text := _extract_cell_text(item)))
    return ""


def _parse_data_table(raw_data: list) -> tuple[list[str], list[list[str]]]:
    """Parse rich-text data table into headers and rows.

    Data tables from NotebookLM have a complex nested structure with position
    markers. This function navigates to the rows array and extracts text from
    each cell.

    Structure: raw_data[0][0][0][0][4][2] contains the rows array where:
    - [0][0][0][0] navigates through wrapper layers
    - [4] contains the table content section [type, flags, rows_array]
    - [2] is the actual rows array

    Each row has format: [start_pos, end_pos, [cell_array]]
    Each cell is deeply nested: [pos, pos, [[pos, pos, [[pos, pos, [["text"]]]]]]]

    Returns:
        Tuple of (headers, rows) where headers is a list of column names
        and rows is a list of row data (each row is a list of cell strings).

    Raises:
        ArtifactParseError: If the data structure cannot be parsed or is empty.
    """
    try:
        # Navigate through nested wrappers to reach the rows array
        rows_array = raw_data[0][0][0][0][4][2]
        if not rows_array:
            raise ArtifactParseError("data_table", details="Empty data table")

        headers: list[str] = []
        rows: list[list[str]] = []

        for i, row_section in enumerate(rows_array):
            # Each row_section is [start_pos, end_pos, cell_array]
            if not isinstance(row_section, list) or len(row_section) < 3:
                continue

            cell_array = row_section[2]
            if not isinstance(cell_array, list):
                continue

            row_values = [_extract_cell_text(cell) for cell in cell_array]

            if i == 0:
                headers = row_values
            else:
                rows.append(row_values)

        # Validate we extracted usable data
        if not headers:
            raise ArtifactParseError(
                "data_table",
                details="Failed to extract headers from data table",
            )

        return headers, rows

    except (IndexError, TypeError, KeyError) as e:
        raise ArtifactParseError(
            "data_table",
            details=f"Failed to parse data table structure: {e}",
            cause=e,
        ) from e


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

    # =========================================================================
    # List/Get Operations
    # =========================================================================

    async def list(self, notebook_id: str, artifact_type: int | None = None) -> list[Artifact]:
        """List all artifacts in a notebook, including mind maps.

        This returns all AI-generated content: Audio Overviews, Video Overviews,
        Reports, Quizzes, Flashcards, Infographics, Slide Decks, Data Tables,
        and Mind Maps.

        Note: Mind maps are stored in a separate system (notes) but are included
        here since they are AI-generated studio content.

        Args:
            notebook_id: The notebook ID.
            artifact_type: Optional StudioContentType value to filter by.
                Use StudioContentType.MIND_MAP (5) to get only mind maps.

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
                if artifact_type is None or artifact._artifact_type == artifact_type:
                    artifacts.append(artifact)

        # Fetch mind maps from notes system (if not filtering to non-mind-map type)
        if artifact_type is None or artifact_type == StudioContentType.MIND_MAP.value:
            try:
                mind_maps = await self._notes.list_mind_maps(notebook_id)
                for mm_data in mind_maps:
                    mind_map_artifact = Artifact.from_mind_map(mm_data)
                    if mind_map_artifact is not None:  # None means deleted (status=2)
                        if (
                            artifact_type is None
                            or mind_map_artifact._artifact_type == artifact_type
                        ):
                            artifacts.append(mind_map_artifact)
            except (RPCError, httpx.HTTPError) as e:
                # Network/API errors - log and continue with studio artifacts
                # This ensures users can see their audio/video/reports even if
                # the mind maps endpoint is temporarily unavailable
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
        return await self.list(notebook_id, StudioContentType.AUDIO.value)

    async def list_video(self, notebook_id: str) -> builtins.list[Artifact]:
        """List video overview artifacts."""
        return await self.list(notebook_id, StudioContentType.VIDEO.value)

    async def list_reports(self, notebook_id: str) -> builtins.list[Artifact]:
        """List report artifacts (Briefing Doc, Study Guide, Blog Post)."""
        return await self.list(notebook_id, StudioContentType.REPORT.value)

    async def list_quizzes(self, notebook_id: str) -> builtins.list[Artifact]:
        """List quiz artifacts (type 4 with variant 2)."""
        all_type4 = await self.list(notebook_id, StudioContentType.QUIZ_FLASHCARD.value)
        return [a for a in all_type4 if a.is_quiz]

    async def list_flashcards(self, notebook_id: str) -> builtins.list[Artifact]:
        """List flashcard artifacts (type 4 with variant 1)."""
        all_type4 = await self.list(notebook_id, StudioContentType.QUIZ_FLASHCARD.value)
        return [a for a in all_type4 if a.is_flashcards]

    async def list_infographics(self, notebook_id: str) -> builtins.list[Artifact]:
        """List infographic artifacts."""
        return await self.list(notebook_id, StudioContentType.INFOGRAPHIC.value)

    async def list_slide_decks(self, notebook_id: str) -> builtins.list[Artifact]:
        """List slide deck artifacts."""
        return await self.list(notebook_id, StudioContentType.SLIDE_DECK.value)

    async def list_data_tables(self, notebook_id: str) -> builtins.list[Artifact]:
        """List data table artifacts."""
        return await self.list(notebook_id, StudioContentType.DATA_TABLE.value)

    # =========================================================================
    # Generate Operations
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
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        source_ids_double = [[sid] for sid in source_ids] if source_ids else []

        format_code = audio_format.value if audio_format else None
        length_code = audio_length.value if audio_length else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                1,  # StudioContentType.AUDIO
                source_ids_triple,
                None,
                None,
                [
                    None,
                    [
                        instructions,
                        length_code,
                        None,
                        source_ids_double,
                        language,
                        None,
                        format_code,
                    ],
                ],
            ],
        ]
        return await self._call_generate(notebook_id, params)

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
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        source_ids_double = [[sid] for sid in source_ids] if source_ids else []

        format_code = video_format.value if video_format else None
        style_code = video_style.value if video_style else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                3,  # StudioContentType.VIDEO
                source_ids_triple,
                None,
                None,
                None,
                None,
                [
                    None,
                    None,
                    [
                        source_ids_double,
                        language,
                        instructions,
                        None,
                        format_code,
                        style_code,
                    ],
                ],
            ],
        ]
        return await self._call_generate(notebook_id, params)

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
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        format_configs = {
            ReportFormat.BRIEFING_DOC: {
                "title": "Briefing Doc",
                "description": "Key insights and important quotes",
                "prompt": (
                    "Create a comprehensive briefing document that includes an "
                    "Executive Summary, detailed analysis of key themes, important "
                    "quotes with context, and actionable insights."
                ),
            },
            ReportFormat.STUDY_GUIDE: {
                "title": "Study Guide",
                "description": "Short-answer quiz, essay questions, glossary",
                "prompt": (
                    "Create a comprehensive study guide that includes key concepts, "
                    "short-answer practice questions, essay prompts for deeper "
                    "exploration, and a glossary of important terms."
                ),
            },
            ReportFormat.BLOG_POST: {
                "title": "Blog Post",
                "description": "Insightful takeaways in readable article format",
                "prompt": (
                    "Write an engaging blog post that presents the key insights "
                    "in an accessible, reader-friendly format. Include an attention-"
                    "grabbing introduction, well-organized sections, and a compelling "
                    "conclusion with takeaways."
                ),
            },
            ReportFormat.CUSTOM: {
                "title": "Custom Report",
                "description": "Custom format",
                "prompt": custom_prompt or "Create a report based on the provided sources.",
            },
        }

        config = format_configs[report_format]
        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        source_ids_double = [[sid] for sid in source_ids] if source_ids else []

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                2,  # StudioContentType.REPORT
                source_ids_triple,
                None,
                None,
                None,
                [
                    None,
                    [
                        config["title"],
                        config["description"],
                        None,
                        source_ids_double,
                        language,
                        config["prompt"],
                        None,
                        True,
                    ],
                ],
            ],
        ]
        return await self._call_generate(notebook_id, params)

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
        return await self.generate_report(
            notebook_id,
            report_format=ReportFormat.STUDY_GUIDE,
            source_ids=source_ids,
            language=language,
        )

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
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        quantity_code = quantity.value if quantity else None
        difficulty_code = difficulty.value if difficulty else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                4,  # StudioContentType.QUIZ_FLASHCARD
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                [
                    None,
                    [
                        2,  # Variant: quiz
                        None,
                        instructions,
                        None,
                        None,
                        None,
                        None,
                        [quantity_code, difficulty_code],
                    ],
                ],
            ],
        ]
        return await self._call_generate(notebook_id, params)

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
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        quantity_code = quantity.value if quantity else None
        difficulty_code = difficulty.value if difficulty else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                4,  # StudioContentType.QUIZ_FLASHCARD
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                [
                    None,
                    [
                        1,  # Variant: flashcards
                        None,
                        instructions,
                        None,
                        None,
                        None,
                        [difficulty_code, quantity_code],
                    ],
                ],
            ],
        ]
        return await self._call_generate(notebook_id, params)

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
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        orientation_code = orientation.value if orientation else None
        detail_code = detail_level.value if detail_level else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                7,  # StudioContentType.INFOGRAPHIC
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [[instructions, language, None, orientation_code, detail_code]],
            ],
        ]
        return await self._call_generate(notebook_id, params)

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
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        format_code = slide_format.value if slide_format else None
        length_code = slide_length.value if slide_length else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                8,  # StudioContentType.SLIDE_DECK
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [[instructions, language, format_code, length_code]],
            ],
        ]
        return await self._call_generate(notebook_id, params)

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
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                9,  # StudioContentType.DATA_TABLE
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [None, [instructions, language]],
            ],
        ]
        return await self._call_generate(notebook_id, params)

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
        import json as json_module

        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_nested = [[[sid]] for sid in source_ids] if source_ids else []

        params = [
            source_ids_nested,
            None,
            None,
            None,
            None,
            ["interactive_mindmap", [["[CONTEXT]", ""]], ""],
            None,
            [2, None, [1]],
        ]

        result = await self._core.rpc_call(
            RPCMethod.ACT_ON_SOURCES,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if result and isinstance(result, list) and len(result) > 0:
            inner = result[0]
            if isinstance(inner, list) and len(inner) > 0:
                mind_map_json = inner[0]

                # Parse the mind map JSON
                if isinstance(mind_map_json, str):
                    try:
                        mind_map_data = json_module.loads(mind_map_json)
                    except json_module.JSONDecodeError:
                        mind_map_data = mind_map_json
                        mind_map_json = str(mind_map_json)
                else:
                    mind_map_data = mind_map_json
                    mind_map_json = json_module.dumps(mind_map_json)

                # Extract title from mind map data
                title = "Mind Map"
                if isinstance(mind_map_data, dict) and "name" in mind_map_data:
                    title = mind_map_data["name"]

                # The ACT_ON_SOURCES RPC generates content but does NOT persist it.
                # We must explicitly create a note to save the mind map.
                note = await self._notes.create(notebook_id, title=title, content=mind_map_json)
                note_id = note.id if note else None

                return {
                    "mind_map": mind_map_data,
                    "note_id": note_id,
                }

        return {"mind_map": None, "note_id": None}

    # =========================================================================
    # Download Operations
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
        artifacts_data = await self._list_raw(notebook_id)

        # Filter for completed audio artifacts
        audio_candidates = [
            a
            for a in artifacts_data
            if isinstance(a, list)
            and len(a) > 4
            and a[2] == StudioContentType.AUDIO
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            audio_art = next((a for a in audio_candidates if a[0] == artifact_id), None)
            if not audio_art:
                raise ArtifactNotReadyError("audio", artifact_id=artifact_id)
        else:
            audio_art = audio_candidates[0] if audio_candidates else None

        if not audio_art:
            raise ArtifactNotReadyError("audio")

        # Extract URL from metadata[6][5]
        try:
            metadata = audio_art[6]
            if not isinstance(metadata, list) or len(metadata) <= 5:
                raise ArtifactParseError(
                    "audio",
                    artifact_id=artifact_id,
                    details="Invalid audio metadata structure",
                )

            media_list = metadata[5]
            if not isinstance(media_list, list) or len(media_list) == 0:
                raise ArtifactParseError(
                    "audio",
                    artifact_id=artifact_id,
                    details="No media URLs found",
                )

            url = None
            for item in media_list:
                if isinstance(item, list) and len(item) > 2 and item[2] == "audio/mp4":
                    url = item[0]
                    break

            if not url and len(media_list) > 0 and isinstance(media_list[0], list):
                url = media_list[0][0]

            if not url:
                raise ArtifactDownloadError(
                    "audio",
                    artifact_id=artifact_id,
                    details="Could not extract download URL",
                )

            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "audio",
                artifact_id=artifact_id,
                details=f"Failed to parse audio artifact structure: {e}",
                cause=e,
            ) from e

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
        artifacts_data = await self._list_raw(notebook_id)

        # Filter for completed video artifacts
        video_candidates = [
            a
            for a in artifacts_data
            if isinstance(a, list)
            and len(a) > 4
            and a[2] == StudioContentType.VIDEO
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            video_art = next((v for v in video_candidates if v[0] == artifact_id), None)
            if not video_art:
                raise ArtifactNotReadyError("video", artifact_id=artifact_id)
        else:
            video_art = video_candidates[0] if video_candidates else None

        if not video_art:
            raise ArtifactNotReadyError("video_overview")

        # Extract URL from metadata[8]
        try:
            if len(video_art) <= 8:
                raise ArtifactParseError("video_artifact", details="Invalid structure")

            metadata = video_art[8]
            if not isinstance(metadata, list):
                raise ArtifactParseError("video_metadata", details="Invalid structure")

            media_list = None
            for item in metadata:
                if (
                    isinstance(item, list)
                    and len(item) > 0
                    and isinstance(item[0], list)
                    and len(item[0]) > 0
                    and isinstance(item[0][0], str)
                    and item[0][0].startswith("http")
                ):
                    media_list = item
                    break

            if not media_list:
                raise ArtifactParseError("media", details="No media URLs found")

            url = None
            for item in media_list:
                if isinstance(item, list) and len(item) > 2 and item[2] == "video/mp4":
                    url = item[0]
                    if item[1] == 4:
                        break

            if not url and len(media_list) > 0:
                url = media_list[0][0]

            if not url:
                raise ArtifactDownloadError("media", details="Could not extract download URL")

            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "video_artifact", details=f"Failed to parse structure: {e}", cause=e
            ) from e

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
        artifacts_data = await self._list_raw(notebook_id)

        # Filter for completed infographic artifacts
        info_candidates = [
            a
            for a in artifacts_data
            if isinstance(a, list)
            and len(a) > 4
            and a[2] == StudioContentType.INFOGRAPHIC
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            info_art = next((i for i in info_candidates if i[0] == artifact_id), None)
            if not info_art:
                raise ArtifactNotReadyError("infographic", artifact_id=artifact_id)
        else:
            info_art = info_candidates[0] if info_candidates else None

        if not info_art:
            raise ArtifactNotReadyError("infographic")

        # Extract URL from metadata
        try:
            metadata = None
            for item in reversed(info_art):
                if isinstance(item, list) and len(item) > 0 and isinstance(item[0], list):
                    if len(item) > 2 and isinstance(item[2], list) and len(item[2]) > 0:
                        content_list = item[2]
                        if isinstance(content_list[0], list) and len(content_list[0]) > 1:
                            img_data = content_list[0][1]
                            if (
                                isinstance(img_data, list)
                                and len(img_data) > 0
                                and isinstance(img_data[0], str)
                                and img_data[0].startswith("http")
                            ):
                                metadata = item
                                break

            if not metadata:
                raise ArtifactParseError("infographic", details="Could not find metadata")

            url = metadata[2][0][1][0]
            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "infographic", details=f"Failed to parse structure: {e}", cause=e
            ) from e

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
        artifacts_data = await self._list_raw(notebook_id)

        # Filter for completed slide deck artifacts
        slide_candidates = [
            a
            for a in artifacts_data
            if isinstance(a, list)
            and len(a) > 4
            and a[2] == StudioContentType.SLIDE_DECK
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            slide_art = next((s for s in slide_candidates if s[0] == artifact_id), None)
            if not slide_art:
                raise ArtifactNotReadyError("slide_deck", artifact_id=artifact_id)
        else:
            slide_art = slide_candidates[0] if slide_candidates else None

        if not slide_art:
            raise ArtifactNotReadyError("slide_deck")

        # Extract PDF URL from metadata at index 16, position 3
        # Structure: artifact[16] = [config, title, slides_list, pdf_url]
        try:
            if len(slide_art) <= 16:
                raise ArtifactParseError("slide_deck_artifact", details="Invalid structure")

            metadata = slide_art[16]
            if not isinstance(metadata, list) or len(metadata) < 4:
                raise ArtifactParseError("slide_deck_metadata", details="Invalid structure")

            pdf_url = metadata[3]
            if not isinstance(pdf_url, str) or not pdf_url.startswith("http"):
                raise ArtifactDownloadError("slide_deck", details="Could not find PDF download URL")

            return await self._download_url(pdf_url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "slide_deck", details=f"Failed to parse structure: {e}", cause=e
            ) from e

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
        """Download quiz or flashcard artifact.

        Args:
            notebook_id: Notebook ID.
            output_path: Output file path.
            artifact_id: Specific artifact ID (optional).
            output_format: Output format - json, markdown, or html.
            artifact_type: Either "quiz" or "flashcards".

        Returns:
            Path to downloaded file.

        Raises:
            ValueError: If no completed artifact found or invalid output_format.
        """
        # Validate output format
        valid_formats = ("json", "markdown", "html")
        if output_format not in valid_formats:
            raise ValueError(
                f"Invalid output_format: {output_format!r}. Use one of: {', '.join(valid_formats)}"
            )

        # Type-specific configuration
        is_quiz = artifact_type == "quiz"
        default_title = "Untitled Quiz" if is_quiz else "Untitled Flashcards"

        # Fetch and filter artifacts
        artifacts = (
            await self.list_quizzes(notebook_id)
            if is_quiz
            else await self.list_flashcards(notebook_id)
        )
        completed = [a for a in artifacts if a.is_completed]
        if not completed:
            raise ArtifactNotReadyError(artifact_type)

        # Sort by creation date to ensure we get the latest by default
        completed.sort(key=lambda a: a.created_at.timestamp() if a.created_at else 0, reverse=True)

        # Select artifact
        if artifact_id:
            artifact = next((a for a in completed if a.id == artifact_id), None)
            if not artifact:
                raise ArtifactNotFoundError(artifact_id, artifact_type=artifact_type)
        else:
            artifact = completed[0]

        # Fetch and parse HTML content
        html_content = await self._get_artifact_content(notebook_id, artifact.id)
        if not html_content:
            raise ArtifactDownloadError(artifact_type, details="Failed to fetch content")

        try:
            app_data = _extract_app_data(html_content)
        except (ValueError, json.JSONDecodeError) as e:
            raise ArtifactParseError(
                artifact_type, details=f"Failed to parse content: {e}", cause=e
            ) from e

        # Format output
        title = artifact.title or default_title
        content = self._format_interactive_content(
            app_data, title, output_format, html_content, is_quiz
        )

        # Create parent directories and write file
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        def _write_file() -> None:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(_write_file)
        return output_path

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
        if output_format == "html":
            return html_content

        if is_quiz:
            questions = app_data.get("quiz", [])
            if output_format == "markdown":
                return _format_quiz_markdown(title, questions)
            return json.dumps({"title": title, "questions": questions}, indent=2)

        cards = app_data.get("flashcards", [])
        if output_format == "markdown":
            return _format_flashcards_markdown(title, cards)
        normalized = [{"front": c.get("f", ""), "back": c.get("b", "")} for c in cards]
        return json.dumps({"title": title, "cards": normalized}, indent=2)

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
        artifacts_data = await self._list_raw(notebook_id)

        report_candidates = [
            a
            for a in artifacts_data
            if isinstance(a, list)
            and len(a) > 7
            and a[2] == StudioContentType.REPORT
            and a[4] == ArtifactStatus.COMPLETED
        ]

        report_art = self._select_artifact(report_candidates, artifact_id, "Report", "report")

        try:
            content_wrapper = report_art[7]
            markdown_content = (
                content_wrapper[0]
                if isinstance(content_wrapper, list) and content_wrapper
                else content_wrapper
            )

            if not isinstance(markdown_content, str):
                raise ArtifactParseError("report_content", details="Invalid structure")

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(markdown_content, encoding="utf-8")
            return str(output)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "report", details=f"Failed to parse structure: {e}", cause=e
            ) from e

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
        mind_maps = await self._notes.list_mind_maps(notebook_id)
        if not mind_maps:
            raise ArtifactNotReadyError("mind_map")

        if artifact_id:
            mind_map = next((mm for mm in mind_maps if mm[0] == artifact_id), None)
            if not mind_map:
                raise ArtifactNotFoundError(artifact_id, artifact_type="mind_map")
        else:
            mind_map = mind_maps[0]

        try:
            json_string = mind_map[1][1]
            if not isinstance(json_string, str):
                raise ArtifactParseError("mind_map_content", details="Invalid structure")

            json_data = json.loads(json_string)

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")
            return str(output)

        except (IndexError, TypeError, json.JSONDecodeError) as e:
            raise ArtifactParseError(
                "mind_map", details=f"Failed to parse structure: {e}", cause=e
            ) from e

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
        artifacts_data = await self._list_raw(notebook_id)

        table_candidates = [
            a
            for a in artifacts_data
            if isinstance(a, list)
            and len(a) > 18
            and a[2] == StudioContentType.DATA_TABLE
            and a[4] == ArtifactStatus.COMPLETED
        ]

        table_art = self._select_artifact(table_candidates, artifact_id, "Data table", "data table")

        try:
            raw_data = table_art[18]
            headers, rows = _parse_data_table(raw_data)

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            with output.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)

            return str(output)

        except (IndexError, TypeError, ValueError) as e:
            raise ArtifactParseError(
                "data_table", details=f"Failed to parse structure: {e}", cause=e
            ) from e

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
            RPCMethod.DELETE_STUDIO,
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
            GenerationStatus with current status.
        """
        # POLL_STUDIO RPC is unreliable - use list as fallback
        params = [task_id, notebook_id, [2]]
        result = await self._core.rpc_call(
            RPCMethod.POLL_STUDIO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if result is None:
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

        status = result[1] if len(result) > 1 else "unknown"
        url = result[2] if len(result) > 2 else None
        error = result[3] if len(result) > 3 else None

        return GenerationStatus(task_id=task_id, status=status, url=url, error=error)

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
        poll_interval: float | None = None,  # Deprecated, use initial_interval
    ) -> GenerationStatus:
        """Wait for a generation task to complete.

        Uses exponential backoff for polling to reduce API load.

        Args:
            notebook_id: The notebook ID.
            task_id: The task/artifact ID to wait for.
            initial_interval: Initial seconds between status checks.
            max_interval: Maximum seconds between status checks.
            timeout: Maximum seconds to wait.
            poll_interval: Deprecated. Use initial_interval instead.

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
        source_ids: builtins.list[str] | None = None,
    ) -> builtins.list[ReportSuggestion]:
        """Get AI-suggested report formats for a notebook.

        Args:
            notebook_id: The notebook ID.
            source_ids: Specific source IDs to analyze.

        Returns:
            List of ReportSuggestion objects.
        """
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        source_ids_nested = [[[sid]] for sid in source_ids] if source_ids else []

        params = [
            source_ids_nested,
            None,
            None,
            None,
            None,
            ["suggested_report_formats", [["[CONTEXT]", ""]], ""],
            None,
            [2, None, [1]],
        ]

        result = await self._core.rpc_call(
            RPCMethod.ACT_ON_SOURCES,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        suggestions = []
        if result and isinstance(result, list):
            for item in result:
                if isinstance(item, list) and len(item) >= 5:
                    suggestions.append(
                        ReportSuggestion(
                            title=item[0] if isinstance(item[0], str) else "",
                            description=item[1] if isinstance(item[1], str) else "",
                            prompt=item[4] if len(item) > 4 and isinstance(item[4], str) else "",
                            audience_level=item[5] if len(item) > 5 else 2,
                        )
                    )

        return suggestions

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _call_generate(
        self, notebook_id: str, params: builtins.list[Any]
    ) -> GenerationStatus:
        """Make a generation RPC call with error handling.

        Wraps the RPC call to handle UserDisplayableError (rate limiting/quota)
        and convert to appropriate GenerationStatus.

        Args:
            notebook_id: The notebook ID.
            params: RPC parameters for the generation call.

        Returns:
            GenerationStatus with task_id on success, or error info on failure.
        """
        # Extract artifact type from params for logging
        artifact_type = params[2][2] if len(params) > 2 and len(params[2]) > 2 else "unknown"
        logger.debug("Generating artifact type=%s in notebook %s", artifact_type, notebook_id)
        try:
            result = await self._core.rpc_call(
                RPCMethod.CREATE_VIDEO,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
            return self._parse_generation_result(result)
        except RPCError as e:
            if e.code == "USER_DISPLAYABLE_ERROR":
                return GenerationStatus(
                    task_id="",
                    status="failed",
                    error=str(e),
                    error_code=e.code,
                )
            raise

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

    def _select_artifact(
        self,
        candidates: builtins.list[Any],
        artifact_id: str | None,
        type_name: str,
        type_name_lower: str,
    ) -> Any:
        """Select an artifact from candidates by ID or return first available.

        Args:
            candidates: List of candidate artifacts.
            artifact_id: Specific artifact ID to select, or None for first.
            type_name: Display name for error messages (e.g., "Report").
            type_name_lower: Lowercase name for error messages (e.g., "report").

        Returns:
            Selected artifact data.

        Raises:
            ValueError: If artifact not found or no candidates available.
        """
        if artifact_id:
            artifact = next((a for a in candidates if a[0] == artifact_id), None)
            if not artifact:
                raise ArtifactNotReadyError(
                    type_name.lower().replace(" ", "_"), artifact_id=artifact_id
                )
            return artifact

        if not candidates:
            raise ArtifactNotReadyError(type_name_lower)

        # Sort by creation timestamp (descending) to get the latest.
        # Timestamp is at index 15, position 0.
        candidates.sort(
            key=lambda a: a[15][0] if len(a) > 15 and isinstance(a[15], list) and a[15] else 0,
            reverse=True,
        )

        return candidates[0]

    async def _download_urls_batch(
        self, urls_and_paths: builtins.list[tuple[str, str]]
    ) -> builtins.list[str]:
        """Download multiple files using httpx with proper cookie handling.

        Args:
            urls_and_paths: List of (url, output_path) tuples.

        Returns:
            List of successfully downloaded output paths.
        """
        downloaded: list[str] = []

        # Load cookies with domain info for cross-domain redirect handling
        cookies = load_httpx_cookies()

        async with httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=60.0,
        ) as client:
            for url, output_path in urls_and_paths:
                try:
                    response = await client.get(url)
                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "")
                    if "text/html" in content_type:
                        raise ArtifactDownloadError(
                            "media", details="Received HTML instead of media file"
                        )

                    output_file = Path(output_path)
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    output_file.write_bytes(response.content)
                    downloaded.append(output_path)
                    logger.debug("Downloaded %s (%d bytes)", url[:60], len(response.content))

                except (httpx.HTTPError, ValueError) as e:
                    logger.warning("Download failed for %s: %s", url[:60], e)

        return downloaded

    async def _download_url(self, url: str, output_path: str) -> str:
        """Download a file from URL using httpx with proper cookie handling.

        Args:
            url: URL to download from.
            output_path: Path to save the file.

        Returns:
            The output path on success.

        Raises:
            ValueError: If download fails or authentication expired.
        """
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Load cookies with domain info for cross-domain redirect handling
        cookies = load_httpx_cookies()

        async with httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=60.0,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                raise ArtifactDownloadError(
                    "media",
                    details="Download failed: received HTML instead of media file. "
                    "Authentication may have expired. Run 'notebooklm login'.",
                )

            output_file.write_bytes(response.content)
            logger.debug("Downloaded %s (%d bytes)", url[:60], len(response.content))
            return output_path

    def _parse_generation_result(self, result: Any) -> GenerationStatus:
        """Parse generation API result into GenerationStatus.

        The API returns a single ID that serves as both the task_id (for polling
        during generation) and the artifact_id (once complete). This ID is at
        position [0][0] in the response and becomes Artifact.id in the list.
        """
        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            artifact_id = (
                artifact_data[0]
                if isinstance(artifact_data, list) and len(artifact_data) > 0
                else None
            )
            status_code = (
                artifact_data[4]
                if isinstance(artifact_data, list) and len(artifact_data) > 4
                else None
            )

            if artifact_id:
                status = (
                    artifact_status_to_str(status_code) if status_code is not None else "pending"
                )
                return GenerationStatus(task_id=artifact_id, status=status)

        return GenerationStatus(
            task_id="", status="failed", error="Generation failed - no artifact_id returned"
        )

    def _get_artifact_type_name(self, artifact_type: int) -> str:
        """Get human-readable name for an artifact type.

        Args:
            artifact_type: The StudioContentType enum value.

        Returns:
            The enum name if valid, otherwise the raw integer as string.
        """
        try:
            return StudioContentType(artifact_type).name
        except ValueError:
            return str(artifact_type)

    def _is_valid_media_url(self, value: Any) -> bool:
        """Check if value is a valid HTTP(S) URL.

        Args:
            value: The value to check.

        Returns:
            True if value is a string starting with http:// or https://.
        """
        return isinstance(value, str) and value.startswith(("http://", "https://"))

    def _find_infographic_url(self, art: builtins.list[Any]) -> str | None:
        """Extract infographic image URL from artifact data.

        Infographic URLs are deeply nested in the artifact structure.
        This method searches backwards through the artifact to find the URL.

        Args:
            art: Raw artifact data from _list_raw().

        Returns:
            The image URL if found, None otherwise.
        """
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

        Artifact array structure (from BATCHEXECUTE responses):
        - art[0]: artifact_id
        - art[2]: artifact_type (StudioContentType enum value)
        - art[4]: status_code (ArtifactStatus enum value)
        - art[6][5]: audio media URL list
        - art[8]: video metadata containing URL list
        - art[16][3]: slide deck PDF URL

        Args:
            art: Raw artifact data from _list_raw().
            artifact_type: The StudioContentType enum value.

        Returns:
            True if media URLs are available, or if artifact is non-media type.
            Returns True on unexpected structure (defensive fallback).
        """
        try:
            if artifact_type == StudioContentType.AUDIO.value:
                # Audio URL is at art[6][5] - check for non-empty media list
                if len(art) > 6 and isinstance(art[6], list) and len(art[6]) > 5:
                    media_list = art[6][5]
                    if isinstance(media_list, list) and len(media_list) > 0:
                        # Check first item has a valid URL
                        first_item = media_list[0]
                        if isinstance(first_item, list) and len(first_item) > 0:
                            return self._is_valid_media_url(first_item[0])
                return False

            elif artifact_type == StudioContentType.VIDEO.value:
                # Video URLs are in art[8] - check for any valid URL in the list
                if len(art) > 8 and isinstance(art[8], list):
                    return any(
                        self._is_valid_media_url(item[0])
                        for item in art[8]
                        if isinstance(item, list) and len(item) > 0
                    )
                return False

            elif artifact_type == StudioContentType.INFOGRAPHIC.value:
                return self._find_infographic_url(art) is not None

            elif artifact_type == StudioContentType.SLIDE_DECK.value:
                # Slide deck PDF URL is at art[16][3]
                return (
                    len(art) > 16
                    and isinstance(art[16], list)
                    and len(art[16]) > 3
                    and self._is_valid_media_url(art[16][3])
                )

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
