"""Transport-neutral artifact business logic.

This is the Click-free core of ``cli/artifact_cmd.py``: it owns the behaviour the
artifact ``get`` / ``rename`` / ``delete`` / ``export`` / ``poll`` / ``wait`` /
``retry`` commands share — kind-aware mind-map dispatch, not-found raising, the
typed generation-status DTO, and the export workflow — and returns **typed**
dataclasses (or raises the public ``notebooklm.exceptions`` hierarchy) instead of
an adapter-shaped envelope dict. Each adapter (the Click CLI today, the FastMCP
server / future HTTP surface tomorrow) renders the typed result into its own
vocabulary; the CLI adapter rebuilds its historical ``--json`` envelope from
these fields.

Design seams worth calling out:

* **The ``list`` / ``get`` ``--json`` envelope build stays in the CLI.** The list
  command's Rich-table rows + emoji ``type`` display + ``notebook_title``
  envelope-extra are pure presentation (``cli/rendering.py`` /
  ``cli/services/listing.py``); the artifact-row ``--json`` projection itself is a
  presentation serializer, so it stays with the adapter rather than being
  replicated here. The neutral layer contributes the *behaviour* (kind-aware
  dispatch, not-found raising, the status DTO), and the adapter builds every
  ``--json`` envelope from the typed results' fields.

* **Mind-map detection is kind-aware and uses two different RPC families.** The
  ``rename`` path consults ``client.mind_maps.list`` (typed :class:`MindMap`
  objects carrying ``kind``) so it can route note-backed maps via ``UPDATE_NOTE``
  and interactive maps via ``RENAME_ARTIFACT`` behind the unified mind-map API
  (#1256). The ``delete`` path consults ``client.notes.list_mind_maps`` (raw note
  rows, matched on ``row[0] == artifact_id``) because note-backed maps are
  cleared via ``notes.delete``, not removed. Both are preserved exactly so the
  recorded RPC call sets stay cassette-stable.

* **The status DTO is neutral.** :class:`ArtifactStatusView` mirrors the public
  :class:`~notebooklm.types.GenerationStatus` fields the adapters read
  (``task_id`` / ``status`` / ``url`` / ``error`` / ``error_code`` / ``metadata``
  / ``is_complete``) so the poll / wait / retry adapters never reach into the
  client dataclass directly; :func:`status_view` projects a ``GenerationStatus``
  (or any object exposing those attributes) into it.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..exceptions import ArtifactNotFoundError
from ..types import Artifact, ExportType

if TYPE_CHECKING:
    from ..client import NotebookLMClient
    from ..types import GenerationStatus


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def get_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    artifact_id: str,
) -> Artifact:
    """Fetch a single artifact, raising :class:`ArtifactNotFoundError` on a miss.

    Mirrors the v0.8.0 fail-loud contract (issue #1247): ``get_or_none``
    returning ``None`` — the artifact was deleted between the partial-id resolve
    and the get, or a canonical UUID points at a since-deleted artifact — is
    surfaced as a typed not-found error the adapter maps to its own exit policy
    (the CLI emits a ``NOT_FOUND`` envelope + exit 1).
    """
    art = await client.artifacts.get_or_none(notebook_id, artifact_id)
    if art is None:
        raise ArtifactNotFoundError(artifact_id)
    return art


# ---------------------------------------------------------------------------
# rename (kind-aware mind-map dispatch)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRenameResult:
    """Typed outcome of ``artifact rename`` (the adapter builds its envelope)."""

    artifact_id: str
    new_title: str
    #: ``True`` when the renamed id was a mind map (routed via ``mind_maps.rename``).
    is_mind_map: bool


async def rename_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    artifact_id: str,
    new_title: str,
) -> ArtifactRenameResult:
    """Rename an artifact, dispatching mind maps through the kind-aware API.

    Mind maps need a kind-aware rename: note-backed maps via ``UPDATE_NOTE``,
    interactive (studio-artifact) maps via ``RENAME_ARTIFACT`` — both behind the
    unified mind-map API (#1256). Regular artifacts use ``RENAME_ARTIFACT``.

    ``return_object=False`` is passed on both paths: the adapter builds its
    confirmation from ``artifact_id`` + ``new_title`` and never uses the
    hydrated object, so the rename re-fetch (a full ``LIST_ARTIFACTS`` / get) is
    skipped. Existence is already proven for partial-id input (the resolver
    listed and raised on an absent id before this runs); a canonical full-UUID
    pointing at a since-deleted artifact prints a benign no-op "success" — a
    pre-existing condition, not introduced here.
    """
    mind_maps = await client.mind_maps.list(notebook_id)
    mind_map = next((m for m in mind_maps if m.id == artifact_id), None)
    if mind_map is not None:
        await client.mind_maps.rename(
            notebook_id, artifact_id, new_title, kind=mind_map.kind, return_object=False
        )
    else:
        await client.artifacts.rename(notebook_id, artifact_id, new_title, return_object=False)
    return ArtifactRenameResult(
        artifact_id=artifact_id,
        new_title=new_title,
        is_mind_map=mind_map is not None,
    )


# ---------------------------------------------------------------------------
# delete (note-backed mind-map vs regular artifact)
# ---------------------------------------------------------------------------


async def delete_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    artifact_id: str,
) -> bool:
    """Delete an artifact, clearing note-backed mind maps via ``notes.delete``.

    Note-backed mind maps live in the notes system; deleting one clears it (it
    is not removed — Google may garbage collect it later), so it routes through
    ``notes.delete`` while regular artifacts use ``artifacts.delete``. The
    membership probe matches the raw note-row ``id`` (``row[0]``) against the
    resolved artifact id, preserving the recorded RPC call set.

    Returns ``True`` when the deleted id was a note-backed mind map (so the
    adapter can flag the cleared-not-removed carve-out in its output), ``False``
    for a regular artifact.
    """
    mind_maps = await client.notes.list_mind_maps(notebook_id)
    for mm in mind_maps:
        if mm[0] == artifact_id:
            await client.notes.delete(notebook_id, artifact_id)
            return True
    await client.artifacts.delete(notebook_id, artifact_id)
    return False


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactExportResult:
    """Typed outcome of ``artifact export`` (the adapter builds its envelope)."""

    artifact_id: str
    title: str
    #: ``"docs"`` or ``"sheets"`` — the adapter's export-target vocabulary.
    export_type: str
    #: The raw export result the backend returned (URL dict or ``None`` on
    #: failure). ``None``/falsy means the export may have failed.
    result: Any

    @property
    def exported(self) -> bool:
        """Whether the backend returned a (truthy) export result.

        A derived typed predicate, not envelope-building — the adapter reads it
        for both its text branch and the ``exported`` JSON key.
        """
        return bool(self.result)


async def export_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    artifact_id: str,
    title: str,
    export_type: str,
) -> ArtifactExportResult:
    """Export an artifact to Google Docs/Sheets.

    ``export_type`` is the adapter's string vocabulary (``"sheets"`` →
    :attr:`ExportType.SHEETS`; anything else, i.e. ``"docs"``, →
    :attr:`ExportType.DOCS`). ``content`` is passed as ``None`` so the backend
    retrieves the content from ``artifact_id`` itself.
    """
    export_type_enum = ExportType.SHEETS if export_type == "sheets" else ExportType.DOCS
    result = await client.artifacts.export(notebook_id, artifact_id, None, title, export_type_enum)
    return ArtifactExportResult(
        artifact_id=artifact_id,
        title=title,
        export_type=export_type,
        result=result,
    )


# ---------------------------------------------------------------------------
# poll / wait / retry — neutral generation-status DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactStatusView:
    """Transport-neutral view of a generation-status snapshot.

    Mirrors the public :class:`~notebooklm.types.GenerationStatus` fields the
    poll / wait / retry adapters read so they never reach into the client
    dataclass directly. ``is_complete`` is captured at projection time (it is a
    derived property on the source object).
    """

    task_id: str
    status: str
    url: str | None
    error: str | None
    error_code: str | None
    metadata: dict[str, Any] | None
    is_complete: bool


def status_view(status: GenerationStatus) -> ArtifactStatusView:
    """Project a :class:`~notebooklm.types.GenerationStatus` into a neutral view.

    Accepts any object exposing the ``GenerationStatus`` attribute surface
    (``task_id`` / ``status`` / ``url`` / ``error`` / ``error_code`` /
    ``metadata`` / ``is_complete``) — including the ``MagicMock`` stubs the CLI
    tests pass — so the adapter projects once and renders off the view.
    """
    return ArtifactStatusView(
        task_id=status.task_id,
        status=status.status,
        url=status.url,
        error=status.error,
        error_code=getattr(status, "error_code", None),
        metadata=getattr(status, "metadata", None),
        is_complete=status.is_complete,
    )


async def poll_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    task_id: str,
) -> GenerationStatus:
    """One-shot generation-status check for ``artifact poll``.

    ``task_id`` is passed through unchanged (no partial-id resolution) so a
    freshly-issued task id from ``generate`` works before the artifact appears
    in ``artifact list``.

    Returns the raw :class:`~notebooklm.types.GenerationStatus` (rather than an
    :class:`ArtifactStatusView`) because the CLI's non-JSON path prints the
    status object directly; the adapter projects it with :func:`status_view`
    only when it builds the JSON envelope.
    """
    return await client.artifacts.poll_status(notebook_id, task_id)


async def retry_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    artifact_id: str,
) -> GenerationStatus:
    """Retry a failed Studio artifact in place (the UI "Retry" action).

    A synchronous refusal (rate limit / quota / not-retryable) propagates as the
    public exception for the adapter's error handler; on acceptance the returned
    status carries the kicked-off ``task_id``. The adapter reads ``task_id`` for
    its resume hint / spinner and projects with :func:`status_view` for JSON.
    """
    return await client.artifacts.retry_failed(notebook_id, artifact_id)


async def wait_for_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    artifact_id: str,
    *,
    initial_interval: float,
    timeout: float,
) -> GenerationStatus:
    """Block until artifact generation reaches a terminal state (or times out).

    Raises ``TimeoutError`` on timeout (the adapter renders its own timeout
    envelope); the transient-spinner UX + Ctrl-C resume-hint stay in the
    adapter, which owns the interactive surface. Returns the raw
    :class:`~notebooklm.types.GenerationStatus` so the adapter reads
    ``is_complete`` / ``url`` / ``error`` for its text branch and projects with
    :func:`status_view` for JSON.
    """
    return await client.artifacts.wait_for_completion(
        notebook_id,
        artifact_id,
        initial_interval=initial_interval,
        timeout=timeout,
    )


__all__ = [
    "ArtifactExportResult",
    "ArtifactRenameResult",
    "ArtifactStatusView",
    "delete_artifact",
    "export_artifact",
    "get_artifact",
    "poll_artifact",
    "rename_artifact",
    "retry_artifact",
    "status_view",
    "wait_for_artifact",
]
