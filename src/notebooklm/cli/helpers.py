"""CLI helper utilities.

Provides common functionality for all CLI commands:
- Compatibility re-exports for runtime/auth helpers
- Error handling
- JSON/Rich output formatting
- Context management (current notebook/conversation)

This module is also the backward-compatible facade for older imports and test
patch targets; see ``cli.runtime``, ``cli.auth_runtime``, ``cli.context``, and
``cli.rendering`` for canonical helpers.
"""

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar

import click

from .. import auth as auth_helpers
from ..auth import AuthTokens
from ..paths import get_context_path
from ..types import ArtifactType
from . import auth_runtime as auth_runtime_helpers
from . import context as context_helpers
from . import rendering as rendering_helpers
from . import research_import as research_import_helpers
from . import runtime as runtime_helpers
from ._encoding import safe_echo

if TYPE_CHECKING:
    from ..types import Artifact

console = rendering_helpers.console
stderr_console = rendering_helpers.stderr_console
logger = logging.getLogger(__name__)
T = TypeVar("T")
ResearchImportResult = research_import_helpers.ResearchImportResult


def build_cookie_jar(*args: Any, **kwargs: Any) -> Any:
    """Compatibility patch target for auth cookie-jar construction."""
    return auth_helpers.build_cookie_jar(*args, **kwargs)


def load_auth_from_storage(*args: Any, **kwargs: Any) -> Any:
    """Compatibility patch target for auth storage loading."""
    return auth_helpers.load_auth_from_storage(*args, **kwargs)


def emit_status(msg: str, *, json_output: bool, style: str | None = None) -> None:
    """Emit a status / diagnostic line."""
    rendering_helpers._emit_status(
        msg,
        json_output=json_output,
        style=style,
        stdout_console=console,
        stderr_output_console=stderr_console,
    )


def cli_name_to_artifact_type(name: str) -> ArtifactType | None:
    """Convert CLI artifact type name to ArtifactType enum."""
    return rendering_helpers.cli_name_to_artifact_type(name)


# =============================================================================
# ASYNC EXECUTION
# =============================================================================


def run_async(coro):
    """Run async coroutine in sync context."""
    return runtime_helpers.run_async(coro)


async def import_with_retry(
    client,
    notebook_id: str,
    task_id: str,
    sources: list[dict],
    *,
    max_elapsed: float = 1800,
    initial_delay: float = 5,
    backoff_factor: float = 2,
    max_delay: float = 60,
    json_output: bool = False,
) -> list[dict[str, str]]:
    """Compatibility wrapper for :func:`cli.research_import.import_with_retry`."""
    return await research_import_helpers.import_with_retry(
        client,
        notebook_id,
        task_id,
        sources,
        max_elapsed=max_elapsed,
        initial_delay=initial_delay,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
        json_output=json_output,
        output_console=console,
    )


def _display_cited_import_selection(
    cited_selection: research_import_helpers.CitedSourceSelection | None,
) -> None:
    """Compatibility wrapper for the research import cited-source display."""
    research_import_helpers._display_cited_import_selection(
        cited_selection,
        output_console=console,
    )


async def import_research_sources(
    client,
    notebook_id: str,
    task_id: str,
    sources: list[dict],
    *,
    report: str = "",
    cited_only: bool = False,
    max_elapsed: float = 1800,
    json_output: bool = False,
    status_message: str | None = None,
) -> research_import_helpers.ResearchImportResult:
    """Compatibility wrapper for :func:`cli.research_import.import_research_sources`."""
    return await research_import_helpers.import_research_sources(
        client,
        notebook_id,
        task_id,
        sources,
        report=report,
        cited_only=cited_only,
        max_elapsed=max_elapsed,
        json_output=json_output,
        status_message=status_message,
        import_func=import_with_retry,
        output_console=console,
    )


# =============================================================================
# AUTHENTICATION
# =============================================================================


