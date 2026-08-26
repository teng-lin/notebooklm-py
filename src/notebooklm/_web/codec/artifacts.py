"""Web artifact response codecs returning transport-neutral records."""

from __future__ import annotations

import logging
import reprlib
from typing import Any

from ..._backend import BackendContractError
from ..._binding import CodecPayload
from ..._operations import Operation
from ..._records import (
    ArtifactCatalogInput,
    ArtifactCatalogResult,
    ArtifactDeleteInput,
    ArtifactDeleteResult,
    ArtifactDownloadInput,
    ArtifactDownloadResult,
    ArtifactInfographicRecord,
    ArtifactMediaRecord,
    ArtifactParseFailureKind,
    ArtifactParseFailureRecord,
    ArtifactPatchTitleInput,
    ArtifactPatchTitleResult,
    ArtifactPollInput,
    ArtifactPollResult,
    ArtifactRecord,
    ArtifactRepresentationRecord,
    ArtifactSlideRecord,
    ArtifactUserStateRecord,
    DriveExportInput,
    DriveExportResult,
    GenerationStatusRecord,
    MindMapRepresentationRecord,
    ReportSuggestionRecord,
    sanitize_artifact_parse_text,
)
from ..._row_adapters.artifacts import (
    ArtifactRow,
    ReportSuggestionRow,
    _ArtifactUserStateValue,
    _AudioUserStateValue,
    _FlashcardUserStateValue,
    _UnknownUserStateValue,
    unwrap_artifact_rows,
)
from ..._row_adapters.notes import NoteRow
from ...exceptions import ArtifactParseError, DecodingError, UnknownRPCMethodError
from ...rpc import ARTIFACT_STATUS_SUGGESTED_WIRE_NAME, ExportType, RPCMethod, safe_index
from ...rpc.types import ArtifactStatus, ArtifactTypeCode, artifact_status_to_str
from .artifact_formatters import _parse_data_table
from .notes import _decode_note_rows

logger = logging.getLogger("notebooklm._types.artifacts")

_DRIVE_EXPORT_DESTINATIONS = {
    "docs": ExportType.DOCS,
    "sheets": ExportType.SHEETS,
}
_DOWNLOAD_CONTENT_ACTIONS = frozenset({"interactive_html", "mind_map_tree"})

_ARTIFACT_FAMILIES = {
    1: "audio",
    2: "report",
    3: "video",
    5: "mind_map",
    6: "fantasy_map",
    7: "infographic",
    8: "slide_deck",
    9: "data_table",
    10: "file",
}
_ARTIFACT_VARIANTS = {1: "flashcards", 2: "quiz", 4: "interactive_mind_map"}
_MEDIA_KINDS = {1: "progressive", 2: "hls", 3: "dash", 4: "download"}


def _capture_parse_failure(exc: Exception) -> ArtifactParseFailureRecord:
    """Capture only reviewed, sanitized causes that the public API historically exposed."""

    kind_by_type = {
        UnknownRPCMethodError: ArtifactParseFailureKind.UNKNOWN_RPC_METHOD,
        IndexError: ArtifactParseFailureKind.INDEX,
        KeyError: ArtifactParseFailureKind.KEY,
        TypeError: ArtifactParseFailureKind.TYPE,
        ValueError: ArtifactParseFailureKind.VALUE,
    }
    kind = kind_by_type.get(type(exc))
    if kind is None:
        raise TypeError(f"unsupported artifact parse failure type: {type(exc).__name__}") from exc
    raw_response = getattr(exc, "raw_response", None)
    data_at_failure = getattr(exc, "data_at_failure", None)
    return ArtifactParseFailureRecord(
        kind=kind,
        message=sanitize_artifact_parse_text(str(exc.args[0]) if exc.args else ""),
        method_id=getattr(exc, "method_id", None),
        path=getattr(exc, "path", None),
        source=getattr(exc, "source", None),
        found_ids=tuple(getattr(exc, "found_ids", ()) or ()),
        raw_response=(
            sanitize_artifact_parse_text(raw_response) if isinstance(raw_response, str) else None
        ),
        data_at_failure=(
            sanitize_artifact_parse_text(data_at_failure) if data_at_failure is not None else None
        ),
        rpc_code=getattr(exc, "rpc_code", None),
    )


