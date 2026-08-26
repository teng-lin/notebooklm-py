"""Web source response codecs returning transport-neutral records."""

from __future__ import annotations

import builtins
import logging
import re
import reprlib
import types
from collections.abc import Sequence
from typing import Any

from ..._backend import BackendContractError, BackendError, BackendErrorReason
from ..._binding import CodecPayload
from ..._operations import Operation
from ..._row_adapters.sources import (
    SourceFulltextRow,
    SourceGuideRow,
    SourceRow,
    interpret_source_freshness,
    unwrap_add_source_rows,
)
from ..._semantic.records import (
    SourceDeleteInput,
    SourceDeleteResult,
    SourceFileRegistrationRecord,
    SourceFreshnessInput,
    SourceFreshnessResult,
    SourceFulltextInput,
    SourceFulltextRecord,
    SourceFulltextResult,
    SourceGetInput,
    SourceGetResult,
    SourceGuideInput,
    SourceGuideRecord,
    SourceGuideResult,
    SourceListInput,
    SourceListResult,
    SourcePatchTitleInput,
    SourcePatchTitleResult,
    SourceRecord,
    SourceRefreshInput,
    SourceRefreshResult,
    SourceRegisterInput,
    SourceRegisterKind,
    SourceRegisterResult,
    SourceWaitSnapshotInput,
    SourceWaitSnapshotResult,
)
from ..._url_utils import pdf_url_display_title
from ...exceptions import SourceNotFoundError
from ...rpc import RPCError, RPCMethod, safe_index
from ...rpc.types import drive_source_status_to_str, source_status_to_str
from .documents import decode_structured_document

_SOURCE_KINDS = {
    0: "unknown",
    1: "google_docs",
    2: "google_slides",
    3: "pdf",
    4: "pasted_text",
    5: "web_page",
    6: "powerpoint",
    8: "markdown",
    9: "youtube",
    10: "media",
    11: "docx",
    13: "image",
    14: "google_spreadsheet",
    16: "csv",
    17: "epub",
}

_SOURCE_ID_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SOURCE_ID_FIELD_NAMES = frozenset({"SOURCE_ID", "source_id", "sourceId"})
_CONTEXTUAL_SOURCE_ID_FIELD_NAMES = frozenset({"id"})
_SOURCE_NAME_FIELD_NAMES = frozenset(
    {"SOURCE_NAME", "source_name", "sourceName", "filename", "fileName", "name", "title"}
)
_SOURCE_ID_ENVELOPE_MAX_DEPTH = 8


def _unwrap_singleton_envelope(value: Any) -> tuple[Any, int]:
    depth = 0
    while isinstance(value, list) and len(value) == 1 and depth < _SOURCE_ID_ENVELOPE_MAX_DEPTH:
        (value,) = value
        depth += 1
    return value, depth


def _coerce_filename_candidate(value: Any) -> str | None:
    value, _depth = _unwrap_singleton_envelope(value)
    return value.strip() if isinstance(value, str) else None


def _looks_like_id_string(candidate: str) -> bool:
    return (
        len(candidate) >= 4
        and not any(character in candidate for character in " \t/")
        and any(character.isdigit() or character in "-_" for character in candidate)
    )


def _coerce_source_id_candidate(value: Any, filename: str) -> str | None:
    value, _depth = _unwrap_singleton_envelope(value)
    if not isinstance(value, str) or len(value) > 1000:
        return None
    candidate = value.strip()
    if not candidate or candidate == filename:
        return None
    if _SOURCE_ID_UUID_PATTERN.match(candidate) or _looks_like_id_string(candidate):
        return candidate
    return None


def _source_context_names(node: dict[Any, Any]) -> list[Any]:
    return [
        value
        for key, value in node.items()
        if isinstance(key, str) and key in _SOURCE_NAME_FIELD_NAMES
    ]


