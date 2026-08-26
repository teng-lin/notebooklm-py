"""Mind-map leaf codec rows (P9.3 mind-map domain).

The four leaves — note-backed listing, interactive tree read, interactive
rename and interactive delete — are ``encode → one native call → decode``
rows whose :class:`NativeCallSpec` is the sole method authority.

Both generate members joined them in P10 R5.1b.  Each was an input-defaulting
*deferred-product* :class:`CustomBinding` (gate table §3.17) that issued a
conditional ``GET_NOTEBOOK`` read whenever ``source_ids`` was omitted; under
ADR-0035 P10 addendum D1(a) source-scope defaulting is a service concern, so
that read belongs to ``MindMapFamilyService``, ``NoteBackedMindMapFamilyService``
and ``NoteService`` above the port.  One generation native is left in each row
and this module has no custom row at all — the last ``deferred-product`` row in
the package went with it.

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
from typing import Any

import httpx

from ..._backend import BackendError, BackendErrorReason
from ..._binding import (
    Binding,
    CodecBinding,
    NativeCallSpec,
    NativeChoice,
)
from ..._operations import Operation
from ..._semantic.records import (
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
    MindMapListInput,
)
from ...rpc import RPCMethod
from ..codec import mind_maps as mind_maps_codec


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

#: The interactive family's one native, declared once so the row's decoder can
#: thread its method id off the spec instead of keeping a second copy of it —
#: the idiom ``_web/bindings/research.py`` already uses for its start rows.
_CREATE_INTERACTIVE = NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT)


def _decode_mind_map_generate_interactive(
    value: MindMapGenerateInteractiveInput, result: Any
) -> MindMapGenerateInteractiveResult:
    return mind_maps_codec.decode_mind_map_generate_interactive(
        value, result, method_id=_CREATE_INTERACTIVE.select_rpc(value).method.value
    )


MIND_MAP_GENERATE_INTERACTIVE = CodecBinding(
    definition=MIND_MAP_GENERATE_INTERACTIVE_DEF,
    encode=mind_maps_codec.encode_mind_map_generate_interactive,
    decode=_decode_mind_map_generate_interactive,
    native=_CREATE_INTERACTIVE,
)


MIND_MAP_GENERATE_NOTE = CodecBinding(
    definition=MIND_MAP_GENERATE_NOTE_DEF,
    encode=mind_maps_codec.encode_mind_map_generate_note,
    decode=mind_maps_codec.decode_mind_map_generate_note,
    native=NativeCallSpec.constant(RPCMethod.GENERATE_MIND_MAP),
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
