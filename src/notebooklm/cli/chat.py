"""Chat and conversation CLI commands.

Commands:
    ask        Ask a notebook a question
    configure  Configure chat persona and response settings
    history    Get conversation history or clear local cache
"""

import logging

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import ChatMode
from .helpers import (
    console,
    get_current_conversation,
    get_current_notebook,
    json_output_response,
    require_notebook,
    resolve_notebook_id,
    resolve_prompt,
    resolve_source_ids,
    set_current_conversation,
    with_client,
)
from .options import _complete_sources, json_option, notebook_option, prompt_file_option

logger = logging.getLogger(__name__)


def _determine_conversation_id(
    *,
    explicit_conversation_id: str | None,
    explicit_notebook_id: str | None,
    resolved_notebook_id: str,
    json_output: bool,
) -> str | None:
    """Determine which conversation ID to use for the ask command.

    Returns None if no cached conversation exists, otherwise returns
    the conversation ID to continue.
    """
    if explicit_conversation_id:
        return explicit_conversation_id

    # Check if user switched notebooks via --notebook flag
    cached_notebook = get_current_notebook()
    if explicit_notebook_id and cached_notebook and resolved_notebook_id != cached_notebook:
        if not json_output:
            console.print("[dim]Different notebook specified, starting new conversation...[/dim]")
        return None

    return get_current_conversation()


