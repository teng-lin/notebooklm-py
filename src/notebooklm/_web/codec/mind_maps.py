"""Web wire codecs for semantic mind-map operations."""

from __future__ import annotations

import json
import logging
import reprlib
from typing import Any

from ..._binding import CodecPayload
from ..._records import (
    MindMapDeleteInput,
    MindMapDeleteResult,
    MindMapGetInput,
    MindMapGetResult,
    MindMapListInput,
    MindMapListResult,
    MindMapUpdateInput,
    MindMapUpdateResult,
)
from ..._row_adapters.artifacts import MIND_MAP_LEAF_ABSENT, unwrap_mind_map_generation_leaf
from ...exceptions import UnknownRPCMethodError
from ...rpc import RPCMethod, safe_index
from .notes import decode_note_backed_mind_maps

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
    """Row decoder for ``mind_map.list``."""
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


__all__ = [
    "decode_created_interactive_id",
    "decode_generated_tree",
    "decode_interactive_tree",
    "decode_mind_map_delete",
    "decode_mind_map_get",
    "decode_mind_map_list",
    "decode_mind_map_update",
    "encode_mind_map_delete",
    "encode_mind_map_get",
    "encode_mind_map_list",
    "encode_mind_map_update",
    "extract_interactive_tree_leaf",
]
