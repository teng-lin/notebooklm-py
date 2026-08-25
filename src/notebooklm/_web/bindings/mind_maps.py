"""Mind-map leaf codec rows (P9.3 mind-map domain).

The four leaves — note-backed listing, interactive tree read, interactive
rename and interactive delete — are ``encode → one native call → decode``
rows whose :class:`NativeCallSpec` is the sole method authority.

The composites are :class:`CustomBinding` rows (P9.4b).  The two generate
members (``MIND_MAP_GENERATE_NOTE``, ``MIND_MAP_GENERATE_INTERACTIVE``) are
input-defaulting *deferred-product* rows (gate table §3.17): an optional
``GET_NOTEBOOK`` read when ``source_ids`` is omitted, then one generation
native.  ``ARTIFACT_GENERATE_MIND_MAP``, ``ARTIFACT_LIST`` and ``ARTIFACT_GET``
are *compatibility* rows (gate table §3.14/§3.15): they reach the note-backed
mind-map family through ``LegacyNoteBackedService`` over the row-scoped
:class:`InvokerRpcCaller`, so the natives that service selects are exactly the
row's declared specs, and the catalog rows keep the legacy partial-availability
net that swallows a raw ``RPCError``/``httpx.HTTPError`` from that collaborator.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

import httpx

from ..._binding import Binding, CodecBinding, CustomBinding, NativeCallSpec, RowInvoker
from ..._deadline import RuntimeDeadline
from ..._mind_map import LegacyNoteBackedService, NoteBackedMindMapService
from ..._operations import Operation
from ..._records import (
    ARTIFACT_GENERATE_MIND_MAP_DEF,
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_LIST_DEF,
    MIND_MAP_UPDATE_DEF,
    ArtifactGetInput,
    ArtifactGetResult,
    ArtifactListInput,
    ArtifactListResult,
    ArtifactRecord,
    MindMapGenerateInput,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
    MindMapGenerateNoteInput,
    MindMapGenerateNoteResult,
    MindMapGenerateResult,
)
from ...exceptions import DecodingError, RPCError
from ...rpc import RPCMethod
from ..codec import artifacts as artifacts_codec
from ..codec import mind_maps as mind_maps_codec
from ..codec import notebooks as notebooks_codec
from ..codec.studio_documents import artifact_feature_unavailable
from ._invoker_caller import InvokerRpcCaller

artifact_logger = logging.getLogger("notebooklm._artifact.listing")

MIND_MAP_LIST = CodecBinding(
    definition=MIND_MAP_LIST_DEF,
    encode=mind_maps_codec.encode_mind_map_list,
    decode=mind_maps_codec.decode_mind_map_list,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS),
)

MIND_MAP_GET = CodecBinding(
    definition=MIND_MAP_GET_DEF,
    encode=mind_maps_codec.encode_mind_map_get,
    decode=mind_maps_codec.decode_mind_map_get,
    native=NativeCallSpec.constant(RPCMethod.GET_INTERACTIVE_HTML),
)

MIND_MAP_UPDATE = CodecBinding(
    definition=MIND_MAP_UPDATE_DEF,
    encode=mind_maps_codec.encode_mind_map_update,
    decode=mind_maps_codec.decode_mind_map_update,
    native=NativeCallSpec.constant(RPCMethod.RENAME_ARTIFACT),
)

MIND_MAP_DELETE = CodecBinding(
    definition=MIND_MAP_DELETE_DEF,
    encode=mind_maps_codec.encode_mind_map_delete,
    decode=mind_maps_codec.decode_mind_map_delete,
    native=NativeCallSpec.constant(RPCMethod.DELETE_ARTIFACT),
)


# --- custom rows (P9.4b) ---------------------------------------------------------

_SOURCES = "sources"
_GENERATE = "generate"
_CREATE = "create"
_CATALOG = "catalog"
_NOTE_ROWS = "note_rows"
_NOTE_CREATE = "note_create"
_NOTE_UPDATE = "note_update"
_NOTE_DELETE = "note_delete"

#: The legacy note-backed family, keyed by the ``RPCMethod`` each helper selects.
_NOTE_FAMILY_SPECS: Mapping[RPCMethod, tuple[str, str | None]] = MappingProxyType(
    {
        RPCMethod.GET_NOTES_AND_MIND_MAPS: (_NOTE_ROWS, None),
        RPCMethod.CREATE_NOTE: (_NOTE_CREATE, "plain"),
        RPCMethod.UPDATE_NOTE: (_NOTE_UPDATE, None),
        RPCMethod.DELETE_NOTE: (_NOTE_DELETE, None),
    }
)


async def _default_source_ids(
    notebook_id: str,
    source_ids: tuple[str, ...] | None,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> tuple[str, ...]:
    """Resolve the omitted source set through the row's ``GET_NOTEBOOK`` spec (silent parse)."""
    if source_ids is not None:
        return source_ids
    notebook = await invoke.call(
        _SOURCES, mind_maps_codec.encode_notebook_sources_read(notebook_id), deadline=deadline
    )
    return notebooks_codec.decode_notebook_source_ids_silent(notebook)


