"""Services for source-mutation commands: delete, delete-by-title, rename, refresh, add-drive.

Each command has its own plan + executor pair. The shared resolver
helpers (``resolve_source_for_delete``, ``resolve_source_by_exact_title``,
``require_yes_in_json``) also live here because they are mutation-specific
and were previously private helpers on ``cli/source_cmd.py``. Typed result
dataclasses carry presentation payloads back to the command layer. The
:class:`MutationPlan` pipeline from
``cli/services/confirming_mutation.py`` handles the resolve → confirm →
execute → serialize flow for the destructive paths without printing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast

from ...types import DriveMimeType, Source
from ..resolve import resolve_source_id, validate_id
from .confirming_mutation import MutationPlan, run_confirmed_mutation
from .source_serializers import source_summary_payload

if TYPE_CHECKING:
    from ...client import NotebookLMClient

DriveMimeChoice = Literal["google-doc", "google-slides", "google-sheets", "pdf"]


class SourceMutationError(Exception):
    """Typed source-mutation error for command-layer rendering and exit policy."""

    def __init__(
        self,
        message: str,
        code: str,
        extra: dict[str, Any] | None = None,
        status_message: str | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.extra = extra
        self.status_message = status_message
        metadata = f" (code={code}, extra={extra})" if extra else f" (code={code})"
        super().__init__(f"{message}{metadata}")


@dataclass(frozen=True)
class SourceIdResolution:
    """Resolved source-id data plus optional status prose for the command layer."""

    source_id: str
    status_message: str | None = None


@dataclass(frozen=True)
class SourceDeleteResult:
    """Outcome of ``source delete``."""

    source_id: str
    notebook_id: str
    success: bool
    status: Literal["completed", "cancelled"]
    status_message: str | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "action": "delete",
            "source_id": self.source_id,
            "notebook_id": self.notebook_id,
            "success": self.success,
            "status": (
                "cancelled"
                if self.status == "cancelled"
                else ("deleted" if self.success else "unknown")
            ),
        }


@dataclass(frozen=True)
class SourceDeleteByTitleResult:
    """Outcome of ``source delete-by-title``."""

    source_id: str
    title: str
    notebook_id: str
    success: bool
    status: Literal["completed", "cancelled"]
    status_message: str | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "action": "delete-by-title",
            "source_id": self.source_id,
            "title": self.title,
            "notebook_id": self.notebook_id,
            "success": self.success,
            "status": (
                "cancelled"
                if self.status == "cancelled"
                else ("deleted" if self.success else "unknown")
            ),
        }


@dataclass(frozen=True)
class SourceRenameResult:
    """Outcome of ``source rename``."""

    source: Source
    notebook_id: str

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "action": "rename",
            "source_id": self.source.id,
            "notebook_id": self.notebook_id,
            "title": self.source.title,
            "status": "renamed",
        }


@dataclass(frozen=True)
class SourceRefreshResult:
    """Outcome of ``source refresh``."""

    source_id: str
    notebook_id: str
    result: Source | bool | None

    @property
    def payload(self) -> dict[str, Any]:
        if self.result and self.result is not True:
            return {
                "action": "refresh",
                "source_id": self.result.id,
                "notebook_id": self.notebook_id,
                "title": self.result.title,
                "status": "refreshed",
            }
        if self.result is True:
            return {
                "action": "refresh",
                "source_id": self.source_id,
                "notebook_id": self.notebook_id,
                "status": "refreshed",
            }
        return {
            "action": "refresh",
            "source_id": self.source_id,
            "notebook_id": self.notebook_id,
            "status": "no_result",
        }


@dataclass(frozen=True)
class SourceAddDriveResult:
    """Outcome of ``source add-drive``."""

    source: Source
    notebook_id: str
    file_id: str
    mime_type: DriveMimeChoice

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "action": "add-drive",
            "source": {
                **source_summary_payload(self.source),
                "drive_file_id": self.file_id,
                "mime_type": self.mime_type,
            },
            "notebook_id": self.notebook_id,
        }


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
    client: NotebookLMClient, notebook_id: str, source_id: str, *, json_output: bool = False
) -> SourceIdResolution:
    """Resolve source-id input for delete into a :class:`SourceIdResolution`.

    Canonical UUIDs take a fast path and skip the live source list
    lookup. Partial IDs are resolved against the live list. Successful
    partial matches include status prose for the command layer to emit.
    """
    source_id = validate_id(source_id, "source")
    if looks_like_full_source_id(source_id):
        return SourceIdResolution(source_id=source_id)

    sources = await client.sources.list(notebook_id)
    matches = [item for item in sources if item.id.lower().startswith(source_id.lower())]

    if len(matches) == 1:
        status_message = None
        if matches[0].id != source_id:
            title = matches[0].title or "(untitled)"
            status_message = f"[dim]Matched: {matches[0].id[:12]}... ({title})[/dim]"
        return SourceIdResolution(source_id=matches[0].id, status_message=status_message)

    if len(matches) > 1:
        raise SourceMutationError(
            build_id_ambiguity_error(source_id, matches),
            "AMBIGUOUS_ID",
        )

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
        raise SourceMutationError("\n".join(lines), "VALIDATION_ERROR")

    raise SourceMutationError(
        f"No source found starting with '{source_id}'. "
        "Run 'notebooklm source list' to see available sources.",
        "NOT_FOUND",
    )


async def resolve_source_by_exact_title(
    client: NotebookLMClient, notebook_id: str, title: str, *, json_output: bool = False
) -> Source:
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
        raise SourceMutationError("\n".join(lines), "AMBIGUOUS_TITLE")

    raise SourceMutationError(
        f"No source found with title '{title}'. "
        "Run 'notebooklm source list' to see available sources.",
        "NOT_FOUND",
    )


def require_yes_in_json(
    *,
    action: str,
    extra: dict[str, Any] | None = None,
    status_message: str | None = None,
) -> NoReturn:
    """Raise a typed ``CONFIRM_REQUIRED`` error for command-layer handling.

    Centralises the JSON-mode confirmation gate used by destructive
    commands (``source delete``, ``source delete-by-title``, ``source
    clean``). Calling this helper always raises a typed error for the
    command layer; it never returns normally.
    """
    payload: dict[str, Any] = {"action": action}
    if extra:
        payload.update(extra)
    raise SourceMutationError(
        "Pass --yes to confirm destructive operation in --json mode",
        "CONFIRM_REQUIRED",
        payload,
        status_message,
    )


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
    client: NotebookLMClient,
    plan: SourceDeletePlan,
    *,
    confirmer: Callable[[str], bool],
) -> SourceDeleteResult:
    """Resolve + confirm + delete a single source by id or partial id."""

    async def resolve_delete(client):
        resolution = await resolve_source_for_delete(
            client, plan.notebook_id, plan.source_id, json_output=plan.json_output
        )
        # P1.T2 bug 1: In --json mode, never prompt — automation cannot
        # answer an interactive confirmation. Require --yes and emit a
        # structured JSON error otherwise.
        if plan.json_output and not plan.yes:
            require_yes_in_json(
                action="delete",
                extra={
                    "source_id": resolution.source_id,
                    "notebook_id": plan.notebook_id,
                },
                status_message=resolution.status_message,
            )
        return {
            "notebook_id": plan.notebook_id,
            "source_id": resolution.source_id,
            "success": False,
            "status_message": resolution.status_message,
        }

    async def execute_delete(client, resolved):
        # delete() now returns None and raises on real failure (issue #1211);
        # reaching here without an exception means success.
        await client.sources.delete(resolved["notebook_id"], resolved["source_id"])
        resolved["success"] = True

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
        confirmer=confirmer,
    )
    return SourceDeleteResult(
        source_id=result.resolved["source_id"],
        notebook_id=result.resolved["notebook_id"],
        success=bool(result.resolved["success"]),
        status=result.status,
        status_message=result.resolved.get("status_message"),
    )


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
    confirmer: Callable[[str], bool],
) -> SourceDeleteByTitleResult:
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
        # delete() now returns None and raises on real failure (issue #1211);
        # reaching here without an exception means success.
        await client.sources.delete(resolved["notebook_id"], resolved["source_id"])
        resolved["success"] = True

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
        confirmer=confirmer,
    )
    return SourceDeleteByTitleResult(
        source_id=result.resolved["source_id"],
        title=result.resolved["title"],
        notebook_id=result.resolved["notebook_id"],
        success=bool(result.resolved["success"]),
        status=result.status,
    )


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
) -> SourceRenameResult:
    """Resolve + rename a single source."""
    resolved_id = await resolve_source_id(
        client, plan.notebook_id, plan.source_id, json_output=plan.json_output
    )
    # return_object defaults to True, so rename returns a Source (or raises
    # SourceNotFoundError on a missing target) — never None on this path. Use
    # cast (not assert, which -O strips) to narrow Source | None for the
    # rename-result dataclass.
    src = cast(
        Source,
        await client.sources.rename(plan.notebook_id, resolved_id, plan.new_title),
    )
    return SourceRenameResult(source=src, notebook_id=plan.notebook_id)


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
) -> SourceRefreshResult:
    """Resolve + refresh a URL/Drive source."""
    resolved_id = await resolve_source_id(
        client, plan.notebook_id, plan.source_id, json_output=plan.json_output
    )

    src = await client.sources.refresh(plan.notebook_id, resolved_id)
    return SourceRefreshResult(source_id=resolved_id, notebook_id=plan.notebook_id, result=src)


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


async def execute_source_add_drive(
    client: NotebookLMClient,
    plan: SourceAddDrivePlan,
) -> SourceAddDriveResult:
    """Add a Google Drive document as a source."""
    mime = _DRIVE_MIME_MAP[plan.mime_type]

    src = await client.sources.add_drive(plan.notebook_id, plan.file_id, plan.title, mime)
    return SourceAddDriveResult(
        source=src,
        notebook_id=plan.notebook_id,
        file_id=plan.file_id,
        mime_type=plan.mime_type,
    )


__all__ = [
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
    "execute_source_delete",
    "execute_source_delete_by_title",
    "execute_source_refresh",
    "execute_source_rename",
    "looks_like_full_source_id",
    "require_yes_in_json",
    "resolve_source_by_exact_title",
    "resolve_source_for_delete",
]