def _decode_user_state(value: _ArtifactUserStateValue) -> ArtifactUserStateRecord:
    if isinstance(value, _AudioUserStateValue):
        return ArtifactUserStateRecord(
            kind="audio",
            playback_position_seconds=float(value.playback_position_seconds),
        )
    if isinstance(value, _FlashcardUserStateValue):
        return ArtifactUserStateRecord(
            kind="flashcards",
            card_acquisitions=value.card_acquisitions,
            current_card_index=value.current_card_index,
            hidden_card_indices=value.hidden_card_indices,
            last_shown_order=value.last_shown_order,
            current_view=value.current_view,
        )
    assert isinstance(value, _UnknownUserStateValue)
    return ArtifactUserStateRecord(kind="unknown", raw=value.raw)


def _artifact_identity(
    type_code: int, variant_code: int | None
) -> tuple[str, int | str | None, str | None, int | str | None]:
    variant = None if variant_code is None else _ARTIFACT_VARIANTS.get(variant_code)
    if type_code == ArtifactTypeCode.QUIZ.value and variant is not None:
        family = "mind_map" if variant == "interactive_mind_map" else variant
        unrecognized_family: int | str | None = None
    else:
        family = _ARTIFACT_FAMILIES.get(type_code, "unknown")
        unrecognized_family = type_code if type_code not in _ARTIFACT_FAMILIES else None
    unrecognized_variant = (
        variant_code
        if type_code == ArtifactTypeCode.QUIZ.value and variant_code is not None and variant is None
        else None
    )
    return family, unrecognized_family, variant, unrecognized_variant


def decode_artifact(data: list[Any]) -> ArtifactRecord:
    """Decode one ``LIST_ARTIFACTS`` row without constructing ``Artifact``."""

    row = ArtifactRow(data)
    type_code = row.type_code
    variant_code = row.variant
    family, unrecognized_family, variant, unrecognized_variant = _artifact_identity(
        type_code, variant_code
    )
    status = artifact_status_to_str(row.status)
    try:
        generation_prompt = row.generation_prompt
    except UnknownRPCMethodError:
        generation_prompt = None
    user_state = row.user_state_value
    return ArtifactRecord(
        id=row.id,
        title=row.title,
        family=family,
        status=status,
        unrecognized_family=unrecognized_family,
        variant=variant,
        interactive_variant_pending=(
            type_code == ArtifactTypeCode.QUIZ.value and variant_code is None
        ),
        unrecognized_variant=unrecognized_variant,
        unrecognized_status=(row.status if status == "unknown" and row.status != 0 else None),
        created_at=row.created_at,
        url=row.artifact_url(type_code, suppress_drift=True),
        generation_prompt=generation_prompt,
        media_urls=tuple(
            ArtifactMediaRecord(
                url=item.url,
                kind=item.kind,
                unrecognized_kind=(
                    item.type_code
                    if item.type_code is not None and item.type_code not in _MEDIA_KINDS
                    else None
                ),
                mime_type=item.mime_type,
            )
            for item in row.media_values
        ),
        duration_seconds=row.duration_seconds,
        slides=tuple(
            ArtifactSlideRecord(
                item.image_url,
                item.width,
                item.height,
                item.alt_text,
                item.text,
            )
            for item in row.slide_values
        ),
        infographics=tuple(
            ArtifactInfographicRecord(
                item.title,
                item.image_url,
                item.width,
                item.height,
                item.alt_text,
                item.text,
            )
            for item in row.infographic_values
        ),
        report_kind=row.report_kind,
        source_ids=row.source_ids,
        last_modified_at=row.last_modified_at,
        etag=row.etag,
        user_state=_decode_user_state(user_state) if user_state is not None else None,
    )


def decode_mind_map_artifact(data: list[Any]) -> ArtifactRecord | None:
    """Decode one note-backed mind-map row, excluding delete tombstones."""

    if not isinstance(data, list) or not data:
        return None
    row = NoteRow(data)
    if row.is_deleted:
        return None
    if row.has_unrecognized_tombstone:
        logger.warning(
            "Mind-map row %s has a null content slot without the "
            "soft-delete sentinel (tombstone drift? a deleted mind map "
            "may be leaking as live): %s",
            row.id,
            reprlib.repr(data),
        )
    return ArtifactRecord(
        id=row.id,
        title=row.title,
        family="mind_map",
        status=artifact_status_to_str(ArtifactStatus.COMPLETED.value),
        created_at=row.created_at,
    )


def decode_report_suggestion(data: list[Any]) -> ReportSuggestionRecord:
    """Decode one suggested-report row."""

    row = ReportSuggestionRow(data)
    return ReportSuggestionRecord(
        title=row.title,
        description=row.description,
        prompt=row.prompt,
        audience_level=row.audience_level,
    )


