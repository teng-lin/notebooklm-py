"""Private artifact generation service implementation.

Holds the ``generate_*`` / ``revise_slide`` / ``retry_failed`` kickoff paths
extracted from :class:`~notebooklm._artifacts.ArtifactsAPI` (the facade keeps
thin delegators preserving the public signatures). This service owns only the
RPC dispatch surface, the source-id resolver, and the note-row primitives — no
polling/status state lives here.
"""

from __future__ import annotations

import builtins
import json as json_module
import logging
from typing import TYPE_CHECKING, Any

from .._env import get_default_language
from .._row_adapters import artifacts as _artifact_rows
from .._types.artifacts import _status_from_code
from .._types.research import MindMapResult
from ..exceptions import (
    ArtifactFeatureUnavailableError,
    DecodingError,
    ValidationError,
)
from ..rpc import (
    AudioFormat,
    AudioLength,
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
    safe_index,
)
from ..types import GenerationStatus
from .payloads import (
    build_audio_artifact_params,
    build_cinematic_video_artifact_params,
    build_data_table_artifact_params,
    build_flashcards_artifact_params,
    build_infographic_artifact_params,
    build_mind_map_params,
    build_quiz_artifact_params,
    build_report_artifact_params,
    build_retry_artifact_params,
    build_revise_slide_params,
    build_slide_deck_artifact_params,
    build_video_artifact_params,
)

if TYPE_CHECKING:
    from .._note_service import NoteService
    from .._notebook_metadata import NotebookSourceIdProvider
    from .._runtime.contracts import RpcCaller

logger = logging.getLogger(__name__)


