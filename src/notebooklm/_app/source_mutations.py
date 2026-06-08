"""Transport-neutral source-mutation business logic.

This is the Click-free core of ``cli/services/source_mutations.py``: it owns the
``delete`` / ``delete-by-title`` / ``rename`` / ``refresh`` / ``add-drive``
workflows, the mutation-specific source-id resolvers, the typed
:class:`SourceMutationError`, and the typed result dataclasses whose
``.payload`` builds each command's stable ``--json`` body. Every transport
adapter (the Click CLI today, the FastMCP server / future HTTP later) drives
this core and renders the typed result / error into its own surface + exit-code
policy.

Two boundary-imposed seams are worth calling out:

* **The id validator + the partial-source-id resolver are injected, never
  imported.** ``cli.resolve.validate_id`` raises ``click.ClickException`` and
  ``cli.resolve.resolve_source_id`` reaches into ``rich`` consoles for its
  "Matched: ..." diagnostic, so this module cannot import either without
  breaking the ``_app`` boundary. Instead the executors take ``validate_id`` /
  ``resolve_source_id`` callables (the CLI wrapper passes its own, the neutral
  ``validate_id`` default raising :class:`~notebooklm.exceptions.ValidationError`).
  Reading the resolver off the wrapper at call time also preserves the
  historical ``monkeypatch.setattr(source_mutations, "resolve_source_id", ...)``
  test seam.
* **``SourceMutationError`` carries Rich markup in ``status_message``.** The
  field is a plain ``str``; the markup→plain conversion and the exit-code
  policy live in the CLI renderer (``_handle_source_mutation_error``), so this
  module stays presentation-neutral while still carrying the hint string.

The confirm → execute → serialize flow for the destructive ``delete`` paths is
inlined here (rather than importing the CLI-services ``confirming_mutation``
pipeline, which ``_app`` cannot reach) so the byte-identical payloads are owned
next to the resolvers. The ``confirmer`` is injected by the adapter
(``click.confirm`` for the CLI).

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast

from ..types import DriveMimeType, Source
from .resolve import validate_id as _neutral_validate_id

if TYPE_CHECKING:
    from ..client import NotebookLMClient

DriveMimeChoice = Literal["google-doc", "google-slides", "google-sheets", "pdf"]

#: Validates + normalizes an entity id (empty → error). The CLI adapter injects
#: ``cli.resolve.validate_id`` (raising ``click.ClickException``); the neutral
#: default raises :class:`~notebooklm.exceptions.ValidationError`.
ValidateIdFn = Callable[[str, str], str]

#: Resolves a (possibly partial) source id to its full id. The CLI adapter
#: injects ``cli.resolve.resolve_source_id``; it is read off the wrapper at call
#: time so the ``monkeypatch.setattr`` test seam keeps landing.
ResolveSourceIdFn = Callable[..., Awaitable[str]]


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
    result: Source | None

    @property
    def payload(self) -> dict[str, Any]:
        if isinstance(self.result, Source):
            return {
                "action": "refresh",
                "source_id": self.result.id,
                "notebook_id": self.notebook_id,
                "title": self.result.title,
                "status": "refreshed",
            }
        # ``sources.refresh`` returns ``None`` on success (#1290); any failure
        # raises before reaching here, so ``None`` is the refreshed-OK case.
        return {
            "action": "refresh",
            "source_id": self.source_id,
            "notebook_id": self.notebook_id,
            "status": "refreshed",
        }


@dataclass(frozen=True)
class SourceAddDriveResult:
    """Outcome of ``source add-drive``.

    Carries only the neutral fields. Unlike the other result dataclasses this
    one has no ``.payload`` property: the add-drive ``--json`` envelope embeds
    the ``source_summary_payload`` serializer (presentation), so the CLI
    renderer (``_render_source_add_drive_result``) builds it from these fields.
    """

    source: Source
    notebook_id: str
    file_id: str
    mime_type: DriveMimeChoice


# ---------------------------------------------------------------------------
# Shared helpers for source-id resolution
# ---------------------------------------------------------------------------


def build_id_ambiguity_error(source_id: str, matches: list[Source]) -> str:
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
    client: NotebookLMClient,
    notebook_id: str,
    source_id: str,
    *,
    json_output: bool = False,
    validate_id: ValidateIdFn = _neutral_validate_id,
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
    client: NotebookLMClient,
    notebook_id: str,
    title: str,
    *,
    json_output: bool = False,
    validate_id: ValidateIdFn = _neutral_validate_id,
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
    validate_id: ValidateIdFn = _neutral_validate_id,
) -> SourceDeleteResult:
    """Resolve + confirm + delete a single source by id or partial id."""
    resolution = await resolve_source_for_delete(
        client,
        plan.notebook_id,
        plan.source_id,
        json_output=plan.json_output,
        validate_id=validate_id,
    )
    # In --json mode, never prompt — automation cannot answer an interactive
    # confirmation. Require --yes and emit a structured JSON error otherwise.
    if plan.json_output and not plan.yes:
        require_yes_in_json(
            action="delete",
            extra={
                "source_id": resolution.source_id,
                "notebook_id": plan.notebook_id,
            },
            status_message=resolution.status_message,
        )

    # Confirm (interactive text mode only); --yes and --json skip the prompt.
    if (
        not plan.yes
        and not plan.json_output
        and not confirmer(f"Delete source {resolution.source_id}?")
    ):
        return SourceDeleteResult(
            source_id=resolution.source_id,
            notebook_id=plan.notebook_id,
            success=False,
            status="cancelled",
            status_message=resolution.status_message,
        )

    # delete() now returns None and raises on real failure (issue #1211);
    # reaching here without an exception means success.
    await client.sources.delete(plan.notebook_id, resolution.source_id)
    return SourceDeleteResult(
        source_id=resolution.source_id,
        notebook_id=plan.notebook_id,
        success=True,
        status="completed",
        status_message=resolution.status_message,
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
    validate_id: ValidateIdFn = _neutral_validate_id,
) -> SourceDeleteByTitleResult:
    """Resolve + confirm + delete a source by exact title."""
    source = await resolve_source_by_exact_title(
        client,
        plan.notebook_id,
        plan.title,
        json_output=plan.json_output,
        validate_id=validate_id,
    )
    # Same JSON-mode confirmation contract as ``source delete``.
    if plan.json_output and not plan.yes:
        require_yes_in_json(
            action="delete-by-title",
            extra={
                "source_id": source.id,
                "title": source.title,
                "notebook_id": plan.notebook_id,
            },
        )

    if (
        not plan.yes
        and not plan.json_output
        and not confirmer(f"Delete source '{source.title}' ({source.id})?")
    ):
        return SourceDeleteByTitleResult(
            source_id=source.id,
            title=cast(str, source.title),
            notebook_id=plan.notebook_id,
            success=False,
            status="cancelled",
        )

    # delete() now returns None and raises on real failure (issue #1211);
    # reaching here without an exception means success.
    await client.sources.delete(plan.notebook_id, source.id)
    return SourceDeleteByTitleResult(
        source_id=source.id,
        title=cast(str, source.title),
        notebook_id=plan.notebook_id,
        success=True,
        status="completed",
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
    *,
    resolve_source_id: ResolveSourceIdFn,
) -> SourceRenameResult:
    """Resolve + rename a single source.

    ``resolve_source_id`` is injected (the CLI passes its
    ``cli.resolve.resolve_source_id``) so this core stays free of the
    ``rich``-coupled resolver and the CLI's monkeypatch seam keeps landing.
    """
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
    *,
    resolve_source_id: ResolveSourceIdFn,
) -> SourceRefreshResult:
    """Resolve + refresh a URL/Drive source.

    ``resolve_source_id`` is injected (see :func:`execute_source_rename`).
    """
    resolved_id = await resolve_source_id(
        client, plan.notebook_id, plan.source_id, json_output=plan.json_output
    )

    # ``sources.refresh`` returns ``None`` on success (#1290); any failure
    # raises before reaching here.
    await client.sources.refresh(plan.notebook_id, resolved_id)
    return SourceRefreshResult(source_id=resolved_id, notebook_id=plan.notebook_id, result=None)


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