def get_client(ctx) -> tuple[dict, str, str]:
    """Get auth components from context."""
    return auth_runtime_helpers.get_client(ctx)


def get_auth_tokens(ctx) -> AuthTokens:
    """Get AuthTokens object from context."""
    return auth_runtime_helpers.get_auth_tokens(ctx)


# =============================================================================
# CONTEXT MANAGEMENT
# =============================================================================


def _current_storage_override() -> Path | None:
    """Resolve the active ``--storage`` override from the current Click context."""
    return context_helpers._current_storage_override()


def _get_context_value(key: str) -> str | None:
    """Read a single value from context.json."""
    return context_helpers._get_context_value(key, context_path_fn=get_context_path)


def _set_context_value(key: str, value: str | None) -> None:
    """Set or clear a single value in context.json."""
    context_helpers._set_context_value(key, value, context_path_fn=get_context_path)


def get_current_notebook() -> str | None:
    """Get the current notebook ID from context."""
    return context_helpers.get_current_notebook(context_path_fn=get_context_path)


def set_current_notebook(
    notebook_id: str,
    title: str | None = None,
    is_owner: bool | None = None,
    created_at: str | None = None,
):
    """Set the current notebook context."""
    context_helpers.set_current_notebook(
        notebook_id,
        title=title,
        is_owner=is_owner,
        created_at=created_at,
        context_path_fn=get_context_path,
    )


def clear_context(*, clear_account: bool = False) -> bool:
    """Clear the current context.

    By default, only notebook/conversation fields are cleared; account
    metadata used for multi-account auth routing is preserved. ``auth logout``
    passes ``clear_account=True`` to remove the whole file.

    Returns True if a context file was changed or removed, False if none
    existed or no clearable fields were present.
    """
    return context_helpers.clear_context(
        clear_account=clear_account, context_path_fn=get_context_path
    )


def get_current_conversation() -> str | None:
    """Get the current conversation ID from context."""
    return context_helpers.get_current_conversation(context_path_fn=get_context_path)


def set_current_conversation(conversation_id: str | None):
    """Set or clear the current conversation ID in context."""
    context_helpers.set_current_conversation(conversation_id, context_path_fn=get_context_path)


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
    """Get notebook ID from argument, env var, or active context.

    Resolution order (env-var precedence):

    1. ``notebook_id`` argument (the resolved value of the ``-n/--notebook``
       Click flag — already env-var-aware via ``cli/options.py:notebook_option``,
       which declares ``envvar="NOTEBOOKLM_NOTEBOOK"``).
    2. ``NOTEBOOKLM_NOTEBOOK`` environment variable. Re-checked here so direct
       callers that don't pass through the Click flag (programmatic usage,
       legacy code paths, tests) honor the same precedence ladder.
    3. The persisted active-notebook context written by ``notebooklm use``.
    4. Hard error → ``SystemExit(1)`` with a discoverability hint listing all
       three resolution paths.

    Args:
        notebook_id: Optional notebook ID from command argument. When the
            Click flag was omitted AND the env var was unset, this is ``None``.

    Returns:
        Notebook ID (from argument, env var, or context), validated and stripped.

    Raises:
        SystemExit: If no notebook ID can be resolved from any source.
        click.ClickException: If the resolved notebook ID is empty/whitespace
            after stripping.
    """
    if notebook_id:
        return validate_id(notebook_id, "Notebook")
    # Env-var fallback runs BEFORE the active-context lookup so per-shell
    # overrides (e.g. ``NOTEBOOKLM_NOTEBOOK=other notebooklm ask "..."``)
    # compose without clobbering the persisted ``notebooklm use`` selection.
    # Empty / whitespace-only values are treated as unset (consistent with
    # ``NOTEBOOKLM_HL``'s same-shape handling) — the next fallback wins.
    env_value = os.environ.get("NOTEBOOKLM_NOTEBOOK")
    if env_value and env_value.strip():
        return validate_id(env_value, "Notebook")
    current = get_current_notebook()
    if current:
        return validate_id(current, "Notebook")
    console.print(
        "[red]No notebook specified. Use 'notebooklm use <id>' to set context, "
        "pass -n/--notebook, or set NOTEBOOKLM_NOTEBOOK.[/red]"
    )
    raise SystemExit(1)


