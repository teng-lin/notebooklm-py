"""Web row decoders for public sharing models."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote

from ..._env import get_base_url
from ..._types.enums import ShareAccess, SharePermission, ShareViewLevel
from ...rpc import RPCMethod, safe_index

if TYPE_CHECKING:
    from ..._types.sharing import SharedUser, ShareStatus

logger = logging.getLogger("notebooklm._types.sharing")

_SHARE_METHOD_ID = RPCMethod.GET_SHARE_STATUS.value
_warned_malformed_share_slots: set[tuple[str, str]] = set()
_MAX_DRIFT_REPR_LEN = 120


@dataclass(frozen=True)
class SharedUserRow:
    """Typed view of one shared-user row from ``GET_SHARE_STATUS``."""

    _raw: list[Any] = field(repr=False)

    _EMAIL_POS: ClassVar[int] = 0
    _PERMISSION_POS: ClassVar[int] = 1
    _USER_INFO_POS: ClassVar[int] = 3
    _DISPLAY_NAME_POS: ClassVar[int] = 0
    _AVATAR_URL_POS: ClassVar[int] = 1

    def decode(self, cls: type[SharedUser]) -> SharedUser:
        email = ""
        if self._raw:
            raw_email = safe_index(
                self._raw,
                self._EMAIL_POS,
                method_id=_SHARE_METHOD_ID,
                source="SharedUser.from_api_response",
            )
            if isinstance(raw_email, str):
                email = raw_email
            elif raw_email is not None:
                logger.warning(
                    "Share user email slot malformed — fabricating empty email "
                    "(expected str at entry[0], got %s; entry=%s)",
                    type(raw_email).__name__,
                    reprlib.repr(self._raw),
                )

        perm_value = (
            safe_index(
                self._raw,
                self._PERMISSION_POS,
                method_id=_SHARE_METHOD_ID,
                source="SharedUser.from_api_response",
            )
            if len(self._raw) > self._PERMISSION_POS
            else SharePermission.VIEWER.value
        )
        try:
            permission = SharePermission(perm_value)
        except (TypeError, ValueError):
            permission = SharePermission.VIEWER

        display_name = None
        avatar_url = None
        user_info = (
            safe_index(
                self._raw,
                self._USER_INFO_POS,
                method_id=_SHARE_METHOD_ID,
                source="SharedUser.from_api_response",
            )
            if len(self._raw) > self._USER_INFO_POS
            else None
        )
        if isinstance(user_info, list):
            display_name = (
                safe_index(
                    user_info,
                    self._DISPLAY_NAME_POS,
                    method_id=_SHARE_METHOD_ID,
                    source="SharedUser.from_api_response",
                )
                if user_info
                else None
            )
            avatar_url = (
                safe_index(
                    user_info,
                    self._AVATAR_URL_POS,
                    method_id=_SHARE_METHOD_ID,
                    source="SharedUser.from_api_response",
                )
                if len(user_info) > self._AVATAR_URL_POS
                else None
            )

        return cls(
            email=email,
            permission=permission,
            display_name=display_name,
            avatar_url=avatar_url,
        )


@dataclass(frozen=True)
class ShareStatusRow:
    """Typed view of a ``GET_SHARE_STATUS`` response row."""

    _raw: list[Any] = field(repr=False)

    _USERS_POS: ClassVar[int] = 0
    _PUBLIC_BLOCK_POS: ClassVar[int] = 1
    _IS_PUBLIC_INNER_POS: ClassVar[int] = 0
    _MAX_SHARE_LIMIT_POS: ClassVar[int] = 2
    _PUBLIC_SHARING_ALLOWED_POS: ClassVar[int] = 3

    def _scalar_at(self, pos: int) -> Any:
        if len(self._raw) <= pos:
            return None
        return safe_index(
            self._raw, pos, method_id=_SHARE_METHOD_ID, source="ShareStatus.from_api_response"
        )

    def decode(self, cls: type[ShareStatus], notebook_id: str) -> ShareStatus:
        from ..._types.sharing import SharedUser

        users = []
        # Preserve the pre-extraction ordering for malformed top-level inputs:
        # a truthy non-list reaches ``safe_index`` first and raises the
        # structured decoder error, while a falsey non-list reaches the later
        # length guard and retains its historical ``TypeError``.
        user_entries = (
            safe_index(
                self._raw,
                self._USERS_POS,
                method_id=_SHARE_METHOD_ID,
                source="ShareStatus.from_api_response",
            )
            if self._raw
            else None
        )
        if isinstance(user_entries, list):
            users = [
                decode_shared_user(SharedUser, user_data)
                for user_data in user_entries
                if isinstance(user_data, list)
            ]

        public_slot = self._scalar_at(self._PUBLIC_BLOCK_POS)
        public_block = public_slot if isinstance(public_slot, list) else None
        is_public = False
        if public_block:
            is_public = bool(
                safe_index(
                    public_block,
                    self._IS_PUBLIC_INNER_POS,
                    method_id=_SHARE_METHOD_ID,
                    source="ShareStatus.from_api_response",
                )
            )

        limit_slot = self._scalar_at(self._MAX_SHARE_LIMIT_POS)
        max_individuals_share_limit = None
        if isinstance(limit_slot, int) and not isinstance(limit_slot, bool):
            max_individuals_share_limit = limit_slot
        else:
            _warn_if_malformed(limit_slot, "maxIndividualsShareLimit", "int")

        allowed_slot = self._scalar_at(self._PUBLIC_SHARING_ALLOWED_POS)
        is_public_sharing_allowed = None
        if isinstance(allowed_slot, bool):
            is_public_sharing_allowed = allowed_slot
        else:
            _warn_if_malformed(allowed_slot, "isPublicSharingAllowed", "bool")

        access = ShareAccess.ANYONE_WITH_LINK if is_public else ShareAccess.RESTRICTED
        share_url = (
            f"{get_base_url()}/notebook/{quote(notebook_id, safe='')}" if is_public else None
        )
        return cls(
            notebook_id=notebook_id,
            is_public=is_public,
            access=access,
            view_level=ShareViewLevel.FULL_NOTEBOOK,
            shared_users=users,
            share_url=share_url,
            max_individuals_share_limit=max_individuals_share_limit,
            is_public_sharing_allowed=is_public_sharing_allowed,
        )


def _warn_if_malformed(value: Any, field_name: str, expected: str) -> None:
    """Log once per malformed sharing slot type."""
    if value is None:
        return
    key = (field_name, type(value).__name__)
    if key in _warned_malformed_share_slots:
        return
    _warned_malformed_share_slots.add(key)
    rendered = repr(value)[:_MAX_DRIFT_REPR_LEN]
    logger.warning(
        "GET_SHARE_STATUS %s slot malformed — reporting 'no claim' (expected %s, got %s: %s)",
        field_name,
        expected,
        type(value).__name__,
        rendered,
    )


def decode_shared_user(cls: type[SharedUser], data: list[Any]) -> SharedUser:
    """Decode a web shared-user row into the requested public model class."""
    return SharedUserRow(data).decode(cls)


def decode_share_status(cls: type[ShareStatus], data: list[Any], notebook_id: str) -> ShareStatus:
    """Decode a web share-status row into the requested public model class."""
    return ShareStatusRow(data).decode(cls, notebook_id)


__all__ = [
    "SharedUserRow",
    "ShareStatusRow",
    "decode_shared_user",
    "decode_share_status",
]