def decode_artifact_representation(data: list[Any]) -> ArtifactRepresentationRecord:
    """Decode only the representation fields relevant to one artifact family."""

    row = ArtifactRow(data)
    type_code = row.type_code
    variant_code = row.variant if type_code == ArtifactTypeCode.QUIZ.value else None
    family, unrecognized_family, variant, unrecognized_variant = _artifact_identity(
        type_code, variant_code
    )
    status = artifact_status_to_str(row.status)
    artifact = ArtifactRecord(
        id=row.id,
        title=row.title,
        family=family,
        status=status,
        unrecognized_family=unrecognized_family,
        variant=variant,
        unrecognized_variant=unrecognized_variant,
        unrecognized_status=(row.status if status == "unknown" and row.status != 0 else None),
        created_at=row.created_at,
    )
    audio_url = video_url = infographic_url = None
    slide_deck_pdf_url = slide_deck_pptx_url = None
    report_markdown = None
    data_table_headers: tuple[str, ...] = ()
    data_table_rows: tuple[tuple[str, ...], ...] = ()
    data_table_error = parse_error = None
    data_table_failure = parse_failure = None
    try:
        if artifact.family == "audio":
            audio_url = row.audio_url
        elif artifact.family == "video":
            video_url = row.video_url
        elif artifact.family == "infographic":
            infographic_url = row.infographic_url
        elif artifact.family == "slide_deck":
            slide_deck_pdf_url = row.slide_deck_pdf_url
            slide_deck_pptx_url = row.slide_deck_pptx_url
        elif artifact.family == "report":
            report_markdown = row.report_markdown
        elif artifact.family == "data_table":
            try:
                headers, rows = _parse_data_table(row.data_table_raw_payload)
            except ArtifactParseError as exc:
                data_table_error = exc.details or str(exc)
                if exc.cause is not None:
                    data_table_failure = _capture_parse_failure(exc.cause)
            else:
                data_table_headers = tuple(headers)
                data_table_rows = tuple(tuple(item) for item in rows)
    except (IndexError, TypeError, UnknownRPCMethodError) as exc:
        parse_error = str(exc)
        parse_failure = _capture_parse_failure(exc)
    return ArtifactRepresentationRecord(
        artifact=artifact,
        audio_url=audio_url,
        video_url=video_url,
        infographic_url=infographic_url,
        slide_deck_pdf_url=slide_deck_pdf_url,
        slide_deck_pptx_url=slide_deck_pptx_url,
        report_markdown=report_markdown,
        data_table_headers=data_table_headers,
        data_table_rows=data_table_rows,
        data_table_error=data_table_error,
        data_table_failure=data_table_failure,
        parse_error=parse_error,
        parse_failure=parse_failure,
    )


def decode_mind_map_representation(data: list[Any]) -> MindMapRepresentationRecord | None:
    """Decode one live note-backed mind map without retaining its wire row."""

    row = NoteRow(data)
    if row.is_deleted or not row.is_mind_map:
        return None
    return MindMapRepresentationRecord(
        id=row.id,
        title=row.title,
        content=row.content,
        created_at=row.created_at,
    )


def decode_mind_map_representations(result: object) -> tuple[MindMapRepresentationRecord, ...]:
    """Decode live note-backed maps from the mixed note collection."""

    return tuple(
        record
        for row in _decode_note_rows(result)
        if (record := decode_mind_map_representation(row)) is not None
    )


def decode_interactive_content(result: object, *, tree: bool) -> str | None:
    """Decode quiz/flashcard HTML or an interactive mind-map tree leaf."""

    if result is None:
        return None
    payload = safe_index(
        result,
        0,
        9,
        method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
        source=(
            "_artifact_downloads._get_interactive_mind_map_tree"
            if tree
            else "_artifact_downloads._get_artifact_content"
        ),
    )
    if not isinstance(payload, list):
        raise UnknownRPCMethodError(
            f"safe_index drift at path (0, 9): options block is "
            f"{type(payload).__name__}, not a list",
            method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
            path=(0, 9),
            source=(
                "_artifact_downloads._get_interactive_mind_map_tree"
                if tree
                else "_artifact_downloads._get_artifact_content"
            ),
            data_at_failure=reprlib.repr(payload),
        )
    index = 3 if tree else 0
    if len(payload) <= index:
        return None
    value = payload[index]
    return value if isinstance(value, str) else None


