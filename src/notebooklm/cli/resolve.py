"""CLI notebook/entity ID resolution helpers."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from ..paths import get_context_path
from . import context as context_helpers
from . import rendering as rendering_helpers

ContextPathFn = Callable[..., Path]
ListFn = Callable[[], Awaitable[list[Any]]]

# Backend entity IDs are canonical UUIDs in the RFC 4122 8-4-4-4-12 hex layout
# (e.g. ``abc12345-6789-4abc-def0-1234567890ab``). Anything shorter — even a
# unique 25-char prefix — must go through the local list-and-match path, or it
# will reach the backend as a truncated ID and 404. The character classes are
# both upper- and lower-case hex so mixed-case IDs returned by the backend keep
# fast-pathing. Exposed publicly so `download_helpers.py` shares the same shape
# rule; the two call sites stay in lockstep until P2.T1 consolidates them.
#
# Tightened past the plan's `^[0-9a-fA-F-]{36}$` to the exact 8-4-4-4-12 dash
# layout so degenerate-but-length-36 inputs (`"-" * 36`, 36 unbroken hex chars,
# wrong dash placement) cannot bypass local resolution — the looser pattern's
# false-positives would still 404 against the backend, but rejecting them
# locally gives the user a clearer error and keeps the contract honest.
FULL_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def validate_id(entity_id: str, entity_name: str = "ID") -> str:
    """Validate and normalize an entity ID.

    Args:
        entity_id: The ID to validate.
        entity_name: Name for error messages, e.g. ``"notebook"`` or
            ``"source"``.

    Returns:
        Stripped ID.

    Raises:
        click.ClickException: If ID is empty or whitespace-only.
    """
    if not entity_id or not entity_id.strip():
        raise click.ClickException(f"{entity_name} ID cannot be empty")
    return entity_id.strip()


def _is_full_id_candidate(entity_id: str) -> bool:
    """Return whether ``entity_id`` is shaped like a concrete backend UUID.

    Only canonical 36-char hex-and-dash strings (case-insensitive) qualify for
    the fast-path. A 25-char prefix of a 36-char UUID — which is unique enough
    to be human-pasted — must still go through the local list-and-match path so
    it can be expanded to the full ID before any backend call. See
    :data:`FULL_ID_PATTERN`.
    """
    return FULL_ID_PATTERN.fullmatch(entity_id) is not None


def require_notebook(
    notebook_id: str | None,
    *,
    context_path_fn: ContextPathFn | None = None,
    output_console: Console = rendering_helpers.console,
) -> str:
    """Get notebook ID from argument, env var, or active context.

    Resolution order (env-var precedence):

    1. ``notebook_id`` argument (the resolved value of the ``-n/--notebook``
       Click flag, which is already env-var-aware via ``cli/options.py``).
    2. ``NOTEBOOKLM_NOTEBOOK`` environment variable.
    3. The persisted active-notebook context written by ``notebooklm use``.
    4. Hard error with a discoverability hint listing all resolution paths.

    Args:
        notebook_id: Optional notebook ID from command argument. When the
            Click flag was omitted and the env var was unset, this is ``None``.
        context_path_fn: Context-path resolver, injectable for compatibility
            wrappers and tests. ``None`` keeps the module-level
            ``get_context_path`` lookup call-time patchable.
        output_console: Console used for the no-notebook diagnostic.

    Returns:
        Notebook ID from argument, env var, or context, validated and stripped.

    Raises:
        SystemExit: If no notebook ID can be resolved from any source.
        click.ClickException: If the resolved notebook ID is empty/whitespace
            after stripping.
    """
    if notebook_id:
        return validate_id(notebook_id, "Notebook")

    env_value = os.environ.get("NOTEBOOKLM_NOTEBOOK")
    if env_value and env_value.strip():
        return validate_id(env_value, "Notebook")

    current = context_helpers.get_current_notebook(
        context_path_fn=context_path_fn or get_context_path
    )
    if current:
        return validate_id(current, "Notebook")

    output_console.print(
        "[red]No notebook specified. Use 'notebooklm use <id>' to set context, "
        "pass -n/--notebook, or set NOTEBOOKLM_NOTEBOOK.[/red]"
    )
    raise SystemExit(1)


async def _resolve_partial_id(
    partial_id: str,
    list_fn: ListFn,
    entity_name: str,
    list_command: str,
    *,
    json_output: bool = False,
    stdout_console: Console = rendering_helpers.console,
    stderr_output_console: Console = rendering_helpers.stderr_console,
) -> str:
    """Resolve a case-insensitive partial ID prefix to a full entity ID.

    Allows users to type partial IDs like ``abc`` instead of full IDs.
    Exact matches are preferred before case-insensitive prefix matches so a
    short-but-complete ID is not treated as ambiguous when another entity
    shares that prefix.

    Args:
        partial_id: Full or partial ID to resolve.
        list_fn: Async function returning items with ``id`` and ``title``
            attributes.
        entity_name: Name for error messages, e.g. ``"notebook"``.
        list_command: CLI command to list items, e.g. ``"source list"``.
        json_output: When true, the successful "Matched..." diagnostic routes
            to stderr so stdout stays parseable JSON.
        stdout_console: Console for human-mode diagnostics.
        stderr_output_console: Console for JSON-mode diagnostics.

    Returns:
        Full ID of the matched item.

    Raises:
        click.ClickException: If ID is empty, no match exists, or the prefix is
            ambiguous.
    """
    partial_id = validate_id(partial_id, entity_name)

    # Concrete IDs are passed through so direct get/delete commands can hit
    # the backend by ID without forcing an extra list RPC first.
    if _is_full_id_candidate(partial_id):
        return partial_id

    items = await list_fn()
    partial_id_lower = partial_id.lower()

    matches = []
    for item in items:
        item_id_lower = item.id.lower()
        # Exact short IDs win over prefix matches to avoid false ambiguity.
        if item_id_lower == partial_id_lower:
            return item.id
        if item_id_lower.startswith(partial_id_lower):
            matches.append(item)

    if len(matches) == 1:
        if matches[0].id != partial_id:
            title = matches[0].title or "(untitled)"
            rendering_helpers.emit_status(
                f"[dim]Matched: {matches[0].id[:12]}... ({title})[/dim]",
                json_output=json_output,
                stdout_console=stdout_console,
                stderr_output_console=stderr_output_console,
            )
        return matches[0].id

    if len(matches) == 0:
        raise click.ClickException(
            f"No {entity_name} found starting with '{partial_id}'. "
            f"Run 'notebooklm {list_command}' to see available {entity_name}s."
        )

    lines = [f"Ambiguous ID '{partial_id}' matches {len(matches)} {entity_name}s:"]
    for item in matches[:5]:
        title = item.title or "(untitled)"
        lines.append(f"  {item.id[:12]}... {title}")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")
    lines.append("\nSpecify more characters to narrow down.")
    raise click.ClickException("\n".join(lines))


async def resolve_notebook_id(
    client,
    partial_id: str,
    *,
    json_output: bool = False,
    stdout_console: Console = rendering_helpers.console,
    stderr_output_console: Console = rendering_helpers.stderr_console,
) -> str:
    """Resolve partial notebook ID to full ID.

    When ``json_output`` is true, the successful "Matched..." diagnostic routes
    to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.notebooks.list(),
        entity_name="notebook",
        list_command="list",
        json_output=json_output,
        stdout_console=stdout_console,
        stderr_output_console=stderr_output_console,
    )


