"""Services for source-mutation commands: delete, delete-by-title, rename, refresh, add-drive.

Each command has its own plan + executor pair. The shared resolver
helpers (``resolve_source_for_delete``, ``resolve_source_by_exact_title``,
``require_yes_in_json``) also live here because they are mutation-specific
and were previously private helpers on ``cli/source_cmd.py``. The
:class:`MutationPlan` pipeline from
``cli/services/confirming_mutation.py`` handles the resolve → confirm →
execute → serialize flow for the destructive paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import click

from ...types import DriveMimeType
from ..error_handler import output_error
from ..rendering import (
    cli_print,
    cli_status,
    console,
    emit_status,
    json_output_response,
)
from ..resolve import resolve_source_id, validate_id
from .confirming_mutation import MutationPlan, run_confirmed_mutation
from .source_serializers import source_summary_payload

if TYPE_CHECKING:
    from ...client import NotebookLMClient

DriveMimeChoice = Literal["google-doc", "google-slides", "google-sheets", "pdf"]


# ---------------------------------------------------------------------------
# Shared helpers for source-id resolution (moved out of cli/source_cmd.py)
# ---------------------------------------------------------------------------


def build_id_ambiguity_error(source_id: str, matches) -> str:
    """Build a consistent ambiguity error for source ID prefix matches."""
    lines = [f"Ambiguous ID '{source_id}' matches {len(matches)} sources:"]
    for item in matches[:5]:
        title = item.title or "(untitled)"
        lines.append(f"  {item.id[:12]}... {title}")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")
    lines.append("Specify more characters to narrow down.")
    return "\n".join(lines)


def looks_like_full_source_id(source_id: str) -> bool:
    """Return True for UUID-shaped source IDs that can skip list-based resolution."""
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            source_id,
        )
    )


async def resolve_source_for_delete(
    client, notebook_id: str, source_id: str, *, json_output: bool = False
) -> str:
    """Resolve a source ID for delete, returning the full source ID string.

    Canonical UUIDs take a fast path and skip the live source list
    lookup. Partial IDs are resolved against the live list. When
    ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    source_id = validate_id(source_id, "source")
    if looks_like_full_source_id(source_id):
        return source_id

    sources = await client.sources.list(notebook_id)
    matches = [item for item in sources if item.id.lower().startswith(source_id.lower())]

    if len(matches) == 1:
        if matches[0].id != source_id:
            title = matches[0].title or "(untitled)"
            emit_status(
                f"[dim]Matched: {matches[0].id[:12]}... ({title})[/dim]",
                json_output=json_output,
            )
        return matches[0].id

    if len(matches) > 1:
        output_error(
            build_id_ambiguity_error(source_id, matches),
            "AMBIGUOUS_ID",
            json_output,
            1,
        )
        raise AssertionError("unreachable")  # pragma: no cover

    title_matches = [item for item in sources if item.title == source_id]
    if title_matches:
        lines = [
            f"'{source_id}' matches {len(title_matches)} source title(s), not source IDs.",
            f"Use 'notebooklm source delete-by-title \"{source_id}\"' or delete by ID:",
        ]
        for item in title_matches[:5]:
            lines.append(f"  {item.id[:12]}... {item.title}")
        if len(title_matches) > 5:
            lines.append(f"  ... and {len(title_matches) - 5} more")
        output_error("\n".join(lines), "VALIDATION_ERROR", json_output, 1)
        raise AssertionError("unreachable")  # pragma: no cover

    output_error(
        f"No source found starting with '{source_id}'. "
        "Run 'notebooklm source list' to see available sources.",
        "NOT_FOUND",
        json_output,
        1,
    )
    raise AssertionError("unreachable")  # pragma: no cover


