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

        # Delete
        deleted = await client.notebooks.delete(notebook.id)
        assert deleted is True
        created_notebooks.remove(notebook.id)

    @pytest.mark.asyncio
    async def test_get_conversation_history(self, client, read_only_notebook_id):
        history = await client.chat.get_history(read_only_notebook_id)
        assert history is not None


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
        assert description.summary is not None
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
        # Summary may be empty string if not generated yet
        assert isinstance(summary, str)

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
        """Test sharing a notebook."""
        result = await client.notebooks.share(temp_notebook.id, public=True)
        # Share returns {"public": bool, "url": str|None, "artifact_id": str|None}
        assert isinstance(result, dict)
        assert result["public"] is True
        assert result["url"] is not None
        assert temp_notebook.id in result["url"]

    @pytest.mark.asyncio
    async def test_revoke_share_notebook(self, client, temp_notebook):
        """Test revoking notebook sharing."""
        result = await client.notebooks.share(temp_notebook.id, public=False)
        assert isinstance(result, dict)
        assert result["public"] is False
        assert result["url"] is None


@requires_auth
class TestNotebookRecent:
    """Tests for recent notebooks operations - use temp_notebook."""

    @pytest.mark.asyncio
    async def test_remove_from_recent(self, client, temp_notebook):
        """Test removing notebook from recent list."""
        # This should complete without error
        await client.notebooks.remove_from_recent(temp_notebook.id)
        # No return value expected, just no exception
