"""Notebook routes — ``/v1/notebooks`` list / get / create / delete.

Thin adapters over the transport-neutral ``_app.notebooks`` core and the public
``client.notebooks`` namespace. Responses go straight through
:func:`notebooklm._app.serialize.to_jsonable` (no intermediate server serializer
layer — the same shape the CLI ``--json`` envelopes use).

``_app`` executors that take an injected ``resolve_notebook_id`` are handed the
shared :func:`notebooklm.server.routes._passthrough.passthrough_notebook_id`
resolver — the REST adapter already works in full ids, so resolution is a
pass-through.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from ..._app import notebooks as core
from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from .._context import get_client

__all__ = ["router"]

router = APIRouter(prefix="/notebooks", tags=["notebooks"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]


class NotebookCreate(BaseModel):
    """Request body for creating a notebook."""

    title: str


@router.get("")
async def list_notebooks(client: ClientDep) -> dict[str, Any]:
    """List all notebooks."""
    notebooks = await client.notebooks.list()
    return {"notebooks": to_jsonable(notebooks)}


@router.get("/{notebook_id}")
async def get_notebook(notebook_id: str, client: ClientDep) -> dict[str, Any]:
    """Fetch one notebook (raises ``NotebookNotFoundError`` → 404 on a miss)."""
    notebook = await client.notebooks.get(notebook_id)
    return to_jsonable(notebook)


@router.post("", status_code=201)
async def create_notebook(body: NotebookCreate, client: ClientDep) -> dict[str, Any]:
    """Create a notebook with the given title."""
    result = await core.execute_notebook_create(client, body.title)
    return to_jsonable(result.notebook)


@router.delete("/{notebook_id}", status_code=204)
async def delete_notebook(notebook_id: str, client: ClientDep) -> Response:
    """Delete a notebook (idempotent-on-missing — never 500 for an absent id)."""
    await core.execute_notebook_delete(client, notebook_id)
    return Response(status_code=204)
