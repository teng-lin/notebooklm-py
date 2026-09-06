"""CLI adapter for source-mutation commands — thin wrapper over ``_app``.

The ``delete`` / ``delete-by-title`` / ``rename`` / ``refresh`` / ``add-drive``
workflows, the mutation-specific source-id resolvers, the typed
:class:`SourceMutationError`, and the typed result dataclasses now live in the
transport-neutral :mod:`notebooklm._app.source_mutations`. This module is the
CLI-side adapter that:

* re-exports the typed plan/result/error/helper names so existing
  ``from ...source_mutations import ...`` imports (the command layer in
  ``cli/source_cmd.py`` and ``cli/_source_render.py``) keep resolving, and
* injects the Click-coupled :func:`validate_id` (raises ``click.ClickException``
  on empty) and the ``rich``-coupled :func:`resolve_source_id` (its
  "Matched: ..." diagnostic) into the neutral executors.

Both injected resolvers are read off **this module's** namespace at call time,
so the historical ``monkeypatch.setattr(source_mutations, "resolve_source_id",
...)`` test seam keeps landing. Command-layer rendering + exit codes live in
``cli/_source_render.py`` per ADR-0008.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from ..._app.source_mutations import (
    DriveMimeChoice,
    SourceAddDriveFilePlan,
    SourceAddDriveFileResult,
    SourceAddDrivePlan,
    SourceAddDriveResult,
    SourceDeleteByTitlePlan,
    SourceDeleteByTitleResult,
    SourceDeletePlan,
    SourceDeleteResult,
    SourceIdResolution,
    SourceMutationError,
    SourceMutationMatch,
    SourceRefreshPlan,
    SourceRefreshResult,
    SourceRenamePlan,
    SourceRenameResult,
    execute_source_add_drive,
    execute_source_add_drive_file,
    looks_like_full_source_id,
)
from ..._app.source_mutations import (
    execute_source_delete as _execute_source_delete,
)
from ..._app.source_mutations import (
    execute_source_delete_by_title as _execute_source_delete_by_title,
)
from ..._app.source_mutations import (
    execute_source_refresh as _execute_source_refresh,
)
from ..._app.source_mutations import (
    execute_source_rename as _execute_source_rename,
)
from ..._app.source_mutations import (
    resolve_source_by_exact_title as _resolve_source_by_exact_title,
)
from ..._app.source_mutations import (
    resolve_source_for_delete as _resolve_source_for_delete,
)
from ..resolve import resolve_source_id, validate_id

if TYPE_CHECKING:
    from ...client import NotebookLMClient


class CliSourceMutationError(Exception):
    """CLI-only failure carrying the historical envelope projection."""

    def __init__(
        self,
        message: str,
        code: str,
        extra: dict[str, object] | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.extra = extra
        super().__init__(message)


def _append_source_matches(lines: list[str], matches: tuple[SourceMutationMatch, ...]) -> None:
    for match in matches[:5]:
        lines.append(f"  {match.id[:12]}... {match.title or '(untitled)'}")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")


def build_id_ambiguity_error(source_id: str, matches) -> str:
    """Compatibility renderer for the established CLI ambiguity wording."""
    semantic = tuple(SourceMutationMatch(item.id, item.title or "") for item in matches)
    lines = [f"Ambiguous ID '{source_id}' matches {len(semantic)} sources:"]
    _append_source_matches(lines, semantic)
    lines.append("Specify more characters to narrow down.")
    return "\n".join(lines)


def source_mutation_error_details(
    exc: SourceMutationError | CliSourceMutationError,
) -> tuple[str, str, dict[str, object] | None]:
    """Map neutral resolution failures or CLI-only policy failures to CLI output."""
    if isinstance(exc, CliSourceMutationError):
        return exc.message, exc.code, exc.extra

    matches = exc.matches
    if exc.reason == "ambiguous_id":
        lines = [f"Ambiguous ID '{exc.token}' matches {len(matches)} sources:"]
        _append_source_matches(lines, matches)
        lines.append("Specify more characters to narrow down.")
        return "\n".join(lines), "AMBIGUOUS_ID", None
    if exc.reason == "title_used_as_id":
        lines = [
            f"'{exc.token}' matches {len(matches)} source title(s), not source IDs.",
            f"Use 'notebooklm source delete-by-title \"{exc.token}\"' or delete by ID:",
        ]
        _append_source_matches(lines, matches)
        return "\n".join(lines), "VALIDATION_ERROR", None
    if exc.reason == "id_not_found":
        return (
            f"No source found starting with '{exc.token}'. "
            "Run 'notebooklm source list' to see available sources.",
            "NOT_FOUND",
            None,
        )
    if exc.reason == "ambiguous_title":
        lines = [f"Title '{exc.token}' matches {len(matches)} sources. Delete by ID instead:"]
        _append_source_matches(lines, matches)
        return "\n".join(lines), "AMBIGUOUS_TITLE", None
    if exc.reason == "title_not_found":
        return (
            f"No source found with title '{exc.token}'. "
            "Run 'notebooklm source list' to see available sources.",
            "NOT_FOUND",
            None,
        )
    raise AssertionError(f"Unhandled source mutation reason: {exc.reason}")


async def resolve_source_for_delete(
    client: NotebookLMClient, notebook_id: str, source_id: str
) -> SourceIdResolution:
    """Resolve a delete source-id input, injecting the Click ``validate_id``.

    Thin adapter over the neutral resolver that passes this module's
    :func:`validate_id` (read at call time so a ``monkeypatch.setattr`` lands).
    """
    return await _resolve_source_for_delete(client, notebook_id, source_id, validate_id=validate_id)


async def resolve_source_by_exact_title(client: NotebookLMClient, notebook_id: str, title: str):
    """Resolve a source by exact title, injecting the Click ``validate_id``."""
    return await _resolve_source_by_exact_title(client, notebook_id, title, validate_id=validate_id)


async def execute_source_delete(
    client: NotebookLMClient,
    plan: SourceDeletePlan,
) -> SourceDeleteResult:
    """Delete an adapter-authorized source target."""
    return await _execute_source_delete(client, plan)


async def execute_source_delete_by_title(
    client: NotebookLMClient,
    plan: SourceDeleteByTitlePlan,
) -> SourceDeleteByTitleResult:
    """Delete an adapter-authorized title-resolved source target."""
    return await _execute_source_delete_by_title(client, plan)


async def run_source_delete(
    client: NotebookLMClient,
    *,
    notebook_id: str,
    source_id: str,
    approved: bool,
    noninteractive: bool,
    confirm: Callable[[str], bool],
) -> SourceDeleteResult:
    """Resolve, obtain adapter authorization, and execute one source delete."""
    target = await resolve_source_for_delete(client, notebook_id, source_id)
    if noninteractive and not approved:
        extra: dict[str, object] = {
            "action": "delete",
            "source_id": target.source_id,
            "notebook_id": notebook_id,
        }
        if target.matched_title is not None:
            extra["status_message"] = (
                f"Matched: {target.source_id[:12]}... ({target.matched_title})"
            )
        raise CliSourceMutationError(
            "Pass --yes to confirm destructive operation in --json mode",
            "CONFIRM_REQUIRED",
            extra,
        )
    if not approved and not confirm(f"Delete source {target.source_id}?"):
        return SourceDeleteResult(
            source_id=target.source_id,
            notebook_id=notebook_id,
            success=False,
            status="cancelled",
            matched_title=target.matched_title,
        )
    return await execute_source_delete(client, SourceDeletePlan(notebook_id, target))


async def run_source_delete_by_title(
    client: NotebookLMClient,
    *,
    notebook_id: str,
    title: str,
    approved: bool,
    noninteractive: bool,
    confirm: Callable[[str], bool],
) -> SourceDeleteByTitleResult:
    """Resolve title once, authorize that immutable target, then delete it."""
    target = await resolve_source_by_exact_title(client, notebook_id, title)
    target_title = target.title or ""
    if noninteractive and not approved:
        raise CliSourceMutationError(
            "Pass --yes to confirm destructive operation in --json mode",
            "CONFIRM_REQUIRED",
            {
                "action": "delete-by-title",
                "source_id": target.id,
                "title": target_title,
                "notebook_id": notebook_id,
            },
        )
    if not approved and not confirm(f"Delete source '{target_title}' ({target.id})?"):
        return SourceDeleteByTitleResult(
            source_id=target.id,
            title=target_title,
            notebook_id=notebook_id,
            success=False,
            status="cancelled",
        )
    return await execute_source_delete_by_title(
        client,
        SourceDeleteByTitlePlan(notebook_id, target.id, target_title),
    )


async def execute_source_rename(
    client: NotebookLMClient,
    plan: SourceRenamePlan,
    *,
    json_output: bool = False,
) -> SourceRenameResult:
    """Resolve + rename a source, injecting the CLI ``resolve_source_id``.

    The resolver is read off this module at call time so the
    ``monkeypatch.setattr(source_mutations, "resolve_source_id", ...)`` seam
    keeps landing.
    """
    return await _execute_source_rename(
        client,
        plan,
        resolve_source_id=partial(resolve_source_id, json_output=json_output),
    )


async def execute_source_refresh(
    client: NotebookLMClient,
    plan: SourceRefreshPlan,
    *,
    json_output: bool = False,
) -> SourceRefreshResult:
    """Resolve + refresh a source, injecting the CLI ``resolve_source_id``."""
    return await _execute_source_refresh(
        client,
        plan,
        resolve_source_id=partial(resolve_source_id, json_output=json_output),
    )


__all__ = [
    "DriveMimeChoice",
    "CliSourceMutationError",
    "SourceAddDriveFilePlan",
    "SourceAddDriveFileResult",
    "SourceAddDrivePlan",
    "SourceAddDriveResult",
    "SourceDeleteByTitlePlan",
    "SourceDeleteByTitleResult",
    "SourceDeletePlan",
    "SourceDeleteResult",
    "SourceIdResolution",
    "SourceMutationError",
    "SourceRefreshPlan",
    "SourceRefreshResult",
    "SourceRenamePlan",
    "SourceRenameResult",
    "build_id_ambiguity_error",
    "execute_source_add_drive",
    "execute_source_add_drive_file",
    "execute_source_delete",
    "execute_source_delete_by_title",
    "execute_source_refresh",
    "execute_source_rename",
    "looks_like_full_source_id",
    "resolve_source_by_exact_title",
    "resolve_source_for_delete",
    "resolve_source_id",
    "run_source_delete",
    "run_source_delete_by_title",
    "source_mutation_error_details",
    "validate_id",
]
