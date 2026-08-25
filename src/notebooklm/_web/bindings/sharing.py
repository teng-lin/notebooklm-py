"""Sharing binding rows (P9.3 codec rows; P9.4 custom rows).

``SHARING_GET`` and ``LEGACY_SHARE_ARTIFACT`` are ``encode → one native call →
decode`` codec rows.  The three mutate-then-readback composites
(``SHARING_SET_PUBLIC``, ``SHARING_SET_VIEW_LEVEL``, ``SHARING_UPDATE_USERS``)
are :class:`CustomBinding` rows: each declares exactly its two natives under the
spec keys ``"mutate"`` and ``"readback"``, and its handler sequences them
through the row-scoped invoker with the same options the P6.5 handlers set (a
guarded ``allow_null`` mutation, then a readback whose pre-dispatch expiry is
commit-uncertain).  They are *deferred-product* rows: gate table §4 orders
their hoists as P9.2-5/6/7, after the stop/go review.  The rows are
module-level assignments because the operation-catalog walker derives execution
authorities from them.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, CustomBinding, NativeCallSpec, RowInvoker
from ..._deadline import RuntimeDeadline
from ..._operations import Operation
from ..._records import (
    LEGACY_SHARE_ARTIFACT_DEF,
    SHARING_GET_DEF,
    SHARING_SET_PUBLIC_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    SHARING_UPDATE_USERS_DEF,
    SharingSetPublicInput,
    SharingSetPublicResult,
    SharingSetViewLevelInput,
    SharingSetViewLevelResult,
    SharingUpdateUsersInput,
    SharingUpdateUsersResult,
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


_MUTATE = "mutate"
_READBACK = "readback"


async def _set_public(
    value: SharingSetPublicInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> SharingSetPublicResult:
    await invoke.call(_MUTATE, sharing_codec.encode_sharing_set_public(value), deadline=deadline)
    raw = await invoke.call(
        _READBACK,
        sharing_codec.encode_share_status_readback(value.notebook_id),
        deadline=deadline,
        outcome_unknown_on_expiry=True,
    )
    return SharingSetPublicResult(status=sharing_codec.decode_share_status(raw, value.notebook_id))


async def _set_view_level(
    value: SharingSetViewLevelInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> SharingSetViewLevelResult:
    await invoke.call(
        _MUTATE, sharing_codec.encode_sharing_set_view_level(value), deadline=deadline
    )
    raw = await invoke.call(
        _READBACK,
        sharing_codec.encode_share_status_readback(value.notebook_id),
        deadline=deadline,
        outcome_unknown_on_expiry=True,
    )
    return SharingSetViewLevelResult(
        status=sharing_codec.decode_share_status(
            raw, value.notebook_id, view_level=value.view_level
        )
    )


async def _update_users(
    value: SharingUpdateUsersInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> SharingUpdateUsersResult:
    await invoke.call(_MUTATE, sharing_codec.encode_sharing_update_users(value), deadline=deadline)
    raw = await invoke.call(
        _READBACK,
        sharing_codec.encode_share_status_readback(value.notebook_id),
        deadline=deadline,
        outcome_unknown_on_expiry=True,
    )
    return SharingUpdateUsersResult(
        status=sharing_codec.decode_share_status(raw, value.notebook_id)
    )


SHARING_SET_PUBLIC = CustomBinding(
    definition=SHARING_SET_PUBLIC_DEF,
    handler=_set_public,
    native=(
        NativeCallSpec.constant(RPCMethod.SHARE_NOTEBOOK, key=_MUTATE),
        NativeCallSpec.constant(RPCMethod.GET_SHARE_STATUS, key=_READBACK),
    ),
    justification="Hoist candidate P9.2-5 per gate table §4; awaits the stop/go review.",
    category="deferred-product",
)

SHARING_SET_VIEW_LEVEL = CustomBinding(
    definition=SHARING_SET_VIEW_LEVEL_DEF,
    handler=_set_view_level,
    native=(
        NativeCallSpec.constant(RPCMethod.RENAME_NOTEBOOK, key=_MUTATE),
        NativeCallSpec.constant(RPCMethod.GET_SHARE_STATUS, key=_READBACK),
    ),
    justification="Hoist candidate P9.2-7 per gate table §4; awaits the stop/go review.",
    category="deferred-product",
)

SHARING_UPDATE_USERS = CustomBinding(
    definition=SHARING_UPDATE_USERS_DEF,
    handler=_update_users,
    native=(
        NativeCallSpec.constant(RPCMethod.SHARE_NOTEBOOK, key=_MUTATE),
        NativeCallSpec.constant(RPCMethod.GET_SHARE_STATUS, key=_READBACK),
    ),
    justification="Hoist candidate P9.2-6 per gate table §4; awaits the stop/go review.",
    category="deferred-product",
)

SHARING_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        SHARING_GET.definition.key: SHARING_GET,
        LEGACY_SHARE_ARTIFACT.definition.key: LEGACY_SHARE_ARTIFACT,
        SHARING_SET_PUBLIC.definition.key: SHARING_SET_PUBLIC,
        SHARING_SET_VIEW_LEVEL.definition.key: SHARING_SET_VIEW_LEVEL,
        SHARING_UPDATE_USERS.definition.key: SHARING_UPDATE_USERS,
    }
)

__all__ = [
    "LEGACY_SHARE_ARTIFACT",
    "SHARING_GET",
    "SHARING_ROWS",
    "SHARING_SET_PUBLIC",
    "SHARING_SET_VIEW_LEVEL",
    "SHARING_UPDATE_USERS",
]
