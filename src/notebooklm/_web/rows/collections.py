"""Web row decoder for the public collection model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from ...exceptions import UnknownRPCMethodError
from ...rpc import safe_index

if TYPE_CHECKING:
    from ..._types.collections import Collection

# Preserve the established diagnostic label across the implementation move.
_SRC = "_types.collections"


@dataclass(frozen=True)
class CollectionRow:
    """Strict typed view of ``[name, notebook_ids, collection_id, emoji]``."""

    _raw: list[Any] = field(repr=False)
    _method_id: str | None = field(default=None, repr=False)

    _NAME_POS: ClassVar[int] = 0
    _MEMBERS_POS: ClassVar[int] = 1
    _ID_POS: ClassVar[int] = 2
    _EMOJI_POS: ClassVar[int] = 3

    def decode(self, cls: type[Collection]) -> Collection:
        name = safe_index(self._raw, self._NAME_POS, method_id=self._method_id, source=_SRC)
        members = safe_index(self._raw, self._MEMBERS_POS, method_id=self._method_id, source=_SRC)
        collection_id = safe_index(self._raw, self._ID_POS, method_id=self._method_id, source=_SRC)
        emoji = safe_index(self._raw, self._EMOJI_POS, method_id=self._method_id, source=_SRC)
        if not isinstance(name, str) or not isinstance(collection_id, str):
            raise UnknownRPCMethodError(
                message="collection tuple name/id not strings",
                method_id=self._method_id,
                source=_SRC,
            )
        if members is None:
            notebook_ids: list[str] = []
        elif isinstance(members, list):
            if not all(isinstance(member, str) for member in members):
                raise UnknownRPCMethodError(
                    message="malformed collection member row",
                    method_id=self._method_id,
                    source=_SRC,
                )
            notebook_ids = list(members)
        else:
            raise UnknownRPCMethodError(
                message="collection notebook_ids slot is neither None nor list",
                method_id=self._method_id,
                source=_SRC,
            )
        if not isinstance(emoji, str):
            raise UnknownRPCMethodError(
                message="collection emoji slot is not a string",
                method_id=self._method_id,
                source=_SRC,
            )
        return cls(
            id=collection_id,
            name=name,
            emoji=emoji or None,
            notebook_ids=notebook_ids,
        )


def decode_collection(
    cls: type[Collection], data: list[Any], *, method_id: str | None = None
) -> Collection:
    """Decode a web collection row into the requested public model class."""
    return CollectionRow(data, method_id).decode(cls)


__all__ = ["CollectionRow", "decode_collection"]
