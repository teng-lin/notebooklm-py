"""Notebook codec rows (P9.3 notebook reads domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``NOTEBOOK_LIST`` is the non-uniform row: its decoder accepts the empty,
``[None]`` and ``[[rows]]`` payload shapes.  ``NOTEBOOK_GET`` needs the input to
select its source-id-only branch.  ``NOTEBOOK_CREATE`` and ``NOTEBOOK_UPDATE``
stay handlers in ``_web/backend.py`` until their P9.2 hoists; the create
composite lists through its own ``_list_notebooks`` helper so its baseline and
probe keep ``notebook.create`` attribution.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, NativeCallSpec
from ..._operations import Operation
from ..._records import (
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
)
from ...rpc import RPCMethod
from ..codec import notebooks as notebooks_codec

NOTEBOOK_LIST = CodecBinding(
    definition=NOTEBOOK_LIST_DEF,
    encode=notebooks_codec.encode_notebook_list,
    decode=notebooks_codec.decode_notebook_list,
    native=NativeCallSpec.constant(RPCMethod.LIST_NOTEBOOKS),
)

NOTEBOOK_GET = CodecBinding(
    definition=NOTEBOOK_GET_DEF,
    encode=notebooks_codec.encode_notebook_get,
    decode=notebooks_codec.decode_notebook_get,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK),
)

NOTEBOOK_DELETE = CodecBinding(
    definition=NOTEBOOK_DELETE_DEF,
    encode=notebooks_codec.encode_notebook_delete,
    decode=notebooks_codec.decode_notebook_delete,
    native=NativeCallSpec.constant(RPCMethod.DELETE_NOTEBOOK),
)

NOTEBOOK_REMOVE_RECENT = CodecBinding(
    definition=NOTEBOOK_REMOVE_RECENT_DEF,
    encode=notebooks_codec.encode_notebook_remove_recent,
    decode=notebooks_codec.decode_notebook_remove_recent,
    native=NativeCallSpec.constant(RPCMethod.REMOVE_RECENTLY_VIEWED),
)

NOTEBOOK_SUMMARIZE = CodecBinding(
    definition=NOTEBOOK_SUMMARIZE_DEF,
    encode=notebooks_codec.encode_notebook_guide_request,
    decode=notebooks_codec.decode_notebook_guide,
    native=NativeCallSpec.constant(RPCMethod.SUMMARIZE),
)

NOTEBOOK_DESCRIBE = CodecBinding(
    definition=NOTEBOOK_DESCRIBE_DEF,
    encode=notebooks_codec.encode_notebook_guide_request,
    decode=notebooks_codec.decode_notebook_guide,
    native=NativeCallSpec.constant(RPCMethod.SUMMARIZE),
)

NOTEBOOK_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        NOTEBOOK_LIST.definition.key: NOTEBOOK_LIST,
        NOTEBOOK_GET.definition.key: NOTEBOOK_GET,
        NOTEBOOK_DELETE.definition.key: NOTEBOOK_DELETE,
        NOTEBOOK_REMOVE_RECENT.definition.key: NOTEBOOK_REMOVE_RECENT,
        NOTEBOOK_SUMMARIZE.definition.key: NOTEBOOK_SUMMARIZE,
        NOTEBOOK_DESCRIBE.definition.key: NOTEBOOK_DESCRIBE,
    }
)

__all__ = [
    "NOTEBOOK_DELETE",
    "NOTEBOOK_DESCRIBE",
    "NOTEBOOK_GET",
    "NOTEBOOK_LIST",
    "NOTEBOOK_REMOVE_RECENT",
    "NOTEBOOK_ROWS",
    "NOTEBOOK_SUMMARIZE",
]
