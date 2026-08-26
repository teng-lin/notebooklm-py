"""Web notebook-collection codecs."""

from __future__ import annotations

from typing import Any

from ..._semantic.records import CollectionRecord
from ...exceptions import UnknownRPCMethodError
from ...rpc import safe_index

_SOURCE = "_types.collections"


def decode_collection(data: list[Any], *, method_id: str | None = None) -> CollectionRecord:
    """Decode one strict collection tuple into a neutral record."""

    name = safe_index(data, 0, method_id=method_id, source=_SOURCE)
    members = safe_index(data, 1, method_id=method_id, source=_SOURCE)
    collection_id = safe_index(data, 2, method_id=method_id, source=_SOURCE)
    emoji = safe_index(data, 3, method_id=method_id, source=_SOURCE)
    if not isinstance(name, str) or not isinstance(collection_id, str):
        raise UnknownRPCMethodError(
            message="collection tuple name/id not strings", method_id=method_id, source=_SOURCE
        )
    if members is None:
        notebook_ids: tuple[str, ...] = ()
    elif isinstance(members, list):
        if not all(isinstance(member, str) for member in members):
            raise UnknownRPCMethodError(
                message="malformed collection member row", method_id=method_id, source=_SOURCE
            )
        notebook_ids = tuple(members)
    else:
        raise UnknownRPCMethodError(
            message="collection notebook_ids slot is neither None nor list",
            method_id=method_id,
            source=_SOURCE,
        )
    if not isinstance(emoji, str):
        raise UnknownRPCMethodError(
            message="collection emoji slot is not a string", method_id=method_id, source=_SOURCE
        )
    return CollectionRecord(
        id=collection_id,
        name=name,
        emoji=emoji or None,
        notebook_ids=notebook_ids,
    )


__all__ = ["decode_collection"]
