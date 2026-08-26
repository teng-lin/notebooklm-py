"""Sharing binding rows (P9.3 codec rows).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
The three mutate-then-readback composites (``SHARING_SET_PUBLIC``,
``SHARING_SET_VIEW_LEVEL``, ``SHARING_UPDATE_USERS``) are service-owned since
P9.2-5/6/7.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, NativeCallSpec
from ..._operations import Operation
from ..._semantic.records import (
    LEGACY_SHARE_ARTIFACT_DEF,
    SHARING_GET_DEF,
)
from ...rpc import RPCMethod
from ..codec import sharing as sharing_codec

SHARING_GET = CodecBinding(
    definition=SHARING_GET_DEF,
    encode=sharing_codec.encode_sharing_get,
    decode=sharing_codec.decode_sharing_get,
    native=NativeCallSpec.constant(RPCMethod.GET_SHARE_STATUS),
)

LEGACY_SHARE_ARTIFACT = CodecBinding(
    definition=LEGACY_SHARE_ARTIFACT_DEF,
    encode=sharing_codec.encode_legacy_share_artifact,
    decode=sharing_codec.decode_legacy_share_artifact,
    native=NativeCallSpec.constant(RPCMethod.SHARE_ARTIFACT),
)


SHARING_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        SHARING_GET.definition.key: SHARING_GET,
        LEGACY_SHARE_ARTIFACT.definition.key: LEGACY_SHARE_ARTIFACT,
    }
)

__all__ = [
    "LEGACY_SHARE_ARTIFACT",
    "SHARING_GET",
    "SHARING_ROWS",
]
