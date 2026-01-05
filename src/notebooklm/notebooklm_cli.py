"""CLI interface for NotebookLM automation.

Command structure:
  notebooklm login                    # Authenticate
  notebooklm use <notebook_id>        # Set current notebook context
  notebooklm status                   # Show current context
  notebooklm list                     # List notebooks (shortcut)
  notebooklm create <title>           # Create notebook (shortcut)
  notebooklm query <text>             # Chat with current notebook

  notebooklm notebook <command>       # Notebook operations
  notebooklm source <command>         # Source operations
  notebooklm artifact <command>       # Artifact management
  notebooklm generate <type>          # Generate content
  notebooklm download <type>          # Download content
  notebooklm note <command>           # Note operations

LLM-friendly design:
  # Set context once, then use simple commands
  notebooklm use nb123
  notebooklm generate video "a funny explainer for kids"
  notebooklm generate audio "deep dive focusing on chapter 3"
  notebooklm query "what are the key themes?"
"""

import asyncio
import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .auth import (
    AuthTokens,
    load_auth_from_storage,
    fetch_tokens,
    DEFAULT_STORAGE_PATH,
)
from .api_client import NotebookLMClient
from .services import NotebookService, SourceService, ArtifactService
from .rpc import (
    AudioFormat,
    AudioLength,
    VideoFormat,
    VideoStyle,
    QuizQuantity,
    QuizDifficulty,
    InfographicOrientation,
    InfographicDetail,
    SlideDeckFormat,
    SlideDeckLength,
)

console = Console()

# Persistent browser profile directory
BROWSER_PROFILE_DIR = Path.home() / ".notebooklm" / "browser_profile"
# Context file for storing current notebook
CONTEXT_FILE = Path.home() / ".notebooklm" / "context.json"


def get_current_notebook() -> str | None:
    """Get the current notebook ID from context."""
    if not CONTEXT_FILE.exists():
        return None
    try:
        data = json.loads(CONTEXT_FILE.read_text())
        return data.get("notebook_id")
    except (json.JSONDecodeError, IOError):
        return None


def set_current_notebook(notebook_id: str, title: str | None = None):
    """Set the current notebook context."""
    CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"notebook_id": notebook_id}
    if title:
        data["title"] = title
    CONTEXT_FILE.write_text(json.dumps(data, indent=2))


def clear_context():
    """Clear the current context."""
    if CONTEXT_FILE.exists():
        CONTEXT_FILE.unlink()


def require_notebook(notebook_id: str | None) -> str:
    """Get notebook ID from argument or context, raise if neither."""
    if notebook_id:
        return notebook_id
    current = get_current_notebook()
    if current:
        return current
    console.print("[red]No notebook specified. Use 'notebooklm use <id>' to set context or provide notebook_id.[/red]")
    raise SystemExit(1)


def run_async(coro):
    """Run async coroutine in sync context."""
    return asyncio.run(coro)


def get_client(ctx) -> tuple[dict, str, str]:
    """Get auth components from context."""
    storage_path = ctx.obj.get("storage_path") if ctx.obj else None
    cookies = load_auth_from_storage(storage_path)
    csrf, session_id = run_async(fetch_tokens(cookies))
    return cookies, csrf, session_id


def handle_error(e: Exception):
    """Handle and display errors consistently."""
    console.print(f"[red]Error: {e}[/red]")
    raise SystemExit(1)


# =============================================================================
# MAIN CLI GROUP
# =============================================================================


@click.group()
@click.version_option(version=__version__, prog_name="NotebookLM CLI")
@click.option(
    "--storage",
    type=click.Path(exists=False),
    default=None,
    help=f"Path to storage_state.json (default: {DEFAULT_STORAGE_PATH})",
)
@click.pass_context
def cli(ctx, storage):
    """NotebookLM automation CLI.

    \b
    Quick start:
      notebooklm login              # Authenticate first
      notebooklm list               # List your notebooks
      notebooklm create "My Notes"  # Create a notebook
      notebooklm query <id> "Hi"    # Chat with a notebook

    \b
    Command groups:
      notebook   Notebook management (list, create, delete, rename, share)
      source     Source management (add, list, delete, refresh)
      artifact   Artifact management (list, get, delete, export)
      generate   Generate content (audio, video, quiz, slides, etc.)
      download   Download generated content
      note       Note management (create, list, edit, delete)
    """
    ctx.ensure_object(dict)
    ctx.obj["storage_path"] = Path(storage) if storage else None


# =============================================================================
# TOP-LEVEL CONVENIENCE COMMANDS
# =============================================================================


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

    \b
    Example:
      notebooklm use nb123
      notebooklm query "what is this about?"   # Uses nb123
      notebooklm generate video "a fun explainer"  # Uses nb123
    """
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _get():
            async with NotebookLMClient(auth) as client:
                return await client.get_notebook(notebook_id)

        notebook = run_async(_get())
        title = None
        if notebook and isinstance(notebook, list) and len(notebook) > 0:
            nb_info = notebook[0]
            if isinstance(nb_info, list) and len(nb_info) > 1:
                title = nb_info[1]

        set_current_notebook(notebook_id, title)
        console.print(f"[green]Now using notebook:[/green] {notebook_id}")
        if title:
            console.print(f"[dim]Title: {title}[/dim]")

    except FileNotFoundError:
        # Allow setting context even without auth (might be used later)
        set_current_notebook(notebook_id)
        console.print(f"[green]Now using notebook:[/green] {notebook_id}")
    except Exception as e:
        # Still set context even if we can't verify the notebook
        set_current_notebook(notebook_id)
        console.print(f"[green]Now using notebook:[/green] {notebook_id}")
        console.print(f"[dim]Warning: {e}[/dim]")


@cli.command("status")
def status():
    """Show current context (active notebook)."""
    notebook_id = get_current_notebook()
    if notebook_id:
        try:
            data = json.loads(CONTEXT_FILE.read_text())
            title = data.get("title", "")
            console.print(f"[bold cyan]Current notebook:[/bold cyan] {notebook_id}")
            if title:
                console.print(f"[dim]Title: {title}[/dim]")
        except (json.JSONDecodeError, IOError):
            console.print(f"[bold cyan]Current notebook:[/bold cyan] {notebook_id}")
    else:
        console.print("[yellow]No notebook selected. Use 'notebooklm use <id>' to set one.[/yellow]")


@cli.command("clear")
def clear_cmd():
    """Clear current notebook context."""
    clear_context()
    console.print("[green]Context cleared[/green]")


@cli.command("list")
@click.pass_context
def list_notebooks_shortcut(ctx):
    """List all notebooks (shortcut for 'notebook list')."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _list():
            async with NotebookLMClient(auth) as client:
                service = NotebookService(client)
                return await service.list()

        notebooks = run_async(_list())

        table = Table(title="Notebooks")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="green")
        table.add_column("Created", style="dim")

        for nb in notebooks:
            created = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else "-"
            table.add_row(nb.id, nb.title, created)

        console.print(table)

    except FileNotFoundError:
        console.print("[red]Auth not found. Run 'notebooklm login' first.[/red]")
        raise SystemExit(1)
    except Exception as e:
        handle_error(e)


