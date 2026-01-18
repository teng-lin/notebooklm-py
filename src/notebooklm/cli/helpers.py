"""CLI helper utilities.

Provides common functionality for all CLI commands:
- Authentication handling (get_client)
- Async execution (run_async)
- Error handling
- JSON/Rich output formatting
- Context management (current notebook/conversation)
- @with_client decorator for command boilerplate reduction
"""

import asyncio
import json
import logging
import os
import time
from functools import wraps

import click
from rich.console import Console
from rich.table import Table

from .._url_utils import is_youtube_url
from ..auth import (
    AuthTokens,
    fetch_tokens,
    load_auth_from_storage,
)
from ..paths import get_browser_profile_dir, get_context_path

console = Console()
logger = logging.getLogger(__name__)

# Backward-compatible module-level constants (for tests that patch these)
# Note: Prefer using get_context_path() and get_browser_profile_dir() for dynamic resolution
# These are evaluated once at import time, so NOTEBOOKLM_HOME changes after import won't affect them
CONTEXT_FILE = get_context_path()
BROWSER_PROFILE_DIR = get_browser_profile_dir()

# Artifact type display mapping
ARTIFACT_TYPE_DISPLAY = {
    1: "🎵 Audio Overview",
    2: "📄 Report",
    3: "🎥 Video Overview",
    4: "📝 Quiz",
    5: "🧠 Mind Map",
    # Note: Type 6 appears unused in current API
    7: "🖼️ Infographic",
    8: "🎞️ Slide Deck",
    9: "📋 Data Table",
}

# CLI artifact type to StudioContentType enum mapping
ARTIFACT_TYPE_MAP = {
    "video": 3,
    "slide-deck": 8,
    "quiz": 4,
    "flashcard": 4,  # Same as quiz
    "infographic": 7,
    "data-table": 9,
    "mind-map": 5,
    "report": 2,
}


# =============================================================================
# ASYNC EXECUTION
# =============================================================================


def run_async(coro):
    """Run async coroutine in sync context."""
    return asyncio.run(coro)


# =============================================================================
# AUTHENTICATION
# =============================================================================


def get_client(ctx) -> tuple[dict, str, str]:
    """Get auth components from context.

    Args:
        ctx: Click context with optional storage_path in obj

    Returns:
        Tuple of (cookies, csrf_token, session_id)

    Raises:
        FileNotFoundError: If auth storage not found
    """
    storage_path = ctx.obj.get("storage_path") if ctx.obj else None
    cookies = load_auth_from_storage(storage_path)
    csrf, session_id = run_async(fetch_tokens(cookies))
    return cookies, csrf, session_id


def get_auth_tokens(ctx) -> AuthTokens:
    """Get AuthTokens object from context.

    Args:
        ctx: Click context

    Returns:
        AuthTokens ready for client construction
    """
    cookies, csrf, session_id = get_client(ctx)
    return AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)


# =============================================================================
# CONTEXT MANAGEMENT
# =============================================================================


def get_current_notebook() -> str | None:
    """Get the current notebook ID from context."""
    context_file = get_context_path()
    if not context_file.exists():
        return None
    try:
        data = json.loads(context_file.read_text())
        return data.get("notebook_id")
    except (OSError, json.JSONDecodeError):
        return None


def set_current_notebook(
    notebook_id: str,
    title: str | None = None,
    is_owner: bool | None = None,
    created_at: str | None = None,
):
    """Set the current notebook context."""
    context_file = get_context_path()
    context_file.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, str | bool] = {"notebook_id": notebook_id}
    if title:
        data["title"] = title
    if is_owner is not None:
        data["is_owner"] = is_owner
    if created_at:
        data["created_at"] = created_at
    context_file.write_text(json.dumps(data, indent=2))


def clear_context():
    """Clear the current context."""
    context_file = get_context_path()
    if context_file.exists():
        context_file.unlink()


def get_current_conversation() -> str | None:
    """Get the current conversation ID from context."""
    context_file = get_context_path()
    if not context_file.exists():
        return None
    try:
        data = json.loads(context_file.read_text())
        return data.get("conversation_id")
    except (OSError, json.JSONDecodeError):
        return None