def _extract_source_id_field_candidates(result: Any, filename: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(value: Any) -> None:
        candidate = _coerce_source_id_candidate(value, filename)
        if candidate is not None and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    def walk(node: Any, depth: int) -> None:
        if depth > _SOURCE_ID_ENVELOPE_MAX_DEPTH:
            return
        if isinstance(node, dict):
            names = _source_context_names(node)
            matched_context = bool(names) and any(
                _coerce_filename_candidate(name) == filename for name in names
            )
            mismatched_context = bool(names) and not matched_context
            for key, value in node.items():
                if not isinstance(key, str):
                    continue
                if (
                    key in _SOURCE_ID_FIELD_NAMES
                    and not mismatched_context
                    and (depth == 0 or matched_context)
                ) or (key in _CONTEXTUAL_SOURCE_ID_FIELD_NAMES and matched_context):
                    add_candidate(value)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)

    walk(result, 0)
    return candidates


def _extract_contextual_source_id_row_candidates(result: Any, filename: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(value: Any) -> None:
        candidate = _coerce_source_id_candidate(value, filename)
        if candidate is not None and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    def walk(node: Any, depth: int) -> None:
        if depth > _SOURCE_ID_ENVELOPE_MAX_DEPTH:
            return
        if isinstance(node, list):
            if len(node) >= 2:
                first, second, *_rest = node
                if _coerce_filename_candidate(second) == filename:
                    add_candidate(first)
                if _coerce_filename_candidate(first) == filename:
                    add_candidate(second)
            for child in node:
                walk(child, depth + 1)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)

    walk(result, 0)
    return candidates


def _extract_singleton_source_id_envelope(result: Any, filename: str) -> str | None:
    node, depth = _unwrap_singleton_envelope(result)
    return None if depth == 0 else _coerce_source_id_candidate(node, filename)


def _extract_prefixed_singleton_source_id_envelope(result: Any, filename: str) -> str | None:
    if not isinstance(result, list) or len(result) != 2:
        return None
    prefix, inner = result
    return _extract_singleton_source_id_envelope(inner, filename) if prefix is None else None


def _extract_register_file_source_id(result: Any, filename: str) -> str | None:
    field_candidates = _extract_source_id_field_candidates(result, filename)
    if len(field_candidates) == 1:
        (candidate,) = field_candidates
        return candidate
    if len(field_candidates) > 1:
        return None

    row_candidates = _extract_contextual_source_id_row_candidates(result, filename)
    if len(row_candidates) == 1:
        (candidate,) = row_candidates
        return candidate
    if len(row_candidates) > 1:
        return None

    prefixed = _extract_prefixed_singleton_source_id_envelope(result, filename)
    if prefixed is not None:
        return prefixed
    return _extract_singleton_source_id_envelope(result, filename)


def _register_response_shape_label(result: Any) -> str:
    if isinstance(result, dict):
        return "object"
    if isinstance(result, list):
        return "array"
    if isinstance(result, str):
        return "string"
    if result is None:
        return "null"
    return type(result).__name__


def _template_block() -> list[Any]:
    """Return the captured create/source request-options wrapper."""
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def _effective_type_code(row: SourceRow) -> int | None:
    type_code = row.type_code
    for mime in (row.content_mime, row.mime):
        if type_code == 14 and mime == "application/pdf":
            type_code = 3
    return type_code


def decode_source_row(row: SourceRow) -> SourceRecord:
    """Decode one normalized source row without constructing ``Source``."""

    type_code = _effective_type_code(row)
    title = row.title
    if title is not None and title == row.url and type_code == 3:
        title = pdf_url_display_title(title) or title
    return SourceRecord(
        id=row.id,
        title=title,
        url=row.url,
        kind=_SOURCE_KINDS.get(type_code, "unknown") if type_code is not None else "unknown",
        unrecognized_kind=(
            type_code if type_code is not None and type_code not in _SOURCE_KINDS else None
        ),
        created_at=row.created_at,
        status=source_status_to_str(row.status),
        drive_document_id=row.drive_document_id,
        drive_status=(
            drive_source_status_to_str(row.drive_status) if row.drive_status is not None else None
        ),
        download_url=row.download_url,
        viewer_url=row.viewer_url,
        content_mime=row.content_mime,
        word_count=row.word_count,
        revision_id=row.revision_id,
        revision_timestamp=row.revision_timestamp,
        last_modified_at=row.last_modified_at,
        kind_present=type_code is not None,
    )


