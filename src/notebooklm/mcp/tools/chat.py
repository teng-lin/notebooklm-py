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

import asyncio
from typing import Any, Literal

from fastmcp import Context

from ..._app import chat as core
from ..._app.serialize import to_jsonable
from .._coerce import coerce_list
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook, resolve_source

#: Reference fields kept in the default ("lite") ``chat_ask`` projection. The full
#: ``ChatReference`` also carries chunk-level char offsets / ``chunk_id`` /
#: ``passage_id`` / ``score`` — useful for deep citation tooling but pure context
#: bloat for a typical agent, so they are dropped unless ``references="full"``.
_LITE_REFERENCE_FIELDS = ("source_id", "citation_number", "cited_text")


def register(mcp: Any) -> None:
    """Register the chat tools on ``mcp``."""

    @mcp.tool
    async def chat_ask(
        ctx: Context,
        notebook: str,
        question: str,
        conversation_id: str | None = None,
        references: Literal["lite", "full"] = "lite",
        source_ids: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """Ask a notebook's sources a question. Accepts a notebook name or ID.

        Pass ``conversation_id`` to continue a specific conversation; omit it to
        continue the notebook's most-recent conversation (or start a new one).

        ``source_ids`` (optional) scopes the question to specific sources by
        id/prefix/title; omit it to query every source. It accepts a real list, a
        JSON-array string, or a comma-separated string (the comma form cannot
        carry a source title that itself contains a comma — use a JSON array or a
        real list for those).

        Returns the ``answer`` plus citation ``references``. The internal
        ``raw_response`` debugging blob is never included. ``references`` controls
        citation detail: ``lite`` (default) returns ``source_id`` / ``citation_number``
        / ``cited_text``; ``full`` adds chunk-level char offsets and scores.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            # Tolerate ``source_ids`` sent as a JSON-array string / comma string /
            # scalar, then resolve each ref (id/prefix/title) the same way every
            # other source-accepting tool does. Omitted/empty stays None (=> all
            # sources, mirroring ``client.chat.ask``'s None contract).
            refs = coerce_list(source_ids)
            resolved_source_ids = (
                list(await asyncio.gather(*(resolve_source(client, nb_id, ref) for ref in refs)))
                if refs
                else None
            )
            result = await client.chat.ask(
                nb_id,
                question,
                source_ids=resolved_source_ids,
                conversation_id=conversation_id,
            )
            payload = to_jsonable(result)
            # Drop the debug-only raw wire-protocol blob (it just burns agent context).
            payload.pop("raw_response", None)
            if references == "lite":
                # ``or []`` (not a get-default) so a null ``references`` value is
                # tolerated, not iterated.
                payload["references"] = [
                    {k: ref[k] for k in _LITE_REFERENCE_FIELDS if ref.get(k) is not None}
                    for ref in (payload.get("references") or [])
                ]
            return payload

    @mcp.tool
    async def chat_configure(
        ctx: Context,
        notebook: str,
        goal: str | None = None,
        response_length: Literal["default", "longer", "shorter"] | None = None,
    ) -> dict[str, Any]:
        """Configure a notebook's chat behavior. Accepts a notebook name or ID.

        ``goal`` is a free-text custom persona/goal for the assistant (selects the
        CUSTOM chat goal); ``response_length`` is one of default|longer|shorter.

        NOTE: this writes the full chat-settings block — omitting a field resets it
        to its default (e.g. setting only ``response_length`` clears a previously-set
        custom ``goal``). Pass both to preserve both.
        """
        client = get_client(ctx)
        with mcp_errors():
            # ``response_length`` is a Literal, so FastMCP/Pydantic rejects an
            # out-of-enum value at the schema boundary (no runtime check needed).
            nb_id = await resolve_notebook(client, notebook)
            result = await core.execute_configure(
                client,
                nb_id,
                chat_mode=None,
                persona=goal,
                response_length=response_length,
            )
            return to_jsonable(result)