async def resolve_source_id(
    client,
    notebook_id: str,
    partial_id: str,
    *,
    json_output: bool = False,
    stdout_console: Console = rendering_helpers.console,
    stderr_output_console: Console = rendering_helpers.stderr_console,
) -> str:
    """Resolve partial source ID to full ID.

    When ``json_output`` is true, the successful "Matched..." diagnostic routes
    to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.sources.list(notebook_id),
        entity_name="source",
        list_command="source list",
        json_output=json_output,
        stdout_console=stdout_console,
        stderr_output_console=stderr_output_console,
    )


async def resolve_artifact_id(
    client,
    notebook_id: str,
    partial_id: str,
    *,
    json_output: bool = False,
    stdout_console: Console = rendering_helpers.console,
    stderr_output_console: Console = rendering_helpers.stderr_console,
) -> str:
    """Resolve partial artifact ID to full ID.

    When ``json_output`` is true, the successful "Matched..." diagnostic routes
    to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.artifacts.list(notebook_id),
        entity_name="artifact",
        list_command="artifact list",
        json_output=json_output,
        stdout_console=stdout_console,
        stderr_output_console=stderr_output_console,
    )


async def resolve_note_id(
    client,
    notebook_id: str,
    partial_id: str,
    *,
    json_output: bool = False,
    stdout_console: Console = rendering_helpers.console,
    stderr_output_console: Console = rendering_helpers.stderr_console,
) -> str:
    """Resolve partial note ID to full ID.

    When ``json_output`` is true, the successful "Matched..." diagnostic routes
    to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.notes.list(notebook_id),
        entity_name="note",
        list_command="note list",
        json_output=json_output,
        stdout_console=stdout_console,
        stderr_output_console=stderr_output_console,
    )


async def resolve_source_ids(
    client,
    notebook_id: str,
    source_ids: tuple[str, ...],
    *,
    json_output: bool = False,
    stdout_console: Console = rendering_helpers.console,
    stderr_output_console: Console = rendering_helpers.stderr_console,
) -> list[str] | None:
    """Resolve multiple partial source IDs to full IDs.

    Args:
        client: NotebookLM client.
        notebook_id: Resolved notebook ID.
        source_ids: Tuple of partial source IDs from CLI.
        json_output: When true, "Matched..." diagnostics for partial matches
            route to stderr so stdout stays parseable JSON.
        stdout_console: Console for human-mode diagnostics.
        stderr_output_console: Console for JSON-mode diagnostics.

    Returns:
        List of resolved source IDs, or ``None`` if no source IDs were provided.
    """
    if not source_ids:
        return None

    validated_source_ids = tuple(validate_id(source_id, "source") for source_id in source_ids)
    if all(_is_full_id_candidate(source_id) for source_id in validated_source_ids):
        return list(validated_source_ids)

    sources = await client.sources.list(notebook_id)

    async def list_sources():
        return sources

    unique_source_ids = tuple(dict.fromkeys(validated_source_ids))
    resolved_unique = await asyncio.gather(
        *(
            _resolve_partial_id(
                source_id,
                list_fn=list_sources,
                entity_name="source",
                list_command="source list",
                json_output=json_output,
                stdout_console=stdout_console,
                stderr_output_console=stderr_output_console,
            )
            for source_id in unique_source_ids
        )
    )
    resolved_by_input = dict(zip(unique_source_ids, resolved_unique, strict=True))
    return [resolved_by_input[source_id] for source_id in validated_source_ids]