async def _get_latest_conversation_from_server(
    client, notebook_id: str, json_output: bool
) -> str | None:
    """Fetch the most recent conversation ID from the server.

    Returns None if unavailable or empty.
    """
    try:
        conv_id = await client.chat.get_conversation_id(notebook_id)
        if conv_id:
            if not json_output:
                console.print(f"[dim]Continuing conversation {conv_id[:8]}...[/dim]")
            return conv_id
    except Exception as e:
        logger.debug(
            "Failed to fetch last conversation (%s): %s",
            type(e).__name__,
            e,
        )
        if not json_output:
            console.print("[dim]Starting new conversation (history unavailable)[/dim]")
    return None


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
        help="Start a fresh conversation, skipping the auto-resume of the last one.",
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
    @click.option("--note-title", default=None, help="Note title (use with --save-as-note)")
    @click.option(
        "--timeout",
        default=None,
        type=click.IntRange(min=1),
        help=(
            "HTTP request timeout in seconds (default: 30, from the library). "
            "Increase for long or complex prompts."
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
        if new_conversation and conversation_id:
            raise click.UsageError(
                "--new and --conversation-id are mutually exclusive: "
                "--new starts a fresh conversation while --conversation-id resumes a specific one."
            )
        question = resolve_prompt(question, prompt_file, "question", required=True)
        nb_id = require_notebook(notebook_id)

        client_kwargs: dict = {}
        if timeout is not None:
            client_kwargs["timeout"] = float(timeout)

        async def _run():
            async with NotebookLMClient(client_auth, **client_kwargs) as client:
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                if new_conversation:
                    # --new: skip both the local-cache and server-side resume so the
                    # server treats this turn as the start of a new conversation.
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

                if json_output:
                    from dataclasses import asdict

                    data = asdict(result)
                    # Exclude raw_response from CLI output for brevity
                    del data["raw_response"]
                    json_output_response(data)
                    if not save_as_note:
                        return
                else:
                    console.print("[bold cyan]Answer:[/bold cyan]")
                    console.print(result.answer)
                    if result.is_follow_up and resumed_from_server:
                        console.print(
                            f"\n[dim]Resumed conversation: {result.conversation_id}[/dim]"
                        )
                    elif result.is_follow_up:
                        console.print(
                            f"\n[dim]Conversation: {result.conversation_id} (turn {result.turn_number or '?'})[/dim]"
                        )
                    else:
                        console.print(f"\n[dim]New conversation: {result.conversation_id}[/dim]")

                if save_as_note:
                    if not result.answer:
                        console.print("[yellow]Warning: No answer to save as note[/yellow]")
                        return
                    try:
                        title = note_title or f"Chat: {question[:50].strip().replace(chr(10), ' ')}"
                        if result.references:
                            # Citation-rich path: server stores [N] markers as
                            # hover-anchored references (issue #660).
                            note = await client.notes.create_from_chat(
                                nb_id_resolved, result, title=title
                            )
                        else:
                            # No citations to preserve — fall back to the
                            # plain-text path so the save still succeeds.
                            console.print(
                                "[dim]No citations in answer; saving as plain-text note.[/dim]"
                            )
                            note = await client.notes.create(nb_id_resolved, title, result.answer)
                        console.print(
                            f"\n[dim]Saved as note: {note.title} ({note.id[:8]}...)[/dim]"
                        )
                    except Exception as e:
                        console.print(f"[yellow]Warning: Failed to save note: {e}[/yellow]")

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
            from ..types import ChatGoal, ChatResponseLength

            async with NotebookLMClient(client_auth) as client:
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                if chat_mode:
                    mode_map = {
                        "default": ChatMode.DEFAULT,
                        "learning-guide": ChatMode.LEARNING_GUIDE,
                        "concise": ChatMode.CONCISE,
                        "detailed": ChatMode.DETAILED,
                    }
                    await client.chat.set_mode(nb_id_resolved, mode_map[chat_mode])
                    if json_output:
                        json_output_response(
                            {
                                "notebook_id": nb_id_resolved,
                                "mode": chat_mode,
                                "configured": True,
                            }
                        )
                        return
                    console.print(f"[green]Chat mode set to: {chat_mode}[/green]")
                    return

                goal = ChatGoal.CUSTOM if persona else None
                length = None
                if response_length:
                    length_map = {
                        "default": ChatResponseLength.DEFAULT,
                        "longer": ChatResponseLength.LONGER,
                        "shorter": ChatResponseLength.SHORTER,
                    }
                    length = length_map[response_length]

                await client.chat.configure(
                    nb_id_resolved, goal=goal, response_length=length, custom_prompt=persona
                )

                if json_output:
                    json_output_response(
                        {
                            "notebook_id": nb_id_resolved,
                            "mode": None,
                            # Lowercase enum name (e.g. "custom") for a stable,
                            # human-readable JSON contract. The underlying RPC
                            # integer is an implementation detail.
                            "goal": goal.name.lower() if goal else None,
                            "persona": persona,
                            "response_length": response_length,
                            "configured": True,
                        }
                    )
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

        Shows all Q&A turns from the most recent conversation.

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
            async with NotebookLMClient(client_auth) as client:
                if clear_cache:
                    result = client.chat.clear_cache()
                    if result:
                        console.print("[green]Local conversation cache cleared[/green]")
                    else:
                        console.print("[yellow]No cache to clear[/yellow]")
                    return

                nb_id = require_notebook(notebook_id)
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                conv_id = await client.chat.get_conversation_id(nb_id_resolved)
                qa_pairs = await client.chat.get_history(
                    nb_id_resolved, limit=limit, conversation_id=conv_id
                )

                if save_as_note:
                    if not qa_pairs:
                        raise click.ClickException(
                            "No conversation history found for this notebook."
                        )
                    content = _format_history(qa_pairs)
                    title = note_title or "Chat History"
                    note = await client.notes.create(nb_id_resolved, title, content)
                    console.print(f"[green]Saved as note: {note.title} ({note.id[:8]}...)[/green]")
                    return

                if json_output:
                    data = {
                        "notebook_id": nb_id_resolved,
                        "conversation_id": conv_id,
                        "count": len(qa_pairs),
                        "qa_pairs": [
                            {"turn": i, "question": q, "answer": a}
                            for i, (q, a) in enumerate(qa_pairs, 1)
                        ],
                    }
                    json_output_response(data)
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


def _format_single_qa(question: str, answer: str) -> str:
    """Format one Q&A pair as note content."""
    parts = []
    if question:
        parts.append(f"**Q:** {question}")
    if answer:
        parts.append(f"**A:** {answer}")
    return "\n\n".join(parts)


def _format_history(qa_pairs: list[tuple[str, str]]) -> str:
    """Format Q&A history as note content."""
    turns = []
    for i, (question, answer) in enumerate(qa_pairs, 1):
        turns.append(f"### Turn {i}\n\n{_format_single_qa(question, answer)}")
    return "\n\n---\n\n".join(turns)
