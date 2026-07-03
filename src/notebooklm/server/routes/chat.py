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

from ..._app import chat as chat_core
from ..._app.chat import ChatModeChoice, ResponseLengthChoice
from ..._app.serialize import to_jsonable
from ..._app.views import ask_result_view
from ...client import NotebookLMClient
from .._context import get_client

__all__ = ["router"]

router = APIRouter(prefix="/notebooks/{notebook_id}/chat", tags=["chat"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]


class ChatAsk(BaseModel):
    """Request body for asking a notebook's sources a question."""

    question: str
    conversation_id: str | None = None


class ChatConfigure(BaseModel):
    """Request body for configuring a notebook's chat behavior.

    Two mutually-exclusive styles (mirrors the MCP ``chat_configure`` tool):

    * ``chat_mode`` applies a predefined preset (``default`` / ``learning-guide``
      / ``concise`` / ``detailed``) and replaces the whole chat-settings block,
      so it cannot be combined with ``goal`` / ``response_length``.
    * ``goal`` (free-text custom persona, selects the CUSTOM chat goal) and/or
      ``response_length`` (``default`` / ``longer`` / ``shorter``) set a custom
      configuration.
    """

    chat_mode: ChatModeChoice | None = None
    goal: str | None = None
    response_length: ResponseLengthChoice | None = None


@router.post("")
async def ask(notebook_id: str, body: ChatAsk, client: ClientDep) -> dict[str, Any]:
    """Ask the notebook's sources a question and return the full answer.

    Pass ``conversation_id`` to continue a specific conversation; omit it to
    continue the notebook's most-recent conversation (or start a new one).
    """
    result = await client.chat.ask(notebook_id, body.question, conversation_id=body.conversation_id)
    # Shared view: drop the internal ``raw_response`` debug blob (identical on the
    # MCP chat_ask surface); the field stays on the dataclass, just not on the wire.
    return ask_result_view(result)


@router.post("/configure")
async def configure(notebook_id: str, body: ChatConfigure, client: ClientDep) -> dict[str, Any]:
    """Configure a notebook's chat behavior (preset OR custom).

    Pass ``chat_mode`` for a predefined preset, or ``goal`` / ``response_length``
    for a custom configuration; the two styles cannot be combined (rejected with
    400, not silently dropped).

    A **partial** custom call (just ``goal`` or just ``response_length``) merges
    with the current settings — the field you omit is preserved, not reset. Only
    a bare call (no ``chat_mode`` and neither custom field) resets every chat
    setting to its default.
    """
    # The preset-vs-custom mutual-exclusion + enum validation live in the shared
    # ``execute_configure`` core, so the CLI, MCP, and this route enforce one rule.
    result = await chat_core.execute_configure(
        client,
        notebook_id,
        chat_mode=body.chat_mode,
        persona=body.goal,
        response_length=body.response_length,
    )
    return {"status": "configured", **to_jsonable(result)}