@cli.command("create")
@click.argument("title")
@click.pass_context
def create_notebook_shortcut(ctx, title):
    """Create a new notebook (shortcut for 'notebook create')."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _create():
            async with NotebookLMClient(auth) as client:
                service = NotebookService(client)
                return await service.create(title)

        notebook = run_async(_create())
        console.print(f"[green]Created notebook:[/green] {notebook.id} - {notebook.title}")

    except Exception as e:
        handle_error(e)


@cli.command("query")
@click.argument("query_text")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--conversation-id", "-c", default=None, help="Continue a conversation")
@click.pass_context
def query_shortcut(ctx, query_text, notebook_id, conversation_id):
    """Chat with a notebook (shortcut for 'notebook query').

    \b
    Example:
      notebooklm use nb123
      notebooklm query "what are the main themes?"
      notebooklm query "tell me more" -c <conversation_id>
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _query():
            async with NotebookLMClient(auth) as client:
                return await client.query(
                    nb_id, query_text, conversation_id=conversation_id
                )

        result = run_async(_query())

        console.print(f"[bold cyan]Answer:[/bold cyan]")
        console.print(result["answer"])
        console.print(f"\n[dim]Conversation ID: {result['conversation_id']}[/dim]")

    except Exception as e:
        handle_error(e)


# =============================================================================
# NOTEBOOK GROUP
# =============================================================================


@cli.group()
def notebook():
    """Notebook management commands.

    \b
    Commands:
      list       List all notebooks
      create     Create a new notebook
      delete     Delete a notebook
      rename     Rename a notebook
      share      Share a notebook
      summary    Get notebook summary
      analytics  Get notebook analytics
      history    Get conversation history
    """
    pass


@notebook.command("list")
@click.pass_context
def notebook_list(ctx):
    """List all notebooks."""
    ctx.invoke(list_notebooks_shortcut)


@notebook.command("create")
@click.argument("title")
@click.pass_context
def notebook_create(ctx, title):
    """Create a new notebook."""
    ctx.invoke(create_notebook_shortcut, title=title)


@notebook.command("delete")
@click.argument("notebook_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def notebook_delete(ctx, notebook_id, yes):
    """Delete a notebook."""
    if not yes and not click.confirm(f"Delete notebook {notebook_id}?"):
        return

    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _delete():
            async with NotebookLMClient(auth) as client:
                service = NotebookService(client)
                return await service.delete(notebook_id)

        success = run_async(_delete())
        if success:
            console.print(f"[green]Deleted notebook:[/green] {notebook_id}")
        else:
            console.print("[yellow]Delete may have failed[/yellow]")

    except Exception as e:
        handle_error(e)


@notebook.command("rename")
@click.argument("notebook_id")
@click.argument("new_title")
@click.pass_context
def notebook_rename(ctx, notebook_id, new_title):
    """Rename a notebook."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _rename():
            async with NotebookLMClient(auth) as client:
                return await client.rename_notebook(notebook_id, new_title)

        run_async(_rename())
        console.print(f"[green]Renamed:[/green] {notebook_id} -> {new_title}")

    except Exception as e:
        handle_error(e)


@notebook.command("share")
@click.argument("notebook_id")
@click.pass_context
def notebook_share(ctx, notebook_id):
    """Configure notebook sharing."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _share():
            async with NotebookLMClient(auth) as client:
                return await client.share_project(notebook_id)

        result = run_async(_share())
        if result:
            console.print(f"[green]Sharing configured[/green]")
            console.print(result)
        else:
            console.print("[yellow]No sharing info returned[/yellow]")

    except Exception as e:
        handle_error(e)


@notebook.command("summary")
@click.argument("notebook_id")
@click.pass_context
def notebook_summary(ctx, notebook_id):
    """Get notebook summary."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _get():
            async with NotebookLMClient(auth) as client:
                return await client.get_summary(notebook_id)

        summary = run_async(_get())
        if summary:
            console.print("[bold cyan]Summary:[/bold cyan]")
            console.print(summary)
        else:
            console.print("[yellow]No summary available[/yellow]")

    except Exception as e:
        handle_error(e)


@notebook.command("analytics")
@click.argument("notebook_id")
@click.pass_context
def notebook_analytics(ctx, notebook_id):
    """Get notebook analytics."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _get():
            async with NotebookLMClient(auth) as client:
                return await client.get_project_analytics(notebook_id)

        analytics = run_async(_get())
        if analytics:
            console.print("[bold cyan]Analytics:[/bold cyan]")
            console.print(analytics)
        else:
            console.print("[yellow]No analytics available[/yellow]")

    except Exception as e:
        handle_error(e)


@notebook.command("history")
@click.argument("notebook_id")
@click.option("--limit", "-n", default=20, help="Number of messages")
@click.pass_context
def notebook_history(ctx, notebook_id, limit):
    """Get conversation history."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _get():
            async with NotebookLMClient(auth) as client:
                return await client.get_conversation_history(notebook_id, limit=limit)

        history = run_async(_get())
        if history:
            console.print(f"[bold cyan]Conversation History (last {limit}):[/bold cyan]")
            console.print(history)
        else:
            console.print("[yellow]No conversation history[/yellow]")

    except Exception as e:
        handle_error(e)


@notebook.command("query")
@click.argument("notebook_id")
@click.argument("query_text")
@click.option("--conversation-id", "-c", default=None, help="Continue a conversation")
@click.pass_context
def notebook_query(ctx, notebook_id, query_text, conversation_id):
    """Chat with a notebook."""
    ctx.invoke(query_shortcut, notebook_id=notebook_id, query_text=query_text, conversation_id=conversation_id)


@notebook.command("research")
@click.argument("notebook_id")
@click.argument("query")
@click.option("--source", type=click.Choice(["web", "drive"]), default="web")
@click.option("--mode", type=click.Choice(["fast", "deep"]), default="fast")
@click.option("--import-all", is_flag=True, help="Import all found sources")
@click.pass_context
def notebook_research(ctx, notebook_id, query, source, mode, import_all):
    """Start a research session."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _research():
            async with NotebookLMClient(auth) as client:
                console.print(f"[yellow]Starting {mode} research on {source}...[/yellow]")
                result = await client.start_research(notebook_id, query, source, mode)
                if not result:
                    return None, None

                task_id = result["task_id"]
                console.print(f"[dim]Task ID: {task_id}[/dim]")

                import time
                for _ in range(60):
                    status = await client.poll_research(notebook_id)
                    if status.get("status") == "completed":
                        return task_id, status
                    elif status.get("status") == "no_research":
                        return None, None
                    time.sleep(5)

                return task_id, {"status": "timeout"}

        task_id, status = run_async(_research())

        if not status:
            console.print("[red]Research failed to start[/red]")
            raise SystemExit(1)

        if status.get("status") == "completed":
            sources = status.get("sources", [])
            console.print(f"\n[green]Found {len(sources)} sources[/green]")

            if import_all and sources and task_id:
                async def _import():
                    async with NotebookLMClient(auth) as client:
                        return await client.import_research_sources(notebook_id, task_id, sources)

                imported = run_async(_import())
                console.print(f"[green]Imported {len(imported)} sources[/green]")
        else:
            console.print(f"[yellow]Status: {status.get('status', 'unknown')}[/yellow]")

    except Exception as e:
        handle_error(e)


@notebook.command("featured")
@click.option("--limit", "-n", default=20, help="Number of notebooks")
@click.pass_context
def notebook_featured(ctx, limit):
    """List featured/public notebooks."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _list():
            async with NotebookLMClient(auth) as client:
                return await client.list_featured_projects(page_size=limit)

        projects = run_async(_list())

        if not projects:
            console.print("[yellow]No featured notebooks found[/yellow]")
            return

        table = Table(title="Featured Notebooks")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="green")

        for proj in projects:
            if isinstance(proj, list) and len(proj) > 0:
                table.add_row(str(proj[0] or "-"), str(proj[1] if len(proj) > 1 else "-"))

        console.print(table)

    except Exception as e:
        handle_error(e)


# =============================================================================
# SOURCE GROUP
# =============================================================================


@cli.group()
def source():
    """Source management commands.

    \b
    Commands:
      list      List sources in a notebook
      add       Add a source (url, text, file, youtube)
      get       Get source details
      delete    Delete a source
      rename    Rename a source
      refresh   Refresh a URL/Drive source
    """
    pass


@source.command("list")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def source_list(ctx, notebook_id):
    """List all sources in a notebook."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _list():
            async with NotebookLMClient(auth) as client:
                notebook = await client.get_notebook(nb_id)
                sources = []
                if notebook and isinstance(notebook, list) and len(notebook) > 0:
                    nb_info = notebook[0]
                    if isinstance(nb_info, list) and len(nb_info) > 1:
                        sources_list = nb_info[1]
                        if isinstance(sources_list, list):
                            for src in sources_list:
                                if isinstance(src, list) and len(src) > 0:
                                    src_id = src[0][0] if isinstance(src[0], list) else src[0]
                                    src_title = src[1] if len(src) > 1 else "Untitled"
                                    sources.append({"id": src_id, "title": src_title})
                return sources

        sources = run_async(_list())

        table = Table(title=f"Sources in {nb_id}")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="green")

        for src in sources:
            table.add_row(src["id"], src["title"])

        console.print(table)

    except Exception as e:
        handle_error(e)


