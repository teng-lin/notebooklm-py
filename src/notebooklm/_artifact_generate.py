"""Artifact generation operations.

Provides the ArtifactGenerator class for creating AI-generated artifacts
including Audio Overviews, Video Overviews, Reports, Quizzes, Flashcards,
Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import builtins
import json
import logging
from typing import TYPE_CHECKING, Any

from ._core import ClientCore
from .rpc import (
    AudioFormat,
    AudioLength,
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
from .types import GenerationStatus

if TYPE_CHECKING:
    from ._notes import NotesAPI

logger = logging.getLogger(__name__)


class ArtifactGenerator:
    """Handles artifact generation operations.

    This class encapsulates all artifact generation logic, making RPC calls
    to create various artifact types in NotebookLM.

    Usage:
        generator = ArtifactGenerator(core, notes_api)
        status = await generator.generate_audio(notebook_id)
    """

    def __init__(self, core: ClientCore, notes_api: "NotesAPI"):
        """Initialize the artifact generator.

        Args:
            core: The core client infrastructure for RPC calls.
            notes_api: The notes API for mind map persistence.
        """
        self._core = core
        self._notes = notes_api

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
                1,  # ArtifactTypeCode.AUDIO
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
                3,  # ArtifactTypeCode.VIDEO
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
                2,  # ArtifactTypeCode.REPORT
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
                4,  # ArtifactTypeCode.QUIZ
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
                4,  # ArtifactTypeCode.QUIZ
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
                7,  # ArtifactTypeCode.INFOGRAPHIC
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
                8,  # ArtifactTypeCode.SLIDE_DECK
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
                9,  # ArtifactTypeCode.DATA_TABLE
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
            RPCMethod.GENERATE_MIND_MAP,
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
                        mind_map_data = json.loads(mind_map_json)
                    except json.JSONDecodeError:
                        mind_map_data = mind_map_json
                        mind_map_json = str(mind_map_json)
                else:
                    mind_map_data = mind_map_json
                    mind_map_json = json.dumps(mind_map_json)

                # Extract title from mind map data
                title = "Mind Map"
                if isinstance(mind_map_data, dict) and "name" in mind_map_data:
                    title = mind_map_data["name"]

                # The GENERATE_MIND_MAP RPC generates content but does NOT persist it.
                # We must explicitly create a note to save the mind map.
                note = await self._notes.create(notebook_id, title=title, content=mind_map_json)
                note_id = note.id if note else None

                return {
                    "mind_map": mind_map_data,
                    "note_id": note_id,
                }

        return {"mind_map": None, "note_id": None}

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
                RPCMethod.CREATE_ARTIFACT,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
            return self._parse_generation_result(result)
        except RPCError as e:
            if e.rpc_code == "USER_DISPLAYABLE_ERROR":
                return GenerationStatus(
                    task_id="",
                    status="failed",
                    error=str(e),
                    error_code=str(e.rpc_code) if e.rpc_code is not None else None,
                )
            raise

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
