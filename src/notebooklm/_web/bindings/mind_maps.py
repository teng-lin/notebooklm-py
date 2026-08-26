"""Mind-map leaf codec rows (P9.3 mind-map domain).

The four leaves — note-backed listing, interactive tree read, interactive
rename and interactive delete — are ``encode → one native call → decode``
rows whose :class:`NativeCallSpec` is the sole method authority.

The remaining composites are :class:`CustomBinding` rows (P9.4b).  The two
generate members (``MIND_MAP_GENERATE_NOTE``, ``MIND_MAP_GENERATE_INTERACTIVE``)
are input-defaulting *deferred-product* rows (gate table §3.17): an optional
``GET_NOTEBOOK`` read when ``source_ids`` is omitted, then one generation
native.

``MIND_MAP_LIST`` carries the one ``map_error`` this module needs.  ``invoke``
translates the closed ``NotebookLMError`` family and nothing else, so the
auth-refresh path's deliberate raw ``httpx.HTTPStatusError`` re-raise leaves the
port untranslated.  The Studio catalog's supplemental read of this leaf has
always swallowed that raw failure into a partial listing (ADR-0019 Rule 3), and
above the port it can only keep doing so if the failure arrives as a neutral
reason.  The mapper therefore fires only for a request the catalog marked
``supplemental``: every other consumer of this leaf — ``client.notes`` and
``client.mind_maps`` — still observes the raw ``httpx`` exception unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import httpx

from ..._backend import BackendError, BackendErrorReason
from ..._binding import (
    Binding,
    CodecBinding,
    CustomBinding,
    NativeCallSpec,
    NativeChoice,
    RowInvoker,
)
from ..._deadline import RuntimeDeadline
from ..._operations import Operation
from ..._records import (
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GENERATE_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_LIST_DEF,
    MIND_MAP_UPDATE_DEF,
    SUPPLEMENTAL_TRANSPORT_FAILURE,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
    MindMapGenerateNoteInput,
    MindMapGenerateNoteResult,
    MindMapListInput,
)
from ...rpc import RPCMethod
from ..codec import mind_maps as mind_maps_codec
from ..codec import notebooks as notebooks_codec
from ..codec.studio_documents import artifact_feature_unavailable


def _map_supplemental_transport_failure(
    value: MindMapListInput,
    raw: Exception,
    native: NativeChoice[RPCMethod],
) -> BackendError | None:
    """Translate the catalog merge's raw transport leaf, and nothing else.

    The discriminator is the whole point: ``mind_map.list`` also backs
    ``client.notes.list_mind_maps`` and ``client.mind_maps.list`` /
    ``get_or_none`` / ``rename``, and a row-level net with no caller context
    would turn the raw ``httpx.HTTPStatusError`` those methods raise on the
    auth-refresh path into a ``NetworkError`` — a public exception-type change.
    """
    del native
    if not value.supplemental or not isinstance(raw, httpx.HTTPError):
        return None
    return BackendError(
        message=str(raw),
        operation=Operation.MIND_MAP_LIST,
        diagnostics=MappingProxyType({SUPPLEMENTAL_TRANSPORT_FAILURE: True}),
        reason=BackendErrorReason.NETWORK,
        dispatched=bool(getattr(raw, "dispatched", False)),
    )


MIND_MAP_LIST = CodecBinding(
    definition=MIND_MAP_LIST_DEF,
    encode=mind_maps_codec.encode_mind_map_list,
    decode=mind_maps_codec.decode_mind_map_list,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS),
    map_error=_map_supplemental_transport_failure,
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

MIND_MAP_GENERATE = CodecBinding(
    definition=MIND_MAP_GENERATE_DEF,
    encode=mind_maps_codec.encode_mind_map_generate,
    decode=mind_maps_codec.decode_mind_map_generate,
    native=NativeCallSpec.constant(RPCMethod.GENERATE_MIND_MAP),
)


# --- custom rows (P9.4b) ---------------------------------------------------------

_SOURCES = "sources"
_GENERATE = "generate"
_CREATE = "create"


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

MIND_MAP_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        MIND_MAP_LIST.definition.key: MIND_MAP_LIST,
        MIND_MAP_GET.definition.key: MIND_MAP_GET,
        MIND_MAP_UPDATE.definition.key: MIND_MAP_UPDATE,
        MIND_MAP_DELETE.definition.key: MIND_MAP_DELETE,
        MIND_MAP_GENERATE.definition.key: MIND_MAP_GENERATE,
        MIND_MAP_GENERATE_NOTE.definition.key: MIND_MAP_GENERATE_NOTE,
        MIND_MAP_GENERATE_INTERACTIVE.definition.key: MIND_MAP_GENERATE_INTERACTIVE,
    }
)

__all__ = [
    "MIND_MAP_DELETE",
    "MIND_MAP_GENERATE",
    "MIND_MAP_GENERATE_INTERACTIVE",
    "MIND_MAP_GENERATE_NOTE",
    "MIND_MAP_GET",
    "MIND_MAP_LIST",
    "MIND_MAP_ROWS",
    "MIND_MAP_UPDATE",
]
