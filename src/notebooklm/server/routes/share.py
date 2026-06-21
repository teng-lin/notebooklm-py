"""Sharing routes — notebook share status, public link, and user access."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from ..._app import sharing as core
from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from ...types import SharePermission
from ...types import ShareViewLevel as ShareViewLevelEnum
from .._context import get_client
from ._passthrough import passthrough_notebook_id

__all__ = ["router"]

router = APIRouter(prefix="/notebooks/{notebook_id}/share", tags=["share"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]


class SharePublic(BaseModel):
    """Request body for toggling public link sharing."""

    enable: bool


class ShareUserAdd(BaseModel):
    """Request body for sharing a notebook with a user."""

    email: str
    permission: Literal["viewer", "editor"] = "viewer"
    notify: bool = True
    welcome_message: str = ""


class ShareUserUpdate(BaseModel):
    """Request body for changing a user's permission."""

    permission: Literal["viewer", "editor"]


class ShareViewLevelUpdate(BaseModel):
    """Request body for changing public viewer access level."""

    level: Literal["full", "chat"]


_PERMISSIONS = {
    "viewer": SharePermission.VIEWER,
    "editor": SharePermission.EDITOR,
}

_VIEW_LEVELS = {
    "full": ShareViewLevelEnum.FULL_NOTEBOOK,
    "chat": ShareViewLevelEnum.CHAT_ONLY,
}


@router.get("")
async def get_share_status(notebook_id: str, client: ClientDep) -> dict[str, Any]:
    """Return notebook sharing status."""
    status = await core.execute_share_status(
        client,
        notebook_id,
        resolve_notebook_id=passthrough_notebook_id,
    )
    return to_jsonable(status)


@router.post("/public")
async def set_public(notebook_id: str, body: SharePublic, client: ClientDep) -> dict[str, Any]:
    """Enable or disable public link sharing."""
    status = await core.execute_share_set_public(
        client,
        notebook_id,
        body.enable,
        resolve_notebook_id=passthrough_notebook_id,
    )
    return to_jsonable(status)


@router.post("/users", status_code=201)
async def add_user(notebook_id: str, body: ShareUserAdd, client: ClientDep) -> dict[str, Any]:
    """Share a notebook with a user."""
    permission = _PERMISSIONS[body.permission]
    resolved_id = await core.execute_share_add_user(
        client,
        notebook_id,
        body.email,
        permission=permission,
        notify=body.notify,
        welcome_message=body.welcome_message,
        resolve_notebook_id=passthrough_notebook_id,
    )
    return {
        "notebook_id": resolved_id,
        "email": body.email,
        "permission": body.permission,
        "notify": body.notify,
    }


@router.patch("/users/{email}")
async def update_user(
    notebook_id: str, email: str, body: ShareUserUpdate, client: ClientDep
) -> dict[str, Any]:
    """Update a shared user's permission.

    NotebookLM creates the membership if the email is not already shared.
    """
    resolved_id = await core.execute_share_update_user(
        client,
        notebook_id,
        email,
        _PERMISSIONS[body.permission],
        resolve_notebook_id=passthrough_notebook_id,
    )
    return {"notebook_id": resolved_id, "email": email, "permission": body.permission}


@router.delete("/users/{email}", status_code=204)
async def remove_user(notebook_id: str, email: str, client: ClientDep) -> Response:
    """Remove a user's notebook access."""
    await core.execute_share_remove_user(client, notebook_id, email)
    return Response(status_code=204)


@router.post("/view-level")
async def set_view_level(
    notebook_id: str, body: ShareViewLevelUpdate, client: ClientDep
) -> dict[str, Any]:
    """Set what public viewers can access."""
    _resolved_id, status = await core.execute_share_set_view_level(
        client,
        notebook_id,
        _VIEW_LEVELS[body.level],
        resolve_notebook_id=passthrough_notebook_id,
    )
    return to_jsonable(status)
