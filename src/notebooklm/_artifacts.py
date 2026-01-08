"""Artifacts API for NotebookLM studio content.

Provides operations for generating, listing, downloading, and managing
AI-generated artifacts including Audio Overviews, Video Overviews, Reports,
Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

from ._core import ClientCore
from .auth import load_httpx_cookies
import httpx

from .rpc import (
    RPCMethod,
    RPCError,
    StudioContentType,
    ArtifactStatus,
    AudioFormat,
    AudioLength,
    VideoFormat,
    VideoStyle,
    QuizQuantity,
    QuizDifficulty,
    InfographicOrientation,
    InfographicDetail,
    SlideDeckFormat,
    SlideDeckLength,
    ReportFormat,
)
from .types import Artifact, GenerationStatus, ReportSuggestion

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

    # =========================================================================
    # List/Get Operations
    # =========================================================================

    async def list(
        self, notebook_id: str, artifact_type: Optional[int] = None
    ) -> List[Artifact]:
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
        artifacts: List[Artifact] = []

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
                if artifact_type is None or artifact.artifact_type == artifact_type:
                    artifacts.append(artifact)

        # Fetch mind maps from notes system (if not filtering to non-mind-map type)
        if artifact_type is None or artifact_type == StudioContentType.MIND_MAP.value:
            try:
                mind_maps = await self._notes.list_mind_maps(notebook_id)
                for mm_data in mind_maps:
                    artifact = Artifact.from_mind_map(mm_data)
                    if artifact is not None:  # None means deleted (status=2)
                        if artifact_type is None or artifact.artifact_type == artifact_type:
                            artifacts.append(artifact)
            except (RPCError, httpx.HTTPError) as e:
                # Network/API errors - log and continue with studio artifacts
                # This ensures users can see their audio/video/reports even if
                # the mind maps endpoint is temporarily unavailable
                logger.warning(f"Failed to fetch mind maps: {e}")

        return artifacts

    async def get(self, notebook_id: str, artifact_id: str) -> Optional[Artifact]:
        """Get a specific artifact by ID.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID.

        Returns:
            Artifact object, or None if not found.
        """
        artifacts = await self.list(notebook_id)
        for artifact in artifacts:
            if artifact.id == artifact_id:
                return artifact
        return None

    async def list_audio(self, notebook_id: str) -> List[Artifact]:
        """List audio overview artifacts."""
        return await self.list(notebook_id, StudioContentType.AUDIO.value)

    async def list_video(self, notebook_id: str) -> List[Artifact]:
        """List video overview artifacts."""
        return await self.list(notebook_id, StudioContentType.VIDEO.value)

    async def list_reports(self, notebook_id: str) -> List[Artifact]:
        """List report artifacts (Briefing Doc, Study Guide, Blog Post)."""
        return await self.list(notebook_id, StudioContentType.REPORT.value)

    async def list_quizzes(self, notebook_id: str) -> List[Artifact]:
        """List quiz artifacts (type 4 with variant 2)."""
        all_type4 = await self.list(notebook_id, StudioContentType.QUIZ_FLASHCARD.value)
        return [a for a in all_type4 if a.is_quiz]

    async def list_flashcards(self, notebook_id: str) -> List[Artifact]:
        """List flashcard artifacts (type 4 with variant 1)."""
        all_type4 = await self.list(notebook_id, StudioContentType.QUIZ_FLASHCARD.value)
        return [a for a in all_type4 if a.is_flashcards]

    async def list_infographics(self, notebook_id: str) -> List[Artifact]:
        """List infographic artifacts."""
        return await self.list(notebook_id, StudioContentType.INFOGRAPHIC.value)

    async def list_slide_decks(self, notebook_id: str) -> List[Artifact]:
        """List slide deck artifacts."""
        return await self.list(notebook_id, StudioContentType.SLIDE_DECK.value)

    async def list_data_tables(self, notebook_id: str) -> List[Artifact]:
        """List data table artifacts."""
        return await self.list(notebook_id, StudioContentType.DATA_TABLE.value)

    # =========================================================================
    # Generate Operations
    # =========================================================================

    async def generate_audio(
        self,
        notebook_id: str,
        source_ids: Optional[List[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
        audio_format: Optional[AudioFormat] = None,
        audio_length: Optional[AudioLength] = None,
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
            source_ids = await self._get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        source_ids_double = [[sid] for sid in source_ids] if source_ids else []

        format_code = audio_format.value if audio_format else 1
        length_code = audio_length.value if audio_length else 2

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
        source_ids: Optional[List[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
        video_format: Optional[VideoFormat] = None,
        video_style: Optional[VideoStyle] = None,
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
            source_ids = await self._get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        source_ids_double = [[sid] for sid in source_ids] if source_ids else []

        format_code = video_format.value if video_format else 1
        style_code = video_style.value if video_style else 1

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
        source_ids: Optional[List[str]] = None,
        language: str = "en",
        custom_prompt: Optional[str] = None,
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
            source_ids = await self._get_source_ids(notebook_id)

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
        source_ids: Optional[List[str]] = None,
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
        source_ids: Optional[List[str]] = None,
        instructions: Optional[str] = None,
        quantity: Optional[QuizQuantity] = None,
        difficulty: Optional[QuizDifficulty] = None,
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
            source_ids = await self._get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        quantity_code = quantity.value if quantity else 2
        difficulty_code = difficulty.value if difficulty else 2

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
        source_ids: Optional[List[str]] = None,
        instructions: Optional[str] = None,
        quantity: Optional[QuizQuantity] = None,
        difficulty: Optional[QuizDifficulty] = None,
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
            source_ids = await self._get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        quantity_code = quantity.value if quantity else 2
        difficulty_code = difficulty.value if difficulty else 2

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
        source_ids: Optional[List[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
        orientation: Optional[InfographicOrientation] = None,
        detail_level: Optional[InfographicDetail] = None,
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
            source_ids = await self._get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        orientation_code = orientation.value if orientation else 1
        detail_code = detail_level.value if detail_level else 2

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
                [
                    None,
                    [instructions, language, None, orientation_code, detail_code],
                ],
            ],
        ]
        return await self._call_generate(notebook_id, params)

    async def generate_slide_deck(
        self,
        notebook_id: str,
        source_ids: Optional[List[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
        slide_format: Optional[SlideDeckFormat] = None,
        slide_length: Optional[SlideDeckLength] = None,
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
            source_ids = await self._get_source_ids(notebook_id)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        format_code = slide_format.value if slide_format else 1
        length_code = slide_length.value if slide_length else 1

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
        source_ids: Optional[List[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
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
            source_ids = await self._get_source_ids(notebook_id)

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
        source_ids: Optional[List[str]] = None,
    ) -> dict[str, Any]:
        """Generate an interactive mind map.

        Args:
            notebook_id: The notebook ID.
            source_ids: Source IDs to include. If None, uses all sources.

        Returns:
            Dictionary with 'mind_map' (JSON data) and 'note_id'.
        """
        if source_ids is None:
            source_ids = await self._get_source_ids(notebook_id)

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
                note_info = inner[2] if len(inner) > 2 else None
                note_id = note_info[0] if note_info and isinstance(note_info, list) else None

                if isinstance(mind_map_json, str):
                    import json
                    try:
                        mind_map_data = json.loads(mind_map_json)
                    except json.JSONDecodeError:
                        mind_map_data = mind_map_json
                else:
                    mind_map_data = mind_map_json

                return {
                    "mind_map": mind_map_data,
                    "note_id": note_id,
                }

        return {"mind_map": None, "note_id": None}

    # =========================================================================
    # Download Operations
    # =========================================================================

    async def download_audio(
        self, notebook_id: str, output_path: str, artifact_id: Optional[str] = None
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
            a for a in artifacts_data
            if isinstance(a, list) and len(a) > 4
            and a[2] == StudioContentType.AUDIO
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            audio_art = next((a for a in audio_candidates if a[0] == artifact_id), None)
            if not audio_art:
                raise ValueError(f"Audio artifact {artifact_id} not found or not ready.")
        else:
            audio_art = audio_candidates[0] if audio_candidates else None

        if not audio_art:
            raise ValueError("No completed audio overview found.")

        # Extract URL from metadata[6][5]
        try:
            metadata = audio_art[6]
            if not isinstance(metadata, list) or len(metadata) <= 5:
                raise ValueError("Invalid audio metadata structure.")

            media_list = metadata[5]
            if not isinstance(media_list, list) or len(media_list) == 0:
                raise ValueError("No media URLs found.")

            url = None
            for item in media_list:
                if isinstance(item, list) and len(item) > 2 and item[2] == "audio/mp4":
                    url = item[0]
                    break

            if not url and len(media_list) > 0 and isinstance(media_list[0], list):
                url = media_list[0][0]

            if not url:
                raise ValueError("Could not extract download URL.")

            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ValueError(f"Failed to parse audio artifact structure: {e}")

    async def download_video(
        self, notebook_id: str, output_path: str, artifact_id: Optional[str] = None
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
            a for a in artifacts_data
            if isinstance(a, list) and len(a) > 4
            and a[2] == StudioContentType.VIDEO
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            video_art = next((v for v in video_candidates if v[0] == artifact_id), None)
            if not video_art:
                raise ValueError(f"Video artifact {artifact_id} not found or not ready.")
        else:
            video_art = video_candidates[0] if video_candidates else None

        if not video_art:
            raise ValueError("No completed video overview found.")

        # Extract URL from metadata[8]
        try:
            if len(video_art) <= 8:
                raise ValueError("Invalid video artifact structure.")

            metadata = video_art[8]
            if not isinstance(metadata, list):
                raise ValueError("Invalid video metadata structure.")

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
                raise ValueError("No media URLs found.")

            url = None
            for item in media_list:
                if isinstance(item, list) and len(item) > 2 and item[2] == "video/mp4":
                    url = item[0]
                    if item[1] == 4:
                        break

            if not url and len(media_list) > 0:
                url = media_list[0][0]

            if not url:
                raise ValueError("Could not extract download URL.")

            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ValueError(f"Failed to parse video artifact structure: {e}")

    async def download_infographic(
        self, notebook_id: str, output_path: str, artifact_id: Optional[str] = None
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
            a for a in artifacts_data
            if isinstance(a, list) and len(a) > 4
            and a[2] == StudioContentType.INFOGRAPHIC
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            info_art = next((i for i in info_candidates if i[0] == artifact_id), None)
            if not info_art:
                raise ValueError(f"Infographic {artifact_id} not found or not ready.")
        else:
            info_art = info_candidates[0] if info_candidates else None

        if not info_art:
            raise ValueError("No completed infographic found.")

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
                raise ValueError("Could not find infographic metadata.")

            url = metadata[2][0][1][0]
            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ValueError(f"Failed to parse infographic structure: {e}")

    async def download_slide_deck(
        self, notebook_id: str, output_path: str, artifact_id: Optional[str] = None
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
            a for a in artifacts_data
            if isinstance(a, list) and len(a) > 4
            and a[2] == StudioContentType.SLIDE_DECK
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            slide_art = next((s for s in slide_candidates if s[0] == artifact_id), None)
            if not slide_art:
                raise ValueError(f"Slide deck {artifact_id} not found or not ready.")
        else:
            slide_art = slide_candidates[0] if slide_candidates else None

        if not slide_art:
            raise ValueError("No completed slide deck found.")

        # Extract PDF URL from metadata at index 16, position 3
        # Structure: artifact[16] = [config, title, slides_list, pdf_url]
        try:
            if len(slide_art) <= 16:
                raise ValueError("Invalid slide deck artifact structure.")

            metadata = slide_art[16]
            if not isinstance(metadata, list) or len(metadata) < 4:
                raise ValueError("Invalid slide deck metadata structure.")

            pdf_url = metadata[3]
            if not isinstance(pdf_url, str) or not pdf_url.startswith("http"):
                raise ValueError("Could not find PDF download URL.")

            return await self._download_url(pdf_url, output_path)

        except (IndexError, TypeError) as e:
            raise ValueError(f"Failed to parse slide deck structure: {e}")

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
        params = [[2], artifact_id]
        await self._core.rpc_call(
            RPCMethod.DELETE_STUDIO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return True

    async def rename(
        self, notebook_id: str, artifact_id: str, new_title: str
    ) -> None:
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
                    status = "in_progress" if status_code == 1 else "completed"
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
        poll_interval: Optional[float] = None,  # Deprecated, use initial_interval
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
        export_type: int = 1,
    ) -> Any:
        """Export a report to Google Docs.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The report artifact ID.
            title: Title for the exported document.
            export_type: 1 for Docs.

        Returns:
            Export result with document URL.
        """
        params = [None, artifact_id, None, title, export_type]
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
        params = [None, artifact_id, None, title, 2]  # 2 for Sheets
        return await self._core.rpc_call(
            RPCMethod.EXPORT_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def export(
        self,
        notebook_id: str,
        artifact_id: Optional[str] = None,
        content: Optional[str] = None,
        title: str = "Export",
        export_type: int = 1,
    ) -> Any:
        """Export an artifact to Google Docs/Sheets.

        Generic export method for any artifact type.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID (optional).
            content: Content to export (optional).
            title: Title for the exported document.
            export_type: 1 for Docs, 2 for Sheets.

        Returns:
            Export result with document URL.
        """
        params = [None, artifact_id, content, title, export_type]
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
        source_ids: Optional[List[str]] = None,
    ) -> List[ReportSuggestion]:
        """Get AI-suggested report formats for a notebook.

        Args:
            notebook_id: The notebook ID.
            source_ids: Specific source IDs to analyze.

        Returns:
            List of ReportSuggestion objects.
        """
        if source_ids is None:
            source_ids = await self._get_source_ids(notebook_id)

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
                    suggestions.append(ReportSuggestion(
                        title=item[0] if isinstance(item[0], str) else "",
                        description=item[1] if isinstance(item[1], str) else "",
                        prompt=item[4] if len(item) > 4 and isinstance(item[4], str) else "",
                        audience_level=item[5] if len(item) > 5 else 2,
                    ))

        return suggestions

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _call_generate(self, notebook_id: str, params: List[Any]) -> GenerationStatus:
        """Make a generation RPC call with error handling.

        Wraps the RPC call to handle UserDisplayableError (rate limiting/quota)
        and convert to appropriate GenerationStatus.

        Args:
            notebook_id: The notebook ID.
            params: RPC parameters for the generation call.

        Returns:
            GenerationStatus with task_id on success, or error info on failure.
        """
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

    async def _list_raw(self, notebook_id: str) -> List[Any]:
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

    async def _get_source_ids(self, notebook_id: str) -> List[str]:
        """Extract source IDs from notebook data."""
        params = [notebook_id, None, [2], None, 0]
        notebook_data = await self._core.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        source_ids: list[str] = []
        if not notebook_data or not isinstance(notebook_data, list):
            return source_ids

        try:
            if len(notebook_data) > 0 and isinstance(notebook_data[0], list):
                notebook_info = notebook_data[0]
                if len(notebook_info) > 1 and isinstance(notebook_info[1], list):
                    sources = notebook_info[1]
                    for source in sources:
                        if isinstance(source, list) and len(source) > 0:
                            first = source[0]
                            if isinstance(first, list) and len(first) > 0:
                                sid = first[0]
                                if isinstance(sid, str):
                                    source_ids.append(sid)
        except (IndexError, TypeError):
            pass

        return source_ids

    async def _download_urls_batch(
        self, urls_and_paths: List[Tuple[str, str]]
    ) -> List[str]:
        """Download multiple files using httpx with proper cookie handling.

        Args:
            urls_and_paths: List of (url, output_path) tuples.

        Returns:
            List of successfully downloaded output paths.
        """
        from pathlib import Path

        downloaded: List[str] = []

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
                        raise ValueError("Received HTML instead of media file")

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
        from pathlib import Path

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
                raise ValueError(
                    "Download failed: received HTML instead of media file. "
                    "Authentication may have expired. Run 'notebooklm login'."
                )

            output_file.write_bytes(response.content)
            logger.debug("Downloaded %s (%d bytes)", url[:60], len(response.content))
            return output_path

    def _parse_generation_result(self, result: Any) -> GenerationStatus:
        """Parse generation API result into GenerationStatus."""
        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            artifact_id = artifact_data[0] if isinstance(artifact_data, list) and len(artifact_data) > 0 else None
            status_code = artifact_data[4] if isinstance(artifact_data, list) and len(artifact_data) > 4 else None

            if artifact_id:
                status = "in_progress" if status_code == 1 else "completed" if status_code == 3 else "pending"
                return GenerationStatus(task_id=artifact_id, status=status)

        return GenerationStatus(task_id="", status="failed", error="Generation failed - no artifact_id returned")