def decode_artifact_poll(
    rows: list[list[Any]],
    task_id: str,
) -> GenerationStatusRecord:
    """Decode one lifecycle observation with the legacy media-settling rule."""

    row = None
    for item in rows:
        candidate = ArtifactRow(item)
        if candidate.id == task_id:
            row = candidate
            break
    if row is None:
        return GenerationStatusRecord(task_id=task_id, status="not_found")

    status_code = row.status
    raw_status = artifact_status_to_str(status_code)
    metadata: tuple[tuple[str, object], ...] = ()
    if status_code == ArtifactStatus.COMPLETED.value and not row.is_media_ready(row.type_code):
        try:
            type_name = ArtifactTypeCode(row.type_code).name
        except ValueError:
            type_name = str(row.type_code)
        metadata = (
            ("artifact_type", type_name),
            ("artifact_type_code", row.type_code),
            ("media_ready", False),
            ("normalized_status", "in_progress"),
            ("raw_status", raw_status),
        )
        status = "in_progress"
    else:
        status = raw_status
    return GenerationStatusRecord(
        task_id=task_id,
        status=status,
        url=row.artifact_url(row.type_code, suppress_drift=True),
        metadata=metadata,
    )


# --- P9.3 Studio codec rows ----------------------------------------------------
# Row-facing payload builders and decoders behind ``_web/bindings/studio.py``.
# Each returns the full request payload one codec row dispatches — params plus
# the notebook route and typed options — and never names a method: the row's
# ``NativeCallSpec`` is the sole method authority.


def encode_studio_catalog_params(notebook_id: str) -> list[Any]:
    """The ``LIST_ARTIFACTS`` params every Studio catalog read issues."""

    return [
        [2],
        notebook_id,
        f'NOT artifact.status = "{ARTIFACT_STATUS_SUGGESTED_WIRE_NAME}"',
    ]


def decode_studio_rows(result: object, *, source: str) -> list[list[object]]:
    """Unwrap one ``LIST_ARTIFACTS`` response into raw Studio rows."""

    if isinstance(result, list):
        return unwrap_artifact_rows(
            result,
            method_id=RPCMethod.LIST_ARTIFACTS.value,
            source=source,
        )
    if not result:
        return []
    raise DecodingError(
        "Unrecognized LIST_ARTIFACTS payload shape",
        raw_response=reprlib.repr(result),
        method_id=RPCMethod.LIST_ARTIFACTS.value,
    )


def encode_artifact_catalog(notebook_id: str) -> CodecPayload:
    """The guarded ``LIST_ARTIFACTS`` read the catalog composites issue (null success accepted)."""

    return CodecPayload(
        params=encode_studio_catalog_params(notebook_id),
        source_path=f"/notebook/{notebook_id}",
        allow_null=True,
    )


def decode_artifact_catalog(result: object, *, source: str) -> list[ArtifactRecord]:
    """Decode one ``LIST_ARTIFACTS`` catalog read into Studio artifact records."""

    rows = decode_studio_rows(result, source=source)
    return [decode_artifact(row) for row in rows if isinstance(row, list) and row]