async def _resolve_partial_id(
    partial_id: str,
    list_fn,
    entity_name: str,
    list_command: str,
    *,
    json_output: bool = False,
) -> str:
    """Generic partial ID resolver.

    Allows users to type partial IDs like 'abc' instead of full UUIDs.
    Matches are case-insensitive prefix matches.

    Args:
        partial_id: Full or partial ID to resolve
        list_fn: Async function that returns list of items with id/title attributes
        entity_name: Name for error messages (e.g., "notebook", "source")
        list_command: CLI command to list items (e.g., "list", "source list")
        json_output: When True, the "Matched..." diagnostic is routed to stderr
            via ``emit_status`` so stdout stays parseable JSON.

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
            emit_status(
                f"[dim]Matched: {matches[0].id[:12]}... ({title})[/dim]",
                json_output=json_output,
            )
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


async def resolve_notebook_id(client, partial_id: str, *, json_output: bool = False) -> str:
    """Resolve partial notebook ID to full ID.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.notebooks.list(),
        entity_name="notebook",
        list_command="list",
        json_output=json_output,
    )


async def resolve_source_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial source ID to full ID.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.sources.list(notebook_id),
        entity_name="source",
        list_command="source list",
        json_output=json_output,
    )


async def resolve_artifact_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial artifact ID to full ID.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.artifacts.list(notebook_id),
        entity_name="artifact",
        list_command="artifact list",
        json_output=json_output,
    )


async def resolve_note_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial note ID to full ID.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.notes.list(notebook_id),
        entity_name="note",
        list_command="note list",
        json_output=json_output,
    )


async def resolve_source_ids(
    client,
    notebook_id: str,
    source_ids: tuple[str, ...],
    *,
    json_output: bool = False,
) -> list[str] | None:
    """Resolve multiple partial source IDs to full IDs.

    Args:
        client: NotebookLM client
        notebook_id: Resolved notebook ID
        source_ids: Tuple of partial source IDs from CLI
        json_output: When True, "Matched..." diagnostics for partial matches
            are routed to stderr so stdout stays parseable JSON.

    Returns:
        List of resolved source IDs, or None if no source IDs provided
    """
    if not source_ids:
        return None
    resolved = []
    for sid in source_ids:
        resolved.append(await resolve_source_id(client, notebook_id, sid, json_output=json_output))
    return resolved


def read_stdin_text(*, source_label: str = "stdin") -> str:
    """Read all of stdin as UTF-8 text and strip surrounding whitespace.

    Centralizes the Unix ``-`` (stdin) convention used by ``ask``, ``note
    create``, ``source add``, and ``--prompt-file -``. Uses
    ``click.get_text_stream("stdin").read()`` so ``CliRunner.invoke(input=...)``
    in tests is honored without monkey-patching ``sys.stdin``.

    Args:
        source_label: Label used in error messages (e.g. ``"prompt file"``)
            so the failure mode tells the user which input was empty.

    Raises:
        click.ClickException: stdin yields a non-UTF-8 byte sequence.
    """
    try:
        text = click.get_text_stream("stdin").read()
    except UnicodeDecodeError as e:
        raise click.ClickException(f"{source_label} (stdin) is not valid UTF-8: {e}") from e
    return text.strip()


