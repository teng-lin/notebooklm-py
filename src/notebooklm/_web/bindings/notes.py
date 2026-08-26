"""Plain-note codec rows (P9.3 notes domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``NoteService`` sequences ``NOTE_CREATE``/``NOTE_UPDATE``/``NOTE_DELETE`` above
the port, and is the only thing that does: the deferred raw note-row service
that reached these natives through a row-scoped caller is gone (P10 R4.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._semantic.binding import Binding, CodecBinding, NativeCallSpec
from ..._semantic.operations import Operation
from ..._semantic.records import (
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
)
from ...rpc import RPCMethod
from ..codec import notes as notes_codec

NOTE_LIST = CodecBinding(
    definition=NOTE_LIST_DEF,
    encode=notes_codec.encode_note_list,
    decode=notes_codec.decode_note_list,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS),
)

NOTE_GET = CodecBinding(
    definition=NOTE_GET_DEF,
    encode=notes_codec.encode_note_get,
    decode=notes_codec.decode_note_get,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS),
)

NOTE_CREATE = CodecBinding(
    definition=NOTE_CREATE_DEF,
    encode=notes_codec.encode_note_create,
    decode=notes_codec.decode_note_create,
    native=NativeCallSpec.constant(RPCMethod.CREATE_NOTE, "plain"),
)

NOTE_UPDATE = CodecBinding(
    definition=NOTE_UPDATE_DEF,
    encode=notes_codec.encode_note_update,
    decode=notes_codec.decode_note_update,
    native=NativeCallSpec.constant(RPCMethod.UPDATE_NOTE),
)

NOTE_DELETE = CodecBinding(
    definition=NOTE_DELETE_DEF,
    encode=notes_codec.encode_note_delete,
    decode=notes_codec.decode_note_delete,
    native=NativeCallSpec.constant(RPCMethod.DELETE_NOTE),
)

NOTE_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        NOTE_LIST.definition.key: NOTE_LIST,
        NOTE_GET.definition.key: NOTE_GET,
        NOTE_CREATE.definition.key: NOTE_CREATE,
        NOTE_UPDATE.definition.key: NOTE_UPDATE,
        NOTE_DELETE.definition.key: NOTE_DELETE,
    }
)

__all__ = [
    "NOTE_CREATE",
    "NOTE_DELETE",
    "NOTE_GET",
    "NOTE_LIST",
    "NOTE_ROWS",
    "NOTE_UPDATE",
]