async def resolve_source_by_exact_title(
    client, notebook_id: str, title: str, *, json_output: bool = False
):
    """Resolve a source by exact title for the explicit delete-by-title flow."""
    title = validate_id(title, "source title")
    sources = await client.sources.list(notebook_id)
    matches = [item for item in sources if item.title == title]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        lines = [f"Title '{title}' matches {len(matches)} sources. Delete by ID instead:"]
        for item in matches[:5]:
            lines.append(f"  {item.id[:12]}... {item.title}")
        if len(matches) > 5:
            lines.append(f"  ... and {len(matches) - 5} more")
        output_error("\n".join(lines), "AMBIGUOUS_TITLE", json_output, 1)

    output_error(
        f"No source found with title '{title}'. "
        "Run 'notebooklm source list' to see available sources.",
        "NOT_FOUND",
        json_output,
        1,
    )
    raise AssertionError("unreachable")  # pragma: no cover


def require_yes_in_json(*, action: str, extra: dict[str, Any] | None = None) -> None:
    """Emit a structured ``CONFIRM_REQUIRED`` error and exit non-zero.

    Centralises the JSON-mode confirmation gate used by destructive
    commands (``source delete``, ``source delete-by-title``, ``source
    clean``). Calling this helper always raises ``SystemExit(1)`` via
    :func:`output_error` — it never returns normally.
    """
    payload: dict[str, Any] = {"action": action}
    if extra:
        payload.update(extra)
    output_error(
        "Pass --yes to confirm destructive operation in --json mode",
        code="CONFIRM_REQUIRED",
        json_output=True,
        exit_code=1,
        extra=payload,
    )
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# source delete
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceDeletePlan:
    """Prepared inputs for ``execute_source_delete``."""

    notebook_id: str
    source_id: str
    yes: bool
    json_output: bool


async def execute_source_delete(
    client: NotebookLMClient, plan: SourceDeletePlan, *, ctx: click.Context | None = None
) -> None:
    """Resolve + confirm + delete a single source by id or partial id."""

    async def resolve_delete(client):
        resolved_id = await resolve_source_for_delete(
            client, plan.notebook_id, plan.source_id, json_output=plan.json_output
        )
        # P1.T2 bug 1: In --json mode, never prompt — automation cannot
        # answer an interactive confirmation. Require --yes and emit a
        # structured JSON error otherwise.
        if plan.json_output and not plan.yes:
            require_yes_in_json(
                action="delete",
                extra={
                    "source_id": resolved_id,
                    "notebook_id": plan.notebook_id,
                },
            )
        return {
            "notebook_id": plan.notebook_id,
            "source_id": resolved_id,
            "success": False,
        }

    async def execute_delete(client, resolved):
        resolved["success"] = bool(
            await client.sources.delete(resolved["notebook_id"], resolved["source_id"])
        )

    def serialize_success(resolved):
        return {
            "action": "delete",
            "source_id": resolved["source_id"],
            "notebook_id": resolved["notebook_id"],
            "success": bool(resolved["success"]),
            "status": "deleted" if resolved["success"] else "unknown",
        }

    mutation = MutationPlan(
        entity_label="source",
        resolve=resolve_delete,
        confirm_message="Delete source {resolved[source_id]}?",
        execute=execute_delete,
        serialize_success=serialize_success,
        serialize_cancel=lambda resolved: {
            "action": "delete",
            "source_id": resolved["source_id"],
            "notebook_id": resolved["notebook_id"],
            "success": False,
            "status": "cancelled",
        },
    )
    result = await run_confirmed_mutation(
        mutation,
        client,
        yes=plan.yes,
        json_output=plan.json_output,
        confirmer=click.confirm,
    )
    if result.status == "cancelled" or plan.json_output:
        return

    resolved_id = result.resolved["source_id"]
    success = bool(result.resolved["success"])
    if success:
        cli_print(f"[green]Deleted source:[/green] {resolved_id}", ctx=ctx)
    else:
        cli_print("[yellow]Delete may have failed[/yellow]", ctx=ctx)


