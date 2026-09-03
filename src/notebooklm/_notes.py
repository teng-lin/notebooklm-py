"""Backend-neutral notes namespace contract."""

from __future__ import annotations

import builtins
import contextlib
from abc import ABC, abstractmethod
from typing import Any

from ._runtime.call_supervisor import OperationLease
from .types import Note


class NotesAPI(ABC):
    """Operations on NotebookLM notes.

    Notes are user-created content, distinct from AI-generated artifacts.
    Notes support operations like export to Docs/Sheets and conversion to sources.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            # Create and update notes
            note = await client.notes.create(notebook_id, "My Note", "Content here")
            await client.notes.update(notebook_id, note.id, "Updated content", "New Title")

            # List and delete
            notes = await client.notes.list(notebook_id)
            await client.notes.delete(notebook_id, note.id)
    """

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

    @abstractmethod
    async def list(self, notebook_id: str) -> list[Note]:
        """List all text notes in the notebook.

        This excludes:
        - Mind maps (stored in same structure but contain JSON with 'children'/'nodes')
        - Deleted notes (status=2, content cleared but ID persists)

        Args:
            notebook_id: The notebook ID.

        Returns:
            List of Note objects.
        """

    @abstractmethod
    async def get(self, notebook_id: str, note_id: str) -> Note:
        """Get a specific note by ID.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        Returns:
            The :class:`~notebooklm.types.Note`.

        Raises:
            NoteNotFoundError: If no note with ``note_id`` exists (matches
                ``notebooks.get``; issue #1247). Use :meth:`get_or_none` for the
                sanctioned ``None``-on-miss lookup.
        """

    @abstractmethod
    async def get_or_none(self, notebook_id: str, note_id: str) -> Note | None:
        """Get a note by ID, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019): unlike :meth:`get`
        — which now raises ``NoteNotFoundError`` on a miss (#1247) — this
        returns ``None`` for a genuine absence and emits no
        deprecation warning. Transport, auth, and decode faults raised by the
        underlying note listing are **not** swallowed; only a real "not found"
        yields ``None``.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        Returns:
            The :class:`~notebooklm.types.Note`, or ``None`` if not found.
        """

    @abstractmethod
    async def create(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
    ) -> Note:
        """Create a new note in the notebook.

        Args:
            notebook_id: The notebook ID.
            title: The note title.
            content: The note content.

        Returns:
            The created Note object.
        """

    @abstractmethod
    async def update(
        self,
        notebook_id: str,
        note_id: str,
        content: str,
        title: str,
    ) -> None:
        """Update a note's content and title.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.
            content: The new content.
            title: The new title.

        Raises:
            NoteNotFoundError: When ``note_id`` does not exist. The
                ``UPDATE_NOTE`` RPC is ``allow_null=True`` and silently no-ops
                on a missing note, so a public-facade existence preflight runs
                first to make a mutate-existing op fail loud per ADR-0019
                Class 5.

        .. versionchanged:: 0.8.0
            **Breaking change:** updating a missing note now raises
            :class:`NoteNotFoundError` instead of silently "succeeding" via the
            ``allow_null=True`` no-op (#1362).
        """

    @abstractmethod
    async def delete(self, notebook_id: str, note_id: str) -> None:
        """Delete a note from the notebook.

        Note: This clears the note content/title rather than removing it
        from the list entirely. Google may garbage collect cleared notes later.

        Idempotent: deleting an already-absent note succeeds (returns
        ``None``) and never raises. Real failures (``403``/``5xx``/auth/
        transport) still propagate.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211). ``if await notes.delete(...):``
            no longer enters its block.
        """

    @abstractmethod
    async def list_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """List all mind maps in the notebook.

        Mind maps are stored in the same internal structure as notes but
        contain JSON data with 'children' or 'nodes' keys.

        Note: For most use cases, prefer `client.artifacts.list()` which returns
        mind maps as Artifact objects alongside other AI-generated content.

        This excludes deleted mind maps (status=2).

        Args:
            notebook_id: The notebook ID.

        Returns:
            List of raw mind map data.
        """

    @abstractmethod
    async def delete_mind_map(self, notebook_id: str, mind_map_id: str) -> None:
        """Delete a mind map from the notebook.

        Idempotent: deleting an already-absent mind map succeeds (returns
        ``None``) and never raises. Real failures (``403``/``5xx``/auth/
        transport) still propagate.

        Args:
            notebook_id: The notebook ID.
            mind_map_id: The mind map ID.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211).
        """


__all__ = ["NotesAPI"]
