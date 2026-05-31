"""CLI notebook/entity ID resolution helpers."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from .. import paths as paths_module
from ..paths import get_context_path
from . import context as context_helpers
from . import rendering as rendering_helpers
from .error_handler import exit_with_code

ContextPathFn = Callable[..., Path]
ListFn = Callable[[], Awaitable[list[Any]]]

# Backend entity IDs are canonical UUIDs in the RFC 4122 8-4-4-4-12 hex layout
# (e.g. ``abc12345-6789-4abc-def0-1234567890ab``). Anything shorter - even a
# unique 25-char prefix - must go through the local list-and-match path, or it
# will reach the backend as a truncated ID and 404. The character classes are
# both upper- and lower-case hex so mixed-case IDs returned by the backend keep
# fast-pathing. Exposed publicly because the canonical sync resolver core and
# the download helper share the same shape rule.
#
# Tightened past the plan's `^[0-9a-fA-F-]{36}$` to the exact 8-4-4-4-12 dash
# layout so degenerate-but-length-36 inputs (`"-" * 36`, 36 unbroken hex chars,
# wrong dash placement) cannot bypass local resolution - the looser pattern's
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
        raise click.ClickException(  # cli-input-validation: entity ID argument validation
            f"{entity_name} ID cannot be empty"
        )
    return entity_id.strip()


def _is_full_id_candidate(entity_id: str) -> bool:
    """Return whether ``entity_id`` is shaped like a concrete backend UUID.

    Only canonical 36-char hex-and-dash strings (case-insensitive) qualify for
    the fast-path. A 25-char prefix of a 36-char UUID - which is unique enough
    to be human-pasted - must still go through the local list-and-match path so
    it can be expanded to the full ID before any backend call. See
    :data:`FULL_ID_PATTERN`.
    """
    return FULL_ID_PATTERN.fullmatch(entity_id) is not None


def _helpers_attr(name: str) -> Any | None:
    """Return a patched ``cli.helpers`` attribute when the facade is imported."""
    helpers_module = sys.modules.get("notebooklm.cli.helpers")
    if helpers_module is None:
        return None
    return getattr(helpers_module, name, None)


def _default_context_path_fn() -> ContextPathFn:
    """Resolve the call-time context path provider.

    ``cli.helpers`` is now a compatibility facade for resolver names, but many
    existing tests still patch ``notebooklm.cli.helpers.get_context_path``.
    Prefer that patched facade value when present; otherwise use this module's
    own call-time lookup so ``notebooklm.cli.resolve.get_context_path`` patches
    continue to work.
    """
    helpers_get_context_path = _helpers_attr("get_context_path")
    if (
        callable(helpers_get_context_path)
        and helpers_get_context_path is not paths_module.get_context_path
    ):
        return helpers_get_context_path
    return get_context_path


def _default_stdout_console(console: Console | None) -> Console:
    if console is not None:
        return console
    helpers_console = _helpers_attr("console")
    if helpers_console is not None and helpers_console is not rendering_helpers.console:
        return helpers_console
    return rendering_helpers.console


def _default_stderr_console(console: Console | None) -> Console:
    if console is not None:
        return console
    helpers_console = _helpers_attr("stderr_console")
    if helpers_console is not None and helpers_console is not rendering_helpers.stderr_console:
        return helpers_console
    return rendering_helpers.stderr_console


def require_notebook(
    notebook_id: str | None,
    *,
    context_path_fn: ContextPathFn | None = None,
    output_console: Console | None = None,
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
        context_path_fn=context_path_fn or _default_context_path_fn()
    )
    if current:
        return validate_id(current, "Notebook")

    output_console = _default_stdout_console(output_console)
    output_console.print(
        "[red]No notebook specified. Use 'notebooklm use <id>' to set context, "
        "pass -n/--notebook, or set NOTEBOOKLM_NOTEBOOK.[/red]"
    )
    exit_with_code(1)


# Accessor types for the sync resolver core. The default ``_attr_id`` /
# ``_attr_title`` accessors match the async ``_resolve_partial_id`` shape
# (entities with ``.id`` / ``.title`` attributes); ``resolve_partial_id_in_items``
# accepts custom accessors so callers with pre-fetched ``dict``-shaped data
# (e.g. ``download_helpers.resolve_partial_artifact_id``) can share the same
# matching logic without first reshaping their inputs.
ItemIdFn = Callable[[Any], str]
ItemTitleFn = Callable[[Any], str | None]
ErrorFactoryFn = Callable[[str], Exception]


def _attr_id(item: Any) -> str:
    return item.id


def _attr_title(item: Any) -> str | None:
    return item.title


def resolve_partial_id_in_items(
    partial_id: str,
    items: list[Any],
    *,
    entity_name: str,
    list_command: str,
    id_of: ItemIdFn = _attr_id,
    title_of: ItemTitleFn = _attr_title,
    error_factory: ErrorFactoryFn = click.ClickException,
    emit_match_status: bool = True,
    json_output: bool = False,
    stdout_console: Console | None = None,
    stderr_output_console: Console | None = None,
    allow_full_id_passthrough: bool = True,
) -> str:
    """Resolve a partial ID against a **pre-fetched** item list.

    Sync core of the partial-ID matching logic. Encapsulates the rules that
    both the async resolver (``_resolve_partial_id``) and the download
    pre-fetched-list resolver (``download_helpers.resolve_partial_artifact_id``)
    share, so behavior cannot drift between the two call paths.

    Matching rules (in order):

    1. Empty/whitespace partial id -> ``error_factory("...cannot be empty")``.
    2. Canonical 36-char 8-4-4-4-12 hex+dash UUID -> returned verbatim (no
       listing required); see :data:`FULL_ID_PATTERN`.
    3. Case-insensitive exact match against any item id -> returned (wins over
       prefix ambiguity so a short-but-complete id isn't reported as
       ambiguous when it's also a prefix of another item).
    4. Case-insensitive prefix match: unique -> return; ambiguous -> raise via
       ``error_factory`` with up to 5 candidates listed; no match -> raise.

    Args:
        partial_id: Full or partial ID to resolve.
        items: Pre-fetched list of items the caller already loaded from the
            backend. Each item is opaque; ``id_of`` and ``title_of``
            accessors extract the relevant fields.
        entity_name: Name for error messages, e.g. ``"notebook"``,
            ``"artifact"``.
        list_command: CLI command users should run to discover items, e.g.
            ``"source list"`` or ``"artifact list"``. Included in the
            "no match" error message.
        id_of: Accessor returning the canonical id for an item. Defaults to
            ``item.id`` (attribute access) for the dataclass-style items the
            async path consumes; the download pre-fetched-list path passes
            ``lambda a: a["id"]`` for its ``ArtifactDict`` shape.
        title_of: Accessor returning the title for diagnostics. Same default
            as ``id_of``.
        error_factory: Exception class to raise on empty input, no match,
            and ambiguous match. ``click.ClickException`` for the async
            CLI path (auto-converted to exit 1 + stderr by Click);
            ``ValueError`` for callers like ``download_helpers`` that catch
            and re-shape the error themselves.
        emit_match_status: Whether a successful partial match should emit the
            "Matched: ..." diagnostic. Async CLI resolvers use the default;
            pre-fetched helpers that historically returned silently can turn it
            off while sharing the same matching rules.
        json_output: When true, the "Matched: ..." diagnostic routes to stderr
            so stdout stays parseable JSON.
        stdout_console: Console for human-mode diagnostics.
        stderr_output_console: Console for JSON-mode diagnostics.
        allow_full_id_passthrough: When true (default), a canonical
            8-4-4-4-12 UUID is returned verbatim without scanning
            ``items``. Callers that have already fetched the
            authoritative item list (e.g. download helpers) pass
            ``False`` so a full-shape ID that isn't in the list raises
            the canonical "not found" error instead of silently
            propagating to a backend 404.

    Returns:
        Full ID of the matched item.

    Raises:
        ``error_factory(...)``: If ID is empty, no match exists, or the
            prefix is ambiguous. The exception **type** is determined by
            ``error_factory`` so callers can pick the shape that fits their
            error-handling layer.
    """
    # ``validate_id`` raises ``click.ClickException`` unconditionally; for
    # callers that asked for a different ``error_factory`` (e.g. ``ValueError``
    # in the download path), we re-shape the empty-input error here so the
    # caller doesn't have to know about ``click.ClickException`` at all.
    if not partial_id or not partial_id.strip():
        raise error_factory(f"{entity_name} ID cannot be empty")
    partial_id = partial_id.strip()

    # Concrete IDs are passed through so direct get/delete commands can hit
    # the backend by ID without forcing an extra list RPC first. Callers
    # that already hold an authoritative item list (download helpers) opt
    # out via ``allow_full_id_passthrough=False`` so a full-shape ID that
    # isn't in the list falls through to the membership check below and
    # surfaces the canonical "not found" error rather than a silent 404.
    if allow_full_id_passthrough and _is_full_id_candidate(partial_id):
        return partial_id

    partial_id_lower = partial_id.lower()

    matches: list[Any] = []
    for item in items:
        item_id = id_of(item)
        item_id_lower = item_id.lower()
        # Exact short IDs win over prefix matches to avoid false ambiguity.
        if item_id_lower == partial_id_lower:
            return item_id
        if item_id_lower.startswith(partial_id_lower):
            matches.append(item)

    if len(matches) == 1:
        matched_id = id_of(matches[0])
        if emit_match_status and matched_id != partial_id:
            title = title_of(matches[0]) or "(untitled)"
            rendering_helpers.emit_status(
                f"[dim]Matched: {matched_id[:12]}... ({title})[/dim]",
                json_output=json_output,
                stdout_console=_default_stdout_console(stdout_console),
                stderr_output_console=_default_stderr_console(stderr_output_console),
            )
        return matched_id

    if len(matches) == 0:
        raise error_factory(
            f"No {entity_name} found starting with '{partial_id}'. "
            f"Run 'notebooklm {list_command}' to see available {entity_name}s."
        )

    lines = [f"Ambiguous ID '{partial_id}' matches {len(matches)} {entity_name}s:"]
    for item in matches[:5]:
        matched_id = id_of(item)
        title = title_of(item) or "(untitled)"
        lines.append(f"  {matched_id[:12]}... {title}")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")
    lines.append("\nSpecify more characters to narrow down.")
    raise error_factory("\n".join(lines))


async def _resolve_partial_id(
    partial_id: str,
    list_fn: ListFn,
    entity_name: str,
    list_command: str,
    *,
    json_output: bool = False,
    stdout_console: Console | None = None,
    stderr_output_console: Console | None = None,
) -> str:
    """Resolve a case-insensitive partial ID prefix to a full entity ID.

    Allows users to type partial IDs like ``abc`` instead of full IDs.
    Exact matches are preferred before case-insensitive prefix matches so a
    short-but-complete ID is not treated as ambiguous when another entity
    shares that prefix.

    Thin async adapter over :func:`resolve_partial_id_in_items` - handles
    the full-ID fast-path locally to avoid a wasted ``await list_fn()`` for
    canonical UUIDs, then defers to the sync core for the prefix-matching
    work so behavior stays in lockstep with
    ``download_helpers.resolve_partial_artifact_id``.

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
    # Validate + fast-path BEFORE awaiting ``list_fn`` so canonical UUIDs
    # don't pay for a backend listing they don't need.
    partial_id = validate_id(partial_id, entity_name)
    if _is_full_id_candidate(partial_id):
        return partial_id

    items = await list_fn()
    return resolve_partial_id_in_items(
        partial_id,
        items,
        entity_name=entity_name,
        list_command=list_command,
        # async path uses the attribute-style accessors (default).
        json_output=json_output,
        stdout_console=stdout_console,
        stderr_output_console=stderr_output_console,
        # async path keeps ``click.ClickException`` (Click -> exit 1 + stderr).
    )


async def resolve_notebook_id(
    client,
    partial_id: str,
    *,
    json_output: bool = False,
    stdout_console: Console | None = None,
    stderr_output_console: Console | None = None,
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
    stdout_console: Console | None = None,
    stderr_output_console: Console | None = None,
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
    stdout_console: Console | None = None,
    stderr_output_console: Console | None = None,
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
    stdout_console: Console | None = None,
    stderr_output_console: Console | None = None,
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
    stdout_console: Console | None = None,
    stderr_output_console: Console | None = None,
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
