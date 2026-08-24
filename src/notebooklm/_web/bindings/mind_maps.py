"""Mind-map leaf codec rows (P9.3 mind-map domain).

The four leaves — note-backed listing, interactive tree read, interactive
rename and interactive delete — are ``encode → one native call → decode``
rows whose :class:`NativeCallSpec` is the sole method authority.  The two
generate members (``MIND_MAP_GENERATE_NOTE``, ``MIND_MAP_GENERATE_INTERACTIVE``)
are input-defaulting composites (gate table §3.17) and stay handlers until
their deferred-product custom rows land in P9.4.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, NativeCallSpec
from ..._operations import Operation
from ..._records import (
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_LIST_DEF,
    MIND_MAP_UPDATE_DEF,
)
from ...rpc import RPCMethod
from ..codec import mind_maps as mind_maps_codec

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

MIND_MAP_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        MIND_MAP_LIST.definition.key: MIND_MAP_LIST,
        MIND_MAP_GET.definition.key: MIND_MAP_GET,
        MIND_MAP_UPDATE.definition.key: MIND_MAP_UPDATE,
        MIND_MAP_DELETE.definition.key: MIND_MAP_DELETE,
    }
)

__all__ = [
    "MIND_MAP_DELETE",
    "MIND_MAP_GET",
    "MIND_MAP_LIST",
    "MIND_MAP_ROWS",
    "MIND_MAP_UPDATE",
]
