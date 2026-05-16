"""Notes API for NotebookLM user-created notes.

Provides operations for creating, updating, listing, and deleting
user-created notes in notebooks. Notes are distinct from artifacts -
they are user-created content, not AI-generated.

Mind-map-related RPC primitives live in :mod:`_mind_map` and are shared
with :class:`ArtifactsAPI`. This module exposes them through the
historical ``NotesAPI`` method surface for backward compatibility.
"""

import builtins
import logging
from typing import Any

from . import _mind_map
from ._core import ClientCore
from .rpc import RPCMethod
from .types import AskResult, Note

logger = logging.getLogger(__name__)


class NotesAPI:
    """Operations on NotebookLM notes.

    Notes are user-created content, distinct from AI-generated artifacts.
    Notes support operations like export to Docs/Sheets and conversion to sources.

    Usage:
        async with await NotebookLMClient.from_storage() as client:
            # Create and update notes
            note = await client.notes.create(notebook_id, "My Note", "Content here")
            await client.notes.update(notebook_id, note.id, "Updated content", "New Title")

            # List and delete
            notes = await client.notes.list(notebook_id)
            await client.notes.delete(notebook_id, note.id)
    """

    def __init__(self, core: ClientCore):
        """Initialize the notes API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core

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
        notes = []

        for item in all_items:
            # Skip deleted items (status=2): ['id', None, 2]
            if self._is_deleted(item):
                continue

            content = self._extract_content(item)
            is_mind_map = content and ('"children":' in content or '"nodes":' in content)
            if not is_mind_map:
                notes.append(self._parse_note(item, notebook_id))

        return notes

    async def get(self, notebook_id: str, note_id: str) -> Note | None:
        """Get a specific note by ID.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        Returns:
            Note object, or None if not found.
        """
        all_items = await self._get_all_notes_and_mind_maps(notebook_id)
        for item in all_items:
            if isinstance(item, list) and len(item) > 0 and item[0] == note_id:
                return self._parse_note(item, notebook_id)
        return None

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
        return await _mind_map.create_note(self._core, notebook_id, title=title, content=content)

    async def create_from_chat(
        self,
        notebook_id: str,
        ask_result: AskResult,
        *,
        title: str | None = None,
    ) -> Note:
        """Save a chat answer as a citation-rich note (issue #660).

        Unlike :meth:`create`, this preserves the ``[N]`` citation
        markers as interactive hover-anchored references in the
        NotebookLM web UI. It mirrors the wire format the web UI's
        "Save to note" button uses.

        The notebook must already have a streaming-chat response in
        ``ask_result`` with non-empty ``references``. Callers without
        citations should fall back to :meth:`create` for plain-text
        notes — this method raises :class:`ValueError` rather than
        silently degrading to plain text, so the caller can decide.

        Args:
            notebook_id: The notebook ID.
            ask_result: Result from a prior ``client.chat.ask()`` call.
                Must have non-empty ``references`` — otherwise this
                method raises ``ValueError``.
            title: Note title. When ``None`` (default), a title is
                derived from the first 50 characters of the answer.
                The NotebookLM server may apply smart-title generation
                for saved-from-chat notes; the returned ``Note.title``
                reflects what the server actually stored.

        Returns:
            The created ``Note``. ``Note.content`` holds the answer
            text WITH ``[N]`` markers; the rich citation anchors live
            server-side and surface via the NotebookLM web UI.

        Raises:
            ValueError: If ``ask_result.references`` is empty.
        """
        if not ask_result.references:
            raise ValueError(
                "create_from_chat requires AskResult.references to be "
                "non-empty; use notes.create() for plain-text notes."
            )
        resolved_title = (
            title
            if title is not None
            else f"Chat: {ask_result.answer[:50].strip().replace(chr(10), ' ')}"
        )
        return await _mind_map.save_chat_answer_as_note(
            self._core,
            notebook_id,
            ask_result.answer,
            ask_result.references,
            resolved_title,
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
        """
        await _mind_map.update_note(self._core, notebook_id, note_id, content, title)

    async def delete(self, notebook_id: str, note_id: str) -> bool:
        """Delete a note from the notebook.

        Note: This clears the note content/title rather than removing it
        from the list entirely. Google may garbage collect cleared notes later.

        Args:
            notebook_id: The notebook ID.
            note_id: The note ID.

        Returns:
            True if deletion succeeded.
        """
        logger.debug("Deleting note %s from notebook %s", note_id, notebook_id)
        params = [notebook_id, None, [note_id]]
        await self._core.rpc_call(
            RPCMethod.DELETE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return True

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
        return await _mind_map.list_mind_maps(self._core, notebook_id)

    async def delete_mind_map(self, notebook_id: str, mind_map_id: str) -> bool:
        """Delete a mind map from the notebook.

        Args:
            notebook_id: The notebook ID.
            mind_map_id: The mind map ID.

        Returns:
            True if deletion succeeded.
        """
        params = [notebook_id, None, [mind_map_id]]
        await self._core.rpc_call(
            RPCMethod.DELETE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return True

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _get_all_notes_and_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """Fetch all notes and mind maps from the API."""
        return await _mind_map.fetch_all_notes_and_mind_maps(self._core, notebook_id)

    def _is_deleted(self, item: builtins.list[Any]) -> bool:
        """Check if a note/mind map item is deleted (status=2).

        Deleted items have structure: ['id', None, 2]
        The content at position [1] is None and status at [2] is 2.

        Args:
            item: Raw note/mind map data.

        Returns:
            True if the item is deleted (soft-deleted with status=2).
        """
        return _mind_map.is_deleted(item)

    def _extract_content(self, item: builtins.list[Any]) -> str | None:
        """Extract content string from note/mind map item."""
        return _mind_map.extract_content(item)

    def _parse_note(self, item: builtins.list[Any], notebook_id: str) -> Note:
        """Parse a raw note item into a Note object."""
        note_id = item[0] if len(item) > 0 else ""

        content = ""
        title = ""

        if len(item) > 1:
            if isinstance(item[1], str):
                # Old format: [note_id, content]
                content = item[1]
            elif isinstance(item[1], list):
                # New format: [note_id, [note_id, content, metadata, None, title]]
                inner = item[1]
                if len(inner) > 1 and isinstance(inner[1], str):
                    content = inner[1]
                if len(inner) > 4 and isinstance(inner[4], str):
                    title = inner[4]

        return Note(
            id=str(note_id),
            notebook_id=notebook_id,
            title=title,
            content=content,
        )
