"""Chat route — ``POST /v1/notebooks/{id}/chat`` (blocking).

A single blocking ``POST`` that calls ``client.chat.ask`` and returns the full
:class:`~notebooklm.types.AskResult` (answer, references, conversation_id). There
is NO SSE — ``client.chat.ask`` returns a complete answer with no public token
stream, so real-token streaming is deferred until a public streaming surface
exists.

The request rides the client's long ``chat_timeout`` (no short server-imposed
ceiling), tolerant of the RPC-semaphore serialization under concurrency.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from .._context import get_client

__all__ = ["router"]

router = APIRouter(prefix="/notebooks/{notebook_id}/chat", tags=["chat"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]


class ChatAsk(BaseModel):
    """Request body for asking a notebook's sources a question."""

    question: str
    conversation_id: str | None = None


@router.post("")
async def ask(notebook_id: str, body: ChatAsk, client: ClientDep) -> dict[str, Any]:
    """Ask the notebook's sources a question and return the full answer.

    Pass ``conversation_id`` to continue a specific conversation; omit it to
    continue the notebook's most-recent conversation (or start a new one).
    """
    result = await client.chat.ask(notebook_id, body.question, conversation_id=body.conversation_id)
    return to_jsonable(result)
