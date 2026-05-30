"""Private artifact generation service implementation."""

from __future__ import annotations

import json as json_module
import logging
from typing import TYPE_CHECKING, Any

from ._artifact_payloads import (
    build_audio_artifact_params,
    build_cinematic_video_artifact_params,
    build_data_table_artifact_params,
    build_flashcards_artifact_params,
    build_infographic_artifact_params,
    build_mind_map_params,
    build_quiz_artifact_params,
    build_report_artifact_params,
    build_revise_slide_params,
    build_slide_deck_artifact_params,
    build_suggest_reports_params,
    build_video_artifact_params,
)
from ._env import get_default_language
from .exceptions import ArtifactFeatureUnavailableError, ValidationError
from .rpc import (
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
    safe_index,
)
from .types import GenerationStatus, ReportSuggestion

if TYPE_CHECKING:
    from ._note_service import NoteService
    from ._notebook_metadata import NotebookSourceIdProvider
    from ._runtime_contracts import RpcCaller

logger = logging.getLogger(__name__)


class ArtifactGenerationService:
    """Artifact generation operations extracted from the public facade."""

    def __init__(
        self,
        *,
        rpc: RpcCaller,
        notebooks: NotebookSourceIdProvider,
        note_service: NoteService,
    ) -> None:
        self._rpc = rpc
        self._notebooks = notebooks
        self._note_service = note_service

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
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_audio_artifact_params(
            notebook_id,
            source_ids,
            language=language,
            instructions=instructions,
            audio_format=audio_format,
            audio_length=audio_length,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="audio",
        )

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
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_video_artifact_params(
            notebook_id,
            source_ids,
            language=language,
            instructions=instructions,
            video_format=video_format,
            video_style=video_style,
            style_prompt=normalized_style_prompt,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="video",
        )

    async def generate_cinematic_video(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a Cinematic Video Overview."""
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_cinematic_video_artifact_params(
            notebook_id,
            source_ids,
            language=language,
            instructions=instructions,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="cinematic video",
        )

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
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_report_artifact_params(
            notebook_id,
            source_ids,
            report_format=report_format,
            language=language,
            custom_prompt=custom_prompt,
            extra_instructions=extra_instructions,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="report",
        )

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
        source_ids: list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        """Generate a quiz."""
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_quiz_artifact_params(
            notebook_id,
            source_ids,
            instructions=instructions,
            quantity=quantity,
            difficulty=difficulty,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="quiz",
        )

    async def generate_flashcards(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus:
        """Generate flashcards."""
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_flashcards_artifact_params(
            notebook_id,
            source_ids,
            instructions=instructions,
            quantity=quantity,
            difficulty=difficulty,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="flashcards",
        )

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
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_infographic_artifact_params(
            notebook_id,
            source_ids,
            language=language,
            instructions=instructions,
            orientation=orientation,
            detail_level=detail_level,
            style=style,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="infographic",
        )

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
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_slide_deck_artifact_params(
            notebook_id,
            source_ids,
            language=language,
            instructions=instructions,
            slide_format=slide_format,
            slide_length=slide_length,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="slide deck",
        )

    async def revise_slide(
        self,
        notebook_id: str,
        artifact_id: str,
        slide_index: int,
        prompt: str,
    ) -> GenerationStatus:
        """Revise an individual slide in a completed slide deck using a prompt."""
        if slide_index < 0:
            raise ValidationError(f"slide_index must be >= 0, got {slide_index}")

        params = build_revise_slide_params(artifact_id, slide_index, prompt)
        try:
            result = await self._rpc.rpc_call(
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
            raise ArtifactFeatureUnavailableError(
                "slide revision",
                method_id=RPCMethod.REVISE_SLIDE.value,
            )
        return self.parse_generation_result(result, method_id=RPCMethod.REVISE_SLIDE.value)

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
    ) -> GenerationStatus:
        """Generate a data table."""
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_data_table_artifact_params(
            notebook_id,
            source_ids,
            language=language,
            instructions=instructions,
        )
        return await self.call_generate(
            notebook_id,
            params,
            null_result_artifact_type="data table",
        )

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
        language: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        """Generate an interactive mind map and persist it as a note."""
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_mind_map_params(
            source_ids,
            language=language,
            instructions=instructions,
        )

        # GENERATE_MIND_MAP is classified PROBE_THEN_CREATE in
        # ``_idempotency.py`` (P0-3). ``operation_variant=None`` is passed
        # explicitly to document this call site as the no-variant default
        # (the registry resolves the same entry either way; the explicit
        # kwarg is a future-proofing marker for a possible variant table).
        result = await self._rpc.rpc_call(
            RPCMethod.GENERATE_MIND_MAP,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            operation_variant=None,
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

                # ``NoteService.create_note`` raises ``RPCError`` when the
                # server omits a usable row id (issue #1162); on success it
                # always returns a ``Note`` with a non-empty id. The
                # ``note.id or None`` below is therefore defensive only —
                # it preserves the public dict contract ("note_id is None
                # means persistence failed") for any future degenerate
                # shape, but the empty-id case now surfaces as an error
                # rather than a silent ``{"note_id": None}``. The original
                # ``if note`` dead-code guard was removed in PR #873.
                note = await self._note_service.create_note(
                    notebook_id,
                    title=title,
                    content=mind_map_json,
                )
                note_id = note.id or None

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
        params = build_suggest_reports_params(notebook_id)

        result = await self._rpc.rpc_call(
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

    async def call_generate(
        self,
        notebook_id: str,
        params: list[Any],
        *,
        null_result_artifact_type: str | None = None,
    ) -> GenerationStatus:
        """Make a generation RPC call with error handling."""
        artifact_type = params[2][2] if len(params) > 2 and len(params[2]) > 2 else "unknown"
        logger.debug("Generating artifact type=%s in notebook %s", artifact_type, notebook_id)
        try:
            # CREATE_ARTIFACT is classified PROBE_THEN_CREATE in
            # ``_idempotency.py`` (P0-3). ``operation_variant=None`` is
            # passed explicitly to document this call site as the
            # no-variant default (the registry resolves the same entry
            # either way; the explicit kwarg is a future-proofing marker
            # for a possible variant table).
            result = await self._rpc.rpc_call(
                RPCMethod.CREATE_ARTIFACT,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                operation_variant=None,
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
        if result is None and null_result_artifact_type is not None:
            raise ArtifactFeatureUnavailableError(
                null_result_artifact_type,
                method_id=RPCMethod.CREATE_ARTIFACT.value,
            )
        return self.parse_generation_result(result, method_id=RPCMethod.CREATE_ARTIFACT.value)

    def parse_generation_result(
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