# ---------------------------------------------------------------------------
# source delete-by-title
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceDeleteByTitlePlan:
    """Prepared inputs for ``execute_source_delete_by_title``."""

    notebook_id: str
    title: str
    yes: bool
    json_output: bool


async def execute_source_delete_by_title(
    client: NotebookLMClient,
    plan: SourceDeleteByTitlePlan,
    *,
    ctx: click.Context | None = None,
) -> None:
    """Resolve + confirm + delete a source by exact title."""

    async def resolve_delete_by_title(client):
        source = await resolve_source_by_exact_title(
            client, plan.notebook_id, plan.title, json_output=plan.json_output
        )
        # P1.T2 bug 2: same JSON-mode confirmation contract as ``source delete``.
        if plan.json_output and not plan.yes:
            require_yes_in_json(
                action="delete-by-title",
                extra={
                    "source_id": source.id,
                    "title": source.title,
                    "notebook_id": plan.notebook_id,
                },
            )
        return {
            "notebook_id": plan.notebook_id,
            "source_id": source.id,
            "title": source.title,
            "success": False,
        }

    async def execute_delete_by_title(client, resolved):
        resolved["success"] = bool(
            await client.sources.delete(resolved["notebook_id"], resolved["source_id"])
        )

    def serialize_success(resolved):
        return {
            "action": "delete-by-title",
            "source_id": resolved["source_id"],
            "title": resolved["title"],
            "notebook_id": resolved["notebook_id"],
            "success": bool(resolved["success"]),
            "status": "deleted" if resolved["success"] else "unknown",
        }

    mutation = MutationPlan(
        entity_label="source",
        resolve=resolve_delete_by_title,
        confirm_message="Delete source '{resolved[title]}' ({resolved[source_id]})?",
        execute=execute_delete_by_title,
        serialize_success=serialize_success,
        serialize_cancel=lambda resolved: {
            "action": "delete-by-title",
            "source_id": resolved["source_id"],
            "title": resolved["title"],
            "notebook_id": resolved["notebook_id"],
            "success": False,
            "status": "cancelled",
        },
    )
    result = await run_confirmed_mutation(
        mutation,
        client,
        yes=plan.yes,
        json_output=plan.json_output,
        confirmer=click.confirm,
    )
    if result.status == "cancelled" or plan.json_output:
        return

    source_id = result.resolved["source_id"]
    success = bool(result.resolved["success"])
    if success:
        cli_print(f"[green]Deleted source:[/green] {source_id}", ctx=ctx)
    else:
        cli_print("[yellow]Delete may have failed[/yellow]", ctx=ctx)


# ---------------------------------------------------------------------------
# source rename
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRenamePlan:
    """Prepared inputs for ``execute_source_rename``."""

    notebook_id: str
    source_id: str
    new_title: str
    json_output: bool


async def execute_source_rename(
    client: NotebookLMClient,
    plan: SourceRenamePlan,
    *,
    ctx: click.Context | None = None,
) -> None:
    """Resolve + rename a single source."""
    resolved_id = await resolve_source_id(
        client, plan.notebook_id, plan.source_id, json_output=plan.json_output
    )
    src = await client.sources.rename(plan.notebook_id, resolved_id, plan.new_title)

    if plan.json_output:
        json_output_response(
            {
                "action": "rename",
                "source_id": src.id,
                "notebook_id": plan.notebook_id,
                "title": src.title,
                "status": "renamed",
            }
        )
        return

    cli_print(f"[green]Renamed source:[/green] {src.id}", ctx=ctx)
    cli_print(f"[bold]New title:[/bold] {src.title}", ctx=ctx)


# ---------------------------------------------------------------------------
# source refresh
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRefreshPlan:
    """Prepared inputs for ``execute_source_refresh``."""

    notebook_id: str
    source_id: str
    json_output: bool