def set_current_conversation(conversation_id: str | None):
    """Set or clear the current conversation ID in context."""
    context_file = get_context_path()
    if not context_file.exists():
        return
    try:
        data = json.loads(context_file.read_text())
        if conversation_id:
            data["conversation_id"] = conversation_id
        elif "conversation_id" in data:
            del data["conversation_id"]
        context_file.write_text(json.dumps(data, indent=2))
    except (OSError, json.JSONDecodeError):
        pass


def validate_id(entity_id: str, entity_name: str = "ID") -> str:
    """Validate and normalize an entity ID.

    Args:
        entity_id: The ID to validate
        entity_name: Name for error messages (e.g., "notebook", "source")

    Returns:
        Stripped ID

    Raises:
        click.ClickException: If ID is empty or whitespace-only
    """
    if not entity_id or not entity_id.strip():
        raise click.ClickException(f"{entity_name} ID cannot be empty")
    return entity_id.strip()


def require_notebook(notebook_id: str | None) -> str:
    """Get notebook ID from argument or context, raise if neither.

    Args:
        notebook_id: Optional notebook ID from command argument

    Returns:
        Notebook ID (from argument or context), validated and stripped

    Raises:
        SystemExit: If no notebook ID available
        click.ClickException: If notebook ID is empty/whitespace
    """
    if notebook_id:
        return validate_id(notebook_id, "Notebook")
    current = get_current_notebook()
    if current:
        return validate_id(current, "Notebook")
    console.print(
        "[red]No notebook specified. Use 'notebooklm use <id>' to set context or provide notebook_id.[/red]"
    )
    raise SystemExit(1)


async def _resolve_partial_id(
    partial_id: str,
    list_fn,
    entity_name: str,
    list_command: str,
) -> str:
    """Generic partial ID resolver.

    Allows users to type partial IDs like 'abc' instead of full UUIDs.
    Matches are case-insensitive prefix matches.

    Args:
        partial_id: Full or partial ID to resolve
        list_fn: Async function that returns list of items with id/title attributes
        entity_name: Name for error messages (e.g., "notebook", "source")
        list_command: CLI command to list items (e.g., "list", "source list")

    Returns:
        Full ID of the matched item

    Raises:
        click.ClickException: If ID is empty, no match, or ambiguous match
    """
    # Validate and normalize the ID
    partial_id = validate_id(partial_id, entity_name)

    # Skip resolution for IDs that look complete (20+ chars)
    if len(partial_id) >= 20:
        return partial_id

    items = await list_fn()
    matches = [item for item in items if item.id.lower().startswith(partial_id.lower())]

    if len(matches) == 1:
        if matches[0].id != partial_id:
            title = matches[0].title or "(untitled)"
            console.print(f"[dim]Matched: {matches[0].id[:12]}... ({title})[/dim]")
        return matches[0].id
    elif len(matches) == 0:
        raise click.ClickException(
            f"No {entity_name} found starting with '{partial_id}'. "
            f"Run 'notebooklm {list_command}' to see available {entity_name}s."
        )
    else:
        lines = [f"Ambiguous ID '{partial_id}' matches {len(matches)} {entity_name}s:"]
        for item in matches[:5]:
            title = item.title or "(untitled)"
            lines.append(f"  {item.id[:12]}... {title}")
        if len(matches) > 5:
            lines.append(f"  ... and {len(matches) - 5} more")
        lines.append("\nSpecify more characters to narrow down.")
        raise click.ClickException("\n".join(lines))


async def resolve_notebook_id(client, partial_id: str) -> str:
    """Resolve partial notebook ID to full ID."""
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.notebooks.list(),
        entity_name="notebook",
        list_command="list",
    )


async def resolve_source_id(client, notebook_id: str, partial_id: str) -> str:
    """Resolve partial source ID to full ID."""
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.sources.list(notebook_id),
        entity_name="source",
        list_command="source list",
    )


async def resolve_artifact_id(client, notebook_id: str, partial_id: str) -> str:
    """Resolve partial artifact ID to full ID."""
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.artifacts.list(notebook_id),
        entity_name="artifact",
        list_command="artifact list",
    )


# =============================================================================
# ERROR HANDLING
# =============================================================================


def handle_error(e: Exception):
    """Handle and display errors consistently."""
    console.print(f"[red]Error: {e}[/red]")
    raise SystemExit(1)


