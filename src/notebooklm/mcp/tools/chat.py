"""Chat MCP tools.

Thin adapters over the chat surface:

* ``chat_ask`` calls ``client.chat.ask`` directly. The neutral ``_app.chat`` core
  owns the CLI's conversation-id selection ladder + save-as-note workflow, none of
  which the MCP tool needs — an explicit ``conversation_id`` passes straight
  through, and omitting it continues the notebook's most-recent conversation (the
  same default the ``ask`` RPC has).
* ``chat_configure`` drives ``_app.chat.execute_configure``. ``goal`` maps to the
  core's ``persona`` argument (a non-empty value selects the ``CUSTOM`` chat goal).

Neither the ``ask`` RPC nor ``execute_configure`` emits progress events, so this
module wires no :class:`~notebooklm._app.events.ProgressSink` — there is nothing
to map and (per the plan) such events are simply dropped. The CLI's Rich-markup
status prose lives only in the ``_app.chat`` *ask-ladder* helpers the MCP tool
deliberately bypasses, so no ``[dim]``/``[yellow]`` markup can reach MCP output.

Both bodies wrap in :func:`mcp_errors`. This module imports NO ``click`` /
``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..._app import chat as core
from ..._app.serialize import to_jsonable
from ...exceptions import ValidationError
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook

_RESPONSE_LENGTHS = ("default", "longer", "shorter")


def register(mcp: Any) -> None:
    """Register the chat tools on ``mcp``."""

    @mcp.tool
    async def chat_ask(
        ctx: Context,
        notebook: str,
        question: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask a notebook's sources a question. Accepts a notebook name or ID.

        Pass ``conversation_id`` to continue a specific conversation; omit it to
        continue the notebook's most-recent conversation (or start a new one).
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            result = await client.chat.ask(nb_id, question, conversation_id=conversation_id)
            return to_jsonable(result)

    @mcp.tool
    async def chat_configure(
        ctx: Context,
        notebook: str,
        goal: str | None = None,
        response_length: str | None = None,
    ) -> dict[str, Any]:
        """Configure a notebook's chat behavior. Accepts a notebook name or ID.

        ``goal`` is a free-text custom persona/goal for the assistant (selects the
        CUSTOM chat goal); ``response_length`` is one of default|longer|shorter.
        """
        client = get_client(ctx)
        with mcp_errors():
            # Validate up front so a bad value returns a clean VALIDATION_ERROR
            # rather than failing deeper in the core.
            if response_length is not None and response_length not in _RESPONSE_LENGTHS:
                raise ValidationError(
                    f"Unknown response_length {response_length!r}; "
                    f"expected one of {'|'.join(_RESPONSE_LENGTHS)}"
                )
            nb_id = await resolve_notebook(client, notebook)
            result = await core.execute_configure(
                client,
                nb_id,
                chat_mode=None,
                persona=goal,
                response_length=response_length,  # type: ignore[arg-type]
            )
            return to_jsonable(result)
