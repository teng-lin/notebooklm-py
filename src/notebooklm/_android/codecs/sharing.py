"""Repository-local wire projection for Android sharing status."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..._env import get_base_url
from ..._types.enums import ShareAccess, SharePermission, ShareViewLevel
from ...exceptions import DecodingError
from ...types import SharedUser, ShareStatus


def decode_share_status(response: Any, notebook_id: str, *, method_id: str) -> ShareStatus:
    """Decode the bundle-evidenced collaborator and public-setting projection."""
    # The generated wire overlay supplies HasField and the named properties.
    has_field = response.HasField
    public_settings = response.public_settings
    is_public = bool(has_field("public_settings") and public_settings.is_publicly_readable)
    max_share_limit = (
        int(response.max_individuals_share_limit)
        if has_field("max_individuals_share_limit")
        else None
    )
    sharing_allowed = (
        bool(response.is_public_sharing_allowed) if has_field("is_public_sharing_allowed") else None
    )
    shared_users = []
    for row in response.shared_users:
        wire_permission = int(row.permission)
        if wire_permission == SharePermission._REMOVE.value:
            continue
        try:
            permission = SharePermission(wire_permission)
        except ValueError:
            raise DecodingError(
                "Android GetProjectDetails returned an unknown collaborator permission",
                method_id=method_id,
            ) from None
        shared_users.append(
            SharedUser(
                email=row.email,
                permission=permission,
                display_name=row.profile.display_name if row.HasField("profile") else None,
                avatar_url=row.profile.avatar_url if row.HasField("profile") else None,
            )
        )
    return ShareStatus(
        notebook_id=notebook_id,
        is_public=is_public,
        access=ShareAccess.ANYONE_WITH_LINK if is_public else ShareAccess.RESTRICTED,
        view_level=ShareViewLevel.FULL_NOTEBOOK,
        shared_users=shared_users,
        share_url=(
            f"{get_base_url()}/notebook/{quote(notebook_id, safe='')}" if is_public else None
        ),
        max_individuals_share_limit=max_share_limit,
        is_public_sharing_allowed=sharing_allowed,
    )


__all__ = ["decode_share_status"]