def handle_auth_error(json_output: bool = False):
    """Handle authentication errors with helpful context."""
    from ..paths import get_path_info, get_storage_path

    path_info = get_path_info()
    storage_path = get_storage_path()
    has_env_var = bool(os.environ.get("NOTEBOOKLM_AUTH_JSON"))
    has_home_env = bool(os.environ.get("NOTEBOOKLM_HOME"))
    storage_source = path_info["home_source"]

    if json_output:
        json_error_response(
            "AUTH_REQUIRED",
            "Auth not found. Run 'notebooklm login' first.",
            extra={
                "checked_paths": {
                    "storage_file": str(storage_path),
                    "storage_source": storage_source,
                    "env_var": "NOTEBOOKLM_AUTH_JSON" if has_env_var else None,
                },
                "help": "Run 'notebooklm login' or set NOTEBOOKLM_AUTH_JSON",
            },
        )
    else:
        console.print("[red]Not logged in.[/red]\n")
        console.print("[dim]Checked locations:[/dim]")
        console.print(f"  • Storage file: [cyan]{storage_path}[/cyan]")
        if has_home_env:
            console.print("    [dim](via $NOTEBOOKLM_HOME)[/dim]")
        env_status = "[yellow]set but invalid[/yellow]" if has_env_var else "[dim]not set[/dim]"
        console.print(f"  • NOTEBOOKLM_AUTH_JSON: {env_status}")
        console.print("\n[bold]Options to authenticate:[/bold]")
        console.print("  1. Run: [green]notebooklm login[/green]")
        console.print("  2. Set [cyan]NOTEBOOKLM_AUTH_JSON[/cyan] env var (for CI/CD)")
        console.print("  3. Use [cyan]--storage /path/to/file.json[/cyan] flag")
        raise SystemExit(1)


# =============================================================================
# DECORATORS
# =============================================================================


def with_client(f):
    """Decorator that handles auth, async execution, and errors for CLI commands.

    This decorator eliminates boilerplate from commands that need:
    - Authentication (get AuthTokens from context)
    - Async execution (run coroutine with asyncio.run)
    - Error handling (auth errors, general exceptions)

    The decorated function stays SYNC (Click doesn't support async) but returns
    a coroutine. The decorator runs the coroutine and handles errors.

    Usage:
        @cli.command("list")
        @click.option("--json", "json_output", is_flag=True)
        @with_client
        def list_notebooks(ctx, json_output, client_auth):
            async def _run():
                async with NotebookLMClient(client_auth) as client:
                    notebooks = await client.notebooks.list()
                    output_notebooks(notebooks, json_output)
            return _run()

    Args:
        f: Function that accepts client_auth (AuthTokens) and returns a coroutine

    Returns:
        Decorated function with Click pass_context
    """

    @wraps(f)
    @click.pass_context
    def wrapper(ctx, *args, **kwargs):
        cmd_name = f.__name__
        start = time.monotonic()
        logger.debug("CLI command starting: %s", cmd_name)

        json_output = kwargs.get("json_output", False)

        def log_result(status: str, detail: str = "") -> float:
            elapsed = time.monotonic() - start
            if detail:
                logger.debug("CLI command %s: %s (%.3fs) - %s", status, cmd_name, elapsed, detail)
            else:
                logger.debug("CLI command %s: %s (%.3fs)", status, cmd_name, elapsed)
            return elapsed

        try:
            auth = get_auth_tokens(ctx)
            coro = f(ctx, *args, client_auth=auth, **kwargs)
            result = run_async(coro)
            log_result("completed")
            return result
        except FileNotFoundError:
            log_result("failed", "not authenticated")
            handle_auth_error(json_output)
        except Exception as e:
            log_result("failed", str(e))
            if json_output:
                json_error_response("ERROR", str(e))
            else:
                handle_error(e)

    return wrapper


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================


def json_output_response(data: dict) -> None:
    """Print JSON response (no colors for machine parsing)."""
    click.echo(json.dumps(data, indent=2, default=str))


def json_error_response(code: str, message: str, extra: dict | None = None) -> None:
    """Print JSON error and exit (no colors for machine parsing).

    Args:
        code: Error code (e.g., "AUTH_REQUIRED", "ERROR")
        message: Human-readable error message
        extra: Optional additional data to include in response
    """
    response = {"error": True, "code": code, "message": message}
    if extra:
        response.update(extra)
    click.echo(json.dumps(response, indent=2))
    raise SystemExit(1)