async def _mind_map_generate_note(
    value: MindMapGenerateNoteInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> MindMapGenerateNoteResult:
    source_ids = await _default_source_ids(value.notebook_id, value.source_ids, deadline, invoke)
    result = await invoke.call(
        _GENERATE,
        mind_maps_codec.encode_mind_map_generate_note(value, source_ids),
        deadline=deadline,
    )
    return mind_maps_codec.decode_mind_map_generate_note(result)


async def _mind_map_generate_interactive(
    value: MindMapGenerateInteractiveInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> MindMapGenerateInteractiveResult:
    source_ids = await _default_source_ids(value.notebook_id, value.source_ids, deadline, invoke)
    result = await invoke.call(
        _CREATE,
        mind_maps_codec.encode_mind_map_generate_interactive(value, source_ids),
        deadline=deadline,
    )
    mind_map_id = mind_maps_codec.decode_created_interactive_id(result)
    if mind_map_id is None:
        raise artifact_feature_unavailable(
            Operation.MIND_MAP_GENERATE_INTERACTIVE,
            "mind_map",
            method_id=RPCMethod.CREATE_ARTIFACT.value,
        )
    return MindMapGenerateInteractiveResult(mind_map_id)


def _note_service(
    invoke: RowInvoker, deadline: RuntimeDeadline | None, operation: Operation
) -> LegacyNoteBackedService:
    caller = InvokerRpcCaller(invoke, deadline, operation=operation, spec_keys=_NOTE_FAMILY_SPECS)
    return LegacyNoteBackedService(cast(Any, caller))


async def _artifact_mind_map_generate(
    value: MindMapGenerateInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> MindMapGenerateResult:
    """Generate the tree, then persist it as a note through the legacy note family."""
    source_ids = await _default_source_ids(value.notebook_id, value.source_ids, deadline, invoke)
    result = await invoke.call(
        _GENERATE,
        mind_maps_codec.encode_artifact_mind_map_generate(value, source_ids),
        deadline=deadline,
    )
    leaf = mind_maps_codec.decode_artifact_mind_map_leaf(result)
    if leaf is None:
        return MindMapGenerateResult()
    mind_map_json, mind_map_data, title = leaf
    note = await _note_service(invoke, deadline, Operation.ARTIFACT_GENERATE_MIND_MAP).create_note(
        value.notebook_id,
        title=title,
        content=mind_map_json,
    )
    return MindMapGenerateResult(
        mind_map=mind_map_data,
        note_id=note.id or None,
        created_at=note.created_at,
    )


async def _catalog_records(
    notebook_id: str,
    *,
    operation: Operation,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
    include_mind_maps: bool,
) -> tuple[ArtifactRecord, ...]:
    """One catalog read plus the conditional note-backed mind-map merge."""
    result = await invoke.call(
        _CATALOG, artifacts_codec.encode_artifact_catalog(notebook_id), deadline=deadline
    )
    artifacts = artifacts_codec.decode_artifact_catalog(
        result, source="WebRpcBackend._artifact_catalog_records"
    )
    if include_mind_maps:
        mind_maps = NoteBackedMindMapService(_note_service(invoke, deadline, operation))
        try:
            mind_map_rows = await mind_maps.list_mind_maps(notebook_id)
            artifacts.extend(
                artifact
                for row in mind_map_rows
                if (artifact := artifacts_codec.decode_mind_map_artifact(row)) is not None
            )
        except DecodingError:
            raise
        except (RPCError, httpx.HTTPError) as exc:
            # Most transport failures are normalized before this composite,
            # but an auth-refresh failure deliberately re-raises its original
            # HTTPStatusError. Preserve the legacy partial-availability net
            # for that raw leaf as well as ordinary RPC failures.
            artifact_logger.warning("Failed to fetch mind maps: %s", exc)
    return tuple(artifacts)


async def _artifact_list(
    value: ArtifactListInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> ArtifactListResult:
    records = await _catalog_records(
        value.notebook_id,
        operation=Operation.ARTIFACT_LIST,
        deadline=deadline,
        invoke=invoke,
        include_mind_maps=value.family in {None, "mind_map"},
    )
    return ArtifactListResult(artifacts=records)


async def _artifact_get(
    value: ArtifactGetInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> ArtifactGetResult:
    records = await _catalog_records(
        value.notebook_id,
        operation=Operation.ARTIFACT_GET,
        deadline=deadline,
        invoke=invoke,
        include_mind_maps=True,
    )
    return ArtifactGetResult(
        artifact=next((item for item in records if item.id == value.artifact_id), None)
    )


_NOTE_ROWS_SPEC = NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS, key=_NOTE_ROWS)
_CATALOG_SWALLOW = (
    "Compatibility: the note-backed merge swallows a raw RPCError/httpx.HTTPError into a "
    "partial-availability result, not yet expressible in neutral reasons (gate table §3.14)."
)

MIND_MAP_GENERATE_NOTE = CustomBinding(
    definition=MIND_MAP_GENERATE_NOTE_DEF,
    handler=_mind_map_generate_note,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.GENERATE_MIND_MAP, key=_GENERATE),
    ),
    justification=(
        "Input-defaulting member kept adapter-owned under P9.2 contract 1; hoisting needs a "
        "resolved-input primitive for the note-backed family (gate table §3.17)."
    ),
    category="deferred-product",
)

MIND_MAP_GENERATE_INTERACTIVE = CustomBinding(
    definition=MIND_MAP_GENERATE_INTERACTIVE_DEF,
    handler=_mind_map_generate_interactive,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=(
        "Input-defaulting member kept adapter-owned under P9.2 contract 1; hoisting needs a "
        "resolved-input primitive for the interactive family (gate table §3.17)."
    ),
    category="deferred-product",
)

ARTIFACT_GENERATE_MIND_MAP = CustomBinding(
    definition=ARTIFACT_GENERATE_MIND_MAP_DEF,
    handler=_artifact_mind_map_generate,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.GENERATE_MIND_MAP, key=_GENERATE),
        NativeCallSpec.constant(RPCMethod.CREATE_NOTE, "plain", key=_NOTE_CREATE),
        NativeCallSpec.constant(RPCMethod.UPDATE_NOTE, key=_NOTE_UPDATE),
        NativeCallSpec.constant(RPCMethod.DELETE_NOTE, key=_NOTE_DELETE),
    ),
    justification=(
        "Compatibility: persists the tree through the legacy note family, whose "
        "shielded finalize/cleanup identity is not yet reproducible from records "
        "(gate table §3.15)."
    ),
    category="compatibility",
)