async def execute_source_refresh(
    client: NotebookLMClient,
    plan: SourceRefreshPlan,
    *,
    ctx: click.Context | None = None,
) -> None:
    """Resolve + refresh a URL/Drive source."""
    resolved_id = await resolve_source_id(
        client, plan.notebook_id, plan.source_id, json_output=plan.json_output
    )

    if plan.json_output:
        src = await client.sources.refresh(plan.notebook_id, resolved_id)
    else:
        with cli_status("Refreshing source...", ctx=ctx):
            src = await client.sources.refresh(plan.notebook_id, resolved_id)

    if plan.json_output:
        # ``refresh`` may return a Source dataclass, ``True``, or
        # falsy/None. Surface the same three states in JSON so
        # automation can branch on ``status`` without scraping text.
        if src and src is not True:
            data = {
                "action": "refresh",
                "source_id": src.id,
                "notebook_id": plan.notebook_id,
                "title": src.title,
                "status": "refreshed",
            }
        elif src is True:
            data = {
                "action": "refresh",
                "source_id": resolved_id,
                "notebook_id": plan.notebook_id,
                "status": "refreshed",
            }
        else:
            data = {
                "action": "refresh",
                "source_id": resolved_id,
                "notebook_id": plan.notebook_id,
                "status": "no_result",
            }
        json_output_response(data)
        return

    if src and src is not True:
        cli_print(f"[green]Source refreshed:[/green] {src.id}", ctx=ctx)
        cli_print(f"[bold]Title:[/bold] {src.title}", ctx=ctx)
    elif src is True:
        cli_print(f"[green]Source refreshed:[/green] {resolved_id}", ctx=ctx)
    else:
        cli_print("[yellow]Refresh returned no result[/yellow]", ctx=ctx)


# ---------------------------------------------------------------------------
# source add-drive
# ---------------------------------------------------------------------------


_DRIVE_MIME_MAP: dict[DriveMimeChoice, str] = {
    "google-doc": DriveMimeType.GOOGLE_DOC.value,
    "google-slides": DriveMimeType.GOOGLE_SLIDES.value,
    "google-sheets": DriveMimeType.GOOGLE_SHEETS.value,
    "pdf": DriveMimeType.PDF.value,
}


@dataclass(frozen=True)
class SourceAddDrivePlan:
    """Prepared inputs for ``execute_source_add_drive``."""

    notebook_id: str
    file_id: str
    title: str
    mime_type: DriveMimeChoice
    json_output: bool


async def execute_source_add_drive(
    client: NotebookLMClient,
    plan: SourceAddDrivePlan,
    *,
    ctx: click.Context | None = None,
) -> None:
    """Add a Google Drive document as a source."""
    mime = _DRIVE_MIME_MAP[plan.mime_type]

    if plan.json_output:
        src = await client.sources.add_drive(plan.notebook_id, plan.file_id, plan.title, mime)
    else:
        with console.status("Adding Drive source..."):
            src = await client.sources.add_drive(plan.notebook_id, plan.file_id, plan.title, mime)

    if plan.json_output:
        json_output_response(
            {
                "action": "add-drive",
                "source": {
                    **source_summary_payload(src),
                    "drive_file_id": plan.file_id,
                    "mime_type": plan.mime_type,
                },
                "notebook_id": plan.notebook_id,
            }
        )
        return

    cli_print(f"[green]Added Drive source:[/green] {src.id}", ctx=ctx)
    cli_print(f"[bold]Title:[/bold] {src.title}", ctx=ctx)


__all__ = [
    "SourceAddDrivePlan",
    "SourceDeleteByTitlePlan",
    "SourceDeletePlan",
    "SourceRefreshPlan",
    "SourceRenamePlan",
    "build_id_ambiguity_error",
    "execute_source_add_drive",
    "execute_source_delete",
    "execute_source_delete_by_title",
    "execute_source_refresh",
    "execute_source_rename",
    "looks_like_full_source_id",
    "require_yes_in_json",
    "resolve_source_by_exact_title",
    "resolve_source_for_delete",
]
