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
from ..._app.chat import ChatModeChoice, ResponseLengthChoice
from ..._app.serialize import to_jsonable
from ...exceptions import ValidationError
from .._coerce import coerce_list
from .._confirm import READ_ONLY
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook, resolve_sources

#: Reference fields kept in the default ("lite") ``chat_ask`` projection. The full
#: ``ChatReference`` also carries chunk-level char offsets / ``chunk_id`` /
#: ``passage_id`` / ``score`` — useful for deep citation tooling but pure context
#: bloat for a typical agent, so they are dropped unless ``references="full"``.
_LITE_REFERENCE_FIELDS = ("source_id", "citation_number", "cited_text")

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

#: ``suggest_prompts`` surface → the ``otmP3b`` (GeneratePromptSuggestions) ``mode``
#: int. The mode selects the product surface + format the prompts are written for.
#: Map established by the #1726 live investigation (2026-07-01): audio formats
#: browser-verified (each Customize-dialog format card decoded its otmP3b mode),
#: video from real web captures, quiz/flashcards client-probed. Supersedes the
#: earlier output-based #1612 guess. ``ask`` (4) is the web chat default. Keyed by
#: ``SuggestSurface`` so mypy rejects any key not in the Literal; a Literal member
#: missing from the map is caught by the per-surface test in ``test_chat.py``.
_SUGGEST_SURFACE: dict[SuggestSurface, int] = {
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
        suggest_followups: bool = False,
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

        ``suggest_followups`` (optional, default ``False``): when ``True`` the
        result also carries a ``suggested_prompts`` list of AI-suggested
        follow-up questions (each a ``{title, prompt}``), scoped to the same
        ``source_ids`` and steered by ``question`` when one is given. It works on
        its own too — pass it with no ``question`` (and ``history`` 0) to get
        suggested questions without asking anything. When omitted/``False`` the
        result never contains a ``suggested_prompts`` key.
        """
        client = get_client(ctx)
        with mcp_errors():
            # A whitespace-only question counts as "no question" (recall path), so
            # a blank string can't slip past the guard into client.chat.ask.
            question = question.strip()
            if history < 0:
                raise ValidationError("history must be >= 0.")
            if not question and history == 0 and not suggest_followups:
                raise ValidationError(
                    "Provide a question to ask, history>0 to recall prior turns, "
                    "or suggest_followups=true for suggested questions."
                )
            nb_id = await resolve_notebook(client, notebook)
            # Resolve source refs ONCE up front so both the ask path and the
            # suggest path share the same ids. Tolerate ``source_ids`` sent as a
            # JSON-array string / comma string / scalar, then resolve each ref
            # (id/prefix/title) the same way every other source-accepting tool
            # does. Omitted/empty stays None (=> all sources, mirroring
            # ``client.chat.ask``'s None contract).
            refs = coerce_list(source_ids)
            # Resolve only when a path actually consumes the ids: the ask path
            # (a question) or the suggest path (suggest_followups). A recall-only
            # turn (history>0, no question, no suggest) does not scope by source, so
            # leave refs unresolved to preserve the prior no-op — no extra
            # ``sources.list`` round-trip and no ``SourceNotFoundError`` on a stale
            # ref that the recall path would have ignored anyway.
            resolved_source_ids = (
                await resolve_sources(client, nb_id, refs)
                if refs and (question or suggest_followups)
                else None
            )
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
            # The ask (client.chat.ask) and the suggestions (suggest_prompts,
            # mode=4 = the chat "ask about the content" surface) are independent
            # RPCs — run them concurrently when both are requested (repo convention).
            # suggest_prompts has no _app core, so it's a direct client call (same
            # as server_info reaching client.settings); its keyword-only args + the
            # up-front-resolved source ids are passed explicitly. ``query`` steers
            # off the question when one was asked (None => unsteered).
            ask_coro = (
                client.chat.ask(
                    nb_id,
                    question,
                    source_ids=resolved_source_ids,
                    conversation_id=conversation_id,
                )
                if question
                else None
            )
            suggest_coro = (
                client.notebooks.suggest_prompts(
                    nb_id,
                    source_ids=resolved_source_ids,
                    mode=4,
                    query=question or None,
                )
                if suggest_followups
                else None
            )
            if ask_coro is not None and suggest_coro is not None:
                ask_result, suggestions = await asyncio.gather(ask_coro, suggest_coro)
            elif ask_coro is not None:
                ask_result, suggestions = await ask_coro, None
            elif suggest_coro is not None:
                ask_result, suggestions = None, await suggest_coro
            else:
                ask_result, suggestions = None, None

            if ask_result is not None:
                ask_payload = to_jsonable(ask_result)
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
            if suggestions is not None:
                payload["suggested_prompts"] = [
                    {"title": s.title, "prompt": s.prompt} for s in suggestions
                ]
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
          and ``response_length`` (``default`` / ``longer`` / ``shorter``) set a
          custom configuration.

        A custom config writes the whole block with no merge, so ``goal`` and
        ``response_length`` are required together — a partial or bare call is
        rejected, not silently reset. To set verbosity only, use a preset.
        """
        client = get_client(ctx)
        with mcp_errors():
            # A custom configuration writes the FULL chat-settings block (no server-side
            # merge and no client getter to read-merge), so an omitted field silently resets
            # to its default. Fail loud instead of clobbering: require BOTH goal and
            # response_length together, and reject a bare call. (A chat_mode preset has no
            # sub-fields, so this only gates the custom branch.) "Supplied" matches the core:
            # an empty goal ("") is a no-op (core uses `if persona:`), and any explicit
            # response_length — incl. "default" — is a real setting.
            if chat_mode is None:
                goal_supplied = bool(goal)
                length_supplied = response_length is not None
                if not goal_supplied and not length_supplied:
                    raise ValidationError(
                        "chat_configure needs at least one setting: a chat_mode preset "
                        "(default / learning-guide / concise / detailed), or BOTH goal and "
                        "response_length for a custom configuration. A bare call would reset "
                        "every chat setting to its default."
                    )
                if goal_supplied != length_supplied:
                    raise ValidationError(
                        "A custom chat_configure writes the full settings block, so the "
                        "omitted field would reset to its default (there is no partial "
                        "merge). Pass BOTH goal and response_length. To change only the "
                        "response length without a custom goal, use a chat_mode preset "
                        "(concise = shorter, detailed = longer)."
                    )

            # ``chat_mode`` / ``response_length`` are Literals, so FastMCP/Pydantic
            # rejects out-of-enum values at the schema boundary. The preset-vs-custom
            # mutual-exclusion (chat_mode cannot be combined with goal/response_length)
            # is enforced transport-neutrally in ``execute_configure`` so the CLI and
            # this tool share one rule.
            nb_id = await resolve_notebook(client, notebook)
            result = await core.execute_configure(
                client,
                nb_id,
                chat_mode=chat_mode,
                persona=goal,
                response_length=response_length,
            )
            return {"status": "configured", **to_jsonable(result)}

    @mcp.tool(annotations=READ_ONLY)
    async def suggest_prompts(
        ctx: Context,
        notebook: str,
        surface: SuggestSurface = "ask",
        source_ids: list[str] | str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Get AI-suggested, ready-to-send prompts for a studio surface. Accepts a
        notebook name or ID.

        ``surface`` selects what the prompts are written for (default ``ask``):
        * ``ask`` — chat questions to ask the notebook's content.
        * ``audio-deep-dive`` / ``audio-brief`` / ``audio-critique`` / ``audio-debate``
          — prompts to steer an Audio Overview in that format.
        * ``video-explainer`` / ``video-short`` — prompts to steer a Video Overview.
        * ``quiz`` / ``flashcards`` — prompts to steer quiz / flashcard generation.

        Each result is a ready-to-send instruction you can pass to the matching
        generator (``chat_ask`` for ``ask``; ``studio_generate``'s ``instructions`` for
        the studio formats). ``source_ids`` (optional) scopes the suggestions to
        specific sources; omit for all. ``query`` optionally steers the suggestions.

        Related: ``chat_ask(suggest_followups=true)`` returns ``ask``-surface
        suggestions inline with a question (ask + follow-ups in one call); this tool
        is the standalone selector across every surface.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            # Tolerate source_ids as a JSON-array string / comma string / scalar,
            # then resolve each ref (id/prefix/title). Omitted/empty stays None
            # (=> all sources, mirroring the client's None contract).
            refs = coerce_list(source_ids)
            resolved_source_ids = await resolve_sources(client, nb_id, refs) if refs else None
            # ``surface`` is a Literal, so FastMCP/Pydantic rejects an out-of-enum
            # value at the schema boundary — the map lookup can't KeyError.
            # ``query`` is passed through as-is: the payload builder
            # (``build_prompt_suggestions_params``) is the single normalization
            # point — it maps None / "" / whitespace-only to a null steer.
            rows = await client.notebooks.suggest_prompts(
                nb_id,
                source_ids=resolved_source_ids,
                mode=_SUGGEST_SURFACE[surface],
                query=query,
            )
            return {"suggestions": [{"title": s.title, "prompt": s.prompt} for s in rows]}
