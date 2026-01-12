"""Chat and conversation CLI commands.

Commands:
    ask        Ask a notebook a question
    configure  Configure chat persona and response settings
    history    Get conversation history or clear local cache
"""

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import ChatMode
from .helpers import (
    console,
    get_current_conversation,
    json_output_response,
    require_notebook,
    set_current_conversation,
    with_client,
)


def register_chat_commands(cli):
    """Register chat commands on the main CLI group."""

    @cli.command("ask")
    @click.argument("question")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set)",
    )
    @click.option("--conversation-id", "-c", default=None, help="Continue a specific conversation")
    @click.option("--new", "new_conversation", is_flag=True, help="Start a new conversation")
    @click.option(
        "--source",
        "-s",
        "source_ids",
        multiple=True,
        help="Limit to specific source IDs (can be repeated)",
    )
    @click.option(
        "--json", "json_output", is_flag=True, help="Output as JSON (includes references)"
    )
    @with_client
    def ask_cmd(
        ctx,
        question,
        notebook_id,
        conversation_id,
        new_conversation,
        source_ids,
        json_output,
        client_auth,
    ):
        """Ask a notebook a question.

        By default, continues the last conversation. Use --new to start fresh.
        The answer includes inline citations like [1], [2] that reference sources.
        Use --json to get structured output with source IDs for each reference.

        \b
        Example:
          notebooklm ask "what are the main themes?"
          notebooklm ask --new "start fresh question"
          notebooklm ask -c <id> "continue this one"
          notebooklm ask -s src_001 -s src_002 "question about specific sources"
          notebooklm ask "explain X" --json     # Get answer with source references
        """
        nb_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                effective_conv_id = None
                if new_conversation:
                    if not json_output:
                        console.print("[dim]Starting new conversation...[/dim]")
                elif conversation_id:
                    effective_conv_id = conversation_id
                else:
                    effective_conv_id = get_current_conversation()
                    if not effective_conv_id:
                        try:
                            history = await client.chat.get_history(nb_id, limit=1)
                            if history and history[0]:
                                last_conv = history[0][-1]
                                effective_conv_id = (
                                    last_conv[0] if isinstance(last_conv, list) else str(last_conv)
                                )
                                if not json_output:
                                    console.print(
                                        f"[dim]Continuing conversation {effective_conv_id[:8]}...[/dim]"
                                    )
                        except Exception:
                            pass

                # Convert source_ids tuple to list, or None if empty
                sources = list(source_ids) if source_ids else None
                result = await client.chat.ask(
                    nb_id, question, source_ids=sources, conversation_id=effective_conv_id
                )

                if result.conversation_id:
                    set_current_conversation(result.conversation_id)

                if json_output:
                    from dataclasses import asdict

                    data = asdict(result)
                    # Exclude raw_response from CLI output for brevity
                    del data["raw_response"]
                    json_output_response(data)
                    return

                console.print("[bold cyan]Answer:[/bold cyan]")
                console.print(result.answer)
                if result.is_follow_up:
                    console.print(
                        f"\n[dim]Conversation: {result.conversation_id} (turn {result.turn_number or '?'})[/dim]"
                    )
                else:
                    console.print(f"\n[dim]New conversation: {result.conversation_id}[/dim]")

        return _run()

    @cli.command("configure")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set)",
    )
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
    @with_client
    def configure_cmd(ctx, notebook_id, chat_mode, persona, response_length, client_auth):
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
        """
        nb_id = require_notebook(notebook_id)

        async def _run():
            from ..rpc import ChatGoal, ChatResponseLength

            async with NotebookLMClient(client_auth) as client:
                if chat_mode:
                    mode_map = {
                        "default": ChatMode.DEFAULT,
                        "learning-guide": ChatMode.LEARNING_GUIDE,
                        "concise": ChatMode.CONCISE,
                        "detailed": ChatMode.DETAILED,
                    }
                    await client.chat.set_mode(nb_id, mode_map[chat_mode])
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
                    nb_id, goal=goal, response_length=length, custom_prompt=persona
                )

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
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set)",
    )
    @click.option("--limit", "-l", default=20, help="Number of messages")
    @click.option("--clear", "clear_cache", is_flag=True, help="Clear local conversation cache")
    @with_client
    def history_cmd(ctx, notebook_id, limit, clear_cache, client_auth):
        """Get conversation history or clear local cache.

        \b
        Example:
          notebooklm history              # Show history for current notebook
          notebooklm history -n nb123     # Show history for specific notebook
          notebooklm history --clear      # Clear local cache
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
                history = await client.chat.get_history(nb_id, limit=limit)

                if history:
                    console.print("[bold cyan]Conversation History:[/bold cyan]")
                    try:
                        conversations = history[0] if history else []
                        if conversations:
                            table = Table()
                            table.add_column("#", style="dim")
                            table.add_column("Conversation ID", style="cyan")
                            for i, conv in enumerate(conversations, 1):
                                conv_id = conv[0] if isinstance(conv, list) and conv else str(conv)
                                table.add_row(str(i), conv_id)
                            console.print(table)
                            console.print(
                                "\n[dim]Note: Only conversation IDs available. Use 'notebooklm ask -c <id>' to continue.[/dim]"
                            )
                        else:
                            console.print("[yellow]No conversations found[/yellow]")
                    except (IndexError, TypeError):
                        console.print(history)
                else:
                    console.print("[yellow]No conversation history[/yellow]")

        return _run()
