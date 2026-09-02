"""Artifacts API for NotebookLM studio content.

Provides operations for generating, listing, downloading, and managing
AI-generated artifacts including Audio Overviews, Video Overviews, Reports,
Quizzes, Flashcards, Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import builtins
import logging
import reprlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._artifact import polling as _artifact_polling
from .._artifact import validation as _artifact_validation
from .._artifact.downloads import AssetDownloadService
from .._artifacts import ArtifactsAPI
from .._idempotency import mark_unconfirmed
from .._notebook_metadata import NotebookSourceIdProvider
from .._types.enums import (
    ArtifactTypeCode,
    ExportType,
)
from .._types.research import MindMapResult
from ..exceptions import (
    ArtifactNotFoundError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from ..rpc import RPCMethod
from ..types import (
    Artifact,
    ArtifactCustomizationChoices,
    ArtifactType,
    CopiedArtifact,
    CustomizationChoice,
    GenerationStatus,
    ReportPreset,
    ReportSuggestion,
)
from .artifact.downloads import ArtifactDownloadService
from .artifact.generation import ArtifactGenerationService
from .artifact.listing import ArtifactListingService
from .contracts import RpcCaller
from .mind_maps import NoteBackedMindMapService
from .notes import NoteService
from .params.artifacts import (
    build_copy_artifacts_params,
    build_customization_choices_params,
    build_suggest_reports_params,
)
from .rows import artifacts as _artifact_rows
from .rows.customization import unwrap_customization_choices
from .rows.transfers import CopiedArtifactRow, unwrap_mapping_rows

if TYPE_CHECKING:
    from .._runtime.call_supervisor import CallSupervisor

logger = logging.getLogger("notebooklm._artifacts")


class WebArtifactsAPI(ArtifactsAPI):
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

    def __init__(
        self,
        *,
        rpc: RpcCaller,
        supervisor: "CallSupervisor",
        notebooks: NotebookSourceIdProvider,
        mind_maps: NoteBackedMindMapService,
        note_service: NoteService,
        storage_path: Path | None = None,
    ) -> None:
        """Initialize the artifacts API.

        Args:
            rpc: RPC dispatch surface (:class:`RpcCaller`) — used for direct
                artifact RPCs (delete, rename, export, list_raw) and threaded
                into the generation and download services.
            supervisor: The single logical-call admission authority used for
                polling scopes, child leaders, loop-affinity checks, and drain
                hook registration.
            notebooks: Source-id resolver. Required — wire from
                ``NotebookLMClient`` (no implicit fallback). Threaded into the
                generation service.
            mind_maps: Note-backed mind-map facade (:class:`NoteBackedMindMapService`)
                — owns the ``list_mind_maps`` / ``extract_content`` paths
                consumed by ``_web.artifact.downloads.download_mind_map``.
            note_service: Backend note-row primitives — owns the ``create_note``
                call site that the generation service's ``generate_mind_map``
                uses to persist generated mind maps.
            storage_path: Path to storage state file for loading download cookies.
        """
        super().__init__(
            supervisor=supervisor,
            notebooks=notebooks,
            asset_downloads=AssetDownloadService(storage_path=storage_path),
        )
        self._rpc = rpc
        self._mind_maps = mind_maps
        self._note_service = note_service
        self._listing = ArtifactListingService()
        self._downloads = ArtifactDownloadService(
            rpc=self._rpc,
            listing=self._listing,
            mind_maps=self._mind_maps,
            download_to_path=self._download_to_path,
            download_urls_batch=self._download_urls_batch,
            format_interactive_content=self._format_interactive_content,
        )
        self._generation = ArtifactGenerationService(
            rpc=self._rpc,
            notebooks=self._notebooks,
            note_service=self._note_service,
        )

    async def _send_create_artifact(
        self,
        notebook_id: str,
        family: str,
        source_ids: builtins.list[str],
        **options: Any,
    ) -> GenerationStatus:
        """Dispatch a validated creation request to the web generation service."""
        generate = getattr(self._generation, f"generate_{family}")
        return await generate(notebook_id, source_ids=source_ids, **options)

    # =========================================================================
    # List/Get Operations
    # =========================================================================

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
        logger.debug("Listing artifacts in notebook %s", notebook_id)
        return await self._listing.list_artifacts(
            notebook_id,
            artifact_type,
            list_raw=self._list_raw,
            list_mind_maps=self._list_mind_maps,
        )

    async def _list_for_download(
        self, notebook_id: str, artifact_type: ArtifactType | None = None
    ) -> tuple[builtins.list[Artifact], builtins.list[Any], builtins.list[Any] | None]:
        """List artifacts + the raw rows fetched to build them — same RPC set as
        :meth:`list`. Internal seam for the ``_app`` download executor (#1488)."""
        return await self._listing.list_artifacts_with_raw(
            notebook_id,
            artifact_type,
            list_raw=self._list_raw,
            list_mind_maps=self._list_mind_maps,
        )

    async def get_prompt(self, notebook_id: str, artifact_id: str) -> str | None:
        """Get the free-text prompt an artifact was generated from (any studio type).

        Returns ``None`` when the artifact stores no prompt (e.g. a note-backed
        mind map); raises :class:`ArtifactNotFoundError` for an unknown id.

        .. versionadded:: 0.8.0
        """
        return await self._listing.get_prompt(notebook_id, artifact_id, list_raw=self._list_raw, list_mind_maps=self._list_mind_maps)  # fmt: skip

    # =========================================================================
    # Generate Operations
    # =========================================================================

    async def revise_slide(
        self,
        notebook_id: str,
        artifact_id: str,
        slide_index: int,
        prompt: str,
    ) -> GenerationStatus:
        """Revise an individual slide in a completed slide deck using a prompt."""
        return await self._generation.revise_slide(notebook_id, artifact_id, slide_index, prompt)

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
        return await self._generation.retry_failed(notebook_id, artifact_id)

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
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download an Audio Overview to a file."""
        return await self._downloads.download_audio(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
        )

    async def download_video(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a Video Overview to a file."""
        return await self._downloads.download_video(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
        )

    async def download_infographic(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download an Infographic to a file."""
        return await self._downloads.download_infographic(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
        )

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
        return await self._downloads.download_slide_deck(
            notebook_id, output_path, artifact_id, output_format, artifacts_data=artifacts_data
        )

    async def _download_interactive_artifact(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        output_format: str,
        artifact_type: str,
        *,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str:
        """Download quiz or flashcard artifact."""
        return await self._downloads.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, artifact_type, artifacts=artifacts
        )

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a report artifact as markdown."""
        return await self._downloads.download_report(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
        )

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
        return await self._downloads.download_mind_map(
            notebook_id,
            output_path,
            artifact_id,
            mind_maps=mind_maps,
            artifacts_data=artifacts_data,
        )

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str:
        """Download a data table as CSV."""
        return await self._downloads.download_data_table(
            notebook_id, output_path, artifact_id, artifacts_data=artifacts_data
        )

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
        return await self._download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "quiz", artifacts=artifacts
        )

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
        return await self._download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "flashcards", artifacts=artifacts
        )

    # =========================================================================
    # Management Operations
    # =========================================================================

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
        logger.debug("Deleting artifact %s from notebook %s", artifact_id, notebook_id)
        params = [[2], artifact_id]  # Single-id only; live batch-shape probes failed.
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
        params = [[artifact_id, new_title], [["title"]]]
        await self._rpc.rpc_call(
            RPCMethod.RENAME_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )
        # Resolve via studio artifacts only — never public ``get()`` (#1247) nor
        # the merged listing (a note-backed mind-map id no-ops on RENAME_ARTIFACT
        # — use ``mind_maps.rename``). v0.8.0 (#1362): the lookup runs on
        # ``False`` too so a missing target is detected, but ``False`` still
        # returns ``None`` on success.
        artifact = await self._listing.get_studio_only(
            notebook_id, artifact_id, list_raw=self._list_raw
        )
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id, method_id=RPCMethod.RENAME_ARTIFACT.value)
        return None if not return_object else artifact

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
        """Export a report to Google Docs (``export_type`` selects DOCS/SHEETS)."""
        params = [None, artifact_id, None, title, int(export_type)]
        return await self._rpc.rpc_call(
            RPCMethod.EXPORT_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )

    async def export_data_table(
        self,
        notebook_id: str,
        artifact_id: str,
        title: str = "Export",
    ) -> Any:
        """Export a data table to Google Sheets."""
        params = [None, artifact_id, None, title, int(ExportType.SHEETS)]
        return await self._rpc.rpc_call(
            RPCMethod.EXPORT_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )

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
        params = [None, artifact_id, content, title, int(export_type)]
        return await self._rpc.rpc_call(
            RPCMethod.EXPORT_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
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

        if not (result and isinstance(result, list)):
            return []

        # GET_SUGGESTED_REPORTS returns a wrapped ``[[row1, ...]]`` envelope or a
        # flat list; the wrap probe + per-row decode are centralised behind
        # ``unwrap_artifact_rows`` / ``ReportSuggestionRow`` (#1491).
        items = _artifact_rows.unwrap_artifact_rows(
            result, method_id=RPCMethod.GET_SUGGESTED_REPORTS.value, source="suggest_reports"
        )
        return [
            ReportSuggestion(
                title=row.title,
                description=row.description,
                prompt=row.prompt,
                audience_level=row.audience_level,
            )
            for row in map(_artifact_rows.ReportSuggestionRow, items)
            if row.is_well_formed
        ]

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

        try:
            result = await self._rpc.rpc_call(
                RPCMethod.COPY_ARTIFACTS,
                build_copy_artifacts_params(list(artifact_ids), target_notebook_id),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                raise_on_null_status=True,
                disable_internal_retries=True,
            )
        except (NetworkError, RateLimitError, ServerError) as exc:
            rpc_code = exc.rpc_code if isinstance(exc, RPCError) else None
            raise mark_unconfirmed(
                RPCError(
                    "UNRESOLVED — CopyArtifactsAsync may have committed before its "
                    "response was lost. Do not blindly retry; list the target notebook's "
                    "artifacts and reconcile first.",
                    method_id=RPCMethod.COPY_ARTIFACTS.value,
                    rpc_code=rpc_code,
                )
            ) from exc

        rows = unwrap_mapping_rows(
            result, method_id=RPCMethod.COPY_ARTIFACTS.value, source="CopyArtifactsAsync"
        )
        # A malformed entry is logged and skipped rather than aborting the
        # decode: the well-formed entries are the only proof of copies that have
        # already committed, and dropping them would hide committed writes.
        copied: builtins.list[CopiedArtifact] = []
        malformed = 0
        for raw in rows:
            row = CopiedArtifactRow(raw)
            artifact = (
                Artifact.from_api_response(row.artifact_row)
                if row.is_well_formed and row.artifact_row is not None
                else None
            )
            if row.original_id is None or artifact is None or not artifact.id:
                malformed += 1
                logger.warning(
                    "CopyArtifactsAsync returned a malformed mapping entry: %s",
                    reprlib.repr(raw),
                )
                continue
            copied.append(CopiedArtifact(original_id=row.original_id, artifact=artifact))

        if not copied:
            if malformed:
                raise DecodingError(
                    "CopyArtifactsAsync returned only malformed mapping entries",
                    raw_response=reprlib.repr(rows),
                    method_id=RPCMethod.COPY_ARTIFACTS.value,
                )
            raise ArtifactNotFoundError(
                ", ".join(artifact_ids), method_id=RPCMethod.COPY_ARTIFACTS.value
            )
        missing = set(artifact_ids) - {item.original_id for item in copied}
        if missing:
            logger.warning(
                "CopyArtifactsAsync copied %d of %d artifact(s) into %s; not copied: %s",
                len(copied),
                len(artifact_ids),
                target_notebook_id,
                ", ".join(sorted(missing)),
            )
        return copied

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
        # ``allow_null=False``: the server always serves the table, so a null
        # (status-bearing or not) is drift / rejection, never "no choices".
        result = await self._rpc.rpc_call(
            RPCMethod.GET_CUSTOMIZATION_CHOICES,
            build_customization_choices_params(notebook_id),
            source_path=f"/notebook/{notebook_id}" if notebook_id else "/",
            allow_null=False,
        )
        view = unwrap_customization_choices(
            result,
            method_id=RPCMethod.GET_CUSTOMIZATION_CHOICES.value,
            source="get_customization_choices",
        )

        def _choices(rows: Any) -> tuple[CustomizationChoice, ...]:
            return tuple(
                CustomizationChoice(code=row.code, title=row.title, description=row.description)
                for row in rows
                if row.is_well_formed and row.code is not None
            )

        return ArtifactCustomizationChoices(
            audio=_choices(view.audio_rows),
            video=_choices(view.video_rows),
            slide_deck=_choices(view.slide_deck_rows),
            reports=tuple(
                ReportPreset(
                    report_type=row.report_type,
                    description=row.description,
                    directive=row.directive,
                )
                for row in view.report_rows
                if row.is_well_formed
            ),
        )

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
        """Make a generation RPC call with error handling.

        Facade hop: tests call ``api._call_generate(...)`` directly; the
        implementation lives on :class:`ArtifactGenerationService`.
        """
        return await self._generation._call_generate(
            notebook_id,
            params,
            null_result_artifact_type=null_result_artifact_type,
        )

    async def _list_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """Get raw mind-map rows via the injected mind-map facade."""
        return await self._mind_maps.list_mind_maps(notebook_id)

    async def _list_raw(self, notebook_id: str) -> builtins.list[Any]:
        """Get raw artifact list data."""
        # Keep this facade hop so callers/tests that patch ``api._list_raw``
        # still affect public listing paths that delegate into the service.
        return await self._listing.list_raw(notebook_id, rpc=self._rpc)

    async def _list_studio(
        self,
        notebook_id: str,
        task_id: str,
    ) -> builtins.list[Artifact]:
        """Return the target poll projection without querying note-backed rows."""
        return await self._listing.list_studio(
            notebook_id,
            task_id,
            list_raw=self._list_raw,
        )

    def _select_artifact(
        self,
        candidates: builtins.list[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> Any:
        """Select an artifact from candidates by ID, or return latest completed.

        Single point of completed-artifact selection: filters the raw
        ``_list_raw`` list to entries matching ``type_code`` with status
        ``COMPLETED``, then applies the explicit-ID or latest-timestamp rule.

        The length guard requires only ``len(a) > 4`` — the minimum to read
        ``a[2]`` (type) and ``a[4]`` (status). A completed-but-too-short
        artifact passes here and surfaces as ``ArtifactParseError`` from the
        downstream extractor rather than ``ArtifactNotReadyError`` from this
        filter (downstream wraps ``IndexError``/``TypeError`` into
        ``ArtifactParseError``). ``no_result_error_key`` is *not* in general
        ``type_name.lower()`` — ``download_video`` passes ``"video_overview"``
        to preserve historical exception keys.

        Raises:
            ArtifactNotReadyError: If no candidate is found after filtering.
        """
        return self._listing.select_artifact(
            candidates,
            artifact_id,
            type_name,
            no_result_error_key,
            type_code=type_code,
        )

    def _parse_generation_result(
        self,
        result: Any,
        *,
        method_id: str,
        source: str = "_parse_generation_result",
    ) -> GenerationStatus:
        """Parse a generation result into GenerationStatus.

        Facade hop: tests call ``api._parse_generation_result(...)`` directly;
        the implementation lives on :class:`ArtifactGenerationService`.
        """
        return self._generation._parse_generation_result(result, method_id=method_id, source=source)

    def _get_artifact_type_name(self, artifact_type: int) -> str:
        """Human-readable name for an ``ArtifactTypeCode``, else the raw int as str."""
        return _artifact_polling._get_artifact_type_name(artifact_type)

    def _is_media_ready(self, art: builtins.list[Any], artifact_type: int) -> bool:
        """Check if a media artifact's download URLs are populated.

        For media artifacts (audio, video, infographic, slide deck) the API may
        set status=COMPLETED before the URLs are populated; this verifies they
        are available. Returns ``True`` for non-media types and (defensively)
        on unexpected structure.

        Positional URL locations (BATCHEXECUTE rows): ``art[6][5]`` audio URL
        list, ``art[8][i][0][0]`` video URL string (nested variants/entries),
        ``art[16][3]`` slide-deck PDF URL.
        """
        try:
            if not isinstance(art, list):
                return artifact_type not in _artifact_rows.ArtifactRow._MEDIA_ARTIFACT_TYPES
            return _artifact_rows.ArtifactRow(art).is_media_ready(artifact_type)
        except (IndexError, TypeError):
            return artifact_type not in _artifact_rows.ArtifactRow._MEDIA_ARTIFACT_TYPES