def decode_source_snapshot(
    notebook_id: str,
    payload: Any,
    *,
    strict: bool = False,
    logger: logging.Logger,
) -> tuple[SourceRecord, ...]:
    """Decode one ``GET_NOTEBOOK`` envelope into unique source records."""

    if not payload or not isinstance(payload, builtins.list):
        logger.warning(
            "SourcesAPI.list: Empty or invalid notebook response when listing sources for %s "
            "(API response structure may have changed)",
            notebook_id,
        )
        raise RPCError(f"Could not list sources for {notebook_id}: API response structure changed")

    notebook_row = safe_index(
        payload,
        0,
        method_id=RPCMethod.GET_NOTEBOOK.value,
        source="decode_source_snapshot",
    )
    if not isinstance(notebook_row, builtins.list) or len(notebook_row) <= 1:
        logger.warning(
            "SourcesAPI.list: Unexpected notebook structure for %s: expected list with "
            "sources at index 1 (API structure may have changed)",
            notebook_id,
        )
        raise RPCError(f"Could not list sources for {notebook_id}: API response structure changed")

    source_rows = safe_index(
        notebook_row,
        1,
        method_id=RPCMethod.GET_NOTEBOOK.value,
        source="decode_source_snapshot",
    )
    if source_rows is None:
        return ()
    if not isinstance(source_rows, builtins.list):
        logger.warning(
            "SourcesAPI.list: Sources data for %s is not a list (type=%s), returning empty "
            "list (API structure may have changed)",
            notebook_id,
            type(source_rows).__name__,
        )
        raise RPCError(
            f"Could not list sources for {notebook_id}: "
            f"sources data is {type(source_rows).__name__}, not list"
        )

    seen: dict[str, SourceRecord] = {}
    sources: list[SourceRecord] = []
    for index, raw_row in enumerate(source_rows):
        if not isinstance(raw_row, builtins.list) or not raw_row:
            if strict:
                raise RPCError(
                    f"Could not list sources for {notebook_id}: "
                    f"malformed source row at index {index}"
                )
            continue
        row = SourceRow.from_entry(raw_row, method_id=RPCMethod.GET_NOTEBOOK.value)
        if not row.has_id:
            logger.warning(
                "SourcesAPI.list: Skipping source with unexpected id shape: %s",
                repr(raw_row)[:500],
            )
            if strict:
                raise RPCError(
                    f"Could not list sources for {notebook_id}: "
                    f"source row at index {index} has no usable id"
                )
            continue
        if strict and (shape_error := row.listing_shape_error()) is not None:
            raise RPCError(
                f"Could not list sources for {notebook_id}: "
                f"incomplete source row at index {index} ({shape_error})"
            )
        source = decode_source_row(row)
        previous = seen.get(source.id)
        if previous is not None:
            if strict and source != previous:
                raise RPCError(
                    f"Could not list sources for {notebook_id}: "
                    f"conflicting duplicate source row at index {index}"
                )
            logger.debug("SourcesAPI.list: Skipping duplicate source id %s", source.id)
            continue
        seen[source.id] = source
        sources.append(source)
    return tuple(sources)


def decode_source(data: list[object], *, method_id: str | None = None) -> SourceRecord:
    """Decode one flat/medium/deep source response."""

    return decode_source_row(SourceRow.from_unknown_shape(data, method_id=method_id))


def encode_add_text(notebook_id: str, title: str, content: str) -> list[Any]:
    """Encode the pasted-text ``ADD_SOURCE`` variant."""
    return [
        [[None, [title, content], None, 2, None, None, None, None, None, None, 1]],
        notebook_id,
        _template_block(),
    ]