def encode_artifact_delete(value: ArtifactDeleteInput) -> CodecPayload:
    """Payload for the ``artifact.delete`` codec row."""

    return CodecPayload(
        params=[[2], value.artifact_id],
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


def decode_artifact_delete(value: ArtifactDeleteInput, result: object) -> ArtifactDeleteResult:
    """Row decoder for ``artifact.delete``: the acknowledgement carries no signal."""

    del value, result
    return ArtifactDeleteResult()


def encode_artifact_patch_title(value: ArtifactPatchTitleInput) -> CodecPayload:
    """Payload for the one-call ``artifact.patch_title`` primitive."""

    return CodecPayload(
        params=[[value.artifact_id, value.new_title], [["title"]]],
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


def decode_artifact_patch_title(
    value: ArtifactPatchTitleInput, result: object
) -> ArtifactPatchTitleResult:
    """Decode the title set-op acknowledgement, whose response carries no signal."""

    del value, result
    return ArtifactPatchTitleResult()


def encode_artifact_catalog_row(value: ArtifactCatalogInput) -> CodecPayload:
    """Payload for one plain Studio catalog read without note-backed mind maps."""

    return CodecPayload(
        params=encode_studio_catalog_params(value.notebook_id),
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


def decode_artifact_catalog_row(
    value: ArtifactCatalogInput, result: object
) -> ArtifactCatalogResult:
    """Decode one plain Studio catalog snapshot in backend order."""

    del value
    rows = decode_studio_rows(result, source="WebRpcBackend._artifact_catalog_records")
    # The row guard matches ``decode_artifact_catalog``'s: ``unwrap_artifact_rows``
    # is a permissive shape probe that can hand back scalars from a drifted
    # payload, and ``decode_artifact`` indexes its argument. Skipping those rows
    # is the catalog listing's long-standing policy; without the guard this leaf
    # raises a bare TypeError/KeyError outside the closed failure family.
    return ArtifactCatalogResult(
        artifacts=tuple(decode_artifact(row) for row in rows if isinstance(row, list) and row)
    )


def encode_artifact_export(value: DriveExportInput) -> CodecPayload:
    """Payload for the ``artifact.export`` codec row (Google Drive companion export)."""

    destination = _DRIVE_EXPORT_DESTINATIONS.get(value.destination)
    if destination is None:
        raise BackendContractError(
            f"unrecognized Drive export destination {value.destination!r}",
            operation=Operation.ARTIFACT_EXPORT,
        )
    return CodecPayload(
        params=[None, value.artifact_id, value.content, value.title, int(destination)],
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


def decode_artifact_export(value: DriveExportInput, result: object) -> DriveExportResult:
    """Row decoder for ``artifact.export``: the opaque response is preserved."""

    del value
    return DriveExportResult(result)


def encode_artifact_wait(value: ArtifactPollInput) -> CodecPayload:
    """Payload for the ``artifact.wait`` codec row (one catalog read per poll tick)."""

    return CodecPayload(
        params=encode_studio_catalog_params(value.notebook_id),
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


def decode_artifact_wait(value: ArtifactPollInput, result: object) -> ArtifactPollResult:
    """Row decoder for ``artifact.wait``: one lifecycle observation for ``task_id``."""

    rows = decode_studio_rows(result, source="WebRpcBackend._artifact_wait")
    return ArtifactPollResult(decode_artifact_poll(rows, value.task_id))


def encode_artifact_download(value: ArtifactDownloadInput) -> CodecPayload:
    """Payload for the ``artifact.download`` codec row, keyed on ``value.action``.

    The row's ``NativeCallSpec`` selects the native from the same action; this
    builder only shapes the params and rejects the inputs the handler rejected
    before dispatch.
    """

    if value.action == "catalog":
        params: list[Any] = encode_studio_catalog_params(value.notebook_id)
    elif value.action == "mind_maps":
        params = [value.notebook_id]
    elif value.action in _DOWNLOAD_CONTENT_ACTIONS:
        if value.artifact_id is None:
            raise BackendContractError(
                f"artifact.download action {value.action!r} requires artifact_id",
                operation=Operation.ARTIFACT_DOWNLOAD,
            )
        params = [value.artifact_id]
    else:
        raise BackendContractError(
            f"unrecognized artifact.download action {value.action!r}",
            operation=Operation.ARTIFACT_DOWNLOAD,
        )
    return CodecPayload(
        params=params,
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


def decode_artifact_download(
    value: ArtifactDownloadInput, result: object
) -> ArtifactDownloadResult:
    """Row decoder for ``artifact.download``, branching on the same action as the encoder."""

    if value.action == "catalog":
        rows = decode_studio_rows(
            result,
            source="ArtifactRepresentationService._list_representations",
        )
        return ArtifactDownloadResult(
            representations=tuple(decode_artifact_representation(row) for row in rows)
        )
    if value.action == "mind_maps":
        return ArtifactDownloadResult(mind_maps=decode_mind_map_representations(result))
    if value.action in _DOWNLOAD_CONTENT_ACTIONS:
        return ArtifactDownloadResult(
            content=decode_interactive_content(result, tree=value.action == "mind_map_tree")
        )
    raise BackendContractError(
        f"unrecognized artifact.download action {value.action!r}",
        operation=Operation.ARTIFACT_DOWNLOAD,
    )


__all__ = [
    "decode_artifact",
    "decode_artifact_catalog",
    "decode_artifact_catalog_row",
    "decode_artifact_delete",
    "decode_artifact_download",
    "decode_artifact_export",
    "decode_artifact_representation",
    "decode_artifact_poll",
    "decode_artifact_patch_title",
    "decode_artifact_wait",
    "decode_interactive_content",
    "decode_mind_map_artifact",
    "decode_mind_map_representation",
    "decode_mind_map_representations",
    "decode_report_suggestion",
    "decode_studio_rows",
    "encode_artifact_catalog",
    "encode_artifact_catalog_row",
    "encode_artifact_delete",
    "encode_artifact_download",
    "encode_artifact_export",
    "encode_artifact_patch_title",
    "encode_artifact_wait",
    "encode_studio_catalog_params",
]
