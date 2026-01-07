"""Session and context management CLI commands.

Commands:
    login   Log in to NotebookLM via browser
    use     Set the current notebook context
    status  Show current context
    clear   Clear current notebook context
"""

import json
from pathlib import Path

import click
from rich.table import Table

from ..auth import AuthTokens, DEFAULT_STORAGE_PATH
from ..client import NotebookLMClient
from .helpers import (
    console,
    run_async,
    get_client,
    CONTEXT_FILE,
    BROWSER_PROFILE_DIR,
    get_current_notebook,
    set_current_notebook,
    clear_context,
    get_current_conversation,
    json_output_response,
    resolve_notebook_id,
)


def register_session_commands(cli):
    """Register session commands on the main CLI group."""

    @cli.command("login")
    @click.option(
        "--storage",
        type=click.Path(),
        default=None,
        help=f"Where to save storage_state.json (default: {DEFAULT_STORAGE_PATH})",
    )
    def login(storage):
        """Log in to NotebookLM via browser.

        Opens a browser window for Google login. After logging in,
        press ENTER in the terminal to save authentication.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            console.print(
                "[red]Playwright not installed. Run:[/red]\n"
                "  pip install notebooklm[browser]\n"
                "  playwright install chromium"
            )
            raise SystemExit(1)

        storage_path = Path(storage) if storage else DEFAULT_STORAGE_PATH
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        console.print("[yellow]Opening browser for Google login...[/yellow]")
        console.print(f"[dim]Using persistent profile: {BROWSER_PROFILE_DIR}[/dim]")

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )

            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://notebooklm.google.com/")

            console.print("\n[bold green]Instructions:[/bold green]")
            console.print("1. Complete the Google login in the browser window")
            console.print("2. Wait until you see the NotebookLM homepage")
            console.print("3. Press [bold]ENTER[/bold] here to save and close\n")

            input("[Press ENTER when logged in] ")

            current_url = page.url
            if "notebooklm.google.com" not in current_url:
                console.print(f"[yellow]Warning: Current URL is {current_url}[/yellow]")
                if not click.confirm("Save authentication anyway?"):
                    context.close()
                    raise SystemExit(1)

            context.storage_state(path=str(storage_path))
            context.close()

        console.print(f"\n[green]Authentication saved to:[/green] {storage_path}")

    @cli.command("use")
    @click.argument("notebook_id")
    @click.pass_context
    def use_notebook(ctx, notebook_id):
        """Set the current notebook context.

        Once set, all commands will use this notebook by default.
        You can still override by passing --notebook explicitly.

        Supports partial IDs - 'notebooklm use abc' matches 'abc123...'

        \b
        Example:
          notebooklm use nb123
          notebooklm ask "what is this about?"   # Uses nb123
          notebooklm generate video "a fun explainer"  # Uses nb123
        """
        try:
            cookies, csrf, session_id = get_client(ctx)
            auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

            async def _get():
                async with NotebookLMClient(auth) as client:
                    # Resolve partial ID to full ID
                    resolved_id = await resolve_notebook_id(client, notebook_id)
                    nb = await client.notebooks.get(resolved_id)
                    return nb, resolved_id

            nb, resolved_id = run_async(_get())

            created_str = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else None
            set_current_notebook(resolved_id, nb.title, nb.is_owner, created_str)

            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")

            created = created_str or "-"
            owner_status = "Owner" if nb.is_owner else "Shared"
            table.add_row(nb.id, nb.title, owner_status, created)

            console.print(table)

        except FileNotFoundError:
            set_current_notebook(notebook_id)
            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")
            table.add_row(notebook_id, "-", "-", "-")
            console.print(table)
        except click.ClickException:
            # Re-raise click exceptions (from resolve_notebook_id)
            raise
        except Exception as e:
            set_current_notebook(notebook_id)
            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")
            table.add_row(notebook_id, f"Warning: {str(e)}", "-", "-")
            console.print(table)

    @cli.command("status")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    def status(json_output):
        """Show current context (active notebook and conversation)."""
        notebook_id = get_current_notebook()
        if notebook_id:
            try:
                data = json.loads(CONTEXT_FILE.read_text())
                title = data.get("title", "-")
                is_owner = data.get("is_owner", True)
                created_at = data.get("created_at", "-")
                conversation_id = data.get("conversation_id")

                if json_output:
                    json_data = {
                        "has_context": True,
                        "notebook": {
                            "id": notebook_id,
                            "title": title if title != "-" else None,
                            "is_owner": is_owner,
                        },
                        "conversation_id": conversation_id,
                    }
                    json_output_response(json_data)
                    return

                table = Table(title="Current Context")
                table.add_column("Property", style="dim")
                table.add_column("Value", style="cyan")

                table.add_row("Notebook ID", notebook_id)
                table.add_row("Title", str(title))
                owner_status = "Owner" if is_owner else "Shared"
                table.add_row("Ownership", owner_status)
                table.add_row("Created", created_at)
                if conversation_id:
                    table.add_row("Conversation", conversation_id)
                else:
                    table.add_row(
                        "Conversation", "[dim]None (will auto-select on next ask)[/dim]"
                    )
                console.print(table)
            except (json.JSONDecodeError, IOError):
                if json_output:
                    json_data = {
                        "has_context": True,
                        "notebook": {
                            "id": notebook_id,
                            "title": None,
                            "is_owner": None,
                        },
                        "conversation_id": None,
                    }
                    json_output_response(json_data)
                    return

                table = Table(title="Current Context")
                table.add_column("Property", style="dim")
                table.add_column("Value", style="cyan")
                table.add_row("Notebook ID", notebook_id)
                table.add_row("Title", "-")
                table.add_row("Ownership", "-")
                table.add_row("Created", "-")
                table.add_row("Conversation", "[dim]None[/dim]")
                console.print(table)
        else:
            if json_output:
                json_data = {
                    "has_context": False,
                    "notebook": None,
                    "conversation_id": None,
                }
                json_output_response(json_data)
                return

            console.print(
                "[yellow]No notebook selected. Use 'notebooklm use <id>' to set one.[/yellow]"
            )

    @cli.command("clear")
    def clear_cmd():
        """Clear current notebook context."""
        clear_context()
        console.print("[green]Context cleared[/green]")