def encode_add_drive(
    notebook_id: str,
    file_id: str,
    title: str,
    mime_type: str,
) -> list[Any]:
    """Encode the live-pinned native Drive ``ADD_SOURCE`` variant."""
    source_data = [
        [file_id, mime_type, 1, title],
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        1,
    ]
    return [
        [source_data],
        notebook_id,
        [2],
        [1, None, None, None, None, None, None, None, None, None, [1]],
    ]


def _url_spec(url: str, *, youtube: bool) -> list[Any]:
    if youtube:
        return [None, None, None, None, None, None, None, [url], None, None, 1]
    return [None, None, [url], None, None, None, None, None, None, None, 1]


def encode_add_url_batch(
    notebook_id: str,
    urls: tuple[str, ...] | list[str],
    *,
    youtube_flags: tuple[bool, ...] | list[bool],
) -> list[Any]:
    """Encode one true-batch URL/YouTube ``ADD_SOURCE`` request."""
    if len(urls) != len(youtube_flags):
        raise ValueError("URL batch and YouTube discriminator counts differ")
    return [
        [_url_spec(url, youtube=youtube) for url, youtube in zip(urls, youtube_flags, strict=True)],
        notebook_id,
        _template_block(),
    ]


def encode_source_snapshot(notebook_id: str) -> list[Any]:
    """Encode the recency-writing ``GET_NOTEBOOK`` source snapshot request."""

    return [notebook_id, None, _template_block(), None, 0]


def encode_delete(source_id: str) -> list[Any]:
    """Encode one source id for the batch-capable delete method."""
    return [[[source_id]]]


def encode_register_file_source(filename: str, notebook_id: str) -> list[Any]:
    """Encode the file-source registration mutation."""

    return [[[filename]], notebook_id, _template_block()]


def encode_update_source(source_id: str, new_title: str) -> list[Any]:
    """Encode one source-title set operation."""

    return [None, [source_id], [[[new_title]]]]


def encode_refresh_or_freshness(source_id: str) -> list[Any]:
    """Encode the shared refresh/freshness identity envelope."""
    return [None, [source_id], [2]]


def encode_get_guide(source_id: str) -> list[Any]:
    """Encode a source-guide request."""
    return [[[[source_id]]]]


def encode_get_fulltext(source_id: str, *, markdown: bool) -> list[Any]:
    """Encode a fulltext request for the text or HTML rendition."""
    return [[source_id], [3], [3]] if markdown else [[source_id], [2], [2]]


def decode_source_record(payload: list[Any], *, method: RPCMethod) -> SourceRecord:
    """Decode one source response through the aggregate source projector grammar."""
    return decode_source(payload, method_id=method.value)


def decode_add_source_records(payload: Any) -> tuple[SourceRecord, ...]:
    """Strictly decode every identified row in an ``ADD_SOURCE`` response."""
    return tuple(
        decode_source_record(row, method=RPCMethod.ADD_SOURCE)
        for row in unwrap_add_source_rows(payload)
    )


def decode_file_registration(payload: Any, *, filename: str) -> SourceFileRegistrationRecord:
    """Decode a file registration without exposing the raw envelope upstream."""

    return SourceFileRegistrationRecord(
        source_id=_extract_register_file_source_id(payload, filename),
        response_shape=_register_response_shape_label(payload),
    )


def decode_source_guide(payload: Any) -> SourceGuideRecord:
    """Soft-decode the optional source guide fields."""
    row = SourceGuideRow(payload)
    return SourceGuideRecord(summary=row.summary, keywords=tuple(row.keywords))


def _extract_all_text(
    data: builtins.list[Any],
    *,
    logger: logging.Logger,
    max_depth: int = 100,
) -> builtins.list[str]:
    """Preserve the historical flat fulltext traversal exactly."""

    if max_depth <= 0:
        logger.warning("Max recursion depth reached in text extraction")
        return []
    texts: builtins.list[str] = []
    for item in data:
        if isinstance(item, str) and item:
            texts.append(item)
        elif isinstance(item, builtins.list):
            texts.extend(_extract_all_text(item, logger=logger, max_depth=max_depth - 1))
    return texts


