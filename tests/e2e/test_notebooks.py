import pytest

from notebooklm import ChatGoal, ChatMode, Notebook, NotebookDescription

from .conftest import requires_auth


@requires_auth
class TestNotebookOperations:
    @pytest.mark.asyncio
    async def test_list_notebooks(self, client):
        notebooks = await client.notebooks.list()
        assert isinstance(notebooks, list)
        assert all(isinstance(nb, Notebook) for nb in notebooks)

    @pytest.mark.asyncio
    async def test_get_notebook(self, client, read_only_notebook_id):
        notebook = await client.notebooks.get(read_only_notebook_id)
        assert notebook is not None
        assert isinstance(notebook, Notebook)
        assert notebook.id == read_only_notebook_id

    @pytest.mark.asyncio
    async def test_create_rename_delete_notebook(
        self, client, created_notebooks, cleanup_notebooks
    ):
        # Create
        notebook = await client.notebooks.create("E2E Test Notebook")
        assert isinstance(notebook, Notebook)
        assert notebook.title == "E2E Test Notebook"
        created_notebooks.append(notebook.id)

        # Rename
        await client.notebooks.rename(notebook.id, "E2E Test Renamed")

        # Delete (v0.7.0: returns None, idempotent — issue #1211)
        deleted = await client.notebooks.delete(notebook.id)
        assert deleted is None
        created_notebooks.remove(notebook.id)

    @pytest.mark.asyncio
    async def test_get_conversation_history(self, client, read_only_notebook_id):
        conversations = await client.chat.get_history(read_only_notebook_id)
        assert isinstance(conversations, list)


@requires_auth
class TestNotebookAsk:
    @pytest.mark.asyncio
    async def test_ask_notebook(self, client, read_only_notebook_id):
        result = await client.chat.ask(read_only_notebook_id, "What is this notebook about?")
        assert result.answer is not None
        assert result.conversation_id is not None


@requires_auth
class TestNotebookDescription:
    @pytest.mark.asyncio
    async def test_get_description(self, client, read_only_notebook_id):
        description = await client.notebooks.get_description(read_only_notebook_id)

        assert isinstance(description, NotebookDescription)
        assert description.summary, "Expected non-empty summary from get_description"
        assert isinstance(description.suggested_topics, list)


@requires_auth
class TestNotebookConfigure:
    @pytest.mark.asyncio
    async def test_configure_learning_mode(self, client, read_only_notebook_id):
        await client.chat.set_mode(read_only_notebook_id, ChatMode.LEARNING_GUIDE)

    @pytest.mark.asyncio
    async def test_configure_custom_persona(self, client, read_only_notebook_id):
        await client.chat.configure(
            read_only_notebook_id,
            goal=ChatGoal.CUSTOM,
            custom_prompt="You are a helpful science tutor",
        )

    @pytest.mark.asyncio
    async def test_reset_to_default(self, client, read_only_notebook_id):
        await client.chat.set_mode(read_only_notebook_id, ChatMode.DEFAULT)


@requires_auth
class TestNotebookSummary:
    """Tests for notebook summary operations."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_summary(self, client, read_only_notebook_id):
        """Test getting notebook summary."""
        summary = await client.notebooks.get_summary(read_only_notebook_id)
        assert summary, "Expected non-empty summary from get_summary"

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_raw(self, client, read_only_notebook_id):
        """Test getting raw notebook data."""
        raw_data = await client.notebooks.get_raw(read_only_notebook_id)
        assert raw_data is not None
        # Raw data is typically a list with notebook structure
        assert isinstance(raw_data, list)


@requires_auth
class TestNotebookSharing:
    """Tests for notebook sharing operations - use temp_notebook."""

    @pytest.mark.asyncio
    async def test_share_notebook(self, client, temp_notebook):
        """Test sharing a notebook (NotebooksAPI.share removed in v0.8.0; #1363)."""
        result = await client.sharing.set_public(temp_notebook.id, True)
        assert result.is_public is True
        assert result.share_url is not None
        assert temp_notebook.id in result.share_url

    @pytest.mark.asyncio
    async def test_revoke_share_notebook(self, client, temp_notebook):
        """Test revoking notebook sharing (use sharing.set_public; #1363)."""
        result = await client.sharing.set_public(temp_notebook.id, False)
        assert result.is_public is False
        assert result.share_url is None


@requires_auth
class TestNotebookRecent:
    """Tests for recent notebooks operations - use temp_notebook."""

    @pytest.mark.asyncio
    async def test_remove_from_recent(self, client, temp_notebook):
        """Test removing notebook from recent list."""
        # This should complete without error
        await client.notebooks.remove_from_recent(temp_notebook.id)
        # No return value expected, just no exception