@source.command("add")
@click.argument("content")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--type", "source_type", type=click.Choice(["url", "text", "file", "youtube"]), default="url")
@click.option("--title", help="Title for text sources")
@click.option("--mime-type", help="MIME type for file sources")
@click.pass_context
def source_add(ctx, content, notebook_id, source_type, title, mime_type):
    """Add a source to a notebook.

    \b
    Examples:
      source add https://example.com              # URL
      source add "My content" --type text --title "My Doc"
      source add ./doc.pdf --type file
      source add https://youtube.com/... --type youtube
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _add():
            async with NotebookLMClient(auth) as client:
                service = SourceService(client)
                if source_type == "url":
                    return await service.add_url(nb_id, content)
                elif source_type == "youtube":
                    return await service.add_url(nb_id, content)
                elif source_type == "text":
                    return await service.add_text(nb_id, title or "Untitled", content)
                elif source_type == "file":
                    return await service.add_file(nb_id, content, mime_type)

        with console.status(f"Adding {source_type} source..."):
            source = run_async(_add())

        console.print(f"[green]Added source:[/green] {source.id}")

    except Exception as e:
        handle_error(e)


@source.command("get")
@click.argument("source_id")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def source_get(ctx, source_id, notebook_id):
    """Get source details."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _get():
            async with NotebookLMClient(auth) as client:
                return await client.get_source(nb_id, source_id)

        source = run_async(_get())
        if source:
            console.print("[bold cyan]Source Details:[/bold cyan]")
            console.print(source)
        else:
            console.print("[yellow]Source not found[/yellow]")

    except Exception as e:
        handle_error(e)


@source.command("delete")
@click.argument("source_id")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def source_delete(ctx, source_id, notebook_id, yes):
    """Delete a source."""
    if not yes and not click.confirm(f"Delete source {source_id}?"):
        return

    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _delete():
            async with NotebookLMClient(auth) as client:
                service = SourceService(client)
                return await service.delete(nb_id, source_id)

        success = run_async(_delete())
        if success:
            console.print(f"[green]Deleted source:[/green] {source_id}")
        else:
            console.print("[yellow]Delete may have failed[/yellow]")

    except Exception as e:
        handle_error(e)


@source.command("rename")
@click.argument("source_id")
@click.argument("new_title")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def source_rename(ctx, source_id, new_title, notebook_id):
    """Rename a source."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _rename():
            async with NotebookLMClient(auth) as client:
                return await client.rename_source(nb_id, source_id, new_title)

        run_async(_rename())
        console.print(f"[green]Renamed:[/green] {source_id} -> {new_title}")

    except Exception as e:
        handle_error(e)


@source.command("refresh")
@click.argument("source_id")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def source_refresh(ctx, source_id, notebook_id):
    """Refresh a URL/Drive source."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _refresh():
            async with NotebookLMClient(auth) as client:
                return await client.refresh_source(nb_id, source_id)

        with console.status(f"Refreshing source..."):
            result = run_async(_refresh())

        if result:
            console.print(f"[green]Source refreshed:[/green] {source_id}")
        else:
            console.print("[yellow]Refresh returned no result[/yellow]")

    except Exception as e:
        handle_error(e)


# =============================================================================
# ARTIFACT GROUP
# =============================================================================


@cli.group()
def artifact():
    """Artifact management commands.

    \b
    Commands:
      list      List all artifacts (or by type)
      get       Get artifact details
      rename    Rename an artifact
      delete    Delete an artifact
      export    Export to Google Docs/Sheets
      poll      Poll generation status
    """
    pass


@artifact.command("list")
@click.argument("notebook_id")
@click.option("--type", "artifact_type",
              type=click.Choice(["all", "video", "slide-deck", "quiz", "flashcard",
                                "infographic", "data-table", "mind-map", "briefing-doc"]),
              default="all", help="Filter by type")