def decode_source_fulltext(
    payload: Any,
    *,
    source_id: str,
    output_format: str,
    logger: logging.Logger,
) -> SourceFulltextRecord | None:
    """Decode the optional ``GET_SOURCE`` envelope into a neutral fulltext record."""

    if not payload or not isinstance(payload, list):
        return None

    source_type: int | None = None
    url: str | None = None
    content = ""
    fulltext_row = SourceFulltextRow(payload)
    title = fulltext_row.title
    metadata = fulltext_row.metadata
    if metadata is not None:
        source_row = fulltext_row.source_row
        source_type = source_row.type_code if source_row is not None else None
        if source_type == 14 and source_row is not None and source_row.mime == "application/pdf":
            source_type = 3
        type_slot = fulltext_row.raw_metadata_type_slot
        if source_type is None and type_slot is not None:
            logger.warning(
                "Source %s metadata type-code slot malformed (expected int at "
                "metadata[4], got %s); treating type as unknown: %s",
                source_id,
                type(type_slot).__name__,
                reprlib.repr(metadata),
            )
        url = SourceRow.url_from_metadata(metadata, allow_bare_http=False)

    content_blocks = fulltext_row.text_content_blocks
    document = (
        decode_structured_document(content_blocks)
        if content_blocks is not None
        else decode_structured_document([])
    )
    if output_format == "markdown":
        html_content = fulltext_row.html_content
        if html_content is not None:
            from ..._markdown import html_to_markdown

            content = html_to_markdown(html_content, source_type=source_type)
        else:
            logger.warning(
                "Source %s (type=%s) has no HTML rendition for output_format='markdown'; "
                "returning empty content. Retry with output_format='text'.",
                source_id,
                source_type,
            )
    elif content_blocks is not None:
        content = "\n".join(_extract_all_text(content_blocks, logger=logger))

    if not content:
        logger.warning(
            "Source %s returned empty content (type=%s, title=%s)",
            source_id,
            source_type,
            title,
        )

    kind = _SOURCE_KINDS.get(source_type, "unknown") if source_type is not None else "unknown"
    if title is not None and title == url and source_type == 3:
        title = pdf_url_display_title(title) or title
    return SourceFulltextRecord(
        source_id=source_id,
        title=title,
        content=content,
        kind=kind,
        unrecognized_kind=(
            source_type if source_type is not None and source_type not in _SOURCE_KINDS else None
        ),
        kind_present=source_type is not None,
        url=url,
        char_count=len(content),
        document=document,
    )


# Row-facing codec helpers (P9.3 source domain). Each encoder returns the full
# request payload one codec row dispatches — params, the notebook route and the
# typed options the handler passed — and never names a method: the row's
# ``NativeCallSpec`` is the sole method authority. Decoders take the input so
# ``SOURCE_GET`` can select its exact id and ``SOURCE_LIST`` can filter.
_ROW_LOGGER = logging.getLogger("notebooklm").getChild("_sources")


def _notebook_route(notebook_id: str) -> str:
    return f"/notebook/{notebook_id}"


def encode_source_list(value: SourceListInput) -> CodecPayload:
    """Payload for the ``source.list`` codec row (the recency-writing snapshot)."""
    return CodecPayload(
        params=encode_source_snapshot(value.notebook_id),
        source_path=_notebook_route(value.notebook_id),
    )


def encode_source_get(value: SourceGetInput) -> CodecPayload:
    """Payload for the ``source.get`` codec row (same snapshot, exact-id select)."""
    return CodecPayload(
        params=encode_source_snapshot(value.notebook_id),
        source_path=_notebook_route(value.notebook_id),
    )


