"""Web wire codecs for semantic mind-map operations."""

from __future__ import annotations

import json
import logging
import reprlib
from typing import Any

from ..._binding import CodecPayload
from ..._env import get_default_language
from ..._operations import Operation
from ..._row_adapters.artifacts import MIND_MAP_LEAF_ABSENT, unwrap_mind_map_generation_leaf
from ..._semantic.records import (
    RAW_MIND_MAP_ROWS,
    MindMapDeleteInput,
    MindMapDeleteResult,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
    MindMapGenerateNoteInput,
    MindMapGenerateNoteResult,
    MindMapGenerateTreeInput,
    MindMapGenerateTreeResult,
    MindMapGetInput,
    MindMapGetResult,
    MindMapListInput,
    MindMapListResult,
    MindMapUpdateInput,
    MindMapUpdateResult,
)
from ...exceptions import UnknownRPCMethodError
from ...rpc import RPCMethod, safe_index
from .artifact_payloads import (
    build_interactive_mind_map_artifact_params,
    build_mind_map_params,
)
from .notes import decode_note_backed_mind_maps, decode_note_row_collection
from .studio_documents import artifact_feature_unavailable

logger = logging.getLogger("notebooklm._mind_maps_api")

_INTERACTIVE_TREE_LEAF_POS = 3
_CREATE_ARTIFACT_ENVELOPE_POS = 0
_CREATE_ARTIFACT_ID_POS = 0


def extract_interactive_tree_leaf(result: Any, *, source: str) -> Any | None:
    """Return the raw interactive tree leaf while distinguishing absence from drift."""

    if result is None:
        return None
    options_block = safe_index(
        result,
        _CREATE_ARTIFACT_ENVELOPE_POS,
        9,
        method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
        source=source,
    )
    if not isinstance(options_block, list):
        raise UnknownRPCMethodError(
            f"safe_index drift at path (0, 9): options block is "
            f"{type(options_block).__name__}, not a list",
            method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
            path=(0, 9),
            source=source,
            data_at_failure=reprlib.repr(options_block),
        )
    if len(options_block) <= _INTERACTIVE_TREE_LEAF_POS:
        logger.warning(
            "Interactive mind-map tree leaf absent at [0][9][%d] (rpcid=%s, source=%s); "
            "treating as not-yet-populated. If this persists, Google may have reshaped "
            "the %s response.",
            _INTERACTIVE_TREE_LEAF_POS,
            RPCMethod.GET_INTERACTIVE_HTML.value,
            source,
            RPCMethod.GET_INTERACTIVE_HTML.name,
        )
        return None
    return options_block[_INTERACTIVE_TREE_LEAF_POS]


def decode_generated_tree(result: Any) -> str | None:
    """Decode GENERATE_MIND_MAP's optional JSON leaf without parsing public values."""

    leaf = unwrap_mind_map_generation_leaf(
        result,
        method_id=RPCMethod.GENERATE_MIND_MAP.value,
        source="MindMapService.generate_note_backed",
    )
    if leaf is MIND_MAP_LEAF_ABSENT:
        return None
    if isinstance(leaf, str):
        return leaf
    # Preserve the historical persistence behavior for non-string JSON values.
    return json.dumps(leaf)


def decode_interactive_tree(result: Any) -> str | None:
    """Decode GET_INTERACTIVE_HTML's tree only when the leaf is textual JSON."""

    leaf = extract_interactive_tree_leaf(result, source="MindMapFamilyService.get_tree")
    return leaf if isinstance(leaf, str) else None


def decode_created_interactive_id(result: Any) -> str | None:
    """Decode CREATE_ARTIFACT's optional ``[[id, ...]]`` identity."""

    if not isinstance(result, list) or not result:
        return None
    inner = safe_index(
        result,
        0,
        method_id=RPCMethod.CREATE_ARTIFACT.value,
        source="MindMapFamilyService.generate",
    )
    if not isinstance(inner, list) or not inner:
        return None
    value = safe_index(
        inner,
        _CREATE_ARTIFACT_ID_POS,
        method_id=RPCMethod.CREATE_ARTIFACT.value,
        source="MindMapFamilyService.generate",
    )
    return value if isinstance(value, str) else None


# Row-facing encoders and decoders (P9.3). Each encoder returns the full
# request payload one codec row dispatches — params plus the notebook route and
# option flags exactly as the handler passed them — and never names a method.
def _notebook_route(notebook_id: str) -> str:
    return f"/notebook/{notebook_id}"


