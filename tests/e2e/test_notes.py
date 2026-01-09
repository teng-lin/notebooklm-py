"""E2E tests for NotesAPI."""

import pytest

from .conftest import requires_auth


@requires_auth
class TestNotesList:
    """Test listing notes."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_notes(self, client, read_only_notebook_id):
        """List notes in test notebook - read-only."""
        notes = await client.notes.list(read_only_notebook_id)
        assert isinstance(notes, list)


@requires_auth
class TestNotesGet:
    """Test getting individual notes."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_note(self, client, read_only_notebook_id):
        """Get a specific note from test notebook - read-only."""
        notes = await client.notes.list(read_only_notebook_id)
        if not notes:
            pytest.skip("No notes available in test notebook")

        note = await client.notes.get(read_only_notebook_id, notes[0].id)
        assert note is not None
        assert note.id == notes[0].id

    @pytest.mark.asyncio
    async def test_get_note_not_found(self, client, read_only_notebook_id):
        """Test getting a non-existent note returns None."""
        note = await client.notes.get(read_only_notebook_id, "nonexistent_note_id")
        assert note is None


@requires_auth
class TestNotesCRUD:
    """Test note CRUD operations - uses temp notebook."""

    @pytest.mark.asyncio
    async def test_create_and_delete_note(self, client, temp_notebook):
        """Create and delete a note in temp notebook."""
        # Create
        note = await client.notes.create(
            temp_notebook.id,
            title="Test Note",
            content="Test content for E2E",
        )
        assert note is not None
        assert note.id != ""
        assert note.title == "Test Note"
        assert note.content == "Test content for E2E"

        # Verify it appears in list
        notes = await client.notes.list(temp_notebook.id)
        note_ids = [n.id for n in notes]
        assert note.id in note_ids

        # Delete
        result = await client.notes.delete(temp_notebook.id, note.id)
        assert result is True

    @pytest.mark.asyncio
    async def test_update_note(self, client, temp_notebook):
        """Update a note's content and title."""
        # Create
        note = await client.notes.create(
            temp_notebook.id,
            title="Original Title",
            content="Original content",
        )

        # Update
        await client.notes.update(
            temp_notebook.id,
            note.id,
            content="Updated content",
            title="Updated Title",
        )

        # Verify
        updated = await client.notes.get(temp_notebook.id, note.id)
        assert updated is not None
        assert updated.title == "Updated Title"
        assert updated.content == "Updated content"

        # Cleanup
        await client.notes.delete(temp_notebook.id, note.id)


@requires_auth
class TestMindMaps:
    """Test mind map operations."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_mind_maps(self, client, read_only_notebook_id):
        """List mind maps in test notebook - read-only."""
        mind_maps = await client.notes.list_mind_maps(read_only_notebook_id)
        assert isinstance(mind_maps, list)