@click.pass_context
def artifact_list(ctx, notebook_id, artifact_type):
    """List artifacts in a notebook."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _list():
            async with NotebookLMClient(auth) as client:
                if artifact_type == "all":
                    return await client.list_artifacts(notebook_id), "all"
                elif artifact_type == "video":
                    return await client.list_video_overviews(notebook_id), "video"
                elif artifact_type == "slide-deck":
                    return await client.list_slide_decks(notebook_id), "slide-deck"
                elif artifact_type == "quiz":
                    return await client.list_quizzes(notebook_id), "quiz"
                elif artifact_type == "flashcard":
                    return await client.list_flashcards(notebook_id), "flashcard"
                elif artifact_type == "infographic":
                    return await client.list_infographics(notebook_id), "infographic"
                elif artifact_type == "data-table":
                    return await client.list_data_tables(notebook_id), "data-table"
                elif artifact_type == "mind-map":
                    return await client.list_mind_maps(notebook_id), "mind-map"
                elif artifact_type == "briefing-doc":
                    return await client.list_briefing_docs(notebook_id), "briefing-doc"
                return [], artifact_type

        artifacts, atype = run_async(_list())

        if not artifacts:
            console.print(f"[yellow]No {atype} artifacts found[/yellow]")
            return

        table = Table(title=f"{atype.title()} Artifacts in {notebook_id}")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="green")
        table.add_column("Status", style="yellow")

        for art in artifacts:
            if isinstance(art, list) and len(art) > 0:
                art_id = str(art[0] or "-")
                title = str(art[1] if len(art) > 1 else "-")
                status_code = art[4] if len(art) > 4 else None
                status = "completed" if status_code == 3 else "processing" if status_code == 1 else str(status_code)
                table.add_row(art_id, title, status)
            elif isinstance(art, dict):
                table.add_row(art.get("id", "-"), art.get("title", "-"), "-")

        console.print(table)

    except Exception as e:
        handle_error(e)


@artifact.command("get")
@click.argument("notebook_id")
@click.argument("artifact_id")
@click.pass_context
def artifact_get(ctx, notebook_id, artifact_id):
    """Get artifact details."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _get():
            async with NotebookLMClient(auth) as client:
                return await client.get_artifact(notebook_id, artifact_id)

        artifact = run_async(_get())
        if artifact:
            console.print("[bold cyan]Artifact Details:[/bold cyan]")
            console.print(artifact)
        else:
            console.print("[yellow]Artifact not found[/yellow]")

    except Exception as e:
        handle_error(e)


@artifact.command("rename")
@click.argument("notebook_id")
@click.argument("artifact_id")
@click.argument("new_title")
@click.pass_context
def artifact_rename(ctx, notebook_id, artifact_id, new_title):
    """Rename an artifact."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _rename():
            async with NotebookLMClient(auth) as client:
                return await client.rename_artifact(notebook_id, artifact_id, new_title)

        run_async(_rename())
        console.print(f"[green]Renamed:[/green] {artifact_id} -> {new_title}")

    except Exception as e:
        handle_error(e)


@artifact.command("delete")
@click.argument("notebook_id")
@click.argument("artifact_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def artifact_delete(ctx, notebook_id, artifact_id, yes):
    """Delete an artifact."""
    if not yes and not click.confirm(f"Delete artifact {artifact_id}?"):
        return

    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _delete():
            async with NotebookLMClient(auth) as client:
                return await client.delete_studio_content(notebook_id, artifact_id)

        run_async(_delete())
        console.print(f"[green]Deleted artifact:[/green] {artifact_id}")

    except Exception as e:
        handle_error(e)


@artifact.command("export")
@click.argument("notebook_id")
@click.argument("artifact_id")
@click.option("--title", required=True, help="Title for exported document")
@click.option("--type", "export_type", type=click.Choice(["docs", "sheets"]), default="docs")
@click.pass_context
def artifact_export(ctx, notebook_id, artifact_id, title, export_type):
    """Export artifact to Google Docs/Sheets."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _export():
            async with NotebookLMClient(auth) as client:
                artifact = await client.get_artifact(notebook_id, artifact_id)
                content = str(artifact) if artifact else ""
                return await client.export_artifact(notebook_id, artifact_id, content, title, export_type)

        result = run_async(_export())
        if result:
            console.print(f"[green]Exported to Google {export_type.title()}[/green]")
            console.print(result)
        else:
            console.print("[yellow]Export may have failed[/yellow]")

    except Exception as e:
        handle_error(e)


@artifact.command("poll")
@click.argument("notebook_id")
@click.argument("task_id")
@click.pass_context
def artifact_poll(ctx, notebook_id, task_id):
    """Poll generation status."""
    try:
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _poll():
            async with NotebookLMClient(auth) as client:
                return await client.poll_studio_status(notebook_id, task_id)

        status = run_async(_poll())
        console.print("[bold cyan]Task Status:[/bold cyan]")
        console.print(status)

    except Exception as e:
        handle_error(e)


# =============================================================================
# GENERATE GROUP
# =============================================================================


@cli.group()
def generate():
    """Generate content from notebook.

    \b
    LLM-friendly design: Describe what you want in natural language.

    \b
    Examples:
      notebooklm use nb123
      notebooklm generate video "a funny explainer for kids age 5"
      notebooklm generate audio "deep dive focusing on chapter 3"
      notebooklm generate quiz "focus on vocabulary terms"

    \b
    Types:
      audio        Audio overview (podcast)
      video        Video overview
      slide-deck   Slide deck
      quiz         Quiz
      flashcards   Flashcards
      infographic  Infographic
      data-table   Data table
      mind-map     Mind map
      timeline     Timeline
      study-guide  Study guide
      faq          FAQ
      briefing-doc Briefing document
    """
    pass


@generate.command("audio")
@click.argument("description", default="", required=False)
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--format", "audio_format", type=click.Choice(["deep-dive", "brief", "critique", "debate"]), default="deep-dive")
@click.option("--length", "audio_length", type=click.Choice(["short", "default", "long"]), default="default")
@click.option("--language", default="en")
@click.option("--wait/--no-wait", default=True)
@click.pass_context
def generate_audio(ctx, description, notebook_id, audio_format, audio_length, language, wait):
    """Generate audio overview (podcast).

    \b
    Example:
      notebooklm generate audio "deep dive focusing on key themes"
      notebooklm generate audio "make it funny and casual" --format debate
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        format_map = {"deep-dive": AudioFormat.DEEP_DIVE, "brief": AudioFormat.BRIEF,
                      "critique": AudioFormat.CRITIQUE, "debate": AudioFormat.DEBATE}
        length_map = {"short": AudioLength.SHORT, "default": AudioLength.DEFAULT, "long": AudioLength.LONG}

        async def _generate():
            async with NotebookLMClient(auth) as client:
                result = await client.generate_audio(
                    nb_id,
                    language=language,
                    instructions=description or None,
                    audio_format=format_map[audio_format],
                    audio_length=length_map[audio_length],
                )

                if not result:
                    return None

                if wait:
                    console.print(f"[yellow]Generating audio...[/yellow] Task: {result.get('artifact_id')}")
                    service = ArtifactService(client)
                    return await service.wait_for_completion(
                        nb_id, result["artifact_id"], poll_interval=10.0
                    )
                return result

        status = run_async(_generate())

        if not status:
            console.print("[red]Audio generation failed[/red]")
        elif hasattr(status, "is_complete") and status.is_complete:
            console.print(f"[green]Audio ready:[/green] {status.url}")
        elif hasattr(status, "is_failed") and status.is_failed:
            console.print(f"[red]Failed:[/red] {status.error}")
        else:
            console.print(f"[yellow]Started:[/yellow] {status}")

    except Exception as e:
        handle_error(e)


@generate.command("video")
@click.argument("description", default="", required=False)
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--format", "video_format", type=click.Choice(["explainer", "brief"]), default="explainer")
@click.option("--style", type=click.Choice(["auto", "classic", "whiteboard", "kawaii", "anime", "watercolor", "retro-print", "heritage", "paper-craft"]), default="auto")
@click.option("--language", default="en")
@click.option("--wait/--no-wait", default=True)
@click.pass_context
def generate_video(ctx, description, notebook_id, video_format, style, language, wait):
    """Generate video overview.

    \b
    Example:
      notebooklm generate video "a funny explainer for kids age 5"
      notebooklm generate video "professional presentation" --style classic
      notebooklm generate video --style kawaii
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        format_map = {"explainer": VideoFormat.EXPLAINER, "brief": VideoFormat.BRIEF}
        style_map = {"auto": VideoStyle.AUTO_SELECT, "classic": VideoStyle.CLASSIC, "whiteboard": VideoStyle.WHITEBOARD,
                     "kawaii": VideoStyle.KAWAII, "anime": VideoStyle.ANIME, "watercolor": VideoStyle.WATERCOLOR,
                     "retro-print": VideoStyle.RETRO_PRINT, "heritage": VideoStyle.HERITAGE, "paper-craft": VideoStyle.PAPER_CRAFT}

        async def _generate():
            async with NotebookLMClient(auth) as client:
                result = await client.generate_video(
                    nb_id, language=language, instructions=description or None,
                    video_format=format_map[video_format], video_style=style_map[style],
                )

                if not result:
                    return None

                if wait and result.get("artifact_id"):
                    console.print(f"[yellow]Generating video...[/yellow] Task: {result.get('artifact_id')}")
                    service = ArtifactService(client)
                    return await service.wait_for_completion(
                        nb_id, result["artifact_id"], poll_interval=10.0, timeout=600.0
                    )
                return result

        status = run_async(_generate())

        if not status:
            console.print("[red]Video generation failed[/red]")
        elif hasattr(status, "is_complete") and status.is_complete:
            console.print(f"[green]Video ready:[/green] {status.url}")
        elif hasattr(status, "is_failed") and status.is_failed:
            console.print(f"[red]Failed:[/red] {status.error}")
        else:
            console.print(f"[yellow]Started:[/yellow] {status}")

    except Exception as e:
        handle_error(e)