def display_research_sources(sources: list[dict], max_display: int = 10) -> None:
    """Display research sources in a formatted table.

    Args:
        sources: List of source dicts with 'title' and 'url' keys
        max_display: Maximum sources to show before truncating (default 10)
    """
    console.print(f"[bold]Found {len(sources)} sources[/bold]")

    if sources:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Title", style="cyan")
        table.add_column("URL", style="dim")
        for src in sources[:max_display]:
            table.add_row(
                src.get("title", "Untitled")[:50],
                src.get("url", "")[:60],
            )
        if len(sources) > max_display:
            table.add_row(f"... and {len(sources) - max_display} more", "")
        console.print(table)


# =============================================================================
# TYPE DISPLAY HELPERS
# =============================================================================


def get_artifact_type_display(
    artifact_type: int, variant: int | None = None, report_subtype: str | None = None
) -> str:
    """Get display string for artifact type.

    Args:
        artifact_type: StudioContentType enum value
        variant: Optional variant code (for type 4: 1=flashcards, 2=quiz)
        report_subtype: Optional report subtype (for type 2: briefing_doc, study_guide, blog_post)

    Returns:
        Display string with emoji
    """
    # Handle quiz/flashcards distinction (both use type 4)
    if artifact_type == 4 and variant is not None:
        if variant == 1:
            return "🃏 Flashcards"
        elif variant == 2:
            return "📝 Quiz"

    # Handle report subtypes (type 2)
    if artifact_type == 2 and report_subtype:
        report_displays = {
            "briefing_doc": "📋 Briefing Doc",
            "study_guide": "📚 Study Guide",
            "blog_post": "✍️ Blog Post",
            "report": "📄 Report",
        }
        return report_displays.get(report_subtype, "📄 Report")

    return ARTIFACT_TYPE_DISPLAY.get(artifact_type, f"Unknown ({artifact_type})")


def detect_source_type(src: list) -> str:
    """Detect source type from API data structure.

    Detection logic:
    - Check src[2][7] for YouTube/URL indicators
    - Check src[3][1] for type code
    - Check file size indicators at src[2][1]
    - Use title extension as fallback (.pdf, .txt, etc.)

    Returns:
        Display string with emoji (e.g., "🎥 YouTube")
    """
    # Check for URL at position [2][7] (YouTube/URL indicator)
    if len(src) > 2 and isinstance(src[2], list) and len(src[2]) > 7:
        url_field = src[2][7]
        if url_field and isinstance(url_field, list) and len(url_field) > 0:
            url = url_field[0]
            return "🎥 YouTube" if is_youtube_url(url) else "🔗 Web URL"

    # Check title for file extension
    title = src[1] if len(src) > 1 else ""
    if title:
        if title.endswith(".pdf"):
            return "📄 PDF"
        elif title.endswith((".txt", ".md", ".doc", ".docx")):
            return "📝 Text File"
        elif title.endswith((".xls", ".xlsx", ".csv")):
            return "📊 Spreadsheet"

    # Check for file size indicator (uploaded files have src[2][1] as size)
    if len(src) > 2 and isinstance(src[2], list) and len(src[2]) > 1:
        if isinstance(src[2][1], int) and src[2][1] > 0:
            return "📎 Upload"

    # Default to pasted text
    return "📝 Pasted Text"


def get_source_type_display(source_type: str) -> str:
    """Get display string for source type.

    Args:
        source_type: Type string from Source object (derived from SourceType enum)

    Returns:
        Display string with emoji
    """
    type_map = {
        # From SourceType enum via source_type_code_to_str()
        "google_docs": "📄 Google Docs",
        "google_other": "📊 Google Workspace",
        "google_spreadsheet": "📊 Google Sheets",
        "pdf": "📄 PDF",
        "pasted_text": "📝 Pasted Text",
        "docx": "📝 DOCX",
        "web_page": "🔗 Web URL",
        "markdown": "📝 Markdown",
        "youtube": "🎥 YouTube",
        "media": "🎵 Media",
        "upload": "📎 Upload",
        "image": "🖼️ Image",
        "csv": "📊 CSV",
        "unknown": "❓ Unknown",
    }
    return type_map.get(source_type, f"❓ {source_type}")
