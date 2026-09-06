"""Transport-neutral source-mutation business logic.

This is the Click-free core of ``cli/services/source_mutations.py``: it owns the
``delete`` / ``delete-by-title`` / ``rename`` / ``refresh`` / ``add-drive``
workflows, the mutation-specific source-id resolvers, the typed
:class:`SourceMutationError`, and the typed-fields-only result dataclasses.
Every transport adapter (the Click CLI today, the FastMCP server / future HTTP
later) drives this core and renders the typed result / error into its own
surface + exit-code policy. The result dataclasses expose typed fields only
(§11); each command's stable ``--json`` body is built by the CLI adapter
(``cli/_source_render.py``) from those fields.

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
Destructive flows are split at an immutable resolved target. Adapters preview
and authorize that target, then pass it to the executor; confirmation wording
and output-mode policy never enter this module.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from ..exceptions import NotebookLMError, ValidationError
from ..types import DriveMimeType, Source, SourceType
from .resolve import FULL_ID_PATTERN
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


SourceMutationReason = Literal[
    "ambiguous_id",
    "title_used_as_id",
    "id_not_found",
    "ambiguous_title",
    "title_not_found",
]


@dataclass(frozen=True)
class SourceMutationMatch:
    """Candidate source carried by a semantic resolution failure."""

    id: str
    title: str


class SourceMutationError(NotebookLMError):
    """Typed source-mutation failure with adapter-neutral reason data."""

    def __init__(
        self,
        reason: SourceMutationReason,
        *,
        token: str,
        matches: tuple[SourceMutationMatch, ...] = (),
    ) -> None:
        self.reason = reason
        self.token = token
        self.matches = matches
        super().__init__(f"source resolution {reason.replace('_', ' ')}: {token}")


@dataclass(frozen=True)
class SourceIdResolution:
    """Resolved source-id data used for adapter-owned match diagnostics."""

    source_id: str
    matched_title: str | None = None


@dataclass(frozen=True)
class SourceDeleteResult:
    """Outcome of ``source delete``.

    Typed-fields-only (§11): the ``--json`` envelope is built by the CLI
    adapter (``cli/_source_render.py``) from these fields, not here.
    """

    source_id: str
    notebook_id: str
    success: bool
    status: Literal["completed", "cancelled"]
    matched_title: str | None = None


@dataclass(frozen=True)
class SourceDeleteByTitleResult:
    """Outcome of ``source delete-by-title``.

    Typed-fields-only (§11): the ``--json`` envelope is built by the CLI
    adapter (``cli/_source_render.py``) from these fields, not here.
    """

    source_id: str
    title: str
    notebook_id: str
    success: bool
    status: Literal["completed", "cancelled"]


@dataclass(frozen=True)
class SourceRenameResult:
    """Outcome of ``source rename``.

    Typed-fields-only (§11): the ``--json`` envelope is built by the CLI
    adapter (``cli/_source_render.py``) from these fields, not here.
    """

    source: Source
    notebook_id: str


@dataclass(frozen=True)
class SourceRefreshResult:
    """Outcome of ``source refresh``.

    Typed-fields-only (§11): the ``--json`` envelope is built by the CLI
    adapter (``cli/_source_render.py``) from these fields, not here.
    """

    source_id: str
    notebook_id: str
    result: Source | None


@dataclass(frozen=True)
class SourceAddDriveResult:
    """Outcome of ``source add-drive``.

    Typed-fields-only (§11): the add-drive ``--json`` envelope (which embeds the
    neutral source summary) is built by the CLI renderer
    (``_render_source_add_drive_result``) from these fields.
    """

    source: Source
    notebook_id: str
    file_id: str
    mime_type: DriveMimeChoice


# ---------------------------------------------------------------------------
# Shared helpers for source-id resolution
# ---------------------------------------------------------------------------


def _source_matches(matches: list[Source]) -> tuple[SourceMutationMatch, ...]:
    """Project source objects onto transport-neutral resolution candidates."""
    return tuple(SourceMutationMatch(item.id, item.title or "") for item in matches)


def looks_like_full_source_id(source_id: str) -> bool:
    """Return True for UUID-shaped source IDs that can skip list-based resolution.

    Reuses :data:`notebooklm._app.resolve.FULL_ID_PATTERN` so the full-id shape
    rule has a single source of truth shared with the generic resolver.
    """
    return bool(FULL_ID_PATTERN.fullmatch(source_id))


async def resolve_source_for_delete(
    client: NotebookLMClient,
    notebook_id: str,
    source_id: str,
    *,
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
    # Exact (case-insensitive) id match wins over prefix matching — mirroring
    # the shared resolvers' Rule 3 (``_app.resolve.resolve_ref`` /
    # ``cli.resolve.resolve_partial_id_in_items``) so ``source delete X`` stays
    # in lockstep with ``source get/rename/refresh X``. Without this, a
    # short-but-complete id that is also a strict prefix of another source's id
    # (e.g. ``abc`` vs ``abcdef``) would be reported as AMBIGUOUS_ID here while
    # the other verbs resolve it (issue #1522). An exact match is not a partial
    # expansion, so there is no match metadata for an adapter to render.
    source_id_lower = source_id.lower()
    matches = []
    for item in sources:
        item_id_lower = item.id.lower()
        if item_id_lower == source_id_lower:
            return SourceIdResolution(source_id=item.id)
        if item_id_lower.startswith(source_id_lower):
            matches.append(item)

    if len(matches) == 1:
        matched_title = None
        if matches[0].id != source_id:
            matched_title = matches[0].title or "(untitled)"
        return SourceIdResolution(source_id=matches[0].id, matched_title=matched_title)

    if len(matches) > 1:
        raise SourceMutationError(
            "ambiguous_id",
            token=source_id,
            matches=_source_matches(matches),
        )

    title_matches = [item for item in sources if item.title == source_id]
    if title_matches:
        raise SourceMutationError(
            "title_used_as_id",
            token=source_id,
            matches=_source_matches(title_matches),
        )

    raise SourceMutationError(
        "id_not_found",
        token=source_id,
    )


async def resolve_source_by_exact_title(
    client: NotebookLMClient,
    notebook_id: str,
    title: str,
    *,
    validate_id: ValidateIdFn = _neutral_validate_id,
) -> Source:
    """Resolve a source by exact title for the explicit delete-by-title flow."""
    title = validate_id(title, "source title")
    sources = await client.sources.list(notebook_id)
    matches = [item for item in sources if item.title == title]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        raise SourceMutationError(
            "ambiguous_title",
            token=title,
            matches=_source_matches(matches),
        )

    raise SourceMutationError(
        "title_not_found",
        token=title,
    )


# ---------------------------------------------------------------------------
# source delete
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceDeletePlan:
    """Prepared inputs for ``execute_source_delete``."""

    notebook_id: str
    target: SourceIdResolution


async def execute_source_delete(
    client: NotebookLMClient,
    plan: SourceDeletePlan,
) -> SourceDeleteResult:
    """Delete the exact immutable target an adapter already authorized."""
    await client.sources.delete(plan.notebook_id, plan.target.source_id)
    return SourceDeleteResult(
        source_id=plan.target.source_id,
        notebook_id=plan.notebook_id,
        success=True,
        status="completed",
        matched_title=plan.target.matched_title,
    )


# ---------------------------------------------------------------------------
# source delete-by-title
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceDeleteByTitlePlan:
    """Prepared inputs for ``execute_source_delete_by_title``."""

    notebook_id: str
    source_id: str
    title: str


async def execute_source_delete_by_title(
    client: NotebookLMClient,
    plan: SourceDeleteByTitlePlan,
) -> SourceDeleteByTitleResult:
    """Delete the exact title-resolved target an adapter already authorized."""
    await client.sources.delete(plan.notebook_id, plan.source_id)
    return SourceDeleteByTitleResult(
        source_id=plan.source_id,
        title=plan.title,
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
    resolved_id = await resolve_source_id(client, plan.notebook_id, plan.source_id)
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


async def execute_source_refresh(
    client: NotebookLMClient,
    plan: SourceRefreshPlan,
    *,
    resolve_source_id: ResolveSourceIdFn,
) -> SourceRefreshResult:
    """Resolve + refresh a URL/Drive source.

    ``resolve_source_id`` is injected (see :func:`execute_source_rename`).
    """
    resolved_id = await resolve_source_id(client, plan.notebook_id, plan.source_id)

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

#: The declared Drive MIME choice → the :class:`~notebooklm.types.SourceType` that
#: labels the imported file.
#:
#: The NotebookLM backend returns a catch-all type code for most Drive imports:
#: everything but a Doc or a PDF comes back as code ``14``
#: (:attr:`~notebooklm.types.SourceType.GOOGLE_DRIVE`), so a Drive-hosted PDF or
#: Sheet would lose its format (#1828). On the Drive add path the caller's declared ``mime_type``
#: is authoritative for the imported file, so we stamp the corresponding type code
#: onto the returned source rather than exposing the raw (ambiguous) backend code.
_DRIVE_MIME_SOURCE_TYPE: dict[DriveMimeChoice, SourceType] = {
    "google-doc": SourceType.GOOGLE_DOCS,
    "google-slides": SourceType.GOOGLE_SLIDES,
    "google-sheets": SourceType.GOOGLE_SPREADSHEET,
    "pdf": SourceType.PDF,
}

#: :class:`SourceType` → the numeric ``_type_code`` the wire decoder assigns it.
#: These four mirror ``notebooklm._types.sources._SOURCE_TYPE_CODE_MAP``; the
#: ``_app`` boundary forbids importing that private map to invert it at runtime, so
#: they are pinned via the public enum here (no bare "magic" integer keys) and a
#: parity test asserts they never drift from the decoder map.
_SOURCE_TYPE_TO_CODE: dict[SourceType, int] = {
    SourceType.GOOGLE_DOCS: 1,
    SourceType.GOOGLE_SLIDES: 2,
    SourceType.PDF: 3,
    SourceType.GOOGLE_SPREADSHEET: 7,
}


def drive_mime_type_code(mime: DriveMimeChoice) -> int:
    """The ``Source._type_code`` a declared Drive ``mime`` should carry (#1828)."""
    return _SOURCE_TYPE_TO_CODE[_DRIVE_MIME_SOURCE_TYPE[mime]]


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
    """Add a Google Drive document as a source.

    Raises:
        ValidationError: ``plan.mime_type`` is not one of the supported Drive
            mime choices. The CLI never reaches this guard (Click validates the
            ``Choice`` first), but a transport adapter that forwards a raw string
            (MCP/HTTP) gets a clean ``VALIDATION`` rather than a leaked
            ``KeyError`` (ADR-0021).
    """
    if plan.mime_type not in _DRIVE_MIME_MAP:
        raise ValidationError(
            f"Invalid mime_type {plan.mime_type!r}; expected one of {sorted(_DRIVE_MIME_MAP)}. "
            "NotebookLM's Drive import only ingests Google-native Docs/Slides/Sheets + PDF; "
            "an upload-only Drive file (e.g. epub/docx/txt/md/rtf/odt/csv) must be "
            "downloaded and added as a `file` source instead."
        )
    mime = _DRIVE_MIME_MAP[plan.mime_type]

    src = await client.sources.add_drive(plan.notebook_id, plan.file_id, plan.title, mime)
    # Stamp the declared type onto the returned source. The backend returns an
    # ambiguous type code for Drive imports (a Drive-hosted PDF comes back as
    # ``14`` → GOOGLE_SPREADSHEET), so the caller's declared ``mime_type`` — not the
    # raw backend code — is authoritative for how the source is labeled (#1828). The
    # freshly returned ``Source`` is ours to finalize; ``kind`` derives from
    # ``_type_code``, so overwriting it is the whole fix.
    src._type_code = drive_mime_type_code(plan.mime_type)
    return SourceAddDriveResult(
        source=src,
        notebook_id=plan.notebook_id,
        file_id=plan.file_id,
        mime_type=plan.mime_type,
    )


# ---------------------------------------------------------------------------
# source add-drive-file (auto-route: download + upload upload-only Drive types)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceAddDriveFileResult:
    """Outcome of ``source add-drive-file`` (#1884).

    Typed-fields-only (§11): the ``--json`` envelope is built by the surface
    adapter (the CLI renderer / MCP tool) from these fields. ``document_id`` is
    the raw id/URL the caller passed, echoed for provenance.
    """

    source: Source
    notebook_id: str
    document_id: str


@dataclass(frozen=True)
class SourceAddDriveFilePlan:
    """Prepared inputs for ``execute_source_add_drive_file``."""

    notebook_id: str
    document_id: str
    title: str | None = None
    wait: bool = False
    wait_timeout: float = 120.0


async def execute_source_add_drive_file(
    client: NotebookLMClient,
    plan: SourceAddDriveFilePlan,
) -> SourceAddDriveFileResult:
    """Auto-route an upload-only Drive file: download it server-side, then upload.

    Thin executor over the public client (boundary-legal): the download +
    classification + resumable upload live in ``SourcesAPI.add_drive_file``. A
    native Google Doc/Slides/Sheet (not directly downloadable), an unsupported
    type, or expired Drive auth surface as :class:`ValidationError` from the
    client, which the surface adapters render.
    """
    src = await client.sources.add_drive_file(
        plan.notebook_id,
        plan.document_id,
        title=plan.title,
        wait=plan.wait,
        wait_timeout=plan.wait_timeout,
    )
    return SourceAddDriveFileResult(
        source=src,
        notebook_id=plan.notebook_id,
        document_id=plan.document_id,
    )


__all__ = [
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
    "SourceMutationMatch",
    "SourceMutationReason",
    "drive_mime_type_code",
    "execute_source_add_drive",
    "execute_source_add_drive_file",
    "execute_source_delete",
    "execute_source_delete_by_title",
    "execute_source_refresh",
    "execute_source_rename",
    "looks_like_full_source_id",
    "resolve_source_by_exact_title",
    "resolve_source_for_delete",
]