class ArtifactGenerationService:
    """Generation kickoff operations extracted from :class:`ArtifactsAPI`.

    Peer to :class:`~notebooklm._artifact.downloads.ArtifactDownloadService`
    and :class:`~notebooklm._artifact.polling.ArtifactPollingService`. Injected
    with the RPC caller, the source-id resolver, and the note-row service so the
    generate paths carry no other facade state.
    """

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
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
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
        return await self._call_generate(
            notebook_id,
            params,
            null_result_artifact_type="audio",
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
        if language is None:
            language = get_default_language()
        normalized_style_prompt = style_prompt.strip() if style_prompt is not None else None
        if video_format == VideoFormat.CINEMATIC and normalized_style_prompt:
            raise ValidationError("style_prompt is not supported for cinematic videos")
        # Short videos have a FIXED visual style — the server silently ignores any
        # style code (live-verified: anime vs watercolor render identically). Reject
        # an explicit style/style_prompt rather than pretend it takes effect (#1805).
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
        return await self._call_generate(
            notebook_id,
            params,
            null_result_artifact_type="video",
        )

    async def generate_cinematic_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
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
        return await self._call_generate(
            notebook_id,
            params,
            null_result_artifact_type="cinematic video",
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
        return await self._call_generate(
            notebook_id,
            params,
            null_result_artifact_type="report",
        )

    async def generate_study_guide(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
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
        source_ids: builtins.list[str] | None = None,
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
        return await self._call_generate(
            notebook_id,
            params,
            null_result_artifact_type="quiz",
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
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_flashcards_artifact_params(
            notebook_id,
            source_ids,
            instructions=instructions,
            quantity=quantity,
            difficulty=difficulty,
        )
        return await self._call_generate(
            notebook_id,
            params,
            null_result_artifact_type="flashcards",
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
        return await self._call_generate(
            notebook_id,
            params,
            null_result_artifact_type="infographic",
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
        return await self._call_generate(
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
        # v0.8.0 (#1342): a synchronous refusal (``RPCError``) propagates rather
        # than being swallowed into a soft ``status="failed"`` return.
        result = await self._rpc.rpc_call(
            RPCMethod.REVISE_SLIDE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if result is None:
            logger.warning("REVISE_SLIDE returned null result for artifact %s", artifact_id)
            raise ArtifactFeatureUnavailableError(
                "slide revision",
                method_id=RPCMethod.REVISE_SLIDE.value,
            )
        return self._parse_generation_result(result, method_id=RPCMethod.REVISE_SLIDE.value)

    async def retry_failed(self, notebook_id: str, artifact_id: str) -> GenerationStatus:
        """Retry a failed Studio artifact in place (the UI "Retry" action).

        Re-runs generation for an already-failed artifact *without* deleting it
        first. The same ``artifact_id`` is preserved and returned as the task
        id, so existing ``poll_status`` / ``wait_for_completion`` flows keep
        working — an accepted retry comes back as
        ``GenerationStatus(status="in_progress")``.

        Follows the ADR-0019 "async kickoff" contract: a synchronous server
        refusal (``USER_DISPLAYABLE_ERROR`` — rate limit, quota, or a
        non-retryable artifact) **raises** ``RateLimitError`` / ``RPCError``
        rather than returning ``status="failed"``, matching the v0.8.0 behavior
        of the sibling ``generate_*`` / ``revise_slide`` methods (#1342).

        ``notebook_id`` is routing-only — it sets the ``source_path`` header;
        the artifact is identified solely by ``artifact_id`` (same trait as
        ``revise_slide``).
        """
        params = build_retry_artifact_params(artifact_id)
        # A USER_DISPLAYABLE_ERROR refusal propagates as RateLimitError/RPCError
        # per ADR-0019 "async kickoff", matching _call_generate / revise_slide
        # after v0.8.0 (#1342).
        #
        # ``allow_null=True`` lets a null decode through to the explicit
        # ``result is None`` guard below (the golden fixture pins the
        # normal-success row, so it records ``allow_null: false`` for that
        # happy-path decode — the two are not in conflict).
        result = await self._rpc.rpc_call(
            RPCMethod.RETRY_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if result is None:
            logger.warning("RETRY_ARTIFACT returned null result for artifact %s", artifact_id)
            raise ArtifactFeatureUnavailableError(
                "retry",
                method_id=RPCMethod.RETRY_ARTIFACT.value,
            )
        # This matches revise_slide / generate_* after v0.8.0 (#1342):
        # no-task rows raise instead of being reported as started-then-failed.
        # A structurally-short row still raises ``UnknownRPCMethodError`` from
        # ``safe_index`` inside ``_parse_generation_result``.
        status = self._parse_generation_result(result, method_id=RPCMethod.RETRY_ARTIFACT.value)
        if not status.task_id:
            logger.warning("RETRY_ARTIFACT returned a row with no artifact id: %r", result)
            raise ArtifactFeatureUnavailableError(
                "retry",
                method_id=RPCMethod.RETRY_ARTIFACT.value,
            )
        return status

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
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
        return await self._call_generate(
            notebook_id,
            params,
            null_result_artifact_type="data table",
        )

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> MindMapResult:
        """Generate a note-backed mind map and persist it as a note.

        Returns:
            A :class:`~notebooklm._types.research.MindMapResult` with
            ``mind_map`` (the parsed mind-map structure, or ``None`` on an
            empty response) and ``note_id`` (the persisted note id, or
            ``None``). Use attribute access (``result.mind_map``).
        """
        if language is None:
            language = get_default_language()
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        params = build_mind_map_params(
            source_ids,
            language=language,
            instructions=instructions,
        )

        # GENERATE_MIND_MAP is the live ``ActOnSources`` — a generic
        # source-action op we drive with mind-map params; it is classified
        # PROBE_THEN_CREATE in ``_idempotency.py``. ``operation_variant=None``
        # is passed explicitly to document this call site as the no-variant
        # default (the registry resolves the same entry either way; the explicit
        # kwarg is a future-proofing marker for a possible variant table).
        result = await self._rpc.rpc_call(
            RPCMethod.GENERATE_MIND_MAP,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            operation_variant=None,
        )

        # The two-level ``[[mind_map_json]]`` leaf descent is centralised behind
        # ``unwrap_mind_map_generation_leaf`` (#1491); the sentinel marks absence
        # (a present leaf, incl. ``None``, is processed as before).
        mind_map_json = _artifact_rows.unwrap_mind_map_generation_leaf(
            result, method_id=RPCMethod.GENERATE_MIND_MAP.value, source="ArtifactsAPI"
        )
        if mind_map_json is not _artifact_rows.MIND_MAP_LEAF_ABSENT:
            if isinstance(mind_map_json, str):
                try:
                    mind_map_data = json_module.loads(mind_map_json)
                except json_module.JSONDecodeError:
                    mind_map_data = mind_map_json
                    mind_map_json = str(mind_map_json)
            else:
                mind_map_data = mind_map_json
                mind_map_json = json_module.dumps(mind_map_json)

            # Only accept ``name`` when it is a non-empty ``str`` — a
            # malformed tree with a ``null``/numeric ``name`` would otherwise
            # flow into the note title and frozen ``MindMap.title: str``
            # (issue #1270).
            title = "Mind Map"
            if isinstance(mind_map_data, dict):
                name = mind_map_data.get("name")
                if isinstance(name, str) and name:
                    title = name

            # ``NoteService.create_note`` raises ``RPCError`` when the
            # server omits a usable row id (issue #1162); on success it
            # always returns a ``Note`` with a non-empty id. The
            # ``note.id or None`` below is therefore defensive only —
            # it preserves the public dict contract ("note_id is None
            # means persistence failed") for any future degenerate
            # shape, but the empty-id case now surfaces as an error
            # rather than a silent ``{"note_id": None}``.
            note = await self._note_service.create_note(
                notebook_id, title=title, content=mind_map_json
            )
            return MindMapResult(
                mind_map=mind_map_data,
                note_id=note.id or None,
                created_at=note.created_at,
            )

        return MindMapResult(mind_map=None, note_id=None)

    async def _call_generate(
        self,
        notebook_id: str,
        params: builtins.list[Any],
        *,
        null_result_artifact_type: str | None = None,
    ) -> GenerationStatus:
        """Make a generation RPC call with error handling."""
        # Best-effort debug label over the OUTGOING request body; the ``[2:3]``
        # slice-pick keeps it off ``name[int]`` (== old guarded ``params[2]``).
        # Unpack the at-most-one-element slice rather than ``next(iter(...))`` so
        # the single-element invariant is explicit (no StopIteration path).
        descriptor = None
        if params[2:3]:
            (descriptor,) = params[2:3]
        artifact_type = "unknown"
        if isinstance(descriptor, list) and descriptor[2:3]:
            (artifact_type,) = descriptor[2:3]
        logger.debug("Generating artifact type=%s in notebook %s", artifact_type, notebook_id)
        # CREATE_ARTIFACT is PROBE_THEN_CREATE (``_idempotency.py``).
        # ``operation_variant=None`` marks this call site as the no-variant
        # default (a future-proofing marker; the registry resolves the same).
        # v0.8.0 (#1342): a synchronous refusal (couldn't-start, ``RPCError``)
        # propagates rather than being swallowed into a soft
        # ``status="failed"`` return.
        result = await self._rpc.rpc_call(
            RPCMethod.CREATE_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            operation_variant=None,
        )
        if result is None and null_result_artifact_type is not None:
            raise ArtifactFeatureUnavailableError(
                null_result_artifact_type,
                method_id=RPCMethod.CREATE_ARTIFACT.value,
            )
        return self._parse_generation_result(result, method_id=RPCMethod.CREATE_ARTIFACT.value)

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
            return GenerationStatus(task_id=artifact_id, status=_status_from_code(status_code))

        # v0.8.0 (#1342): a missing id means no task was created — raise.
        # Null id (feature gated) -> ArtifactFeatureUnavailableError; else drift.
        if artifact_id is None:
            raise ArtifactFeatureUnavailableError("artifact", method_id=method_id)
        raise DecodingError(f"No artifact id (source={source})", method_id=method_id)