@generate.command("slide-deck")
@click.argument("description", default="", required=False)
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--format", "deck_format", type=click.Choice(["detailed", "presenter"]), default="detailed")
@click.option("--length", "deck_length", type=click.Choice(["default", "short"]), default="default")
@click.option("--language", default="en")
@click.option("--wait/--no-wait", default=True)
@click.pass_context
def generate_slide_deck(ctx, description, notebook_id, deck_format, deck_length, language, wait):
    """Generate slide deck.

    \b
    Example:
      notebooklm generate slide-deck "include speaker notes"
      notebooklm generate slide-deck "executive summary" --format presenter --length short
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        format_map = {"detailed": SlideDeckFormat.DETAILED_DECK, "presenter": SlideDeckFormat.PRESENTER_SLIDES}
        length_map = {"default": SlideDeckLength.DEFAULT, "short": SlideDeckLength.SHORT}

        async def _generate():
            async with NotebookLMClient(auth) as client:
                result = await client.generate_slide_deck(
                    nb_id, language=language, instructions=description or None,
                    slide_deck_format=format_map[deck_format], slide_deck_length=length_map[deck_length],
                )

                if not result:
                    return None

                if wait and result.get("artifact_id"):
                    console.print(f"[yellow]Generating slide deck...[/yellow] Task: {result.get('artifact_id')}")
                    service = ArtifactService(client)
                    return await service.wait_for_completion(
                        nb_id, result["artifact_id"], poll_interval=10.0
                    )
                return result

        status = run_async(_generate())

        if not status:
            console.print("[red]Slide deck generation failed[/red]")
        elif hasattr(status, "is_complete") and status.is_complete:
            console.print(f"[green]Slide deck ready:[/green] {status.url}")
        else:
            console.print(f"[yellow]Started:[/yellow] {status}")

    except Exception as e:
        handle_error(e)


@generate.command("quiz")
@click.argument("description", default="", required=False)
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
@click.option("--wait/--no-wait", default=True)
@click.pass_context
def generate_quiz(ctx, description, notebook_id, quantity, difficulty, wait):
    """Generate quiz.

    \b
    Example:
      notebooklm generate quiz "focus on vocabulary terms"
      notebooklm generate quiz "test key concepts" --difficulty hard --quantity more
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        quantity_map = {"fewer": QuizQuantity.FEWER, "standard": QuizQuantity.STANDARD, "more": QuizQuantity.MORE}
        difficulty_map = {"easy": QuizDifficulty.EASY, "medium": QuizDifficulty.MEDIUM, "hard": QuizDifficulty.HARD}

        async def _generate():
            async with NotebookLMClient(auth) as client:
                result = await client.generate_quiz(
                    nb_id, instructions=description or None,
                    quantity=quantity_map[quantity], difficulty=difficulty_map[difficulty],
                )

                if not result:
                    return None

                task_id = result.get("artifact_id") or (result[0] if isinstance(result, list) else None)
                if wait and task_id:
                    console.print(f"[yellow]Generating quiz...[/yellow]")
                    service = ArtifactService(client)
                    return await service.wait_for_completion(nb_id, task_id, poll_interval=5.0)
                return result

        status = run_async(_generate())

        if hasattr(status, "is_complete") and status.is_complete:
            console.print("[green]Quiz ready[/green]")
        else:
            console.print(f"[yellow]Result:[/yellow] {status}")

    except Exception as e:
        handle_error(e)


