"""Transport-neutral note business logic.

This is the Click-free core of ``cli/note_cmd.py``: it owns the
``create`` / ``get`` / ``save`` / ``rename`` / ``delete`` workflows, the
new-note-id extraction from the ``notes.create`` RPC shape, and the
get-then-update "preserve content" rename path (``resolve_note_content``). It
returns typed result dataclasses instead of an adapter-shaped envelope dict.
Every transport adapter (the Click CLI today, the FastMCP server / future HTTP
later) drives this core and renders the typed result into its own surface +
exit-code policy.

Two boundary-imposed seams are worth calling out:

* **The partial-id resolvers are injected, never imported.**
  ``cli.resolve.resolve_notebook_id`` / ``resolve_note_id`` reach into ``rich``
  consoles for their "Matched: ..." diagnostics, so this module cannot import
  them without breaking the ``_app`` boundary. The executors take
  ``resolve_notebook_id`` / ``resolve_note_id`` callables (the CLI wrapper
  passes its own, read at call time so the ``monkeypatch`` test seam lands).
* **The not-found typed error + exit policy stay in the CLI.** ``get`` / ``rename``
  return a result whose ``found`` flag is ``False`` when the row vanished
  between the partial-id resolve and the ``get``; the command layer maps that to
  its ``NOT_FOUND`` / exit-1 ``--json`` envelope (the BREAKING behaviour from
  issue #1247 / docs/cli-exit-codes.md). This core never raises for the
  concurrent-delete race — it reports it.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ..types import Note

if TYPE_CHECKING:
    from ..client import NotebookLMClient

#: Resolves a (possibly partial) notebook id to its full id (CLI injects
#: ``cli.resolve.resolve_notebook_id``; read at call time for the seam).
ResolveNotebookIdFn = Callable[..., Awaitable[str]]

#: Resolves a (possibly partial) note id to its full id (CLI injects
#: ``cli.resolve.resolve_note_id``).
ResolveNoteIdFn = Callable[..., Awaitable[str]]


def extract_new_note_id(result: Any) -> str | None:
    """Pull the new note id out of the ``notes.create`` RPC return.

    ``notes.create`` returns a nested list whose first element is the new note
    id, e.g. ``["note_xyz", ["note_xyz", content, ...]]``. Extract it
    defensively; a non-list / empty / non-string-first shape yields ``None``.
    """
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, str):
            return first
    return None


# ---------------------------------------------------------------------------
# note create
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoteCreateResult:
    """Outcome of ``note create``.

    ``raw`` is the unmodified ``notes.create`` RPC return (the text view prints
    it verbatim); ``note_id`` is the extracted new id (``None`` when the RPC
    shape did not yield one, which the CLI renders as the failure envelope).
    """

    notebook_id: str
    title: str
    note_id: str | None
    raw: Any

    @property
    def created(self) -> bool:
        return bool(self.raw) and self.note_id is not None


async def execute_note_create(
    client: NotebookLMClient,
    notebook_id: str,
    title: str,
    content: str,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    json_output: bool = False,
) -> NoteCreateResult:
    """Resolve the notebook + create a note, extracting the new id."""
    nb_id_resolved = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    raw = await client.notes.create(nb_id_resolved, title, content)
    return NoteCreateResult(
        notebook_id=nb_id_resolved,
        title=title,
        note_id=extract_new_note_id(raw),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# note get
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoteGetResult:
    """Outcome of ``note get``.

    ``found`` is ``False`` (and ``note`` is ``None``) when the row vanished
    between the partial-id resolve and the ``get`` — the CLI renders that as the
    typed ``NOT_FOUND`` / exit-1 envelope.
    """

    notebook_id: str
    note_id: str
    note: Note | None

    @property
    def found(self) -> bool:
        return isinstance(self.note, Note)


async def execute_note_get(
    client: NotebookLMClient,
    notebook_id: str,
    note_id: str,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    resolve_note_id: ResolveNoteIdFn,
    json_output: bool = False,
) -> NoteGetResult:
    """Resolve the notebook + note ids and fetch the note content."""
    nb_id_resolved = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    resolved_id = await resolve_note_id(client, nb_id_resolved, note_id, json_output=json_output)
    note = await client.notes.get_or_none(nb_id_resolved, resolved_id)
    return NoteGetResult(
        notebook_id=nb_id_resolved,
        note_id=resolved_id,
        note=note if isinstance(note, Note) else None,
    )


# ---------------------------------------------------------------------------
# note save (update)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoteSaveResult:
    """Outcome of ``note save``."""

    notebook_id: str
    note_id: str


async def execute_note_save(
    client: NotebookLMClient,
    notebook_id: str,
    note_id: str,
    *,
    title: str | None,
    content: str | None,
    resolve_notebook_id: ResolveNotebookIdFn,
    resolve_note_id: ResolveNoteIdFn,
    json_output: bool = False,
) -> NoteSaveResult:
    """Resolve the notebook + note ids and update the note.

    The "no changes" guard (neither ``--title`` nor ``--content``) is a CLI-side
    early return so it can avoid a network round-trip and render its own no-op
    envelope; this core is only reached once at least one field is supplied.
    """
    nb_id_resolved = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    resolved_id = await resolve_note_id(client, nb_id_resolved, note_id, json_output=json_output)
    # ``update`` is typed ``content/title: str`` but the RPC + facade accept
    # ``None`` for "leave unchanged" (the historical CLI relied on this); the
    # ``--title``/``--content`` early-return guard in the command layer ensures
    # at least one is supplied. Cast to preserve the exact runtime call.
    await client.notes.update(
        nb_id_resolved,
        resolved_id,
        content=cast(str, content),
        title=cast(str, title),
    )
    return NoteSaveResult(notebook_id=nb_id_resolved, note_id=resolved_id)


# ---------------------------------------------------------------------------
# note rename (get-then-update, preserving content)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoteRenameResult:
    """Outcome of ``note rename``.

    ``found`` is ``False`` when the note vanished between ``resolve_note_id`` and
    the content-preserving ``get`` (concurrent-delete race); the CLI maps that to
    the same typed ``NOT_FOUND`` / exit-1 path as ``note get``.
    """

    notebook_id: str
    note_id: str
    new_title: str
    found: bool


async def execute_note_rename(
    client: NotebookLMClient,
    notebook_id: str,
    note_id: str,
    new_title: str,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    resolve_note_id: ResolveNoteIdFn,
    json_output: bool = False,
) -> NoteRenameResult:
    """Resolve + rename a note, preserving its content (``resolve_note_content``).

    Fetches the current note to carry its content through the update. If the
    note vanished between the resolve and this ``get`` (a concurrent
    ``note delete`` won the race), reports ``found=False`` so the CLI emits the
    typed not-found error rather than a misleading success.
    """
    nb_id_resolved = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    resolved_id = await resolve_note_id(client, nb_id_resolved, note_id, json_output=json_output)
    note = await client.notes.get_or_none(nb_id_resolved, resolved_id)
    if not isinstance(note, Note):
        return NoteRenameResult(
            notebook_id=nb_id_resolved,
            note_id=resolved_id,
            new_title=new_title,
            found=False,
        )

    await client.notes.update(
        nb_id_resolved, resolved_id, content=note.content or "", title=new_title
    )
    return NoteRenameResult(
        notebook_id=nb_id_resolved,
        note_id=resolved_id,
        new_title=new_title,
        found=True,
    )


# ---------------------------------------------------------------------------
# note delete
# ---------------------------------------------------------------------------


async def resolve_note_for_delete(
    client: NotebookLMClient,
    notebook_id: str,
    note_id: str,
    *,
    resolve_notebook_id: ResolveNotebookIdFn,
    resolve_note_id: ResolveNoteIdFn,
    json_output: bool = False,
) -> tuple[str, str]:
    """Resolve the notebook + note ids for a delete, returning ``(nb_id, note_id)``."""
    nb_id_resolved = await resolve_notebook_id(client, notebook_id, json_output=json_output)
    resolved_id = await resolve_note_id(client, nb_id_resolved, note_id, json_output=json_output)
    return nb_id_resolved, resolved_id


async def execute_note_delete(
    client: NotebookLMClient,
    notebook_id: str,
    note_id: str,
) -> None:
    """Delete a note by its full id (raises on real failure)."""
    await client.notes.delete(notebook_id, note_id)


__all__ = [
    "NoteCreateResult",
    "NoteGetResult",
    "NoteRenameResult",
    "NoteSaveResult",
    "ResolveNoteIdFn",
    "ResolveNotebookIdFn",
    "execute_note_create",
    "execute_note_delete",
    "execute_note_get",
    "execute_note_rename",
    "execute_note_save",
    "extract_new_note_id",
    "resolve_note_for_delete",
]
