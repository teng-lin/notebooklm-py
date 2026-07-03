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

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from ..._app import notebooks as core
from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from .._context import get_client
from .._pagination import MAX_LIMIT, paginate_envelope
from ._passthrough import passthrough_notebook_id

__all__ = ["router"]

router = APIRouter(prefix="/notebooks", tags=["notebooks"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]

#: ``suggested-prompts`` ``surface`` → the ``otmP3b`` (GeneratePromptSuggestions)
#: ``mode`` int selecting the product surface/format the prompts are written for.
#: DUPLICATED from the MCP ``suggest_prompts`` tool's ``_SUGGEST_SURFACE`` (the
#: server layer must not import ``mcp/``); ``tests/server/test_notebooks.py`` pins
#: the two equal so they cannot drift. Map established by the #1726 live probe.
SuggestSurface = Literal[
    "ask",
    "audio-deep-dive",
    "audio-brief",
    "audio-critique",
    "audio-debate",
    "video-explainer",
    "video-short",
    "quiz",
    "flashcards",
]

_SUGGEST_SURFACE: dict[str, int] = {
    "ask": 4,
    "audio-deep-dive": 1,
    "audio-brief": 2,
    "audio-critique": 5,
    "audio-debate": 6,
    "video-explainer": 3,
    "video-short": 10,
    "quiz": 8,
    "flashcards": 9,
}


class NotebookCreate(BaseModel):
    """Request body for creating a notebook."""

    title: str


class NotebookRename(BaseModel):
    """Request body for renaming a notebook."""

    title: str


@router.get("")
async def list_notebooks(
    client: ClientDep,
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """List all notebooks.

    Defaults to the full collection under ``notebooks`` (unchanged). Supply
    ``?limit=`` to slice and add a ``meta`` block; ``?offset=`` pages forward.
    """
    notebooks = await client.notebooks.list()
    return paginate_envelope(to_jsonable(notebooks), key="notebooks", limit=limit, offset=offset)


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


@router.patch("/{notebook_id}")
async def rename_notebook(
    notebook_id: str, body: NotebookRename, client: ClientDep
) -> dict[str, Any]:
    """Rename a notebook."""
    result = await core.execute_notebook_rename(
        client, notebook_id, body.title, resolve_notebook_id=passthrough_notebook_id
    )
    return {"status": "renamed", **to_jsonable(result)}


@router.get("/{notebook_id}/suggested-prompts")
async def suggested_prompts(
    notebook_id: str,
    client: ClientDep,
    surface: Annotated[SuggestSurface, Query()] = "ask",
    source_ids: Annotated[list[str] | None, Query()] = None,
    query: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Get AI-suggested, ready-to-send prompts for a studio surface.

    ``surface`` (default ``ask``) selects what the prompts are written for — chat
    questions (``ask``), or steering an audio / video / quiz / flashcards
    generation. ``source_ids`` (repeatable query param) scopes to specific
    sources; omit for all. ``query`` optionally steers the suggestions. Mirrors
    the MCP ``suggest_prompts`` tool.
    """
    rows = await client.notebooks.suggest_prompts(
        notebook_id,
        source_ids=list(source_ids) if source_ids else None,
        mode=_SUGGEST_SURFACE[surface],
        query=query,
    )
    return {
        "notebook_id": notebook_id,
        "suggestions": [{"title": s.title, "prompt": s.prompt} for s in rows],
    }


@router.delete("/{notebook_id}", status_code=204)
async def delete_notebook(notebook_id: str, client: ClientDep) -> Response:
    """Delete a notebook (idempotent-on-missing — never 500 for an absent id)."""
    await core.execute_notebook_delete(client, notebook_id)
    return Response(status_code=204)
