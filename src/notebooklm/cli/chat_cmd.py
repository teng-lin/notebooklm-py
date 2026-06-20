"""Chat and conversation CLI commands.

Commands:
    ask             Ask a notebook a question
    suggest-prompts Get AI-suggested prompts for a notebook
    configure       Configure chat persona and response settings
    history         Get conversation history or clear local cache
"""

import logging
from typing import Any

import click
from rich.table import Table

from .._app.chat import (
    ClearCacheResult,
    ConfigureResult,
    determine_conversation_id,
    execute_clear_cache,
    execute_configure,
    fetch_history,
    format_history,
    format_single_qa,
    get_latest_conversation_from_server,
    save_answer_as_note,
    validate_ask_flags,
)
from .._app.events import ProgressEvent
from ..exceptions import ValidationError
from .auth_runtime import resolve_client_factory, with_client
from .context import get_current_conversation, get_current_notebook, set_current_conversation
from .error_handler import _output_error, exit_with_code
from .input import resolve_prompt
from .options import _complete_sources, json_option, notebook_option, prompt_file_option
from .rendering import (
    cli_print,
    console,
    emit_status,
    json_output_response,
)
from .resolve import require_notebook, resolve_notebook_id, resolve_source_ids

logger = logging.getLogger(__name__)

# Inclusive ``--mode`` range for ``suggest-prompts``. Mirrors the canonical 1..9
# bound that ``NotebooksAPI.suggest_prompts`` enforces (the CLI boundary forbids
# importing the runtime layer's private constant, so this is a deliberate copy);
# the method re-validates server-side-bound, so a drift here only changes which
# layer reports the same out-of-range error, never correctness.
_SUGGEST_PROMPTS_MODE_MIN = 1
_SUGGEST_PROMPTS_MODE_MAX = 9


def _configure_json_payload(config: ConfigureResult) -> dict[str, Any]:
    """Build the ``configure --json`` envelope from the typed result.

    Discriminated by ``config.mode``: a predefined ``--mode`` emits the compact
    mode envelope; the persona / response-length branch emits the full settings
    envelope. ``_app`` returns typed results only, so this CLI adapter owns the
    envelope shape (byte-stable keys/order for scripted callers).
    """
    if config.mode is not None:
        return {
            "notebook_id": config.notebook_id,
            "mode": config.mode,
            "configured": True,
        }
    return {
        "notebook_id": config.notebook_id,
        "mode": None,
        # Lowercase enum name (e.g. "custom") for a stable, human-readable JSON
        # contract. The underlying RPC integer is an implementation detail.
        "goal": config.goal_name,
        "persona": config.persona,
        "response_length": config.response_length,
        "configured": True,
    }


def _clear_cache_json_payload(result: ClearCacheResult) -> dict[str, Any]:
    """Build the ``history --clear --json`` envelope from the typed result."""
    return {"cleared": result.cleared, "count": result.count}