def encode_mind_map_list(value: MindMapListInput) -> CodecPayload:
    """Payload for the ``mind_map.list`` codec row (mixed note-row read)."""
    return CodecPayload(
        params=[value.notebook_id],
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def encode_mind_map_get(value: MindMapGetInput) -> CodecPayload:
    """Payload for the ``mind_map.get`` codec row (interactive tree read)."""
    return CodecPayload(
        params=[value.mind_map_id],
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def encode_mind_map_update(value: MindMapUpdateInput) -> CodecPayload:
    """Payload for the ``mind_map.update`` codec row (interactive title set-op)."""
    return CodecPayload(
        params=[[value.mind_map_id, value.title], [["title"]]],
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def encode_mind_map_delete(value: MindMapDeleteInput) -> CodecPayload:
    """Payload for the ``mind_map.delete`` codec row (interactive delete)."""
    return CodecPayload(
        params=[[2], value.mind_map_id],
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def decode_mind_map_list(value: MindMapListInput, data: Any) -> MindMapListResult:
    """Row decoder for ``mind_map.list``; the input selects the raw-row branch."""
    if value.raw_rows is not None:
        # Undecoded compatibility branch. ``NotesAPI.list_mind_maps`` and
        # ``NotesAPI._get_all_notes_and_mind_maps`` publish the wire rows, so no
        # record projection may run here — it would turn a row those helpers
        # returned verbatim into a record that drops fields they exposed.
        return MindMapListResult(
            (),
            decode_note_row_collection(data, mind_maps_only=value.raw_rows == RAW_MIND_MAP_ROWS),
        )
    return MindMapListResult(decode_note_backed_mind_maps(data, value.notebook_id))


def decode_mind_map_get(value: MindMapGetInput, data: Any) -> MindMapGetResult:
    """Row decoder for ``mind_map.get``."""
    del value
    return MindMapGetResult(decode_interactive_tree(data))


def decode_mind_map_update(value: MindMapUpdateInput, data: Any) -> MindMapUpdateResult:
    """Row decoder for ``mind_map.update``; the wire echo carries nothing neutral."""
    del value, data
    return MindMapUpdateResult()


def decode_mind_map_delete(value: MindMapDeleteInput, data: Any) -> MindMapDeleteResult:
    """Row decoder for ``mind_map.delete``; the wire echo carries nothing neutral."""
    del value, data
    return MindMapDeleteResult()


def _encode_generation(
    notebook_id: str,
    source_ids: tuple[str, ...],
    language: str,
    instructions: str | None,
) -> CodecPayload:
    """The one ``GENERATE_MIND_MAP`` body both note-backed rows encode.

    ``mind_map.generate`` (the P10 R4.2 primitive) and ``mind_map.generate_note``
    (the product operation ``MindMapsAPI.generate`` reaches) dispatch the same
    native over the same already-resolved scope, so they share this body rather
    than keeping two copies of it that could drift apart on the wire.
    """
    return CodecPayload(
        params=build_mind_map_params(
            list(source_ids),
            language=language,
            instructions=instructions,
        ),
        source_path=_notebook_route(notebook_id),
        allow_null=True,
    )


def encode_mind_map_generate_note(value: MindMapGenerateNoteInput) -> CodecPayload:
    """Payload for the ``mind_map.generate_note`` codec row (P10 R5.1b).

    The input carries its resolved source scope and language, so this encoder is
    a pure function of the record and never reads a notebook of its own.
    """
    return _encode_generation(
        value.notebook_id, value.source_ids, value.language, value.instructions
    )


def decode_mind_map_generate_note(
    value: MindMapGenerateNoteInput, result: Any
) -> MindMapGenerateNoteResult:
    """Row decoder for ``mind_map.generate_note``: the optional JSON leaf alone."""
    del value
    return MindMapGenerateNoteResult(decode_generated_tree(result))


def encode_mind_map_generate(value: MindMapGenerateTreeInput) -> CodecPayload:
    """Payload for the ``mind_map.generate`` leaf (already-resolved source set)."""
    return _encode_generation(
        value.notebook_id,
        value.source_ids,
        get_default_language() if value.language is None else value.language,
        value.instructions,
    )


def decode_mind_map_generate(
    value: MindMapGenerateTreeInput, result: Any
) -> MindMapGenerateTreeResult:
    """Row decoder for ``mind_map.generate``; only the serialized tree crosses."""
    del value
    return MindMapGenerateTreeResult(decode_generated_tree(result))


def encode_mind_map_generate_interactive(
    value: MindMapGenerateInteractiveInput,
) -> CodecPayload:
    """Payload for the ``mind_map.generate_interactive`` codec row (P10 R5.1b).

    The input carries its resolved source scope, so this encoder is a pure
    function of the record and never reads a notebook of its own.
    """
    return CodecPayload(
        params=build_interactive_mind_map_artifact_params(
            value.notebook_id,
            list(value.source_ids),
            instructions=value.instructions,
        ),
        source_path=_notebook_route(value.notebook_id),
        allow_null=True,
    )


def decode_mind_map_generate_interactive(
    value: MindMapGenerateInteractiveInput, result: Any, *, method_id: str
) -> MindMapGenerateInteractiveResult:
    """Row decoder for ``mind_map.generate_interactive``.

    A response that allocates no identity is the closed
    ``ARTIFACT_FEATURE_UNAVAILABLE`` failure the composite raised, kept here so
    the public exception is unchanged. ``method_id`` is threaded from the row's
    ``NativeCallSpec`` value, as the research rows already do, so this decoder
    keeps no second copy of the native it decodes.
    """
    del value
    mind_map_id = decode_created_interactive_id(result)
    if mind_map_id is None:
        raise artifact_feature_unavailable(
            Operation.MIND_MAP_GENERATE_INTERACTIVE, "mind_map", method_id=method_id
        )
    return MindMapGenerateInteractiveResult(mind_map_id)


__all__ = [
    "decode_created_interactive_id",
    "decode_generated_tree",
    "decode_interactive_tree",
    "decode_mind_map_delete",
    "decode_mind_map_generate",
    "decode_mind_map_generate_interactive",
    "decode_mind_map_generate_note",
    "decode_mind_map_get",
    "decode_mind_map_list",
    "decode_mind_map_update",
    "encode_mind_map_delete",
    "encode_mind_map_generate",
    "encode_mind_map_generate_interactive",
    "encode_mind_map_generate_note",
    "encode_mind_map_get",
    "encode_mind_map_list",
    "encode_mind_map_update",
    "extract_interactive_tree_leaf",
]