def encode_source_patch_title(value: SourcePatchTitleInput) -> CodecPayload:
    """Payload for the primitive ``source.patch_title`` title set-op."""
    return CodecPayload(
        params=encode_update_source(value.source_id, value.new_title),
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def encode_source_wait(value: SourceWaitSnapshotInput) -> CodecPayload:
    """Payload for the ``source.wait`` codec row (one snapshot per poll tick)."""
    return CodecPayload(
        params=encode_source_snapshot(value.notebook_id),
        source_path=_notebook_route(value.notebook_id),
    )


def encode_source_delete(value: SourceDeleteInput) -> CodecPayload:
    """Payload for the ``source.delete`` codec row."""
    return CodecPayload(
        params=encode_delete(value.source_id),
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def encode_source_refresh(value: SourceRefreshInput) -> CodecPayload:
    """Payload for the ``source.refresh`` codec row."""
    return CodecPayload(
        params=encode_refresh_or_freshness(value.source_id),
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def encode_source_check_freshness(value: SourceFreshnessInput) -> CodecPayload:
    """Payload for the ``source.check_freshness`` codec row."""
    return CodecPayload(
        params=encode_refresh_or_freshness(value.source_id),
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def encode_source_get_guide(value: SourceGuideInput) -> CodecPayload:
    """Payload for the ``source.get_guide`` codec row."""
    return CodecPayload(
        params=encode_get_guide(value.source_id),
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def encode_source_get_fulltext(value: SourceFulltextInput) -> CodecPayload:
    """Payload for the ``source.get_fulltext`` codec row.

    The output-format validation and the optional ``markdownify`` dependency
    check run here, before any wire call, exactly as the handler ordered them.
    """
    if value.output_format not in ("text", "markdown"):
        raise ValueError(f"Invalid format: '{value.output_format}'. Must be 'text' or 'markdown'.")
    if value.output_format == "markdown":
        try:
            import markdownify  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'markdown' format requires the 'markdownify' package. "
                "Install it with: pip install 'notebooklm-py[markdown]'"
            ) from None
    return CodecPayload(
        params=encode_get_fulltext(value.source_id, markdown=value.output_format == "markdown"),
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def decode_source_list(value: SourceListInput, payload: Any) -> SourceListResult:
    """Row decoder for ``source.list``: decode the snapshot, then apply the filters."""
    records = decode_source_snapshot(
        value.notebook_id,
        payload,
        strict=value.strict,
        logger=_ROW_LOGGER,
    )
    if value.statuses is not None:
        records = tuple(record for record in records if record.status in value.statuses)
    if value.kinds is not None:
        records = tuple(record for record in records if record.kind in value.kinds)
    return SourceListResult(sources=records)


def decode_source_get(value: SourceGetInput, payload: Any) -> SourceGetResult:
    """Row decoder for ``source.get``: list-then-filter by exact source id."""
    records = decode_source_snapshot(value.notebook_id, payload, logger=_ROW_LOGGER)
    return SourceGetResult(
        source=next((source for source in records if source.id == value.source_id), None)
    )


def decode_source_patch_title(
    value: SourcePatchTitleInput,
    payload: Any,
    *,
    method_id: str,
) -> SourcePatchTitleResult:
    """Decode the optional mutation echo using the binding-selected method id."""
    del value
    return SourcePatchTitleResult(
        source=(decode_source(payload, method_id=method_id) if payload else None)
    )


def decode_source_wait(value: SourceWaitSnapshotInput, payload: Any) -> SourceWaitSnapshotResult:
    """Row decoder for ``source.wait``: one neutral snapshot for one poll tick."""
    return SourceWaitSnapshotResult(
        decode_source_snapshot(value.notebook_id, payload, logger=_ROW_LOGGER)
    )


def decode_source_delete(value: SourceDeleteInput, payload: Any) -> SourceDeleteResult:
    """Row decoder for ``source.delete``: the acknowledgement carries no signal."""
    del value, payload
    return SourceDeleteResult()


def decode_source_refresh(value: SourceRefreshInput, payload: Any) -> SourceRefreshResult:
    """Row decoder for ``source.refresh``: the acknowledgement carries no signal."""
    del value, payload
    return SourceRefreshResult()


def decode_source_check_freshness(
    value: SourceFreshnessInput, payload: Any
) -> SourceFreshnessResult:
    """Row decoder for ``source.check_freshness``."""
    del value
    return SourceFreshnessResult(interpret_source_freshness(payload))


def decode_source_get_guide(value: SourceGuideInput, payload: Any) -> SourceGuideResult:
    """Row decoder for ``source.get_guide``."""
    del value
    return SourceGuideResult(decode_source_guide(payload))


def decode_source_get_fulltext(value: SourceFulltextInput, payload: Any) -> SourceFulltextResult:
    """Row decoder for ``source.get_fulltext``; a missing source keeps its legacy identity."""
    fulltext = decode_source_fulltext(
        payload,
        source_id=value.source_id,
        output_format=value.output_format,
        logger=_ROW_LOGGER,
    )
    if fulltext is None:
        legacy_source_reference = (
            f"Source {value.source_id} not found in notebook {value.notebook_id}"
        )
        raise BackendError(
            message=f"Source not found: {legacy_source_reference}",
            operation=Operation.SOURCE_GET_FULLTEXT,
            # ``import types`` rather than ``from types import``: the P3 codec
            # boundary guardrail reads a ``from types import`` as a public-model
            # import.
            diagnostics=types.MappingProxyType(
                {
                    # Compatibility: the legacy renderer passed this whole
                    # sentence as SourceNotFoundError's ``source_id`` and did
                    # not attach GET_SOURCE transport evidence.
                    "source_id": legacy_source_reference,
                    "method_id": None,
                    "raw_response": None,
                }
            ),
            reason=BackendErrorReason.SOURCE_NOT_FOUND,
        )
    return SourceFulltextResult(fulltext)


# Phase payloads for the source-add custom rows (P9.4b). Each returns the full
# request one phase dispatches — params, the notebook route and exactly the
# typed options the P6.7 handler passed — and never names a method: the row's
# keyed ``NativeCallSpec`` supplies ``(method, variant)``.
def encode_source_snapshot_payload(notebook_id: str) -> CodecPayload:
    """Payload for a composite's own recency-writing source snapshot."""
    return CodecPayload(
        params=encode_source_snapshot(notebook_id),
        source_path=_notebook_route(notebook_id),
    )


def encode_add_url_payload(
    notebook_id: str,
    urls: Sequence[str],
    *,
    youtube_flags: Sequence[bool],
) -> CodecPayload:
    """Payload for one generic/YouTube URL create (single or true batch)."""
    return CodecPayload(
        params=encode_add_url_batch(notebook_id, list(urls), youtube_flags=list(youtube_flags)),
        source_path=_notebook_route(notebook_id),
    )


def encode_add_text_payload(notebook_id: str, title: str, content: str) -> CodecPayload:
    """Payload for the pasted-text allocation."""
    return CodecPayload(
        params=encode_add_text(notebook_id, title, content),
        source_path=_notebook_route(notebook_id),
    )


def encode_add_drive_payload(
    notebook_id: str,
    file_id: str,
    title: str,
    mime_type: str,
) -> CodecPayload:
    """Payload for the Drive-document allocation (null echoes are legal)."""
    return CodecPayload(
        params=encode_add_drive(notebook_id, file_id, title, mime_type),
        source_path=_notebook_route(notebook_id),
        allow_null=True,
    )


def encode_register_file_source_payload(filename: str, notebook_id: str) -> CodecPayload:
    """Payload for the file-source registration intent."""
    return CodecPayload(
        params=encode_register_file_source(filename, notebook_id),
        source_path=_notebook_route(notebook_id),
        allow_null=False,
    )


def source_register_variant(value: SourceRegisterInput) -> str:
    """Return the one wire variant a registration request dispatches under."""
    return value.kind.value


def encode_source_register(value: SourceRegisterInput) -> CodecPayload:
    """Encode one registration write, rejecting a payload its kind cannot carry.

    Each branch delegates to the same encoder the pre-P10 source-add handlers
    used, so the request body — and therefore every cassette's ``freq`` match —
    is unchanged by the leaf.
    """
    kind = value.kind
    if kind is SourceRegisterKind.URL:
        if not value.urls or len(value.urls) != len(value.youtube_flags):
            raise BackendContractError(
                "source.register url needs one YouTube discriminator per URL",
                operation=Operation.SOURCE_REGISTER,
            )
        return encode_add_url_payload(
            value.notebook_id,
            value.urls,
            youtube_flags=value.youtube_flags,
        )
    if kind is SourceRegisterKind.TEXT:
        if value.title is None or value.content is None:
            raise BackendContractError(
                "source.register text needs both a title and a body",
                operation=Operation.SOURCE_REGISTER,
            )
        return encode_add_text_payload(value.notebook_id, value.title, value.content)
    if value.file_id is None or value.title is None or value.mime_type is None:
        raise BackendContractError(
            "source.register drive needs a file id, a title and a MIME type",
            operation=Operation.SOURCE_REGISTER,
        )
    return encode_add_drive_payload(
        value.notebook_id,
        value.file_id,
        value.title,
        value.mime_type,
    )


def decode_source_register(value: SourceRegisterInput, payload: Any) -> SourceRegisterResult:
    """Decode every identified row a registration echoed, preserving wire order.

    A Drive create legally echoes ``None`` (``allow_null``) and a URL batch may
    echo fewer rows than requested; both surface here as a shorter tuple for
    the sequencing workflow to reconcile.
    """
    del value
    if payload is None:
        return SourceRegisterResult(())
    return SourceRegisterResult(decode_add_source_records(payload))


def encode_rename_source_payload(notebook_id: str, source_id: str, new_title: str) -> CodecPayload:
    """Payload for the optional post-create title set-op (null echoes are legal)."""
    return CodecPayload(
        params=encode_update_source(source_id, new_title),
        source_path=_notebook_route(notebook_id),
        allow_null=True,
    )


def decode_renamed_source(payload: list[Any]) -> SourceRecord:
    """Decode a non-null ``UPDATE_SOURCE`` echo for the source-add rename phase."""
    return decode_source_record(payload, method=RPCMethod.UPDATE_SOURCE)


def rename_target_missing(source_id: str) -> SourceNotFoundError:
    """The null-echo hydration found no source with ``source_id``."""
    return SourceNotFoundError(source_id, method_id=RPCMethod.UPDATE_SOURCE.value)


__all__ = [
    "decode_add_source_records",
    "decode_renamed_source",
    "decode_source_register",
    "encode_add_drive_payload",
    "encode_add_text_payload",
    "encode_add_url_payload",
    "encode_register_file_source_payload",
    "encode_rename_source_payload",
    "encode_source_register",
    "encode_source_snapshot_payload",
    "rename_target_missing",
    "source_register_variant",
    "decode_file_registration",
    "decode_source",
    "decode_source_check_freshness",
    "decode_source_delete",
    "decode_source_fulltext",
    "decode_source_get",
    "decode_source_get_fulltext",
    "decode_source_get_guide",
    "decode_source_guide",
    "decode_source_list",
    "decode_source_record",
    "decode_source_refresh",
    "decode_source_row",
    "decode_source_snapshot",
    "decode_source_wait",
    "encode_add_drive",
    "encode_add_text",
    "encode_add_url_batch",
    "encode_delete",
    "encode_get_fulltext",
    "encode_get_guide",
    "encode_refresh_or_freshness",
    "encode_register_file_source",
    "encode_source_check_freshness",
    "encode_source_delete",
    "encode_source_get",
    "encode_source_get_fulltext",
    "encode_source_get_guide",
    "encode_source_list",
    "encode_source_refresh",
    "encode_source_snapshot",
    "encode_source_wait",
    "encode_update_source",
]
