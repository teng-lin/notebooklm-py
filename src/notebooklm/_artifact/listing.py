"""Private artifact listing and selection helpers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from .._row_adapters.artifacts import ArtifactRow, unwrap_artifact_rows
from .._row_adapters.notes import NoteRow
from .._runtime.contracts import RpcCaller
from ..exceptions import DecodingError
from ..rpc import (
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    ArtifactTypeCode,
    RPCError,
    RPCMethod,
)
from ..types import Artifact, ArtifactNotFoundError, ArtifactNotReadyError, ArtifactType

logger = logging.getLogger(__name__)

ListRawCallback = Callable[[str], Awaitable[list[Any]]]
ListMindMapsCallback = Callable[[str], Awaitable[list[Any]]]
ListArtifactsCallback = Callable[[str], Awaitable[list[Artifact]]]

_ARTIFACT_TYPE_CODES_BY_KIND = {
    ArtifactType.AUDIO: ArtifactTypeCode.AUDIO.value,
    ArtifactType.REPORT: ArtifactTypeCode.REPORT.value,
    ArtifactType.VIDEO: ArtifactTypeCode.VIDEO.value,
    ArtifactType.MIND_MAP: ArtifactTypeCode.MIND_MAP.value,
    ArtifactType.INFOGRAPHIC: ArtifactTypeCode.INFOGRAPHIC.value,
    ArtifactType.SLIDE_DECK: ArtifactTypeCode.SLIDE_DECK.value,
    ArtifactType.DATA_TABLE: ArtifactTypeCode.DATA_TABLE.value,
}
_KNOWN_ARTIFACT_TYPE_CODES = frozenset(_ARTIFACT_TYPE_CODES_BY_KIND.values())


def iter_artifact_rows(candidates: Sequence[Any]) -> list[ArtifactRow]:
    """Wrap raw list-shaped artifact candidates in ``ArtifactRow`` adapters."""
    return [ArtifactRow(candidate) for candidate in candidates if isinstance(candidate, list)]


def find_artifact_row_by_id(candidates: Sequence[Any], artifact_id: str) -> ArtifactRow | None:
    """Find any artifact row by ID without filtering by completion status."""
    for row in iter_artifact_rows(candidates):
        if row.id == artifact_id:
            return row
    return None


def _matches_artifact_type(artifact: Artifact, artifact_type: ArtifactType | None) -> bool:
    """Return whether ``artifact`` matches ``artifact_type`` without noisy kind warnings."""
    if artifact_type is None:
        return True

    if artifact_type == ArtifactType.QUIZ:
        return (
            artifact._artifact_type == ArtifactTypeCode.QUIZ.value
            and artifact._variant == QUIZ_VARIANT
        )
    if artifact_type == ArtifactType.FLASHCARDS:
        return (
            artifact._artifact_type == ArtifactTypeCode.QUIZ.value
            and artifact._variant == FLASHCARDS_VARIANT
        )
    if artifact_type == ArtifactType.MIND_MAP:
        # Two backings: note-backed (synthetic type 5) and interactive
        # (studio artifact, type 4 / variant 4). Match either.
        return (
            artifact._artifact_type == ArtifactTypeCode.MIND_MAP.value
            or artifact.is_interactive_mind_map
        )
    if artifact_type == ArtifactType.UNKNOWN:
        if artifact._artifact_type == ArtifactTypeCode.QUIZ.value:
            return artifact._variant not in (
                FLASHCARDS_VARIANT,
                QUIZ_VARIANT,
                INTERACTIVE_MIND_MAP_VARIANT,
            )
        return artifact._artifact_type not in _KNOWN_ARTIFACT_TYPE_CODES

    type_code = _ARTIFACT_TYPE_CODES_BY_KIND.get(artifact_type)
    if type_code is not None:
        return artifact._artifact_type == type_code

    return False


class ArtifactListingService:
    """List, filter, and select artifacts without depending on the facade."""

    async def list_raw(self, notebook_id: str, *, rpc: RpcCaller) -> list[Any]:
        """Get raw studio artifact rows from NotebookLM."""
        params = [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']
        result = await rpc.rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # LIST_ARTIFACTS returns either a wrapped single-element envelope
        # (``[[row1, row2, ...]]``) or an already-flat list of rows. The wrap
        # probe (``result[0]`` / ``inner[0]``) is centralised in
        # ``unwrap_artifact_rows`` so the envelope-position knowledge lives in
        # one place (issue #1491); it returns the flat rows unchanged for the
        # already-flat shape.
        if isinstance(result, list):
            return unwrap_artifact_rows(
                result,
                method_id=RPCMethod.LIST_ARTIFACTS.value,
                source="ArtifactListingService.list_raw",
            )
        if not result:
            return []
        # A truthy non-list payload is schema drift, not an empty notebook —
        # raise so callers can tell a miss from drift instead of an empty list.
        raise DecodingError(
            "Unrecognized LIST_ARTIFACTS payload shape",
            raw_response=repr(result),
            method_id=RPCMethod.LIST_ARTIFACTS.value,
        )

    async def list_artifacts(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None,
        *,
        list_raw: ListRawCallback,
        list_mind_maps: ListMindMapsCallback,
    ) -> list[Artifact]:
        """List public artifacts from studio rows plus mind-map rows."""
        artifacts, _raw, _mm = await self.list_artifacts_with_raw(
            notebook_id,
            artifact_type,
            list_raw=list_raw,
            list_mind_maps=list_mind_maps,
        )
        return artifacts

    async def list_artifacts_with_raw(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None,
        *,
        list_raw: ListRawCallback,
        list_mind_maps: ListMindMapsCallback,
    ) -> tuple[list[Artifact], list[Any], list[Any] | None]:
        """List artifacts *and* return the raw rows fetched to build them.

        Returns ``(typed_artifacts, raw_studio_rows, mind_map_rows)`` from a
        single pass over each backing RPC. The download executor uses this to
        select a target from the typed list while threading the raw rows it
        already fetched into the per-type ``download_<x>`` method — so that
        method does not re-issue ``LIST_ARTIFACTS`` / ``GET_NOTES_AND_MIND_MAPS``
        (issue #1488). The typed projection / merge / partial-availability policy
        is identical to :meth:`list_artifacts`, which now delegates here.

        ``mind_map_rows`` is the raw note-backed list when the mind-map sub-fetch
        ran (``artifact_type`` is ``None`` or ``MIND_MAP``) and succeeded — ``[]``
        included, meaning "fetched, genuinely no mind maps". It is ``None`` when
        the sub-fetch's transport failed (``RPCError`` / ``HTTPError``): a
        distinct "fetch failed, value unknown" sentinel so a caller threading it
        into ``download_mind_map`` passes ``None`` and the method re-fetches (and
        surfaces the error) rather than mistaking the outage for "no mind maps".
        It is also ``[]`` when the sub-fetch was skipped (a specific non-mind-map
        ``artifact_type``); that list is never threaded to ``download_mind_map``.
        """
        raw_studio_rows = await list_raw(notebook_id)
        artifacts = self._filter_studio_artifacts(raw_studio_rows, artifact_type)

        mind_map_rows: list[Any] | None = []
        if artifact_type is None or artifact_type == ArtifactType.MIND_MAP:
            try:
                mind_map_rows = await list_mind_maps(notebook_id)
                artifacts.extend(self._filter_mind_map_artifacts(mind_map_rows, artifact_type))
            except DecodingError:
                # Schema drift is not a transient outage: surface it (#1344)
                # rather than masking drifted mind-map rows as "no mind maps".
                raise
            except (RPCError, httpx.HTTPError) as e:
                # Network/API errors - log and continue with studio artifacts so
                # users still see audio/video/reports when the mind-map endpoint
                # is temporarily unavailable. Use ``None`` (not ``[]``) as the
                # "fetch failed" sentinel so a downstream caller re-fetches.
                mind_map_rows = None
                logger.warning("Failed to fetch mind maps: %s", e)

        return artifacts, raw_studio_rows, mind_map_rows

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        list_artifacts: ListArtifactsCallback,
    ) -> Artifact | None:
        """Get a public artifact by ID from the public artifact listing."""
        artifacts = await list_artifacts(notebook_id)
        for artifact in artifacts:
            if artifact.id == artifact_id:
                return artifact
        return None

    async def get_studio_only(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        list_raw: ListRawCallback,
    ) -> Artifact | None:
        """Get a studio artifact by ID, excluding note-backed mind-map rows.

        ``RENAME_ARTIFACT`` only applies to genuine studio artifacts; note-backed
        mind maps rename via ``UPDATE_NOTE`` (see ``MindMapsAPI``). Hydrating the
        rename result from the *merged* listing (studio + note-backed mind maps)
        would let a note-backed mind-map id read back as a stale "success" after
        a no-op ``RENAME_ARTIFACT``. Restricting to studio rows makes such an id
        correctly absent here so the caller raises ``ArtifactNotFoundError``
        and is steered to ``mind_maps.rename``.
        """
        for artifact in self._filter_studio_artifacts(await list_raw(notebook_id), None):
            if artifact.id == artifact_id:
                return artifact
        return None

    async def get_prompt(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        list_raw: ListRawCallback,
        list_mind_maps: ListMindMapsCallback | None = None,
    ) -> str | None:
        """Return the generation prompt for a single studio artifact.

        Looks the artifact up in the studio listing (any status — the prompt is
        stored at creation, so failed artifacts carry it too) and reads its
        prompt through :attr:`ArtifactRow.generation_prompt`.

        Returns ``None`` when the artifact exists but has no readable prompt
        (e.g. a type whose prompt slot is absent), or when ``artifact_id``
        belongs to a note-backed mind map (not in the studio listing) and
        ``list_mind_maps`` is provided and confirms the id exists there.

        Raises :class:`ArtifactNotFoundError` when no studio artifact matches
        ``artifact_id`` and either ``list_mind_maps`` is ``None`` or the id is
        absent from the mind-map listing too.
        """
        row = find_artifact_row_by_id(await list_raw(notebook_id), artifact_id)
        if row is not None:
            return row.generation_prompt
        if list_mind_maps is not None:
            mind_map_rows = await list_mind_maps(notebook_id)
            if any(NoteRow(m).id == artifact_id for m in mind_map_rows):
                return None
        raise ArtifactNotFoundError(artifact_id, method_id=RPCMethod.LIST_ARTIFACTS.value)

    def select_artifact(
        self,
        candidates: Sequence[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> Any:
        """Select an artifact from candidates by ID or return latest completed.

        Position knowledge (``a[2]`` type, ``a[4]`` status, ``a[15][0]``
        timestamp) is delegated to
        :class:`notebooklm._row_adapters.artifacts.ArtifactRow` — when Google
        reshapes the wire, the position constants change there and this
        method adapts automatically.

        The error-key asymmetry is intentional: explicit-ID misses
        derive the key from ``type_name`` while empty-filter results use
        ``no_result_error_key`` verbatim.

        Returns the **raw row** (not an :class:`ArtifactRow`) to preserve
        the historical private helper contract. New internal callers that
        need typed access should use :meth:`select_completed_artifact_row`.
        """
        return self.select_completed_artifact_row(
            candidates,
            artifact_id,
            type_name,
            no_result_error_key,
            type_code=type_code,
        ).raw

    def select_completed_artifact_row(
        self,
        candidates: Sequence[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> ArtifactRow:
        """Select a completed artifact row by ID or latest timestamp."""
        rows = iter_artifact_rows(candidates)
        filtered = [row for row in rows if row.matches_type(type_code, completed_only=True)]

        if artifact_id:
            match = next((row for row in filtered if row.id == artifact_id), None)
            if not match:
                raise ArtifactNotReadyError(
                    type_name.lower().replace(" ", "_"), artifact_id=artifact_id
                )
            return match

        if not filtered:
            raise ArtifactNotReadyError(no_result_error_key)

        # Sort by raw timestamp so missing / ``None`` / non-list shapes
        # coerce to ``0`` without crashing the comparison (mirrors the
        # historical ``(a[15][0] or 0)`` falsy-coerce trick that pinned
        # the ``test_handles_none_at_timestamp_position_without_typeerror``
        # contract).
        filtered.sort(key=lambda row: row.created_at_raw or 0, reverse=True)
        # ``filtered`` is a non-empty list of typed ``ArtifactRow`` objects (not
        # a raw RPC payload); take the most-recent head via ``head, *_ = filtered``
        # so this typed-sequence pick is not the ``name[int]`` RPC-row shape.
        head, *_ = filtered  # typed ArtifactRow head; unpack avoids the name[int] ratchet
        return head

    def _filter_studio_artifacts(
        self,
        artifacts_data: Sequence[Any],
        artifact_type: ArtifactType | None,
    ) -> list[Artifact]:
        artifacts: list[Artifact] = []
        for art_data in artifacts_data:
            if isinstance(art_data, list) and len(art_data) > 0:
                artifact = Artifact.from_api_response(art_data)
                if _matches_artifact_type(artifact, artifact_type):
                    artifacts.append(artifact)
        return artifacts

    def _filter_mind_map_artifacts(
        self,
        mind_maps: Sequence[Any],
        artifact_type: ArtifactType | None,
    ) -> list[Artifact]:
        artifacts: list[Artifact] = []
        for mm_data in mind_maps:
            if isinstance(mm_data, list):
                mind_map_artifact = Artifact.from_mind_map(mm_data)
                if mind_map_artifact is not None:
                    if _matches_artifact_type(mind_map_artifact, artifact_type):
                        artifacts.append(mind_map_artifact)
        return artifacts
