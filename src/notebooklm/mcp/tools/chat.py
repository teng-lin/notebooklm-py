"""Chat MCP tools.

Thin adapters over the chat surface:

* ``chat_ask`` calls ``client.chat.ask`` directly. The neutral ``_app.chat`` core
  owns the CLI's conversation-id selection ladder + save-as-note workflow, none of
  which the MCP tool needs — an explicit ``conversation_id`` passes straight
  through, and omitting it continues the notebook's most-recent conversation (the
  same default the ``ask`` RPC has).
* ``chat_configure`` drives ``_app.chat.execute_configure``. ``goal`` maps to the
  core's ``persona`` argument (a non-empty value selects the ``CUSTOM`` chat goal).
* ``chat_start`` / ``chat_status`` are the watchdog-safe pair for slow
  generations: ``chat_start`` detaches the ask into a server-owned task
  (:mod:`notebooklm.mcp._chattasks`) and returns a ``task_id`` immediately;
  ``chat_status`` polls it and returns the finished answer inline. Remote MCP
  transports cut a call at ~60s to the first response byte (the watchdog
  ``tools/_fileupload.py`` documents for uploads), which kills a blocking
  ``chat_ask`` mid-generation — the re-invoke poll, not any keepalive, is the
  load-bearing completion mechanism (same ADR-0024 shape as ``await_upload``,
  same contract ``studio_generate``/``studio_status`` ship for studio work).
* ``chat_status(notebook=...)`` and ``chat_cancel`` expose Google's live
  session controls. Cancellation is paired with local task cancellation when a
  detached ``task_id`` is supplied, because the Web response stream does not
  close itself after the server stops generating.

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
import time
from typing import Any, Literal

from fastmcp import Context

from ..._app import chat as core
from ..._app.chat import ChatModeChoice, ResponseLengthChoice
from ..._app.notebooks import SUGGEST_SURFACE_MAP, SuggestSurface
from ..._app.serialize import to_jsonable
from ..._app.views import ask_result_view
from ...exceptions import RateLimitError, ValidationError
from .._chattasks import ChatTaskCapacityError, compute_chat_task_key
from .._coerce import coerce_list
from .._confirm import READ_ONLY
from .._context import get_chat_tasks, get_client
from .._errors import mcp_errors, tool_error_payload
from .._resolve import resolve_notebook, resolve_sources

#: Reference fields kept in the default ("lite") ``chat_ask`` projection. The full
#: ``ChatReference`` also carries chunk-level char offsets / ``chunk_id`` /
#: ``passage_id`` / ``score`` — useful for deep citation tooling but pure context
#: bloat for a typical agent, so they are dropped unless ``references="full"``.
_LITE_REFERENCE_FIELDS = ("source_id", "citation_number", "cited_text")

#: Ceiling on ids per ``chat_status`` call. The registry retains at most 256
#: entries, so a legitimate batch is small; the cap keeps a hostile or runaway
#: caller from turning one poll into unbounded per-id work.
_MAX_STATUS_BATCH = 64


def _entry_status_payload(entry: Any) -> dict[str, Any]:
    """Project one :class:`~notebooklm.mcp._chattasks.ChatTaskEntry` onto the
    ``chat_status`` wire vocabulary (shared by the single and batch shapes).

    Timings ride the terminal states (``queued_s`` — submission → generation
    start; ``generation_s`` — start → finish) so real-world concurrency can be
    calibrated from ordinary usage.
    """
    if entry.done_at is None:
        waiting = entry.started_at is None
        return {
            "status": "pending",
            "task_id": entry.task_id,
            "state": "queued" if waiting else "generating",
            "waited_s": round(time.monotonic() - entry.created_at, 1),
            "hint": (
                "waiting for a generation slot — re-invoke chat_status in ~20-30s"
                if waiting
                else "the answer is still generating — re-invoke chat_status "
                "with the same task_id in ~20-30s"
            ),
        }
    timings = {
        "queued_s": round((entry.started_at or entry.created_at) - entry.created_at, 1),
        "generation_s": round(entry.done_at - (entry.started_at or entry.created_at), 1),
    }
    if entry.error is not None:
        if isinstance(entry.error, asyncio.CancelledError):
            return {
                "status": "cancelled",
                "task_id": entry.task_id,
                **timings,
                "hint": "generation was cancelled; call chat_start to ask again",
            }
        return {
            "status": "failed",
            "task_id": entry.task_id,
            "error": tool_error_payload(entry.error),
            **timings,
            "hint": (
                "the ask failed — a retriable error is worth one chat_start "
                "retry with the same question (a retry re-runs the generation)"
            ),
        }
    return {"status": "completed", "task_id": entry.task_id, **timings, **(entry.result or {})}


def _ask_result_payload(ask_result: Any, references: str) -> dict[str, Any]:
    """Serialize an ask result for the wire — shared by ``chat_ask`` and the
    detached ``chat_start`` task so the two paths cannot drift.

    The shared :func:`ask_result_view` projection (which already drops the
    debug-only ``raw_response`` blob), minus the chunk-level reference detail
    unless ``references="full"``. ``or []`` (not a get-default) so a null
    ``references`` value is tolerated, not iterated.
    """
    payload = ask_result_view(ask_result)
    if references == "lite":
        payload["references"] = [
            {k: ref[k] for k in _LITE_REFERENCE_FIELDS if ref.get(k) is not None}
            for ref in (payload.get("references") or [])
        ]
    return payload


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

        Returns the ``answer``, ``turn_number``, and citation ``references``
        (when a question is asked).
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
            client = await get_client(ctx)
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
            # Seed with the resolved canonical notebook_id so every exit shape
            # (ask / recall-only / suggest-followups) echoes it — an automation that
            # passed a notebook *name* gets the id back for deterministic chaining
            # (#1808).
            payload: dict[str, Any] = {"notebook_id": nb_id}
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
                # Independent RPCs → run concurrently, but drive explicit tasks so a
                # failure in one cancels + drains the still-running sibling instead of
                # leaking it (mirrors ``_sources._wait_all_sources``).
                tasks = (
                    asyncio.create_task(ask_coro),
                    asyncio.create_task(suggest_coro),
                )
                try:
                    ask_result, suggestions = await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
            elif ask_coro is not None:
                ask_result, suggestions = await ask_coro, None
            elif suggest_coro is not None:
                ask_result, suggestions = None, await suggest_coro
            else:
                ask_result, suggestions = None, None

            if ask_result is not None:
                # Shared view + reference projection (:func:`_ask_result_payload`) —
                # identical on the REST chat route and the detached ``chat_start`` task.
                payload.update(_ask_result_payload(ask_result, references))
            elif conversation_id is not None:
                # Recall-only: echo the conversation we read so the caller can
                # target it explicitly on a later turn (the ask path echoes its own).
                payload["conversation_id"] = conversation_id
            if suggestions is not None:
                payload["suggested_prompts"] = [
                    {"title": s.title, "prompt": s.prompt} for s in suggestions
                ]
            # Echo the resolved canonical source scope when the caller passed one
            # (by id/prefix/title) so a title-scoped call hands back the ids for
            # deterministic chaining (#1808). Omitted when unscoped (all sources).
            if resolved_source_ids is not None:
                payload["source_ids"] = resolved_source_ids
            return payload

    @mcp.tool
    async def chat_start(
        ctx: Context,
        notebook: str,
        question: str,
        conversation_id: str | None = None,
        references: Literal["lite", "full"] = "lite",
        source_ids: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """Start a chat ask as a detached background task and return immediately.

        The watchdog-safe way to ask when generation may run long (large/shared
        notebooks routinely take 1–3 minutes, and remote MCP transports cut a
        call at ~60s of silence — which kills a blocking ``chat_ask``
        mid-generation). The ask runs server-side regardless of what happens to
        this call; ``chat_status`` collects the answer. For questions expected
        to answer quickly, ``chat_ask`` stays the one-call path.

        Parameters mirror ``chat_ask``'s ask path (``history`` /
        ``suggest_followups`` are ``chat_ask``-only). Returns one of:

        * ``{"status": "started", "task_id": ...}`` — **call ``chat_status``
          with this ``task_id`` in ~20–30s**; re-invoke while ``pending``.
        * ``{"status": "already_running", "task_id": ...}`` — an identical ask
          is still in flight; poll that ``task_id`` (no double generation).

        A finished ask is never replayed: starting the same question again
        re-asks and appends a new conversation turn, exactly like ``chat_ask``
        (so an answer can't go stale after ``source_add`` / ``chat_configure``).
        A finished payload stays pollable by its ``task_id`` for ~30 minutes,
        then ``chat_status`` reports ``unknown``.
        """
        with mcp_errors():
            question = question.strip()
            if not question:
                raise ValidationError("chat_start needs a non-empty question.")
            client = await get_client(ctx)
            nb_id = await resolve_notebook(client, notebook)
            # Same source-scoping contract as chat_ask: tolerant input shapes,
            # resolved ONCE up front; omitted/empty stays None (=> all sources).
            refs = coerce_list(source_ids)
            resolved_source_ids = await resolve_sources(client, nb_id, refs) if refs else None
            resolved_conversation_id = conversation_id
            if resolved_conversation_id is None:
                resolved_conversation_id = await client.chat.get_conversation_id(nb_id)
            # Key on the RESOLVED inputs so retries dedupe however the caller
            # spelled the notebook/source refs (see compute_chat_task_key).
            key = compute_chat_task_key(
                nb_id, question, resolved_source_ids, resolved_conversation_id, references
            )

            async def _run_ask() -> dict[str, Any]:
                # Runs as a server-owned task (not a child of this request), so a
                # transport cutting the start call cannot kill the generation.
                ask_result = await client.chat.ask(
                    nb_id,
                    question,
                    source_ids=resolved_source_ids,
                    conversation_id=resolved_conversation_id,
                )
                payload: dict[str, Any] = {"notebook_id": nb_id}
                payload.update(_ask_result_payload(ask_result, references))
                if resolved_source_ids is not None:
                    payload["source_ids"] = resolved_source_ids
                return payload

            try:
                entry, how = get_chat_tasks(ctx).start(
                    key,
                    _run_ask,
                    notebook_id=nb_id,
                    conversation_id=resolved_conversation_id,
                )
            except ChatTaskCapacityError as exc:
                # Nothing about the arguments is wrong and the same call succeeds
                # once a slot frees — so it must project as a RETRIABLE category
                # (RATE_LIMITED: "back off and retry"), not VALIDATION, which the
                # guide tells agents to stop on.
                raise RateLimitError(str(exc)) from exc
            return {
                "status": "started" if how == "created" else "already_running",
                "task_id": entry.task_id,
                "notebook_id": nb_id,
                "hint": (
                    "the answer usually lands within 1-3 minutes — call chat_status "
                    "with this task_id in ~20-30s, and re-invoke it while it reports "
                    "pending"
                ),
            }

    @mcp.tool(annotations=READ_ONLY)
    async def chat_status(
        ctx: Context,
        task_id: str | list[str] | None = None,
        notebook: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Poll detached asks, or inspect a notebook's live generation session.

        Detached-task mode: ``task_id`` accepts one id, a list, or a
        comma-separated string — **poll
        a whole batch in ONE call** and re-invoke every ~20–30s while anything
        is pending. Per-task statuses:

        * ``pending`` (``state``: ``queued`` waiting for a generation slot, or
          ``generating``) — re-invoke; polling never restarts a generation.
        * ``completed`` — the full ``chat_ask``-shaped payload, plus
          ``queued_s`` / ``generation_s`` timings.
        * ``failed`` — ``error: {code, message, retriable, ...}``; a retriable
          code is worth one ``chat_start`` retry (which re-runs the generation).
        * ``unknown`` — no such task, or its result expired; ``chat_start``
          again.

        A single string id returns that task's status dict; a list (even of
        one) returns ``{"tasks": [...]}`` in input order. At most 64 ids per
        call.

        Live-session mode: pass ``notebook`` instead of ``task_id`` to inspect
        Google's current chat session. ``conversation_id`` is optional and
        defaults to that notebook's most-recent conversation. The result is
        ``idle`` or ``generating`` and includes the generation token when Google
        supplies one. The two modes are mutually exclusive.
        """
        with mcp_errors():
            if notebook is not None:
                if task_id is not None:
                    raise ValidationError(
                        "chat_status accepts either notebook or task_id, not both."
                    )
                client = await get_client(ctx)
                nb_id = await resolve_notebook(client, notebook)
                resolved_id = conversation_id or await client.chat.get_conversation_id(nb_id)
                if resolved_id is None:
                    return {
                        "status": "idle",
                        "notebook_id": nb_id,
                        "conversation_id": None,
                        "generation_token": None,
                    }
                session = await client.chat.session_status(nb_id, resolved_id)
                return {
                    "status": "generating" if session.generating else "idle",
                    "notebook_id": nb_id,
                    "conversation_id": resolved_id,
                    "generation_token": session.token,
                }
            if conversation_id is not None:
                raise ValidationError("conversation_id requires notebook in chat_status.")
            registry = get_chat_tasks(ctx)
            ids = coerce_list(task_id)
            if not ids:
                raise ValidationError("chat_status needs at least one task_id.")
            if len(ids) > _MAX_STATUS_BATCH:
                raise ValidationError(
                    f"chat_status accepts at most {_MAX_STATUS_BATCH} task_ids per call "
                    f"(got {len(ids)}); poll in smaller batches."
                )

            def _one(tid: str) -> dict[str, Any]:
                entry = registry.status(tid)
                if entry is None:
                    return {
                        "status": "unknown",
                        "task_id": tid,
                        "hint": (
                            "no such task (or its result expired) — call chat_start "
                            "again with the original question"
                        ),
                    }
                return _entry_status_payload(entry)

            if isinstance(task_id, str) and len(ids) == 1:
                # Tuple-unpack instead of ids[0]: the positional-indexing ratchet
                # (ADR-0011 / #1491) flags single-level subscripts wholesale.
                (only_id,) = ids
                return _one(only_id)
            return {"tasks": [_one(tid) for tid in ids]}

    @mcp.tool
    async def chat_cancel(
        ctx: Context,
        notebook: str,
        conversation_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Stop a notebook's active chat generation.

        ``conversation_id`` defaults to the notebook's most-recent session.
        Pass the ``task_id`` returned by ``chat_start`` when cancelling a
        detached ask: the MCP server abandons its local response stream first,
        then cancels the associated Google session when one exists.
        An unknown task ID or one belonging to another notebook is rejected.
        """
        with mcp_errors():
            client = await get_client(ctx)
            nb_id = await resolve_notebook(client, notebook)
            registry = get_chat_tasks(ctx)
            entry = registry.status(task_id) if task_id is not None else None
            if task_id is not None and entry is None:
                raise ValidationError(f"unknown or expired chat task_id: {task_id}")
            if entry is not None and entry.notebook_id not in (None, nb_id):
                raise ValidationError("task_id belongs to a different notebook.")
            if (
                entry is not None
                and entry.conversation_id is not None
                and conversation_id is not None
                and entry.conversation_id != conversation_id
            ):
                raise ValidationError("task_id belongs to a different conversation.")

            # A task-specific cancel owns the local task first. This makes the
            # terminal check race-safe: an old completed task can never cancel a
            # newer generation that happens to share its conversation.
            local_cancelled = False
            task_started = False
            if task_id is not None:
                local_cancelled = await registry.cancel(task_id)
                if not local_cancelled:
                    return {
                        "status": "already_finished",
                        "cancelled": False,
                        "local_task_cancelled": False,
                        "notebook_id": nb_id,
                        "conversation_id": entry.conversation_id if entry is not None else None,
                    }
                task_started = entry is not None and entry.started_at is not None

            resolved_id = conversation_id or (entry.conversation_id if entry is not None else None)
            if resolved_id is None and (task_id is None or task_started):
                resolved_id = await client.chat.get_conversation_id(nb_id)
            if resolved_id is None:
                return {
                    "status": "cancelled" if local_cancelled else "idle",
                    "cancelled": local_cancelled,
                    "local_task_cancelled": local_cancelled,
                    "notebook_id": nb_id,
                    "conversation_id": None,
                }

            await client.chat.cancel(nb_id, resolved_id)
            return {
                "status": "cancelled",
                "cancelled": True,
                "local_task_cancelled": local_cancelled,
                "notebook_id": nb_id,
                "conversation_id": resolved_id,
            }

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

        * ``chat_mode`` is a preset — one of ``default`` / ``learning-guide`` /
          ``concise`` / ``detailed``. It replaces the whole block, so it can't be
          combined with ``goal`` / ``response_length`` (that's rejected).
        * ``goal`` (custom persona; selects the CUSTOM goal) and
          ``response_length`` (``default`` / ``longer`` / ``shorter``) set a custom
          config. A partial call (just one) merges with the current settings — the
          omitted field is preserved. Only a bare call (no preset, neither field)
          is rejected, as it would reset every setting to its default.
        """
        with mcp_errors():
            # A partial custom config now merges (read-modify-write in
            # execute_configure), so goal/response_length are NO LONGER required
            # together — the omitted field is preserved. The one call still worth
            # rejecting is a fully bare one (no preset, no goal, no length): that
            # resets every setting to default, which is almost never what an agent
            # meant. "Supplied" matches the core: an empty goal ("") is a no-op
            # (core uses `if persona:`), and any explicit response_length —
            # incl. "default" — is a real setting.
            if chat_mode is None and not goal and response_length is None:
                raise ValidationError(
                    "chat_configure needs at least one setting: a chat_mode preset "
                    "(default / learning-guide / concise / detailed), a custom goal, "
                    "and/or a response_length. A bare call would reset every chat "
                    "setting to its default."
                )

            # ``chat_mode`` / ``response_length`` are Literals, so FastMCP/Pydantic
            # rejects out-of-enum values at the schema boundary. The preset-vs-custom
            # mutual-exclusion (chat_mode cannot be combined with goal/response_length)
            # is enforced transport-neutrally in ``execute_configure`` so the CLI and
            # this tool share one rule.
            client = await get_client(ctx)
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
        with mcp_errors():
            client = await get_client(ctx)
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
                mode=SUGGEST_SURFACE_MAP[surface],
                query=query,
            )
            payload: dict[str, Any] = {"notebook_id": nb_id}
            # Echo the resolved canonical source scope when one was passed (#1808).
            if resolved_source_ids is not None:
                payload["source_ids"] = resolved_source_ids
            payload["suggestions"] = [{"title": s.title, "prompt": s.prompt} for s in rows]
            return payload
