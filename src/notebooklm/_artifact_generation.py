"""Private artifact generation service implementation."""

from __future__ import annotations

import json as json_module
import logging
import sys
from typing import Any

from ._env import get_default_language
from .exceptions import ValidationError
from .rpc import (
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
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
    nest_source_ids,
    safe_index,
)
from .types import GenerationStatus, ReportSuggestion

logger = logging.getLogger(__name__)


def _artifact_seams() -> Any:
    """Return the facade module that legacy tests patch."""
    try:
        return sys.modules["notebooklm._artifacts"]
    except KeyError as e:
        # Normal construction imports this service from _artifacts first; the
        # lookup exists only so call sites can preserve _artifacts patch seams.
        raise RuntimeError("notebooklm._artifacts must be imported before generation runs") from e


class ArtifactGenerationService:
    """Artifact generation operations extracted from the public facade."""

    def __init__(self, api: Any):
        self._api = api

    async def generate_audio(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
        audio_format: AudioFormat | None = None,
        audio_length: AudioLength | None = None,
    ) -> GenerationStatus:
        """Generate an Audio Overview (podcast)."""
        api = self._api
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_triple = nest_source_ids(source_ids, 2)
        source_ids_double = nest_source_ids(source_ids, 1)

        format_code = audio_format.value if audio_format else None
        length_code = audio_length.value if audio_length else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.AUDIO.value,
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
        return await api._call_generate(notebook_id, params)

    async def generate_video(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
        video_format: VideoFormat | None = None,
        video_style: VideoStyle | None = None,
        style_prompt: str | None = None,
    ) -> GenerationStatus:
        """Generate a Video Overview."""
        api = self._api
        if language is None:
            language = get_default_language()
        normalized_style_prompt = style_prompt.strip() if style_prompt is not None else None
        if video_format == VideoFormat.CINEMATIC and normalized_style_prompt:
            raise ValidationError("style_prompt is not supported for cinematic videos")
        if video_style == VideoStyle.CUSTOM and not normalized_style_prompt:
            raise ValidationError("style_prompt is required when video_style is CUSTOM")
        if normalized_style_prompt and video_style != VideoStyle.CUSTOM:
            raise ValidationError("style_prompt requires video_style=VideoStyle.CUSTOM")

        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_triple = nest_source_ids(source_ids, 2)
        source_ids_double = nest_source_ids(source_ids, 1)

        format_code = video_format.value if video_format else None
        style_code = video_style.value if video_style else None

        video_config = [
            source_ids_double,
            language,
            instructions,
            None,
            format_code,
            style_code,
        ]
        if normalized_style_prompt:
            video_config.append(normalized_style_prompt)

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.VIDEO.value,
                source_ids_triple,
                None,
                None,
                None,
                None,
                [
                    None,
                    None,
                    video_config,
                ],
            ],
        ]
        return await api._call_generate(notebook_id, params)

    async def generate_cinematic_video(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a Cinematic Video Overview."""
        api = self._api
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_triple = nest_source_ids(source_ids, 2)
        source_ids_double = nest_source_ids(source_ids, 1)

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.VIDEO.value,
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
                        VideoFormat.CINEMATIC.value,
                    ],
                ],
            ],
        ]
        return await api._call_generate(notebook_id, params)

    async def generate_report(
        self,
        notebook_id: str,
        report_format: ReportFormat = ReportFormat.BRIEFING_DOC,
        source_ids: list[str] | None = None,
        language: str | None = None,
        custom_prompt: str | None = None,
        extra_instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a report artifact."""
        api = self._api
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

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
        if extra_instructions and report_format != ReportFormat.CUSTOM:
            config = {**config, "prompt": f"{config['prompt']}\n\n{extra_instructions}"}
        source_ids_triple = nest_source_ids(source_ids, 2)
        source_ids_double = nest_source_ids(source_ids, 1)

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.REPORT.value,
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
        return await api._call_generate(notebook_id, params)

    async def generate_study_guide(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        extra_instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a study guide report."""
        if language is None:
            language = get_default_language()
        # Preserve the historical facade seam for callers/tests that patch
        # ArtifactsAPI.generate_report.
        return await self._api.generate_report(
            notebook_id,
            report_format=ReportFormat.STUDY_GUIDE,
            source_ids=source_ids,
            language=language,
            extra_instructions=extra_instructions,
        )

    async def generate_quiz(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        """Generate a quiz."""
        api = self._api
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_triple = nest_source_ids(source_ids, 2)
        quantity_code = quantity.value if quantity else None
        difficulty_code = difficulty.value if difficulty else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.QUIZ_FLASHCARD.value,
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                [
                    None,
                    [
                        2,
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
        return await api._call_generate(notebook_id, params)

    async def generate_flashcards(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        """Generate flashcards."""
        api = self._api
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_triple = nest_source_ids(source_ids, 2)
        quantity_code = quantity.value if quantity else None
        difficulty_code = difficulty.value if difficulty else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.QUIZ_FLASHCARD.value,
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                [
                    None,
                    [
                        1,
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
        return await api._call_generate(notebook_id, params)

    async def generate_infographic(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
        orientation: InfographicOrientation | None = None,
        detail_level: InfographicDetail | None = None,
        style: InfographicStyle | None = None,
    ) -> GenerationStatus:
        """Generate an infographic."""
        api = self._api
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_triple = nest_source_ids(source_ids, 2)
        orientation_code = orientation.value if orientation else None
        detail_code = detail_level.value if detail_level else None
        style_code = style.value if style else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.INFOGRAPHIC.value,
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
                [[instructions, language, None, orientation_code, detail_code, style_code]],
            ],
        ]
        return await api._call_generate(notebook_id, params)

    async def generate_slide_deck(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
        slide_format: SlideDeckFormat | None = None,
        slide_length: SlideDeckLength | None = None,
    ) -> GenerationStatus:
        """Generate a slide deck."""
        api = self._api
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_triple = nest_source_ids(source_ids, 2)
        format_code = slide_format.value if slide_format else None
        length_code = slide_length.value if slide_length else None

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.SLIDE_DECK.value,
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
        return await api._call_generate(notebook_id, params)

    async def revise_slide(
        self,
        notebook_id: str,
        artifact_id: str,
        slide_index: int,
        prompt: str,
    ) -> GenerationStatus:
        """Revise an individual slide in a completed slide deck using a prompt."""
        api = self._api
        if slide_index < 0:
            raise ValidationError(f"slide_index must be >= 0, got {slide_index}")

        params = [
            [2],
            artifact_id,
            [[[slide_index, prompt]]],
        ]
        try:
            result = await api._core.rpc_call(
                RPCMethod.REVISE_SLIDE,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
        except RPCError as e:
            if e.rpc_code == "USER_DISPLAYABLE_ERROR":
                return GenerationStatus(
                    task_id="",
                    status="failed",
                    error=str(e),
                    error_code=str(e.rpc_code) if e.rpc_code is not None else None,
                )
            raise
        if result is None:
            logger.warning("REVISE_SLIDE returned null result for artifact %s", artifact_id)
        # Call through the facade so patches on ArtifactsAPI._parse_generation_result
        # remain effective after extraction.
        return api._parse_generation_result(result, method_id=RPCMethod.REVISE_SLIDE.value)

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a data table."""
        api = self._api
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_triple = nest_source_ids(source_ids, 2)

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                ArtifactTypeCode.DATA_TABLE.value,
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
        return await api._call_generate(notebook_id, params)

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        """Generate an interactive mind map and persist it as a note."""
        api = self._api
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await api._core.get_source_ids(notebook_id)

        source_ids_nested = nest_source_ids(source_ids, 2)

        params = [
            source_ids_nested,
            None,
            None,
            None,
            None,
            ["interactive_mindmap", [["[CONTEXT]", instructions or ""]], language],
            None,
            [2, None, [1]],
        ]

        result = await api._core.rpc_call(
            RPCMethod.GENERATE_MIND_MAP,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if result and isinstance(result, list) and len(result) > 0:
            inner = result[0]
            if isinstance(inner, list) and len(inner) > 0:
                mind_map_json = inner[0]

                if isinstance(mind_map_json, str):
                    try:
                        mind_map_data = json_module.loads(mind_map_json)
                    except json_module.JSONDecodeError:
                        mind_map_data = mind_map_json
                        mind_map_json = str(mind_map_json)
                else:
                    mind_map_data = mind_map_json
                    mind_map_json = json_module.dumps(mind_map_json)

                title = "Mind Map"
                if isinstance(mind_map_data, dict) and "name" in mind_map_data:
                    title = mind_map_data["name"]

                note = await _artifact_seams()._mind_map.create_note(
                    api._core,
                    notebook_id,
                    title=title,
                    content=mind_map_json,
                )
                note_id = note.id if note else None

                return {
                    "mind_map": mind_map_data,
                    "note_id": note_id,
                }

        return {"mind_map": None, "note_id": None}

    async def suggest_reports(
        self,
        notebook_id: str,
    ) -> list[ReportSuggestion]:
        """Get AI-suggested report formats for a notebook."""
        api = self._api
        params = [[2], notebook_id]

        result = await api._core.rpc_call(
            RPCMethod.GET_SUGGESTED_REPORTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        suggestions = []
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

    async def _call_generate(self, notebook_id: str, params: list[Any]) -> GenerationStatus:
        """Make a generation RPC call with error handling."""
        api = self._api
        artifact_type = params[2][2] if len(params) > 2 and len(params[2]) > 2 else "unknown"
        logger.debug("Generating artifact type=%s in notebook %s", artifact_type, notebook_id)
        try:
            result = await api._core.rpc_call(
                RPCMethod.CREATE_ARTIFACT,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
        except RPCError as e:
            if e.rpc_code == "USER_DISPLAYABLE_ERROR":
                return GenerationStatus(
                    task_id="",
                    status="failed",
                    error=str(e),
                    error_code=str(e.rpc_code) if e.rpc_code is not None else None,
                )
            raise
        # Call through the facade so patches on ArtifactsAPI._parse_generation_result
        # remain effective after extraction.
        return api._parse_generation_result(result, method_id=RPCMethod.CREATE_ARTIFACT.value)

    def _parse_generation_result(
        self,
        result: Any,
        *,
        method_id: str,
        source: str = "_parse_generation_result",
    ) -> GenerationStatus:
        """Parse generation API result into GenerationStatus."""
        artifact_id = safe_index(result, 0, 0, method_id=method_id, source=source)

        if artifact_id:
            status_code = safe_index(result, 0, 4, method_id=method_id, source=source)
            status = artifact_status_to_str(status_code) if status_code is not None else "pending"
            return GenerationStatus(task_id=artifact_id, status=status)

        return GenerationStatus(
            task_id="", status="failed", error="Generation failed - no artifact_id returned"
        )
