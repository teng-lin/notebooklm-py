"""Artifacts API for NotebookLM studio content.

Provides operations for generating, listing, downloading, and managing
AI-generated artifacts including Audio Overviews, Video Overviews, Reports,
Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import builtins
import json as json_module
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

# ``_mind_map`` is re-exported as ``_artifacts._mind_map`` so legacy patch
# seams can still resolve the module via the artifacts facade. The runtime code
# path in this module talks to the injected ``NoteBackedMindMapService`` /
# ``NoteService`` instances; the bare module re-export is for monkeypatch
# convenience only.
from . import _mind_map  # noqa: F401 — re-exported as facade attribute
from ._artifact import formatters as _artifact_formatters
from ._artifact import polling as _artifact_polling
from ._artifact.downloads import ArtifactDownloadService, DownloadResult
from ._artifact.listing import ArtifactListingService
from ._artifact.payloads import (
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
    build_suggest_reports_params,
    build_video_artifact_params,
)
from ._deprecation import future_errors_enabled
from ._env import get_default_language
from ._lookup import resolve_get
from ._mind_map import NoteBackedMindMapService
from ._note_service import NoteService
from ._notebook_metadata import NotebookSourceIdProvider
from ._polling_registry import PollRegistry
from ._runtime.contracts import RpcCaller
from ._types.research import MindMapResult
from .exceptions import (
    ArtifactFeatureUnavailableError,
    ArtifactNotFoundError,
    DecodingError,
    ValidationError,
)

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
    RPCError,
    RPCMethod,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
    artifact_status_to_str,
    safe_index,
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
            rpc: RPC dispatch surface (:class:`RpcCaller`). Used for
                direct artifact RPCs (delete, rename, export, list_raw)
                and threaded into the generation and download services.
            drain: Transport drain coordinator. Owns ``operation_scope``
                (used by the polling service) and ``register_drain_hook``
                (used here to register the polling-service close-time
                cleanup hook).
            lifecycle: Client lifecycle seam. Owns ``assert_bound_loop``
                used by the polling service before it touches loop-bound
                state.
            notebooks: Source-id resolver. Required — wire from
                ``NotebookLMClient`` (no implicit fallback).
            mind_maps: Note-backed mind-map facade. Owns the
                ``list_mind_maps`` / ``extract_content`` paths consumed
                by ``_artifact.downloads.download_mind_map``. Renamed
                from ``mind_map_service`` to reflect the
                concrete adapter type (:class:`NoteBackedMindMapService`).
            note_service: Backend note-row primitives. Owns the
                ``create_note`` call site that this API's
                ``generate_mind_map`` uses to persist generated mind
                maps. The generation path no longer
                reaches into a module-level ``_mind_map.create_note``
                shim.
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

    async def get(self, notebook_id: str, artifact_id: str) -> Artifact:
        """Get a specific artifact by ID.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID.

        Returns:
            The :class:`~notebooklm.types.Artifact`.

        Raises:
            ArtifactNotFoundError: If no artifact with ``artifact_id`` exists
                (matches ``notebooks.get``; issue #1247). Use :meth:`get_or_none`
                for the sanctioned ``None``-on-miss lookup.
        """
        # ``resolve_get`` single-sources the raise-on-miss decision (#1247).
        # Internal callers needing the silent lookup use get_or_none.
        return resolve_get(
            await self.get_or_none(notebook_id, artifact_id),
            not_found=ArtifactNotFoundError(artifact_id),
        )

    async def get_or_none(self, notebook_id: str, artifact_id: str) -> Artifact | None:
        """Get an artifact by ID, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019): unlike :meth:`get`
        — which now raises
        :class:`~notebooklm.exceptions.ArtifactNotFoundError` on a miss
        (#1247) — this returns ``None`` for a genuine absence and
        emits no deprecation warning. This method neither catches nor synthesizes
        a miss itself; it lists once and id-matches, inheriting :meth:`list`'s
        behavior unchanged. (Per ADR-0019 Rule 3, ``list`` keeps its deliberate
        *partial-availability* policy: a transport failure of the mind-map
        sub-fetch logs a warning and yields the studio artifacts that did load,
        so a note-backed mind-map id can read absent while that sub-fetch is
        down. That cross-namespace policy is decided separately and is not
        re-litigated here.) Faults raised by the primary studio-artifact listing
        propagate unchanged.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID.

        Returns:
            The :class:`~notebooklm.types.Artifact`, or ``None`` if not found.
        """
        logger.debug("Getting artifact %s from notebook %s", artifact_id, notebook_id)
        return await self._listing.get(notebook_id, artifact_id, list_artifacts=self.list)

    # Internal optional-lookup alias: a stable private name so internal call
    # sites and tests use the ``None``-on-miss lookup rather than the raising get().
    _get_or_none = get_or_none

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
        try:
            result = await self._rpc.rpc_call(
                RPCMethod.REVISE_SLIDE,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
        except RPCError as e:
            # v0.8.0 preview (#1342): a refusal raises; see ``_call_generate``.
            if e.rpc_code == "USER_DISPLAYABLE_ERROR" and not future_errors_enabled():
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
        return self._parse_generation_result(result, method_id=RPCMethod.REVISE_SLIDE.value)

    async def retry_failed(self, notebook_id: str, artifact_id: str) -> GenerationStatus:
        """Retry a failed Studio artifact in place (the UI "Retry" action).

        Re-runs generation for an already-failed artifact *without* deleting it
        first. The same ``artifact_id`` is preserved and returned as the task
        id, so existing :meth:`poll_status` / :meth:`wait_for_completion` flows
        keep working — an accepted retry comes back as
        ``GenerationStatus(status="in_progress")``.

        A single retry may itself fail again provider-side; this is a single
        in-place operation, so callers decide whether to re-invoke after a
        later terminal ``failed`` status (observed by polling).

        This method follows the ADR-0019 "async kickoff" contract: a
        synchronous server refusal (``USER_DISPLAYABLE_ERROR`` — e.g. rate
        limit, quota, or a non-retryable artifact) **raises** the underlying
        :class:`~notebooklm.exceptions.RateLimitError` /
        :class:`~notebooklm.exceptions.RPCError` rather than returning
        ``status="failed"``. (As a brand-new method it is born on the right
        side of the contract; the ``generate_*`` / :meth:`revise_slide` methods
        still swallow refusals into ``status="failed"`` until v0.8.0, issue
        #1342.)

        Args:
            notebook_id: The notebook ID. Routing-only — it sets the
                ``source_path`` header; the artifact is identified solely by
                ``artifact_id`` in the RPC payload (same trait as
                :meth:`revise_slide`).
            artifact_id: The ID of the failed artifact to retry.

        Returns:
            A :class:`~notebooklm.types.GenerationStatus` whose ``task_id`` is
            the same ``artifact_id`` and whose ``status`` is ``"in_progress"``
            once the retry is accepted.

        Raises:
            RateLimitError: The server refused the retry with a rate-limit /
                quota ``USER_DISPLAYABLE_ERROR``.
            RPCError: Any other synchronous server refusal.
            ArtifactFeatureUnavailableError: The RPC returned a null /
                missing-id result (no generation task was created).
        """
        params = build_retry_artifact_params(artifact_id)
        # Unlike ``_call_generate`` / ``revise_slide``, a USER_DISPLAYABLE_ERROR
        # refusal is intentionally NOT swallowed into status="failed" — it
        # propagates as RateLimitError/RPCError per ADR-0019 "async kickoff".
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
        # Born ADR-0019-correct: a missing/empty artifact id means no
        # generation task was created, so raise rather than return the
        # synthesized ``status="failed"`` that ``_parse_generation_result``
        # produces for a falsy id (a refusal must never masquerade as a
        # started-then-failed task). This is stricter than ``revise_slide`` /
        # ``generate_*``, which still soft-fail that case until v0.8.0 (#1342).
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
        """Generate an interactive mind map and persist it as a note.

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

        # GENERATE_MIND_MAP is classified PROBE_THEN_CREATE in
        # ``_idempotency.py``. ``operation_variant=None`` is passed
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
                    notebook_id,
                    title=title,
                    content=mind_map_json,
                )
                note_id = note.id or None

                return MindMapResult(mind_map=mind_map_data, note_id=note_id)

        return MindMapResult(mind_map=None, note_id=None)

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

    async def delete(self, notebook_id: str, artifact_id: str) -> None:
        """Delete an artifact.

        Idempotent: deleting an already-absent artifact succeeds (returns
        ``None``) and never raises ``ArtifactNotFoundError``. Real failures
        (``403``/``5xx``/auth/transport) still propagate.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID to delete.

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

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID to rename.
            new_title: The new title.
            return_object: When ``True`` (default), re-fetch (a full
                ``LIST_ARTIFACTS`` call) and return the renamed
                :class:`~notebooklm.types.Artifact`; when ``False``, return
                ``None`` without re-fetching. Under the v0.8.0 preview ``False``
                still returns ``None`` but adds miss-detection (the flag gates
                detection, not the return — see ``Raises``).

        Returns:
            The renamed :class:`~notebooklm.types.Artifact`, or ``None`` when
            ``return_object=False``.

        Raises:
            ArtifactNotFoundError: if the artifact does not exist (detected via
                a list fetch, not a 404). Always when ``return_object=True``;
                also on ``False`` under ``NOTEBOOKLM_FUTURE_ERRORS``. Note-backed
                mind-map ids are *not* renameable here — use ``mind_maps.rename``.

        .. versionchanged:: 0.7.0
            **Breaking change:** no longer returns ``None`` on success; it
            re-fetches and raises :class:`ArtifactNotFoundError` for a missing
            target (#1255), plus the ``return_object`` opt-out.
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
        # — use ``mind_maps.rename``). v0.7.0 ``False`` short-circuits; the v0.8.0
        # preview (#1362) runs the lookup on ``False`` too but still returns None.
        if not return_object and not future_errors_enabled():
            return None
        artifact = await self._listing.get_studio_only(
            notebook_id, artifact_id, list_raw=self._list_raw
        )
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id, method_id=RPCMethod.RENAME_ARTIFACT.value)
        return None if not return_object else artifact

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
        """Wait for a generation task to complete.

        Uses exponential backoff for polling to reduce API load.

        Concurrent callers for the same ``(notebook_id, task_id)`` share a
        single underlying poll loop through this API's feature-owned
        ``PollRegistry``. The first caller is the *leader* and drives the poll
        loop; subsequent *followers* attach to the leader's future without
        issuing their own ``LIST_ARTIFACTS`` requests. Cancellation is
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
            max_not_found: Consecutive "not found" polls before treating
                the task as *removed*.  When the API removes an artifact
                from the list (e.g. after a daily-quota rejection), the
                poller would otherwise spin until *timeout*.  The returned
                status is ``"removed"`` (see :attr:`GenerationStatus.is_removed`),
                kept distinct from ``"failed"`` so a delisted artifact is not
                conflated with one the server actually marked terminal-FAILED.
                Defaults to 5 to tolerate brief replication lag and slow
                networks. (Leader only.)
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
        """Export a data table to Google Sheets.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The data table artifact ID.
            title: Title for the exported spreadsheet.

        Returns:
            Export result with spreadsheet URL.
        """
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

        suggestions = []
        if result and isinstance(result, list) and len(result) > 0:
            # GET_SUGGESTED_REPORTS returns a wrapped ``[[row1, ...]]`` envelope or an
            # already-flat ``[row1, ...]``; only unwrap the wrapped case (single outer
            # element whose first inner element is itself a row). Bind ``inner`` so the
            # wrap probe is ``inner[0]`` not chained ``result[0][0]``.
            items = result
            if len(result) == 1 and isinstance(result[0], list):
                inner = result[0]
                if not inner or isinstance(inner[0], list):
                    items = inner
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

    async def _call_generate(
        self,
        notebook_id: str,
        params: builtins.list[Any],
        *,
        null_result_artifact_type: str | None = None,
    ) -> GenerationStatus:
        """Make a generation RPC call with error handling."""
        # Best-effort debug label via single-level ``descriptor[2]`` (not chained).
        descriptor = params[2] if len(params) > 2 else None
        artifact_type = (
            descriptor[2] if isinstance(descriptor, list) and len(descriptor) > 2 else "unknown"
        )
        logger.debug("Generating artifact type=%s in notebook %s", artifact_type, notebook_id)
        try:
            # CREATE_ARTIFACT is PROBE_THEN_CREATE (``_idempotency.py``).
            # ``operation_variant=None`` marks this call site as the no-variant
            # default (a future-proofing marker; the registry resolves the same).
            result = await self._rpc.rpc_call(
                RPCMethod.CREATE_ARTIFACT,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                operation_variant=None,
            )
        except RPCError as e:
            # v0.8.0 preview (#1342): a refusal raises (couldn't-start); default-off swallow.
            if e.rpc_code == "USER_DISPLAYABLE_ERROR" and not future_errors_enabled():
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
        return self._parse_generation_result(result, method_id=RPCMethod.CREATE_ARTIFACT.value)

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
        artifact_id = safe_index(result, 0, 0, method_id=method_id, source=source)

        if artifact_id:
            status_code = safe_index(result, 0, 4, method_id=method_id, source=source)
            status = artifact_status_to_str(status_code) if status_code is not None else "pending"
            return GenerationStatus(task_id=artifact_id, status=status)

        # v0.8.0 preview (#1342): a missing id means no task was created — raise.
        # Null id (feature gated) -> ArtifactFeatureUnavailableError; else drift.
        if future_errors_enabled():
            if artifact_id is None:
                raise ArtifactFeatureUnavailableError("artifact", method_id=method_id)
            raise DecodingError(f"No artifact id (source={source})", method_id=method_id)
        return GenerationStatus(
            task_id="", status="failed", error="Generation failed - no artifact_id returned"
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
        return _artifact_polling._extract_artifact_error(art)

    def _get_artifact_type_name(self, artifact_type: int) -> str:
        """Get human-readable name for an artifact type.

        Args:
            artifact_type: The ArtifactTypeCode enum value.

        Returns:
            The enum name if valid, otherwise the raw integer as string.
        """
        return _artifact_polling._get_artifact_type_name(artifact_type)

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
        return _artifact_polling._is_media_ready(art, artifact_type)