ARTIFACT_LIST = CustomBinding(
    definition=ARTIFACT_LIST_DEF,
    handler=_artifact_list,
    native=(
        NativeCallSpec.constant(RPCMethod.LIST_ARTIFACTS, key=_CATALOG),
        _NOTE_ROWS_SPEC,
    ),
    justification=_CATALOG_SWALLOW,
    category="compatibility",
)

ARTIFACT_GET = CustomBinding(
    definition=ARTIFACT_GET_DEF,
    handler=_artifact_get,
    native=(
        NativeCallSpec.constant(RPCMethod.LIST_ARTIFACTS, key=_CATALOG),
        _NOTE_ROWS_SPEC,
    ),
    justification=_CATALOG_SWALLOW,
    category="compatibility",
)

MIND_MAP_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        MIND_MAP_LIST.definition.key: MIND_MAP_LIST,
        MIND_MAP_GET.definition.key: MIND_MAP_GET,
        MIND_MAP_UPDATE.definition.key: MIND_MAP_UPDATE,
        MIND_MAP_DELETE.definition.key: MIND_MAP_DELETE,
        MIND_MAP_GENERATE_NOTE.definition.key: MIND_MAP_GENERATE_NOTE,
        MIND_MAP_GENERATE_INTERACTIVE.definition.key: MIND_MAP_GENERATE_INTERACTIVE,
        ARTIFACT_GENERATE_MIND_MAP.definition.key: ARTIFACT_GENERATE_MIND_MAP,
        ARTIFACT_LIST.definition.key: ARTIFACT_LIST,
        ARTIFACT_GET.definition.key: ARTIFACT_GET,
    }
)

__all__ = [
    "ARTIFACT_GENERATE_MIND_MAP",
    "ARTIFACT_GET",
    "ARTIFACT_LIST",
    "MIND_MAP_DELETE",
    "MIND_MAP_GENERATE_INTERACTIVE",
    "MIND_MAP_GENERATE_NOTE",
    "MIND_MAP_GET",
    "MIND_MAP_LIST",
    "MIND_MAP_ROWS",
    "MIND_MAP_UPDATE",
]
