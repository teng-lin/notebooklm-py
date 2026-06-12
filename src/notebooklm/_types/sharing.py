"""Private sharing type implementations."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .._env import get_base_url
from ..rpc import RPCMethod, safe_index
from ..rpc.types import ShareAccess, SharePermission, ShareViewLevel

logger = logging.getLogger(__name__)

# The RPC that produces every sharing payload parsed in this module. Passed to
# ``safe_index`` so a shape-drift ``UnknownRPCMethodError`` points at the right
# method. Every ``safe_index`` call below sits AFTER a length/isinstance/truthy
# guard that already proves the slot is present, so the read stays byte-for-byte
# as soft as the legacy ``data[i]`` it replaced — ``safe_index`` never raises on
# any input the old guarded code accepted; it centralises the descent and only
# fires if the guard's invariant is somehow violated (genuine drift).
_SHARE_METHOD_ID = RPCMethod.GET_SHARE_STATUS.value


@dataclass
class SharedUser:
    """A user the notebook is shared with."""

    email: str
    permission: SharePermission
    display_name: str | None = None
    avatar_url: str | None = None

    @classmethod
    def from_api_response(cls, data: list[Any]) -> SharedUser:
        """Parse from GET_SHARE_STATUS user entry.

        Entry format: [email, permission, [], [name, avatar]]
        """
        # ``data[0]`` is the user email. An absent / ``None`` slot keeps the
        # historical silent ``""``-degrade (this factory parses entries out of
        # the whole shared-user list, so raising would abort sibling entries).
        # A *present-but-malformed* slot (non-str, non-None) also degrades to
        # ``""`` for the same reason, but now logs a WARNING instead of
        # silently fabricating an empty email (#1485 absence-vs-malformed
        # policy).
        email = ""
        if data:
            # ``data`` is non-empty here, so slot 0 is present: ``safe_index``
            # can never raise — it stays the soft read it replaces.
            raw_email = safe_index(
                data, 0, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response"
            )
            if isinstance(raw_email, str):
                email = raw_email
            elif raw_email is not None:
                logger.warning(
                    "Share user email slot malformed — fabricating empty email "
                    "(expected str at entry[0], got %s; entry=%s)",
                    type(raw_email).__name__,
                    reprlib.repr(data),
                )
        # ``len(data) > 1`` proves slot 1 is present before descending.
        perm_value = (
            safe_index(data, 1, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response")
            if len(data) > 1
            else 3
        )
        try:
            permission = SharePermission(perm_value)
        except (TypeError, ValueError):
            permission = SharePermission.VIEWER

        display_name = None
        avatar_url = None
        # ``len(data) > 3`` proves slot 3 is present; ``isinstance(..., list)``
        # is checked on the same descended value so the original guard shape is
        # preserved.
        user_info_block = (
            safe_index(data, 3, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response")
            if len(data) > 3
            else None
        )
        if isinstance(user_info_block, list):
            user_info = user_info_block
            # ``if user_info`` / ``len(user_info) > 1`` guard each slot below.
            display_name = (
                safe_index(
                    user_info, 0, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response"
                )
                if user_info
                else None
            )
            avatar_url = (
                safe_index(
                    user_info, 1, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response"
                )
                if len(user_info) > 1
                else None
            )

        return cls(
            email=email,
            permission=permission,
            display_name=display_name,
            avatar_url=avatar_url,
        )


@dataclass
class ShareStatus:
    """Current sharing configuration for a notebook."""

    notebook_id: str
    is_public: bool
    access: ShareAccess
    view_level: ShareViewLevel
    shared_users: list[SharedUser] = field(default_factory=list)
    share_url: str | None = None

    @classmethod
    def from_api_response(cls, data: list[Any], notebook_id: str) -> ShareStatus:
        """Parse from GET_SHARE_STATUS response.

        Response format: [user_entries, public_block_or_null, 1000], where
        user_entries is a list of [email, permission, [], [name, avatar]] rows.
        """
        # Parse users from [0]. ``if data`` proves slot 0 is present before the
        # ``safe_index`` descent, so the read stays as soft as the legacy
        # ``data[0]`` it replaces.
        users = []
        user_entries = (
            safe_index(data, 0, method_id=_SHARE_METHOD_ID, source="ShareStatus.from_api_response")
            if data
            else None
        )
        if isinstance(user_entries, list):
            for user_data in user_entries:
                if isinstance(user_data, list):
                    users.append(SharedUser.from_api_response(user_data))

        # Parse is_public from [1]. Bind the ``[is_public]`` block to a local so
        # the flag read is a single-level index rather than a chained
        # ``data[1][0]`` descent; an absent/empty block legitimately means
        # "not public".
        is_public = False
        # ``len(data) > 1`` proves slot 1 is present before the descent; the
        # ``isinstance(..., list)`` check runs on the descended value so the
        # original "absent/empty block means not-public" contract is preserved.
        public_slot = (
            safe_index(data, 1, method_id=_SHARE_METHOD_ID, source="ShareStatus.from_api_response")
            if len(data) > 1
            else None
        )
        public_block = public_slot if isinstance(public_slot, list) else None
        if public_block:
            # ``if public_block`` proves it is a non-empty list, so slot 0 exists.
            is_public = bool(
                safe_index(
                    public_block,
                    0,
                    method_id=_SHARE_METHOD_ID,
                    source="ShareStatus.from_api_response",
                )
            )

        access = ShareAccess.ANYONE_WITH_LINK if is_public else ShareAccess.RESTRICTED

        # view_level not in GET_SHARE_STATUS response - default to FULL_NOTEBOOK
        view_level = ShareViewLevel.FULL_NOTEBOOK

        # Construct share URL if public. Percent-encode the id with ``safe=""``
        # so reserved characters cannot escape the path position and rewrite
        # the URL into another endpoint (mirrors ``_sharing_manager.build_share_url``).
        share_url = (
            f"{get_base_url()}/notebook/{quote(notebook_id, safe='')}" if is_public else None
        )

        return cls(
            notebook_id=notebook_id,
            is_public=is_public,
            access=access,
            view_level=view_level,
            shared_users=users,
            share_url=share_url,
        )