@generate.command("flashcards")
@click.argument("description", default="", required=False)
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
@click.option("--wait/--no-wait", default=True)
@click.pass_context
def generate_flashcards(ctx, description, notebook_id, quantity, difficulty, wait):
    """Generate flashcards.

    \b
    Example:
      notebooklm generate flashcards "vocabulary terms only"
      notebooklm generate flashcards --quantity more --difficulty easy
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        quantity_map = {"fewer": QuizQuantity.FEWER, "standard": QuizQuantity.STANDARD, "more": QuizQuantity.MORE}
        difficulty_map = {"easy": QuizDifficulty.EASY, "medium": QuizDifficulty.MEDIUM, "hard": QuizDifficulty.HARD}

        async def _generate():
            async with NotebookLMClient(auth) as client:
                result = await client.generate_flashcards(
                    nb_id, instructions=description or None,
                    quantity=quantity_map[quantity], difficulty=difficulty_map[difficulty],
                )

                if not result:
                    return None

                task_id = result.get("artifact_id") or (result[0] if isinstance(result, list) else None)
                if wait and task_id:
                    console.print(f"[yellow]Generating flashcards...[/yellow]")
                    service = ArtifactService(client)
                    return await service.wait_for_completion(nb_id, task_id, poll_interval=5.0)
                return result

        status = run_async(_generate())

        if hasattr(status, "is_complete") and status.is_complete:
            console.print("[green]Flashcards ready[/green]")
        else:
            console.print(f"[yellow]Result:[/yellow] {status}")

    except Exception as e:
        handle_error(e)


@generate.command("infographic")
@click.argument("description", default="", required=False)
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--orientation", type=click.Choice(["landscape", "portrait", "square"]), default="landscape")
@click.option("--detail", type=click.Choice(["concise", "standard", "detailed"]), default="standard")
@click.option("--language", default="en")
@click.option("--wait/--no-wait", default=True)
@click.pass_context
def generate_infographic(ctx, description, notebook_id, orientation, detail, language, wait):
    """Generate infographic.

    \b
    Example:
      notebooklm generate infographic "include statistics and key findings"
      notebooklm generate infographic --orientation portrait --detail detailed
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        orientation_map = {"landscape": InfographicOrientation.LANDSCAPE, "portrait": InfographicOrientation.PORTRAIT, "square": InfographicOrientation.SQUARE}
        detail_map = {"concise": InfographicDetail.CONCISE, "standard": InfographicDetail.STANDARD, "detailed": InfographicDetail.DETAILED}

        async def _generate():
            async with NotebookLMClient(auth) as client:
                result = await client.generate_infographic(
                    nb_id, language=language, instructions=description or None,
                    orientation=orientation_map[orientation], detail_level=detail_map[detail],
                )

                if not result:
                    return None

                task_id = result.get("artifact_id") or (result[0] if isinstance(result, list) else None)
                if wait and task_id:
                    console.print(f"[yellow]Generating infographic...[/yellow]")
                    service = ArtifactService(client)
                    return await service.wait_for_completion(nb_id, task_id, poll_interval=5.0)
                return result

        status = run_async(_generate())

        if hasattr(status, "is_complete") and status.is_complete:
            console.print("[green]Infographic ready[/green]")
        else:
            console.print(f"[yellow]Result:[/yellow] {status}")

    except Exception as e:
        handle_error(e)


@generate.command("data-table")
@click.argument("description")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--language", default="en")
@click.option("--wait/--no-wait", default=True)
@click.pass_context
def generate_data_table(ctx, description, notebook_id, language, wait):
    """Generate data table.

    \b
    Example:
      notebooklm generate data-table "comparison of key concepts"
      notebooklm generate data-table "timeline of events"
    """
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _generate():
            async with NotebookLMClient(auth) as client:
                result = await client.generate_data_table(nb_id, language=language, instructions=description)

                if not result:
                    return None

                task_id = result.get("artifact_id") or (result[0] if isinstance(result, list) else None)
                if wait and task_id:
                    console.print(f"[yellow]Generating data table...[/yellow]")
                    service = ArtifactService(client)
                    return await service.wait_for_completion(nb_id, task_id, poll_interval=5.0)
                return result

        status = run_async(_generate())

        if hasattr(status, "is_complete") and status.is_complete:
            console.print("[green]Data table ready[/green]")
        else:
            console.print(f"[yellow]Result:[/yellow] {status}")

    except Exception as e:
        handle_error(e)


@generate.command("mind-map")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def generate_mind_map(ctx, notebook_id):
    """Generate mind map."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _generate():
            async with NotebookLMClient(auth) as client:
                return await client.generate_mind_map(nb_id)

        with console.status("Generating mind map..."):
            result = run_async(_generate())

        if result:
            console.print("[green]Mind map generated:[/green]")
            if isinstance(result, dict):
                console.print(f"  Note ID: {result.get('note_id', '-')}")
                mind_map = result.get("mind_map", {})
                if isinstance(mind_map, dict):
                    console.print(f"  Root: {mind_map.get('name', '-')}")
                    console.print(f"  Children: {len(mind_map.get('children', []))} nodes")
            else:
                console.print(result)
        else:
            console.print("[yellow]No result[/yellow]")

    except Exception as e:
        handle_error(e)


@generate.command("timeline")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def generate_timeline(ctx, notebook_id):
    """Generate timeline."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _generate():
            async with NotebookLMClient(auth) as client:
                return await client.generate_timeline(nb_id)

        with console.status("Generating timeline..."):
            result = run_async(_generate())

        if result:
            console.print("[green]Timeline generated:[/green]")
            console.print(result)
        else:
            console.print("[yellow]No result[/yellow]")

    except Exception as e:
        handle_error(e)


@generate.command("study-guide")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def generate_study_guide(ctx, notebook_id):
    """Generate study guide."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _generate():
            async with NotebookLMClient(auth) as client:
                return await client.generate_study_guide(nb_id)

        with console.status("Generating study guide..."):
            result = run_async(_generate())

        if result:
            console.print("[green]Study guide generated:[/green]")
            console.print(result)
        else:
            console.print("[yellow]No result[/yellow]")

    except Exception as e:
        handle_error(e)


@generate.command("faq")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def generate_faq(ctx, notebook_id):
    """Generate FAQ."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _generate():
            async with NotebookLMClient(auth) as client:
                return await client.generate_faq(nb_id)

        with console.status("Generating FAQ..."):
            result = run_async(_generate())

        if result:
            console.print("[green]FAQ generated:[/green]")
            console.print(result)
        else:
            console.print("[yellow]No result[/yellow]")

    except Exception as e:
        handle_error(e)


