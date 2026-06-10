"""Private sharing type implementations."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .._env import get_base_url
from ..rpc.types import ShareAccess, SharePermission, ShareViewLevel

logger = logging.getLogger(__name__)


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
            raw_email = data[0]
            if isinstance(raw_email, str):
                email = raw_email
            elif raw_email is not None:
                logger.warning(
                    "Share user email slot malformed — fabricating empty email "
                    "(expected str at entry[0], got %s; entry=%s)",
                    type(raw_email).__name__,
                    reprlib.repr(data),
                )
        perm_value = data[1] if len(data) > 1 else 3
        try:
            permission = SharePermission(perm_value)
        except (TypeError, ValueError):
            permission = SharePermission.VIEWER

        display_name = None
        avatar_url = None
        if len(data) > 3 and isinstance(data[3], list):
            user_info = data[3]
            display_name = user_info[0] if user_info else None
            avatar_url = user_info[1] if len(user_info) > 1 else None

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

        Response format: [[[user_entries]], [is_public], 1000]
        """
        # Parse users from [0]
        users = []
        if data and isinstance(data[0], list):
            for user_data in data[0]:
                if isinstance(user_data, list):
                    users.append(SharedUser.from_api_response(user_data))

        # Parse is_public from [1]. Bind the ``[is_public]`` block to a local so
        # the flag read is a single-level index rather than a chained
        # ``data[1][0]`` descent; an absent/empty block legitimately means
        # "not public".
        is_public = False
        public_block = data[1] if len(data) > 1 and isinstance(data[1], list) else None
        if public_block:
            is_public = bool(public_block[0])

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
