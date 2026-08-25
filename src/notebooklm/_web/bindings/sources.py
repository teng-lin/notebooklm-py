"""Source read and single-native source mutation codec rows (P9.3 source domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``SOURCE_LIST``/``SOURCE_GET``/``SOURCE_WAIT`` share the recency-writing
``GET_NOTEBOOK`` snapshot; ``SOURCE_GET`` selects its exact id inside ``decode``
and ``SOURCE_WAIT`` is the one ``DeadlineMode.IGNORE`` row (source polling
historically never clamps an in-flight read).  The source-add family,
The source-add family and upload-pipeline callbacks stay handlers in
``_web/source_variants.py`` and keep reading through its snapshot helper;
``SOURCE_UPDATE`` is service-owned since P9.2-4.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, DeadlineMode, NativeCallSpec
from ..._operations import Operation
from ..._records import (
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_LIST_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_WAIT_DEF,
)
from ...rpc import RPCMethod
from ..codec import sources as sources_codec

SOURCE_LIST = CodecBinding(
    definition=SOURCE_LIST_DEF,
    encode=sources_codec.encode_source_list,
    decode=sources_codec.decode_source_list,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK),
)

SOURCE_GET = CodecBinding(
    definition=SOURCE_GET_DEF,
    encode=sources_codec.encode_source_get,
    decode=sources_codec.decode_source_get,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK),
)

SOURCE_WAIT = CodecBinding(
    definition=SOURCE_WAIT_DEF,
    encode=sources_codec.encode_source_wait,
    decode=sources_codec.decode_source_wait,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK),
    deadline=DeadlineMode.IGNORE,
)

SOURCE_DELETE = CodecBinding(
    definition=SOURCE_DELETE_DEF,
    encode=sources_codec.encode_source_delete,
    decode=sources_codec.decode_source_delete,
    native=NativeCallSpec.constant(RPCMethod.DELETE_SOURCE),
)

SOURCE_REFRESH = CodecBinding(
    definition=SOURCE_REFRESH_DEF,
    encode=sources_codec.encode_source_refresh,
    decode=sources_codec.decode_source_refresh,
    native=NativeCallSpec.constant(RPCMethod.REFRESH_SOURCE),
)

SOURCE_CHECK_FRESHNESS = CodecBinding(
    definition=SOURCE_CHECK_FRESHNESS_DEF,
    encode=sources_codec.encode_source_check_freshness,
    decode=sources_codec.decode_source_check_freshness,
    native=NativeCallSpec.constant(RPCMethod.CHECK_SOURCE_FRESHNESS),
)

SOURCE_GET_GUIDE = CodecBinding(
    definition=SOURCE_GET_GUIDE_DEF,
    encode=sources_codec.encode_source_get_guide,
    decode=sources_codec.decode_source_get_guide,
    native=NativeCallSpec.constant(RPCMethod.GET_SOURCE_GUIDE),
)

SOURCE_GET_FULLTEXT = CodecBinding(
    definition=SOURCE_GET_FULLTEXT_DEF,
    encode=sources_codec.encode_source_get_fulltext,
    decode=sources_codec.decode_source_get_fulltext,
    native=NativeCallSpec.constant(RPCMethod.GET_SOURCE),
)

SOURCE_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        SOURCE_LIST.definition.key: SOURCE_LIST,
        SOURCE_GET.definition.key: SOURCE_GET,
        SOURCE_WAIT.definition.key: SOURCE_WAIT,
        SOURCE_DELETE.definition.key: SOURCE_DELETE,
        SOURCE_REFRESH.definition.key: SOURCE_REFRESH,
        SOURCE_CHECK_FRESHNESS.definition.key: SOURCE_CHECK_FRESHNESS,
        SOURCE_GET_GUIDE.definition.key: SOURCE_GET_GUIDE,
        SOURCE_GET_FULLTEXT.definition.key: SOURCE_GET_FULLTEXT,
    }
)

__all__ = [
    "SOURCE_CHECK_FRESHNESS",
    "SOURCE_DELETE",
    "SOURCE_GET",
    "SOURCE_GET_FULLTEXT",
    "SOURCE_GET_GUIDE",
    "SOURCE_LIST",
    "SOURCE_REFRESH",
    "SOURCE_ROWS",
    "SOURCE_WAIT",
]