def _history_json_payload(
    notebook_id: str,
    conversation_id: str | None,
    qa_pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    """Build the shared JSON envelope for ``history --json`` modes.

    Same shape whether or not ``--save`` is set; the save branch merges a
    ``note`` field on top of this base envelope. Owned by the CLI adapter
    (``_app`` returns typed results only).
    """
    return {
        "notebook_id": notebook_id,
        "conversation_id": conversation_id,
        "count": len(qa_pairs),
        "qa_pairs": [
            {"turn": i, "question": q, "answer": a} for i, (q, a) in enumerate(qa_pairs, 1)
        ],
    }


class _CliPrintStatusSink:
    """:class:`ProgressSink` routing neutral status events through ``cli_print``.

    Used for the conversation-selection prose, which the historical command only
    emitted under ``not json_output`` — so the sink is constructed only on that
    path and forwards every event to ``cli_print`` (honoring root ``--quiet``).
    Rich markup in the message is preserved.
    """

    def emit(self, event: ProgressEvent) -> None:
        cli_print(event.message)


class _EmitStatusSink:
    """:class:`ProgressSink` routing neutral status events through ``emit_status``.

    Used for the ``ask --save-as-note`` status lines: routes to stderr under
    ``--json`` (keeping stdout JSON-pure) and to stdout otherwise, honoring root
    ``--quiet``. Rich markup is preserved.
    """

    def __init__(self, *, json_output: bool) -> None:
        self._json_output = json_output

    def emit(self, event: ProgressEvent) -> None:
        # Status forwarder for a secondary --save-as-note action: the neutral
        # workflow folds its own failures into the returned outcome (never
        # raised), so this emit is never on an error path.
        emit_status(event.message, json_output=self._json_output)


# Re-export the neutral note-content formatters under their historical
# command-local names so ``from notebooklm.cli.chat_cmd import _format_single_qa``
# keeps resolving (the logic lives in ``_app.chat``).
_format_single_qa = format_single_qa
_format_history = format_history


def _determine_conversation_id(
    *,
    explicit_conversation_id: str | None,
    explicit_notebook_id: str | None,
    resolved_notebook_id: str,
    json_output: bool,
) -> str | None:
    """Determine which conversation ID to use for the ask command.

    Thin CLI adapter over :func:`notebooklm._app.chat.determine_conversation_id`:
    passes the CLI context helpers as **lazy callables** (read at call time, and
    only on the branches that need them — preserving the historical
    short-circuit) so the ``patch("...chat_cmd.get_current_*")`` seams keep
    landing, and routes the neutral "starting new conversation" status into
    ``cli_print`` only under ``not json_output``.
    """
    progress = None if json_output else _CliPrintStatusSink()
    return determine_conversation_id(
        explicit_conversation_id=explicit_conversation_id,
        explicit_notebook_id=explicit_notebook_id,
        resolved_notebook_id=resolved_notebook_id,
        cached_notebook_id=get_current_notebook,
        cached_conversation_id=get_current_conversation,
        progress=progress,
    )


async def _get_latest_conversation_from_server(
    client, notebook_id: str, json_output: bool
) -> str | None:
    """Fetch the most recent conversation ID from the server.

    Thin CLI adapter over
    :func:`notebooklm._app.chat.get_latest_conversation_from_server`: routes the
    neutral status events into ``cli_print`` only under ``not json_output`` (so
    status prose honors root ``--quiet`` and stays off JSON stdout).
    """
    progress = None if json_output else _CliPrintStatusSink()
    return await get_latest_conversation_from_server(client, notebook_id, progress=progress)


def register_chat_commands(cli):
    """Register chat commands on the main CLI group."""

    @cli.command("ask")
    @click.argument("question", default="", required=False)
    @prompt_file_option
    @notebook_option
    @click.option("--conversation-id", "-c", default=None, help="Continue a specific conversation")
    @click.option(
        "--new",
        "new_conversation",
        is_flag=True,
        help=(
            "Start a fresh conversation. DESTRUCTIVE: this deletes the "
            "notebook's current server-side conversation (turns are not "
            "recoverable) before asking. Prompts for confirmation unless "
            "``--yes`` is passed."
        ),
    )
    @click.option(
        "--yes",
        "-y",
        "assume_yes",
        is_flag=True,
        help=(
            "Skip the ``--new`` destructive-delete confirmation prompt. "
            "``--json`` implies ``--yes`` so scripted callers never hang."
        ),
    )
    @click.option(
        "--source",
        "-s",
        "source_ids",
        multiple=True,
        help="Limit to specific source IDs (can be repeated)",
        shell_complete=_complete_sources,
    )
    @click.option(
        "--json", "json_output", is_flag=True, help="Output as JSON (includes references)"
    )
    @click.option(
        "--save-as-note",
        is_flag=True,
        help=(
            "Save response as a note. When the answer has citations, the saved "
            "note preserves interactive [N] hover-anchor links (matching the "
            "NotebookLM web UI's 'Save to note' behavior); otherwise falls "
            "back to a plain-text note."
        ),
    )
    # ``-t`` consistently means "note title" across `note create`, `chat history`,
    # and here, so the short flag carries the same meaning everywhere it appears.
    @click.option(
        "-t",
        "--note-title",
        "note_title",
        default=None,
        help="Note title (use with --save-as-note)",
    )
    # ``--request-timeout`` is the self-documenting canonical name: this is the
    # per-request HTTP socket timeout, NOT the poll/wait budget that other
    # commands spell ``--timeout``. ``--timeout`` stays as a back-compat alias.
    @click.option(
        "--request-timeout",
        "--timeout",
        "timeout",
        default=None,
        type=click.IntRange(min=1),
        help=(
            "HTTP request/read timeout in seconds for this ask. Defaults to the "
            "library's chat timeout. (--timeout is a back-compat alias.)"
        ),
    )
    @with_client
    def ask_cmd(
        ctx,
        question,
        prompt_file,
        notebook_id,
        conversation_id,
        new_conversation,
        assume_yes,
        source_ids,
        json_output,
        save_as_note,
        note_title,
        timeout,
        client_auth,
    ):
        """Ask a notebook a question.

        By default, continues the last conversation. Use --new to start fresh.
        The answer includes inline citations like [1], [2] that reference sources.
        Use --json to get structured output with source IDs for each reference.

        \b
        Example:
          notebooklm ask "what are the main themes?"
          notebooklm ask -c <id> "continue this one"
          notebooklm ask --new "ignore last conversation, start fresh"
          notebooklm ask -s src_001 -s src_002 "question about specific sources"
          notebooklm ask "explain X" --json             # Get answer with source references
          notebooklm ask "explain X" --save-as-note     # Save response as a note
        """
        # Per ADR-0015 §2: under --json this mutual-exclusion conflict must emit
        # the typed JSON envelope and exit 1 (VALIDATION_ERROR), not ride
        # Click's parse-time UsageError path (exit 2, usage text on stderr, no
        # JSON on stdout). Under text mode we preserve the existing Click UX so
        # interactive users still get the ``Usage: ... / Error: ...`` formatting.
        # The neutral core raises the public ``ValidationError``; this adapter
        # maps it to the CLI's own ``VALIDATION_ERROR`` code / Click UsageError.
        try:
            validate_ask_flags(new_conversation=new_conversation, conversation_id=conversation_id)
        except ValidationError as exc:
            message = str(exc)
            if json_output:
                _output_error(message, "VALIDATION_ERROR", json_output, 1)
            raise click.UsageError(  # cli-input-validation: --new and --conversation-id are mutually exclusive
                message
            ) from exc
        question = resolve_prompt(question, prompt_file, "question", required=True)
        nb_id = require_notebook(notebook_id)

        client_kwargs: dict = {}
        if timeout is not None:
            timeout_value = float(timeout)
            client_kwargs["timeout"] = timeout_value
            client_kwargs["chat_timeout"] = timeout_value

        async def _run():
            async with resolve_client_factory(ctx)(client_auth, **client_kwargs) as client:
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                if new_conversation:
                    # Dropping ``conversation_id`` alone extends the most-recent
                    # conversation (see ChatAPI.ask Note). Deleting it first
                    # leaves the next ask nothing to attach to. No prior
                    # conversation is fine — skip both the prompt and the
                    # delete; ``ask`` then creates the notebook's first one.
                    last_conv_id = await client.chat.get_conversation_id(nb_id_resolved)
                    if last_conv_id:
                        # ``--json`` implies ``--yes`` so scripted callers don't
                        # hang on stdin (which would also clobber JSON stdout
                        # purity). See ``cli/artifact_cmd.py::artifact_delete``
                        # for the same pattern.
                        if (
                            not assume_yes
                            and not json_output
                            and not click.confirm(
                                f"This will permanently delete conversation "
                                f"{last_conv_id[:8]}... and all its turns. Continue?",
                                default=False,
                            )
                        ):
                            # Exit 1 (BaseException-bypassing ``SystemExit``)
                            # so scripts can distinguish "user said no" from
                            # "ask succeeded" — the intended ``ask`` did not
                            # run. ``click.exceptions.Exit`` and ``ctx.exit``
                            # both raise ``RuntimeError`` subclasses that the
                            # ``handle_errors`` catch-all (error_handler.py)
                            # would remap to exit 2.
                            console.print("[yellow]Aborted — no conversation deleted.[/yellow]")
                            exit_with_code(1)
                        await client.chat.delete_conversation(nb_id_resolved, last_conv_id)
                    effective_conv_id: str | None = None
                else:
                    effective_conv_id = _determine_conversation_id(
                        explicit_conversation_id=conversation_id,
                        explicit_notebook_id=notebook_id,
                        resolved_notebook_id=nb_id_resolved,
                        json_output=json_output,
                    )

                resumed_from_server = False
                if not new_conversation and not effective_conv_id:
                    # If no conversation ID yet, try to get the most recent one from server
                    effective_conv_id = await _get_latest_conversation_from_server(
                        client, nb_id_resolved, json_output
                    )
                    if effective_conv_id:
                        resumed_from_server = True

                sources = await resolve_source_ids(
                    client, nb_id_resolved, source_ids, json_output=json_output
                )
                result = await client.chat.ask(
                    nb_id_resolved,
                    question,
                    source_ids=sources,
                    conversation_id=effective_conv_id,
                )

                if result.conversation_id:
                    set_current_conversation(result.conversation_id)

                # Text-mode: original interactive layout (Answer first,
                # save-as-note status after). In JSON mode:
                # save-as-note runs first into a stderr-routed status path
                # and its outcome is merged into the JSON envelope, which
                # is emitted LAST as the terminal stdout output.
                if not json_output:
                    console.print("[bold cyan]Answer:[/bold cyan]")
                    console.print(result.answer)
                    if result.is_follow_up and resumed_from_server:
                        console.print(
                            f"\n[dim]Resumed conversation: {result.conversation_id}[/dim]"
                        )
                    elif result.is_follow_up:
                        console.print(
                            f"\n[dim]Conversation: {result.conversation_id} "
                            f"(turn {result.turn_number or '?'})[/dim]"
                        )
                    else:
                        console.print(f"\n[dim]New conversation: {result.conversation_id}[/dim]")

                note_save_result: dict[str, str] | None = None
                note_save_error: str | None = None

                if save_as_note:
                    # The save-as-note workflow (citation-rich vs plain-text
                    # fallback, the non-fatal error fold) lives in
                    # ``_app.chat.save_answer_as_note``. Its Rich-markup status
                    # lines route through ``_EmitStatusSink`` (stderr under
                    # ``--json``, honoring root ``--quiet``); the outcome's note
                    # / error are merged into the JSON envelope below.
                    outcome = await save_answer_as_note(
                        client,
                        nb_id_resolved,
                        result,
                        note_title=note_title,
                        question=question,
                        progress=_EmitStatusSink(json_output=json_output),
                    )
                    note_save_result = outcome.note
                    note_save_error = outcome.error

                if json_output:
                    from dataclasses import asdict

                    data = asdict(result)
                    # Exclude raw_response from CLI output for brevity.
                    del data["raw_response"]
                    if save_as_note:
                        # Merge note-save outcome into the envelope so the
                        # caller can observe success/failure from stdout
                        # alone without parsing stderr text.
                        if note_save_result is not None:
                            data["note"] = note_save_result
                        if note_save_error is not None:
                            data["note_save_error"] = note_save_error
                    json_output_response(data)

        return _run()

    @cli.command("suggest-prompts")
    @notebook_option
    @click.option(
        "--mode",
        default=4,
        type=int,
        help=(
            "Suggestion surface to target (1-9, default 4). 4=default chat "
            "questions, 5=critique, 6=audio/debate, 8=quiz, 9=flashcards. "
            "Out-of-range values exit 1 with a validation error."
        ),
    )
    @click.option(
        "--query",
        default=None,
        help="Free-text steer for the kind of prompts to suggest.",
    )
    @click.option(
        "--source",
        "-s",
        "source_ids",
        multiple=True,
        help="Limit to specific source IDs (can be repeated). Defaults to all sources.",
        shell_complete=_complete_sources,
    )
    @json_option
    @with_client
    def suggest_prompts_cmd(
        ctx,
        notebook_id,
        mode,
        query,
        source_ids,
        json_output,
        client_auth,
    ):
        """Get AI-suggested prompts for a notebook.

        Returns a short list of suggested prompts (each a title plus a
        ready-to-send instruction) that you can pass to ``notebooklm ask``.
        ``--mode`` selects the product surface the prompts are written for:
        4 (default) = chat questions, 5 = critique, 6 = audio/debate,
        8 = quiz, 9 = flashcards. A mode outside 1-9 exits 1.

        \b
        Example:
          notebooklm suggest-prompts
          notebooklm suggest-prompts --mode 8           # quiz prompts
          notebooklm suggest-prompts --query "key risks"
          notebooklm suggest-prompts -s src_001 -s src_002
          notebooklm suggest-prompts --json
        """
        nb_id = require_notebook(notebook_id)
        # Validate ``mode`` up front -- BEFORE any notebook/source resolution --
        # so an out-of-range value always surfaces the mode error (exit 1) and
        # never pays for a ``sources.list`` resolution RPC. ``suggest_prompts``
        # re-validates the same 1..9 range before its own RPC; this mirror keeps
        # the bad-mode failure deterministic regardless of how ``-s`` resolves.
        # Raises the public ``ValidationError`` so the shared error envelope
        # (``@with_client``) maps it to a VALIDATION_ERROR exit-1 / JSON envelope.
        if not _SUGGEST_PROMPTS_MODE_MIN <= mode <= _SUGGEST_PROMPTS_MODE_MAX:
            raise ValidationError(
                f"mode must be in the inclusive range "
                f"{_SUGGEST_PROMPTS_MODE_MIN}..{_SUGGEST_PROMPTS_MODE_MAX}, got {mode!r}"
            )

        async def _run():
            async with resolve_client_factory(ctx)(client_auth) as client:
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                sources = await resolve_source_ids(
                    client, nb_id_resolved, source_ids, json_output=json_output
                )
                suggestions = await client.notebooks.suggest_prompts(
                    nb_id_resolved,
                    source_ids=sources,
                    mode=mode,
                    query=query,
                )

                if json_output:
                    json_output_response(
                        {
                            "notebook_id": nb_id_resolved,
                            "suggestions": [
                                {"title": s.title, "prompt": s.prompt} for s in suggestions
                            ],
                            "count": len(suggestions),
                        }
                    )
                    return

                if not suggestions:
                    console.print("[yellow]No prompt suggestions returned[/yellow]")
                    return

                console.print("[bold cyan]Suggested prompts:[/bold cyan]")
                for i, suggestion in enumerate(suggestions, 1):
                    console.print(f"\n[bold]{i}. {suggestion.title}[/bold]")
                    console.print(suggestion.prompt)

        return _run()

    @cli.command("configure")
    @notebook_option
    @click.option(
        "--mode",
        "chat_mode",
        type=click.Choice(["default", "learning-guide", "concise", "detailed"]),
        default=None,
        help="Predefined chat mode",
    )
    @click.option("--persona", default=None, help="Custom persona prompt (up to 10,000 chars)")
    @click.option(
        "--response-length",
        type=click.Choice(["default", "longer", "shorter"]),
        default=None,
        help="Response verbosity",
    )
    @json_option
    @with_client
    def configure_cmd(
        ctx, notebook_id, chat_mode, persona, response_length, json_output, client_auth
    ):
        """Configure chat persona and response settings.

        \b
        Modes:
          default        General purpose (default behavior)
          learning-guide Educational focus with learning-oriented responses
          concise        Brief, to-the-point responses
          detailed       Verbose, comprehensive responses

        \b
        Examples:
          notebooklm configure --mode learning-guide
          notebooklm configure --persona "Act as a chemistry tutor"
          notebooklm configure --mode detailed --response-length longer
          notebooklm configure --mode concise --json   # Machine-readable output
        """
        nb_id = require_notebook(notebook_id)

        async def _run():
            async with resolve_client_factory(ctx)(client_auth) as client:
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                # The mode/goal/length mapping + RPC dispatch live in
                # ``_app.chat.execute_configure``; the adapter keeps the
                # ``--json`` envelope build + text prose.
                config = await execute_configure(
                    client,
                    nb_id_resolved,
                    chat_mode=chat_mode,
                    persona=persona,
                    response_length=response_length,
                )

                if chat_mode:
                    if json_output:
                        json_output_response(_configure_json_payload(config))
                        return
                    console.print(f"[green]Chat mode set to: {chat_mode}[/green]")
                    return

                if json_output:
                    json_output_response(_configure_json_payload(config))
                    return

                parts = []
                if persona:
                    parts.append(
                        f'persona: "{persona[:50]}..."'
                        if len(persona) > 50
                        else f'persona: "{persona}"'
                    )
                if response_length:
                    parts.append(f"response length: {response_length}")
                result = (
                    f"Chat configured: {', '.join(parts)}"
                    if parts
                    else "Chat configured (no changes)"
                )
                console.print(f"[green]{result}[/green]")

        return _run()

    @cli.command("history")
    @notebook_option
    @click.option("--limit", "-l", default=100, help="Maximum number of Q&A turns to show")
    @click.option("--clear", "clear_cache", is_flag=True, help="Clear local conversation cache")
    @click.option("--save", "save_as_note", is_flag=True, help="Save history as a note")
    @click.option("-t", "--note-title", "note_title", default=None, help="Note title (with --save)")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.option("--show-all", is_flag=True, help="Show full Q&A content instead of preview")
    @click.option(
        "--no-truncate",
        "no_truncate",
        is_flag=True,
        default=False,
        help="Disable the 50-char preview cap on Question/Answer columns in the table view.",
    )
    @with_client
    def history_cmd(
        ctx,
        notebook_id,
        limit,
        clear_cache,
        save_as_note,
        note_title,
        json_output,
        show_all,
        no_truncate,
        client_auth,
    ):
        """Get conversation history or save it as a note.

        Shows up to ``--limit`` Q&A turns from the most recent conversation.

        \b
        Example:
          notebooklm history                      # Show Q&A history
          notebooklm history -n nb123             # Show history for specific notebook
          notebooklm history --clear              # Clear local cache
          notebooklm history --save               # Save history as a note
          notebooklm history --save --note-title "Summary"  # Save with custom title
          notebooklm history --json               # Machine-readable JSON output
          notebooklm history --show-all           # Full Q&A content
          notebooklm history --no-truncate        # Full Q&A content in the table view
        """

        async def _run():
            async with resolve_client_factory(ctx)(client_auth) as client:
                if clear_cache:
                    # The pre-clear count capture + clear lives in
                    # ``_app.chat.execute_clear_cache``; the adapter keeps the
                    # ``--json`` envelope + text prose.
                    cache_result = execute_clear_cache(client)
                    if json_output:
                        # In JSON mode, stdout must be a single JSON
                        # document; no Rich/text output.
                        json_output_response(_clear_cache_json_payload(cache_result))
                        return
                    if cache_result.cleared:
                        console.print("[green]Local conversation cache cleared[/green]")
                    else:
                        console.print("[yellow]No cache to clear[/yellow]")
                    return

                nb_id = require_notebook(notebook_id)
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                fetched = await fetch_history(client, nb_id_resolved, limit=limit)
                conv_id = fetched.conversation_id
                qa_pairs = fetched.qa_pairs

                if save_as_note:
                    if not qa_pairs:
                        _output_error(
                            "Error: No conversation history found for this notebook.",
                            "NOT_FOUND",
                            json_output,
                            1,
                        )
                    content = _format_history(qa_pairs)
                    title = note_title or "Chat History"
                    note = await client.notes.create(nb_id_resolved, title, content)
                    if json_output:
                        # In JSON mode, emit a single JSON envelope that
                        # carries both the history payload and the
                        # note-save outcome. Status text routes to stderr.
                        emit_status(
                            f"[green]Saved as note: {note.title} ({note.id[:8]}...)[/green]",
                            json_output=json_output,
                        )
                        json_output_response(
                            {
                                **_history_json_payload(nb_id_resolved, conv_id, qa_pairs),
                                "note": {"id": note.id, "title": note.title},
                            }
                        )
                        return
                    console.print(f"[green]Saved as note: {note.title} ({note.id[:8]}...)[/green]")
                    return

                if json_output:
                    json_output_response(_history_json_payload(nb_id_resolved, conv_id, qa_pairs))
                    return

                if not qa_pairs:
                    console.print("[yellow]No conversation history[/yellow]")
                    return

                console.print("[bold cyan]Conversation History:[/bold cyan]")

                if show_all:
                    if conv_id:
                        console.print(f"\n[bold]── {conv_id} ──[/bold]")
                    for i, (question, answer) in enumerate(qa_pairs, 1):
                        console.print(f"[bold]#{i} Q:[/bold] {question}")
                        console.print(f"   A: {answer}\n")
                    return

                if conv_id:
                    console.print(f"\n[dim]── {conv_id} ──[/dim]")
                table = Table()
                table.add_column("#", style="dim", width=4)
                # ``--no-truncate`` lifts both the column-level
                # ``max_width=50`` constraint and the ``[:50]`` cell slice so
                # the table view can render long Q/A turns in full. Default
                # behavior is unchanged — the 50-char preview is preserved
                # to match the existing UX when the flag is not passed.
                if no_truncate:
                    table.add_column("Question", style="white", overflow="fold")
                    table.add_column("Answer", style="dim", overflow="fold")
                    for i, (question, answer) in enumerate(qa_pairs, 1):
                        table.add_row(str(i), question, answer)
                else:
                    table.add_column("Question", style="white", max_width=50)
                    table.add_column("Answer preview", style="dim", max_width=50)
                    for i, (question, answer) in enumerate(qa_pairs, 1):
                        table.add_row(str(i), question[:50], answer[:50])
                console.print(table)
                console.print("\n[dim]Use 'notebooklm history --save' to save as a note.[/dim]")

        return _run()