@generate.command("briefing-doc")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def generate_briefing_doc(ctx, notebook_id):
    """Generate briefing document."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _generate():
            async with NotebookLMClient(auth) as client:
                return await client.generate_briefing_doc(nb_id)

        with console.status("Generating briefing document..."):
            result = run_async(_generate())

        if result:
            console.print("[green]Briefing document generated:[/green]")
            console.print(result)
        else:
            console.print("[yellow]No result[/yellow]")

    except Exception as e:
        handle_error(e)


# =============================================================================
# DOWNLOAD GROUP
# =============================================================================


@cli.group()
def download():
    """Download generated content.

    \b
    Types:
      audio        Download audio file
      video        Download video file
      slide-deck   Download slide deck images
      infographic  Download infographic image
    """
    pass


@download.command("audio")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option("--latest", is_flag=True, default=True, help="Download latest (default)")
@click.option("--earliest", is_flag=True, help="Download earliest")
@click.option("--all", "download_all", is_flag=True, help="Download all artifacts")
@click.option("--name", help="Filter by artifact title (fuzzy match)")
@click.option("--artifact-id", help="Select by exact artifact ID")
@click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text")
@click.option("--dry-run", is_flag=True, help="Preview without downloading")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--no-clobber", is_flag=True, help="Skip if file exists")
@click.pass_context
def download_audio(ctx, output_path, notebook, latest, earliest, download_all, name, artifact_id, json_output, dry_run, force, no_clobber):
    """Download audio overview(s) to file.

    \b
    Examples:
      # Download latest audio to default filename
      notebooklm download audio

      # Download to specific path
      notebooklm download audio my-podcast.mp3

      # Download all audio files to directory
      notebooklm download audio --all ./audio/

      # Download specific artifact by name
      notebooklm download audio --name "chapter 3"

      # Preview without downloading
      notebooklm download audio --all --dry-run
    """
    from .download_helpers import select_artifact, artifact_title_to_filename
    from pathlib import Path
    from typing import Any

    # Constants
    AUDIO_EXTENSION = ".mp3"
    DEFAULT_OUTPUT_DIR = "./audio"

    # Validate conflict flags
    if force and no_clobber:
        console.print("[red]Cannot specify both --force and --no-clobber[/red]")
        raise SystemExit(1)

    # Validate selection flags
    if latest and earliest:
        console.print("[red]Cannot specify both --latest and --earliest[/red]")
        raise SystemExit(1)

    if download_all and artifact_id:
        console.print("[red]Cannot specify both --all and --artifact-id[/red]")
        raise SystemExit(1)

    def _resolve_file_conflict(
        file_path: Path,
        force: bool,
        no_clobber: bool
    ) -> tuple[Path | None, dict | None]:
        """
        Resolve file conflicts for download.

        Returns:
            tuple: (resolved_path, error_dict)
            - If file can be used/created: (Path, None)
            - If should skip (no_clobber): (None, error_dict)
        """
        if not file_path.exists():
            return file_path, None

        if no_clobber:
            return None, {
                "status": "skipped",
                "reason": "file exists",
                "path": str(file_path)
            }

        if not force:
            # Auto-rename with (2), (3), etc.
            counter = 2
            base = file_path.stem
            ext = file_path.suffix
            parent = file_path.parent

            while file_path.exists():
                file_path = parent / f"{base} ({counter}){ext}"
                counter += 1

        return file_path, None

    try:
        nb_id = require_notebook(notebook)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _download() -> dict[str, Any]:
            async with NotebookLMClient(auth) as client:
                # Get all artifacts and filter for audio
                all_artifacts = await client.list_artifacts(nb_id)

                # Filter: type=1 (audio), status=3 (completed)
                audio_artifacts_raw = [
                    a for a in all_artifacts
                    if isinstance(a, list) and len(a) > 4 and a[2] == 1 and a[4] == 3
                ]

                if not audio_artifacts_raw:
                    return {"error": "No completed audio artifacts found"}

                # Convert to dict format for select_artifact helper
                audio_artifacts = [
                    {"id": a[0], "title": a[1], "created_at": a[3] if len(a) > 3 else 0}
                    for a in audio_artifacts_raw
                ]

                # Handle --all flag
                if download_all:
                    # Default directory for --all
                    output_dir = Path(output_path) if output_path else Path(DEFAULT_OUTPUT_DIR)

                    if dry_run:
                        return {
                            "dry_run": True,
                            "operation": "download_all",
                            "count": len(audio_artifacts),
                            "output_dir": str(output_dir),
                            "artifacts": [
                                {
                                    "id": a["id"],
                                    "title": a["title"],
                                    "filename": artifact_title_to_filename(a["title"], AUDIO_EXTENSION, set())
                                }
                                for a in audio_artifacts
                            ]
                        }

                    # Create output directory
                    output_dir.mkdir(parents=True, exist_ok=True)

                    results = []
                    existing_files = set()
                    total = len(audio_artifacts)

                    for i, artifact in enumerate(audio_artifacts, 1):
                        # Progress indicator (not in JSON mode)
                        if not json_output:
                            console.print(f"[dim]Downloading {i}/{total}:[/dim] {artifact['title']}")

                        # Generate filename
                        filename = artifact_title_to_filename(
                            artifact["title"], AUDIO_EXTENSION, existing_files
                        )
                        existing_files.add(filename)
                        file_path = output_dir / filename

                        # Resolve file conflicts
                        resolved_path, skip_info = _resolve_file_conflict(file_path, force, no_clobber)
                        if skip_info:
                            results.append({
                                "id": artifact["id"],
                                "title": artifact["title"],
                                "filename": filename,
                                **skip_info
                            })
                            continue

                        # Update filename if path was auto-renamed
                        file_path = resolved_path
                        filename = file_path.name

                        # Download
                        try:
                            await client.download_audio(nb_id, str(file_path), artifact_id=artifact["id"])
                            results.append({
                                "id": artifact["id"],
                                "title": artifact["title"],
                                "filename": filename,
                                "path": str(file_path),
                                "status": "downloaded"
                            })
                        except Exception as e:
                            results.append({
                                "id": artifact["id"],
                                "title": artifact["title"],
                                "filename": filename,
                                "status": "failed",
                                "error": str(e)
                            })

                    return {
                        "operation": "download_all",
                        "output_dir": str(output_dir),
                        "total": len(audio_artifacts),
                        "results": results
                    }

                # Single artifact selection
                try:
                    selected, reason = select_artifact(
                        audio_artifacts,
                        latest=latest,
                        earliest=earliest,
                        name=name,
                        artifact_id=artifact_id
                    )
                except ValueError as e:
                    return {"error": str(e)}

                # Determine output path
                if not output_path:
                    # Default: ./[artifact-title].mp3
                    default_filename = artifact_title_to_filename(selected["title"], AUDIO_EXTENSION, set())
                    final_output_path = Path.cwd() / default_filename
                else:
                    final_output_path = Path(output_path)

                if dry_run:
                    return {
                        "dry_run": True,
                        "operation": "download_single",
                        "artifact": {
                            "id": selected["id"],
                            "title": selected["title"],
                            "selection_reason": reason
                        },
                        "output_path": str(final_output_path)
                    }

                # Resolve file conflicts
                resolved_path, skip_error = _resolve_file_conflict(final_output_path, force, no_clobber)
                if skip_error:
                    return {
                        "error": f"File exists: {final_output_path}",
                        "artifact": selected,
                        "suggestion": "Use --force to overwrite or choose a different path"
                    }

                final_output_path = resolved_path

                # Download
                try:
                    result_path = await client.download_audio(
                        nb_id, str(final_output_path), artifact_id=selected["id"]
                    )
                    return {
                        "operation": "download_single",
                        "artifact": {
                            "id": selected["id"],
                            "title": selected["title"],
                            "selection_reason": reason
                        },
                        "output_path": result_path,
                        "status": "downloaded"
                    }
                except Exception as e:
                    return {
                        "error": str(e),
                        "artifact": selected
                    }

        result = run_async(_download())

        # Handle JSON output
        if json_output:
            console.print(json.dumps(result, indent=2))
            return

        # Handle errors
        if "error" in result:
            console.print(f"[red]Error:[/red] {result['error']}")
            if "suggestion" in result:
                console.print(f"[dim]{result['suggestion']}[/dim]")
            raise SystemExit(1)

        # Handle dry-run
        if result.get("dry_run"):
            if result["operation"] == "download_all":
                console.print(f"[yellow]DRY RUN:[/yellow] Would download {result['count']} audio files to: {result['output_dir']}")
                console.print("\n[bold]Preview:[/bold]")
                for art in result["artifacts"]:
                    console.print(f"  {art['filename']} <- {art['title']}")
            else:
                console.print(f"[yellow]DRY RUN:[/yellow] Would download:")
                console.print(f"  Artifact: {result['artifact']['title']}")
                console.print(f"  Reason: {result['artifact']['selection_reason']}")
                console.print(f"  Output: {result['output_path']}")
            return

        # Handle download_all results
        if result.get("operation") == "download_all":
            downloaded = [r for r in result["results"] if r["status"] == "downloaded"]
            skipped = [r for r in result["results"] if r["status"] == "skipped"]
            failed = [r for r in result["results"] if r["status"] == "failed"]

            console.print(f"[bold]Downloaded {len(downloaded)}/{result['total']} audio files to:[/bold] {result['output_dir']}")

            if downloaded:
                console.print("\n[green]Downloaded:[/green]")
                for r in downloaded:
                    console.print(f"  {r['filename']} <- {r['title']}")

            if skipped:
                console.print("\n[yellow]Skipped:[/yellow]")
                for r in skipped:
                    console.print(f"  {r['filename']} ({r['reason']})")

            if failed:
                console.print("\n[red]Failed:[/red]")
                for r in failed:
                    console.print(f"  {r['filename']}: {r.get('error', 'unknown error')}")

        # Handle single download
        else:
            console.print(f"[green]Audio saved to:[/green] {result['output_path']}")
            console.print(f"[dim]Artifact: {result['artifact']['title']} ({result['artifact']['selection_reason']})[/dim]")

    except Exception as e:
        handle_error(e)


@download.command("video")
@click.argument("output_path", type=click.Path())
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--artifact-id", help="Specific artifact ID")
@click.pass_context
def download_video(ctx, output_path, notebook_id, artifact_id):
    """Download video to file."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _download():
            async with NotebookLMClient(auth) as client:
                return await client.download_video(nb_id, output_path, artifact_id=artifact_id)

        with console.status(f"Downloading video..."):
            result_path = run_async(_download())

        console.print(f"[green]Video saved to:[/green] {result_path}")

    except Exception as e:
        handle_error(e)


