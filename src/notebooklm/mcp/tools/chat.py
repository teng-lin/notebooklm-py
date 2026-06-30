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

from typing import Any, Literal

from fastmcp import Context

from ..._app import chat as core
from ..._app.chat import ChatModeChoice, ResponseLengthChoice
from ..._app.serialize import to_jsonable
from ...exceptions import ValidationError
from .._coerce import coerce_list
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook, resolve_sources

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
        question: str = "",
        conversation_id: str | None = None,
        references: Literal["lite", "full"] = "lite",
        source_ids: list[str] | str | None = None,
        history: int = 0,
    ) -> dict[str, Any]:
        """Ask a notebook's sources a question, and/or recall prior turns. Accepts a
        notebook name or ID.

        Pass ``conversation_id`` to continue a specific conversation; omit it to
        continue the notebook's most-recent conversation (or start a new one).

        ``source_ids`` (optional) scopes the question to specific sources by
        id/prefix/title; omit it to query every source. It accepts a real list, a
        JSON-array string, or a comma-separated string (the comma form cannot
        carry a source title that itself contains a comma — use a JSON array or a
        real list for those).

        ``history`` (optional, default 0): the max number of prior Q&A pairs
        (each a ``{question, answer}``) to also return (oldest-first), from the
        conversation as it stood *before* this question. There is no unbounded
        "all" value — pass a generously large number (e.g. 100) for the whole
        conversation. Omit ``question`` (leave it empty) with ``history`` > 0 to
        recall prior pairs without asking anything new; a recall-only call also
        echoes the ``conversation_id`` it read. Pass neither and the call is
        rejected.

        Returns the ``answer`` plus citation ``references`` (when a question is
        asked). The internal ``raw_response`` debugging blob is never included.
        ``references`` controls citation detail: ``lite`` (default) returns
        ``source_id`` / ``citation_number`` / ``cited_text``; ``full`` adds
        chunk-level char offsets and scores.
        """
        client = get_client(ctx)
        with mcp_errors():
            # A whitespace-only question counts as "no question" (recall path), so
            # a blank string can't slip past the guard into client.chat.ask.
            question = question.strip()
            if history < 0:
                raise ValidationError("history must be >= 0.")
            if not question and history == 0:
                raise ValidationError(
                    "Provide a question to ask, or history>0 to recall prior turns."
                )
            nb_id = await resolve_notebook(client, notebook)
            # When recall and a new question both target the "most-recent"
            # conversation, resolve it ONCE so the two awaits can't land on
            # different conversations (and so recall-only can echo the id).
            if conversation_id is None and history > 0:
                conversation_id = await client.chat.get_conversation_id(nb_id)
            payload: dict[str, Any] = {}
            # Fetch history first so it reflects the conversation *before* this
            # question (the new turn isn't double-reported in the recall list).
            # ``limit`` counts individual role-rows (~2 per Q&A pair), so double the
            # caller's pair count to honor the {question, answer} contract. With no
            # conversation yet, skip the fetch — get_history would otherwise re-resolve
            # the (still absent) conversation id for an empty result.
            if history > 0:
                if conversation_id is None:
                    payload["history"] = []
                else:
                    qa_pairs = await client.chat.get_history(
                        nb_id, limit=history * 2, conversation_id=conversation_id
                    )
                    payload["history"] = [{"question": q, "answer": a} for q, a in qa_pairs]
            if question:
                # Tolerate ``source_ids`` sent as a JSON-array string / comma string /
                # scalar, then resolve each ref (id/prefix/title) the same way every
                # other source-accepting tool does. Omitted/empty stays None (=> all
                # sources, mirroring ``client.chat.ask``'s None contract).
                refs = coerce_list(source_ids)
                resolved_source_ids = await resolve_sources(client, nb_id, refs) if refs else None
                result = await client.chat.ask(
                    nb_id,
                    question,
                    source_ids=resolved_source_ids,
                    conversation_id=conversation_id,
                )
                ask_payload = to_jsonable(result)
                # Drop the debug-only raw wire-protocol blob (it just burns agent context).
                ask_payload.pop("raw_response", None)
                if references == "lite":
                    # ``or []`` (not a get-default) so a null ``references`` value is
                    # tolerated, not iterated.
                    ask_payload["references"] = [
                        {k: ref[k] for k in _LITE_REFERENCE_FIELDS if ref.get(k) is not None}
                        for ref in (ask_payload.get("references") or [])
                    ]
                payload.update(ask_payload)
            elif conversation_id is not None:
                # Recall-only: echo the conversation we read so the caller can
                # target it explicitly on a later turn (the ask path echoes its own).
                payload["conversation_id"] = conversation_id
            return payload

    @mcp.tool
    async def chat_configure(
        ctx: Context,
        notebook: str,
        chat_mode: ChatModeChoice | None = None,
        goal: str | None = None,
        response_length: ResponseLengthChoice | None = None,
    ) -> dict[str, Any]:
        """Configure a notebook's chat behavior. Accepts a notebook name or ID.

        Two mutually-exclusive ways to configure:

        * ``chat_mode`` applies a predefined preset — one of ``default`` /
          ``learning-guide`` / ``concise`` / ``detailed``. A preset *replaces* the
          whole chat-settings block, so it cannot be combined with ``goal`` /
          ``response_length`` (doing so is rejected, not silently dropped).
        * ``goal`` (free-text custom persona/goal; selects the CUSTOM chat goal)
          and/or ``response_length`` (``default`` / ``longer`` / ``shorter``) set a
          custom configuration.

        NOTE: in the custom (``goal`` / ``response_length``) branch this writes the
        full chat-settings block, so an omitted field resets to its default (e.g.
        setting only ``response_length`` clears a previously-set custom ``goal``).
        Pass every field you want to keep. (A ``chat_mode`` preset has no sub-fields.)
        """
        client = get_client(ctx)
        with mcp_errors():
            # ``chat_mode`` / ``response_length`` are Literals, so FastMCP/Pydantic
            # rejects out-of-enum values at the schema boundary (no runtime check
            # needed). A preset and a custom field can't both apply (execute_configure
            # short-circuits on chat_mode), so reject the combination rather than
            # silently dropping goal/response_length.
            # An empty ``goal`` ("") is a no-op in execute_configure (only a truthy
            # persona selects CUSTOM), so don't reject a preset for it; any explicit
            # ``response_length`` (incl. "default") is a real setting, so reject that.
            if chat_mode is not None and (goal or response_length is not None):
                raise ValidationError(
                    "chat_mode applies a full preset and cannot be combined with "
                    "goal/response_length; pass one style or the other."
                )
            nb_id = await resolve_notebook(client, notebook)
            result = await core.execute_configure(
                client,
                nb_id,
                chat_mode=chat_mode,
                persona=goal,
                response_length=response_length,
            )
            return to_jsonable(result)
