"""Source-label and collection codec rows (P9.3 labels/collections domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``LABEL_GET``/``COLLECTION_GET`` are list-then-select: one ``LIST_LABELS`` read
whose exact-id selection lives in ``decode``. Since P9.2 the four create/update
composites are service-owned workflows over these rows and the shared
``label.allocate``/``label.mutate`` primitives.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, NativeCallSpec
from ..._operations import Operation
from ..._records import (
    COLLECTION_DELETE_DEF,
    COLLECTION_GET_DEF,
    COLLECTION_LIST_DEF,
    LABEL_DELETE_DEF,
    LABEL_GENERATE_DEF,
    LABEL_GET_DEF,
    LABEL_LIST_DEF,
)
from ...rpc import RPCMethod
from ..codec import labels as labels_codec

LABEL_LIST = CodecBinding(
    definition=LABEL_LIST_DEF,
    encode=labels_codec.encode_label_list,
    decode=labels_codec.decode_label_list_result,
    native=NativeCallSpec.constant(RPCMethod.LIST_LABELS),
)

LABEL_GET = CodecBinding(
    definition=LABEL_GET_DEF,
    encode=labels_codec.encode_label_get,
    decode=labels_codec.decode_label_get_result,
    native=NativeCallSpec.constant(RPCMethod.LIST_LABELS),
)

LABEL_GENERATE = CodecBinding(
    definition=LABEL_GENERATE_DEF,
    encode=labels_codec.encode_label_generate,
    decode=labels_codec.decode_label_generate_result,
    native=NativeCallSpec.constant(RPCMethod.CREATE_LABEL),
)

LABEL_DELETE = CodecBinding(
    definition=LABEL_DELETE_DEF,
    encode=labels_codec.encode_label_delete,
    decode=labels_codec.decode_label_delete_result,
    native=NativeCallSpec.constant(RPCMethod.DELETE_LABEL),
)

COLLECTION_LIST = CodecBinding(
    definition=COLLECTION_LIST_DEF,
    encode=labels_codec.encode_collection_list,
    decode=labels_codec.decode_collection_list_result,
    native=NativeCallSpec.constant(RPCMethod.LIST_LABELS),
)

COLLECTION_GET = CodecBinding(
    definition=COLLECTION_GET_DEF,
    encode=labels_codec.encode_collection_get,
    decode=labels_codec.decode_collection_get_result,
    native=NativeCallSpec.constant(RPCMethod.LIST_LABELS),
)

COLLECTION_DELETE = CodecBinding(
    definition=COLLECTION_DELETE_DEF,
    encode=labels_codec.encode_collection_delete,
    decode=labels_codec.decode_label_delete_result,
    native=NativeCallSpec.constant(RPCMethod.DELETE_LABEL),
)

LABEL_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        LABEL_LIST.definition.key: LABEL_LIST,
        LABEL_GET.definition.key: LABEL_GET,
        LABEL_GENERATE.definition.key: LABEL_GENERATE,
        LABEL_DELETE.definition.key: LABEL_DELETE,
        COLLECTION_LIST.definition.key: COLLECTION_LIST,
        COLLECTION_GET.definition.key: COLLECTION_GET,
        COLLECTION_DELETE.definition.key: COLLECTION_DELETE,
    }
)

__all__ = [
    "COLLECTION_DELETE",
    "COLLECTION_GET",
    "COLLECTION_LIST",
    "LABEL_DELETE",
    "LABEL_GENERATE",
    "LABEL_GET",
    "LABEL_LIST",
    "LABEL_ROWS",
]
