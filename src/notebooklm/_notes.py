"""Notes API for NotebookLM user-created notes.

Provides operations for creating, updating, listing, and deleting
user-created notes in notebooks. Notes are distinct from artifacts -
they are user-created content, not AI-generated.
"""

import builtins
import logging
from typing import Any

from ._core import ClientCore
from .rpc import RPCMethod
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
        logger.debug("Creating note in notebook %s: %s", notebook_id, title)
        params = [notebook_id, "", [1], None, "New Note"]
        result = await self._core.rpc_call(
            RPCMethod.CREATE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        note_id = None
        if result and isinstance(result, list) and len(result) > 0:
            if isinstance(result[0], list) and len(result[0]) > 0:
                note_id = result[0][0]
            elif isinstance(result[0], str):
                note_id = result[0]

        if note_id:
            # Google ignores title param in CREATE_NOTE, so always update
            await self.update(notebook_id, note_id, content, title)

        return Note(
            id=note_id or "",
            notebook_id=notebook_id,
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
        """
        logger.debug("Updating note %s in notebook %s", note_id, notebook_id)
        params = [
            notebook_id,
            note_id,
            [[[content, title, [], 0]]],
        ]
        await self._core.rpc_call(
            RPCMethod.UPDATE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

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
        all_items = await self._get_all_notes_and_mind_maps(notebook_id)
        mind_maps = []

        for item in all_items:
            # Skip deleted items (status=2): ['id', None, 2]
            if self._is_deleted(item):
                continue

            content = self._extract_content(item)
            if content and ('"children":' in content or '"nodes":' in content):
                mind_maps.append(item)

        return mind_maps

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
        params = [notebook_id]
        result = await self._core.rpc_call(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if result and isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
            notes_list = result[0]
            valid_notes = []
            for item in notes_list:
                if isinstance(item, list) and len(item) > 0 and isinstance(item[0], str):
                    valid_notes.append(item)
            return valid_notes
        return []

    def _is_deleted(self, item: builtins.list[Any]) -> bool:
        """Check if a note/mind map item is deleted (status=2).

        Deleted items have structure: ['id', None, 2]
        The content at position [1] is None and status at [2] is 2.

        Args:
            item: Raw note/mind map data.

        Returns:
            True if the item is deleted (soft-deleted with status=2).
        """
        if not isinstance(item, list) or len(item) < 3:
            return False
        return item[1] is None and item[2] == 2

    def _extract_content(self, item: builtins.list[Any]) -> str | None:
        """Extract content string from note/mind map item."""
        if len(item) <= 1:
            return None

        if isinstance(item[1], str):
            return item[1]
        elif isinstance(item[1], list) and len(item[1]) > 1 and isinstance(item[1][1], str):
            return item[1][1]
        return None

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
