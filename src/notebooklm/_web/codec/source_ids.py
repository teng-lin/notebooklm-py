"""The one notebook source-id decoder every input-defaulting web row shares (P9.4b).

Before P9.4b six near-identical resolvers read the embedded source ids out of a
``GET_NOTEBOOK`` payload (``_audio_source_ids``, ``_document_source_ids``,
``_data_source_ids``, ``_generation_source_ids``, ``_visual_source_selection``
and the prompt-suggestion resolver).  They differed only in what they said about a
malformed payload, so :func:`decode_notebook_source_ids` takes that as an explicit
per-family :class:`~notebooklm._records.SourceIdDiagnostics` mode and the
``notebooklm._notebooks`` warning surface of every family is preserved byte for
byte.

Since P10 R5.1 the mode is a neutral record field rather than a codec-owned
enum: the callers that pick it live above the port and reach the decoder through
``NotebookGetInput.source_diagnostics``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..._binding import CodecPayload
from ..._records import SourceIdDiagnostics
from ..._row_adapters.sources import SourceRow
from ...rpc import RPCMethod, safe_index

logger = logging.getLogger("notebooklm._notebooks")


def encode_notebook_source_read(notebook_id: str) -> CodecPayload:
    """Payload for the default-source ``GET_NOTEBOOK`` read a row issues."""
    # Function-local: ``_notebook_payloads`` closes an import cycle through ``_source``.
    from ..._notebook_payloads import build_get_notebook_params

    return CodecPayload(
        params=build_get_notebook_params(notebook_id),
        source_path=f"/notebook/{notebook_id}",
    )


def _warn(diagnostics: SourceIdDiagnostics, message: str, *args: object) -> None:
    if diagnostics is not SourceIdDiagnostics.SILENT:
        logger.warning(message, *args)


def _decode(
    data: Any,
    *,
    notebook_id: str,
    diagnostics: SourceIdDiagnostics,
    into: list[str],
) -> None:
    method_id = RPCMethod.GET_NOTEBOOK.value
    notebook_info = safe_index(
        data,
        0,
        method_id=method_id,
        source="NotebooksAPI.get_source_ids",
    )
    if not isinstance(notebook_info, list):
        _warn(
            diagnostics,
            "get_source_ids: notebook_data[0] shape unexpected for %s (schema drift?). top-type=%s",
            notebook_id,
            type(notebook_info).__name__,
        )
        return
    if len(notebook_info) <= 1:
        _warn(
            diagnostics,
            "get_source_ids: notebook_info has no sources slot for %s (schema drift?). len=%d",
            notebook_id,
            len(notebook_info),
        )
        return
    sources = safe_index(
        notebook_info,
        1,
        method_id=method_id,
        source="NotebooksAPI.get_source_ids",
    )
    if sources is None:
        return
    if not isinstance(sources, list):
        _warn(
            diagnostics,
            "get_source_ids: notebook_info[1] not list for %s (schema drift?). len=%d",
            notebook_id,
            len(notebook_info),
        )
        return
    for source in sources:
        if not (isinstance(source, list) and source):
            continue
        source_id = SourceRow.from_entry(source, method_id=method_id).id
        if source_id:
            into.append(source_id)


def decode_notebook_source_ids(
    data: Any,
    *,
    notebook_id: str,
    diagnostics: SourceIdDiagnostics,
) -> tuple[str, ...]:
    """Decode the embedded source ids with one family's tolerant diagnostics."""
    source_ids: list[str] = []
    if not data or not isinstance(data, list):
        return ()
    if diagnostics is not SourceIdDiagnostics.GUARDED:
        _decode(data, notebook_id=notebook_id, diagnostics=diagnostics, into=source_ids)
        return tuple(source_ids)
    try:
        _decode(data, notebook_id=notebook_id, diagnostics=diagnostics, into=source_ids)
    except (IndexError, TypeError) as exc:
        # The ids decoded before the failure are kept, exactly as the legacy
        # prompt-suggestion resolver returned its partial list.
        logger.warning(
            "get_source_ids: unexpected exception despite guards for %s: %s",
            notebook_id,
            exc,
            exc_info=True,
        )
    return tuple(source_ids)


__all__ = ["decode_notebook_source_ids", "encode_notebook_source_read"]