@download.command("slide-deck")
@click.argument("output_dir", type=click.Path())
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--artifact-id", help="Specific artifact ID")
@click.pass_context
def download_slide_deck(ctx, output_dir, notebook_id, artifact_id):
    """Download slide deck images to directory."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _download():
            async with NotebookLMClient(auth) as client:
                return await client.download_slide_deck(nb_id, output_dir, artifact_id=artifact_id)

        with console.status(f"Downloading slide deck..."):
            paths = run_async(_download())

        console.print(f"[green]Downloaded {len(paths)} images to:[/green] {output_dir}")

    except Exception as e:
        handle_error(e)


@download.command("infographic")
@click.argument("output_path", type=click.Path())
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--artifact-id", help="Specific artifact ID")
@click.pass_context
def download_infographic(ctx, output_path, notebook_id, artifact_id):
    """Download infographic to file."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _download():
            async with NotebookLMClient(auth) as client:
                return await client.download_infographic(nb_id, output_path, artifact_id=artifact_id)

        with console.status(f"Downloading infographic..."):
            result_path = run_async(_download())

        console.print(f"[green]Infographic saved to:[/green] {result_path}")

    except Exception as e:
        handle_error(e)


# =============================================================================
# NOTE GROUP
# =============================================================================


@cli.group()
def note():
    """Note management commands.

    \b
    Commands:
      list    List all notes
      create  Create a new note
      get     Get note content
      save    Update note content
      delete  Delete a note
    """
    pass


@note.command("list")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def note_list(ctx, notebook_id):
    """List all notes in a notebook."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _list():
            async with NotebookLMClient(auth) as client:
                return await client.list_notes(nb_id)

        notes = run_async(_list())

        if not notes:
            console.print("[yellow]No notes found[/yellow]")
            return

        table = Table(title=f"Notes in {nb_id}")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="green")
        table.add_column("Preview", style="dim", max_width=50)

        for n in notes:
            if isinstance(n, dict):
                preview = n.get("content", "")[:50]
                table.add_row(n.get("id", "-"), n.get("title", "Untitled"), preview + "..." if len(n.get("content", "")) > 50 else preview)

        console.print(table)

    except Exception as e:
        handle_error(e)


@note.command("create")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--title", default="Untitled Note", help="Note title")
@click.option("--content", default="", help="Note content")
@click.pass_context
def note_create(ctx, notebook_id, title, content):
    """Create a new note."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _create():
            async with NotebookLMClient(auth) as client:
                return await client.create_note(nb_id, title, content)

        result = run_async(_create())

        if result:
            console.print("[green]Note created[/green]")
            console.print(result)
        else:
            console.print("[yellow]Creation may have failed[/yellow]")

    except Exception as e:
        handle_error(e)


@note.command("get")
@click.argument("note_id")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.pass_context
def note_get(ctx, note_id, notebook_id):
    """Get note content."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _get():
            async with NotebookLMClient(auth) as client:
                return await client.get_note(nb_id, note_id)

        n = run_async(_get())

        if n:
            if isinstance(n, dict):
                console.print(f"[bold cyan]Title:[/bold cyan] {n.get('title', 'Untitled')}")
                console.print(f"[bold cyan]Content:[/bold cyan]\n{n.get('content', '')}")
            else:
                console.print(n)
        else:
            console.print("[yellow]Note not found[/yellow]")

    except Exception as e:
        handle_error(e)


@note.command("save")
@click.argument("note_id")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--title", help="New title")
@click.option("--content", help="New content")
@click.pass_context
def note_save(ctx, note_id, notebook_id, title, content):
    """Update note content."""
    if not title and not content:
        console.print("[yellow]Provide --title and/or --content[/yellow]")
        return

    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _save():
            async with NotebookLMClient(auth) as client:
                return await client.save_note_content(nb_id, note_id, content=content, title=title)

        run_async(_save())
        console.print(f"[green]Note updated:[/green] {note_id}")

    except Exception as e:
        handle_error(e)


@note.command("delete")
@click.argument("note_id")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def note_delete(ctx, note_id, notebook_id, yes):
    """Delete a note."""
    if not yes and not click.confirm(f"Delete note {note_id}?"):
        return

    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _delete():
            async with NotebookLMClient(auth) as client:
                return await client.delete_note(nb_id, note_id)

        run_async(_delete())
        console.print(f"[green]Deleted note:[/green] {note_id}")

    except Exception as e:
        handle_error(e)


# =============================================================================
# MISC GROUP (guidebooks, share-audio)
# =============================================================================


@cli.command("share-audio")
@click.option("-n", "--notebook", "notebook_id", default=None, help="Notebook ID (uses current if not set)")
@click.option("--public/--private", default=False, help="Make audio public or private")
@click.pass_context
def share_audio_cmd(ctx, notebook_id, public):
    """Share or unshare audio overview."""
    try:
        nb_id = require_notebook(notebook_id)
        cookies, csrf, session_id = get_client(ctx)
        auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

        async def _share():
            async with NotebookLMClient(auth) as client:
                return await client.share_audio(nb_id, public=public)

        result = run_async(_share())

        if result:
            status = "public" if public else "private"
            console.print(f"[green]Audio is now {status}[/green]")
            console.print(result)
        else:
            console.print("[yellow]Share returned no result[/yellow]")

    except Exception as e:
        handle_error(e)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def main():
    cli()


if __name__ == "__main__":
    main()
