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
from . import input as input_helpers
from . import rendering as rendering_helpers
from . import research_import as research_import_helpers
from . import resolve as resolve_helpers
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
    rendering_helpers.emit_status(
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
    """Validate and normalize an entity ID."""
    return resolve_helpers.validate_id(entity_id, entity_name)


def require_notebook(notebook_id: str | None) -> str:
    """Get notebook ID from argument, env var, or active context."""
    return resolve_helpers.require_notebook(
        notebook_id,
        context_path_fn=get_context_path,
        output_console=console,
    )


async def _resolve_partial_id(
    partial_id: str,
    list_fn,
    entity_name: str,
    list_command: str,
    *,
    json_output: bool = False,
) -> str:
    """Generic partial ID resolver."""
    return await resolve_helpers._resolve_partial_id(
        partial_id,
        list_fn,
        entity_name,
        list_command,
        json_output=json_output,
        stdout_console=console,
        stderr_output_console=stderr_console,
    )


async def resolve_notebook_id(client, partial_id: str, *, json_output: bool = False) -> str:
    """Resolve partial notebook ID to full ID."""
    return await resolve_helpers.resolve_notebook_id(
        client,
        partial_id,
        json_output=json_output,
        stdout_console=console,
        stderr_output_console=stderr_console,
    )


async def resolve_source_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial source ID to full ID."""
    return await resolve_helpers.resolve_source_id(
        client,
        notebook_id,
        partial_id,
        json_output=json_output,
        stdout_console=console,
        stderr_output_console=stderr_console,
    )


async def resolve_artifact_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial artifact ID to full ID."""
    return await resolve_helpers.resolve_artifact_id(
        client,
        notebook_id,
        partial_id,
        json_output=json_output,
        stdout_console=console,
        stderr_output_console=stderr_console,
    )


async def resolve_note_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial note ID to full ID."""
    return await resolve_helpers.resolve_note_id(
        client,
        notebook_id,
        partial_id,
        json_output=json_output,
        stdout_console=console,
        stderr_output_console=stderr_console,
    )


async def resolve_source_ids(
    client,
    notebook_id: str,
    source_ids: tuple[str, ...],
    *,
    json_output: bool = False,
) -> list[str] | None:
    """Resolve multiple partial source IDs to full IDs."""
    return await resolve_helpers.resolve_source_ids(
        client,
        notebook_id,
        source_ids,
        json_output=json_output,
        stdout_console=console,
        stderr_output_console=stderr_console,
    )


def read_stdin_text(*, source_label: str = "stdin") -> str:
    """Read all of stdin as UTF-8 text and strip surrounding whitespace."""
    return input_helpers.read_stdin_text(source_label=source_label)


def resolve_prompt(
    argument_value: str | None,
    prompt_file: str | None,
    param_name: str = "prompt",
    *,
    required: bool = False,
) -> str:
    """Resolve prompt text from a positional argument or ``--prompt-file``."""
    return input_helpers.resolve_prompt(
        argument_value,
        prompt_file,
        param_name,
        required=required,
    )


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
