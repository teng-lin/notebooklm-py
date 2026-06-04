"""Notes API for NotebookLM user-created notes.

Provides operations for creating, updating, listing, and deleting
user-created notes in notebooks. Notes are distinct from artifacts -
they are user-created content, not AI-generated.

Note-row primitives live in :mod:`_note_service` and the
mind-map-only facade lives in :mod:`_mind_map` as
:class:`NoteBackedMindMapService`. Saving a chat answer as a
citation-rich note lives on :class:`ChatAPI` as ``save_answer_as_note``
(refactor-history.md Step 8, ADR-0013); the former
``NotesAPI.create_from_chat`` forwarder was removed in v0.7.0.
"""

from __future__ import annotations

import builtins
import logging
from typing import Any

from ._deprecation import future_errors_enabled
from ._lookup import resolve_get
from ._mind_map import NoteBackedMindMapService
from ._note_service import NoteRowKind, NoteService
from ._row_adapters.notes import NoteRow
from .exceptions import NoteNotFoundError
from .types import Note

logger = logging.getLogger(__name__)


class NotesAPI:
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

    def __init__(
        self,
        *,
        notes: NoteService,
        mind_maps: NoteBackedMindMapService,
    ):
        """Initialize the notes API.

        Args:
            notes: Backend note-row primitives. Owns
                ``fetch_note_rows`` / ``classify_row`` / ``create_note``
                / ``update_note`` / ``delete_note``.
            mind_maps: Mind-map-only facade backed by ``notes``. Owns
                the ``list_mind_maps`` / ``delete_mind_map`` paths the
                public ``NotesAPI`` surface forwards through.
        """
        self._notes = notes
        self._mind_maps = mind_maps

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
        logger.debug("Listing notes in notebook: %s", notebook_id)
        all_items = await self._get_all_notes_and_mind_maps(notebook_id)
        notes: list[Note] = []

        for item in all_items:
            kind = self._notes.classify_row(item)
            if kind in (NoteRowKind.DELETED, NoteRowKind.MIND_MAP):
                continue
            notes.append(self._parse_note(item, notebook_id))

        return notes

    async def get(self, notebook_id: str, note_id: str) -> Note | None:
        """Get a specific note by ID.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        Returns:
            Note object, or None if not found.

        .. deprecated:: 0.7.0
            Returning ``None`` for a missing note is deprecated and emits a
            :class:`DeprecationWarning`. In **v0.8.0** this method will raise
            ``NoteNotFoundError`` instead, to match ``notebooks.get`` (issue
            #1247). Wrap the call in ``try/except NoteNotFoundError`` to keep
            handling missing notes. Suppress the warning with
            ``NOTEBOOKLM_QUIET_DEPRECATIONS``, or set
            ``NOTEBOOKLM_FUTURE_ERRORS=1`` to preview the v0.8.0 raise now.
        """
        # The warn-runway / raise decision is single-sourced in
        # ``_lookup.resolve_get``: it warns and returns ``None`` today, or
        # raises ``NoteNotFoundError`` under ``NOTEBOOKLM_FUTURE_ERRORS`` (the
        # v0.8.0 flip, issue #1247). Internal callers that need the silent
        # optional-lookup must use ``get_or_none`` directly.
        return resolve_get(
            await self.get_or_none(notebook_id, note_id),
            not_found=NoteNotFoundError(note_id),
            resource="note",
        )

    async def get_or_none(self, notebook_id: str, note_id: str) -> Note | None:
        """Get a note by ID, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019): unlike :meth:`get`
        — which is slated to raise ``NoteNotFoundError`` on a miss in v0.8.0
        (issue #1247) — this returns ``None`` for a genuine absence and emits no
        deprecation warning. Transport, auth, and decode faults raised by the
        underlying note listing are **not** swallowed; only a real "not found"
        yields ``None``.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        Returns:
            The :class:`~notebooklm.types.Note`, or ``None`` if not found.
        """
        all_items = await self._get_all_notes_and_mind_maps(notebook_id)
        for item in all_items:
            if isinstance(item, list) and len(item) > 0 and item[0] == note_id:
                return self._parse_note(item, notebook_id)
        return None

    # Internal optional-lookup alias: kept as a stable private name so existing
    # internal call sites and tests can probe without the public deprecation.
    _get_or_none = get_or_none

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
        return await self._notes.create_note(
            notebook_id,
            title=title,
            content=content,
        )

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
            NoteNotFoundError: When ``NOTEBOOKLM_FUTURE_ERRORS`` (the v0.8.0
                preview, issue #1362) is enabled and ``note_id`` does not exist.
                The ``UPDATE_NOTE`` RPC is ``allow_null=True`` and silently
                no-ops on a missing note, so the current (v0.7.0) contract
                silently "succeeds" on a miss; the preview adds a public-facade
                existence preflight so a mutate-existing op fails loud per
                ADR-0019 Class 5.
        """
        if future_errors_enabled():
            # v0.8.0 preview (issue #1362): detect a missing target at the
            # public facade (never inside ``_note_service`` — that crosses the
            # layer boundary). ``get_or_none`` is the silent optional-lookup;
            # only a genuine miss yields ``None`` (transport/auth/decode faults
            # propagate). Default-off keeps the historical silent no-op.
            if await self.get_or_none(notebook_id, note_id) is None:
                raise NoteNotFoundError(note_id)
        await self._notes.update_note(notebook_id, note_id, content, title)

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
        logger.debug("Deleting note %s from notebook %s", note_id, notebook_id)
        await self._notes.delete_note(notebook_id, note_id)

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
        return await self._mind_maps.list_mind_maps(notebook_id)

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
        await self._mind_maps.delete_mind_map(notebook_id, mind_map_id)

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _get_all_notes_and_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """Fetch all notes and mind maps from the API."""
        return await self._notes.fetch_note_rows(notebook_id)

    def _is_deleted(self, item: builtins.list[Any]) -> bool:
        """Check if a note/mind map item is deleted (status=2).

        Delegates to :meth:`NoteService.classify_row`, which reads the
        deletion sentinel via :attr:`NoteRow.is_deleted`. The wire
        shape (``[id, None, 2]`` — content slot ``None`` plus the
        soft-delete sentinel at position 2) is documented on
        :class:`NoteRow`; this method exists only as the historical
        ``NotesAPI`` private surface.

        Args:
            item: Raw note/mind map data.

        Returns:
            True if the item is deleted (soft-deleted with status=2).
        """
        return self._notes.classify_row(item) == NoteRowKind.DELETED

    def _extract_content(self, item: builtins.list[Any]) -> str | None:
        """Extract content string from note/mind map item."""
        return self._notes.extract_content(item)

    def _parse_note(self, item: builtins.list[Any], notebook_id: str) -> Note:
        """Parse a raw note item into a Note object.

        Position knowledge (legacy ``[id, content]`` vs current
        ``[id, [id, content, metadata, None, title]]`` dispatch, and
        the title slot at ``raw[1][4]``) lives in
        :class:`notebooklm._row_adapters.notes.NoteRow` — this method just
        reads the named properties. ``content`` defaults to ``""``
        (not ``None``) here to preserve the v0.4.1 :class:`Note`
        contract.
        """
        row = NoteRow(item)
        return Note(
            id=row.id,
            notebook_id=notebook_id,
            title=row.title,
            content=row.content or "",
        )