def resolve_prompt(
    argument_value: str | None,
    prompt_file: str | None,
    param_name: str = "prompt",
    *,
    required: bool = False,
) -> str:
    """Resolve prompt text from a positional argument or ``--prompt-file``.

    Exactly one source may be provided. The file is read as UTF-8 with surrounding
    whitespace stripped. When ``required`` is True and neither source yields
    text, a ``UsageError`` is raised; otherwise an empty string is returned.

    The literal ``-`` is recognized as "read stdin" for either source,
    matching the Unix convention.

    Args:
        argument_value: Value of the positional CLI argument (may be empty).
        prompt_file: Path passed via ``--prompt-file`` (may be ``None``).
        param_name: Name of the positional argument, used in error messages.
        required: When True, raise ``UsageError`` if both sources are empty.

    Raises:
        click.UsageError: Both sources provided, or ``required`` and both empty.
        click.ClickException: Prompt file unreadable or not valid UTF-8.
    """
    if argument_value and prompt_file:
        raise click.UsageError(
            f"Cannot use both the {param_name} argument and --prompt-file. Choose one."
        )

    if prompt_file == "-" or argument_value == "-":
        # Unix ``-`` convention: read text from stdin. The label hints which
        # input is the empty one if the required check fires below.
        label = "prompt file" if prompt_file == "-" else param_name
        text = read_stdin_text(source_label=label)
    elif prompt_file:
        path = Path(prompt_file)
        if not path.is_file():
            raise click.ClickException(f"Prompt file '{prompt_file}' is not a regular file.")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise click.ClickException(f"Failed to read prompt file '{prompt_file}': {e}") from e
        except UnicodeDecodeError as e:
            raise click.ClickException(
                f"Prompt file '{prompt_file}' is not valid UTF-8: {e}"
            ) from e
    else:
        text = argument_value or ""

    if required and not text:
        raise click.UsageError(f"Provide a {param_name} argument or --prompt-file.")
    return text


# =============================================================================
# ERROR HANDLING
# =============================================================================


def handle_error(e: Exception):
    """Handle and display errors consistently."""
    message = f"Error: {e}"
    try:
        console.print(f"[red]{message}[/red]")
    except UnicodeEncodeError:
        safe_echo(message, err=True)
    raise SystemExit(1)


def handle_auth_error(json_output: bool = False) -> NoReturn:
    """Handle authentication errors with helpful context."""
    auth_runtime_helpers.handle_auth_error(json_output)


# =============================================================================
# DECORATORS
# =============================================================================


def with_auth_and_errors(
    ctx: click.Context,
    *,
    command_name: str,
    json_output: bool,
    body: Callable[[AuthTokens], Awaitable[T]],
    auth_loader: Callable[[click.Context], AuthTokens] | None = None,
) -> T:
    """Run a CLI command body with shared auth bootstrap and error handling."""
    return auth_runtime_helpers.with_auth_and_errors(
        ctx,
        command_name=command_name,
        json_output=json_output,
        body=body,
        auth_loader=auth_loader,
    )


def with_client(f):
    """Decorator that handles auth, async execution, and errors for CLI commands."""
    return auth_runtime_helpers.with_client(f)


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================


def json_output_response(data: dict | list) -> None:
    """Print JSON response (no colors for machine parsing)."""
    rendering_helpers.json_output_response(data)


def json_error_response(code: str, message: str, extra: dict | None = None) -> NoReturn:
    """Print JSON error and exit (no colors for machine parsing)."""
    rendering_helpers.json_error_response(code, message, extra)


def display_research_sources(sources: list[dict], max_display: int = 10) -> None:
    """Display research sources in a formatted table."""
    rendering_helpers._display_research_sources(
        sources, max_display=max_display, output_console=console
    )


def display_report(report: str, max_chars: int = 1000, json_hint: bool = True) -> None:
    """Display a research report, truncated for terminal output."""
    rendering_helpers._display_report(
        report, max_chars=max_chars, json_hint=json_hint, output_console=console
    )


# =============================================================================
# TYPE DISPLAY HELPERS
# =============================================================================


def get_artifact_type_display(artifact: "Artifact") -> str:
    """Get display string for artifact type."""
    return rendering_helpers.get_artifact_type_display(artifact)


def get_source_type_display(source_type: str) -> str:
    """Get display string for source type."""
    return rendering_helpers.get_source_type_display(source_type)
