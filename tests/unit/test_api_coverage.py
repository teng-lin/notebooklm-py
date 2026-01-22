"""Unit tests for new API coverage features."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.rpc.types import (
    ChatGoal,
    ChatResponseLength,
    DriveMimeType,
    RPCMethod,
)


class TestNewEnums:
    """Tests for newly added enums."""

    def test_chat_goal_values(self):
        """Test ChatGoal enum values match API spec."""
        assert ChatGoal.DEFAULT == 1
        assert ChatGoal.CUSTOM == 2
        assert ChatGoal.LEARNING_GUIDE == 3

    def test_chat_response_length_values(self):
        """Test ChatResponseLength enum values match API spec."""
        assert ChatResponseLength.DEFAULT == 1
        assert ChatResponseLength.LONGER == 4
        assert ChatResponseLength.SHORTER == 5

    def test_drive_mime_type_values(self):
        """Test DriveMimeType enum values."""
        assert DriveMimeType.GOOGLE_DOC == "application/vnd.google-apps.document"
        assert DriveMimeType.GOOGLE_SLIDES == "application/vnd.google-apps.presentation"
        assert DriveMimeType.GOOGLE_SHEETS == "application/vnd.google-apps.spreadsheet"
        assert DriveMimeType.PDF == "application/pdf"

    def test_get_suggested_reports_rpc_id(self):
        """Test GET_SUGGESTED_REPORTS RPC ID exists."""
        assert RPCMethod.GET_SUGGESTED_REPORTS == "ciyUvf"


class TestConfigureChat:
    """Tests for configure_chat method."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock NotebookLMClient."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="test_csrf",
            session_id="test_session",
        )
        client = NotebookLMClient(auth)
        client._core._http_client = MagicMock()
        client._core.rpc_call = AsyncMock(return_value=None)
        return client

    @pytest.mark.asyncio
    async def test_configure_chat_default(self, mock_client):
        """Test configure_chat with default settings."""
        await mock_client.chat.configure("notebook_123")

        mock_client._core.rpc_call.assert_called_once()
        call_args = mock_client._core.rpc_call.call_args
        params = call_args[0][1]

        # Verify payload structure
        assert params[0] == "notebook_123"
        assert params[1][0][7] == [[1], [1]]  # Default goal, default length

    @pytest.mark.asyncio
    async def test_configure_chat_custom_prompt(self, mock_client):
        """Test configure_chat with custom prompt."""
        await mock_client.chat.configure(
            "notebook_123",
            goal=ChatGoal.CUSTOM,
            custom_prompt="Be an expert analyst",
        )

        call_args = mock_client._core.rpc_call.call_args
        params = call_args[0][1]

        # Verify custom prompt is included
        assert params[1][0][7][0] == [2, "Be an expert analyst"]

    @pytest.mark.asyncio
    async def test_configure_chat_custom_requires_prompt(self, mock_client):
        """Test configure_chat raises error when CUSTOM goal without prompt."""
        from notebooklm.exceptions import ValidationError

        with pytest.raises(ValidationError, match="custom_prompt is required"):
            await mock_client.chat.configure(
                "notebook_123",
                goal=ChatGoal.CUSTOM,
            )

    @pytest.mark.asyncio
    async def test_configure_chat_learning_guide(self, mock_client):
        """Test configure_chat with learning guide mode."""
        await mock_client.chat.configure(
            "notebook_123",
            goal=ChatGoal.LEARNING_GUIDE,
            response_length=ChatResponseLength.LONGER,
        )

        call_args = mock_client._core.rpc_call.call_args
        params = call_args[0][1]

        assert params[1][0][7] == [[3], [4]]  # Learning guide, longer


class TestGetSourceGuide:
    """Tests for get_source_guide method."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock NotebookLMClient."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="test_csrf",
            session_id="test_session",
        )
        client = NotebookLMClient(auth)
        client._core._http_client = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_get_source_guide_parses_response(self, mock_client):
        """Test get_source_guide correctly parses API response."""
        # Real API returns 3 levels of nesting: [[[null, [summary], [[keywords]], []]]]
        mock_response = [
            [
                [
                    None,
                    ["This is a **summary** of the document."],
                    [["Topic 1", "Topic 2", "Topic 3"]],
                    [],
                ]
            ]
        ]
        mock_client._core.rpc_call = AsyncMock(return_value=mock_response)

        result = await mock_client.sources.get_guide("notebook_123", "source_456")

        assert result["summary"] == "This is a **summary** of the document."
        assert result["keywords"] == ["Topic 1", "Topic 2", "Topic 3"]

    @pytest.mark.asyncio
    async def test_get_source_guide_handles_empty(self, mock_client):
        """Test get_source_guide handles empty response."""
        mock_client._core.rpc_call = AsyncMock(return_value=None)

        result = await mock_client.sources.get_guide("notebook_123", "source_456")

        assert result["summary"] == ""
        assert result["keywords"] == []


class TestGetSuggestedReportFormats:
    """Tests for get_suggested_report_formats method."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock NotebookLMClient."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="test_csrf",
            session_id="test_session",
        )
        client = NotebookLMClient(auth)
        client._core._http_client = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_get_suggested_report_formats_parses_response(self, mock_client):
        """Test get_suggested_report_formats correctly parses API response."""
        # Response format: [[[title, description, null, null, prompt, audience_level], ...]]
        mock_response = [
            [
                ["Strategy Report", "Analysis of...", None, None, "Create a detailed...", 2],
                ["Summary Brief", "Quick overview...", None, None, "Summarize the...", 1],
            ]
        ]
        mock_client._core.rpc_call = AsyncMock(return_value=mock_response)
        mock_client._core.get_notebook = AsyncMock(return_value=[[None, []]])
        mock_client.notebooks.get = AsyncMock(return_value=MagicMock(sources=[]))

        result = await mock_client.artifacts.suggest_reports("notebook_123")

        assert len(result) == 2
        assert result[0].title == "Strategy Report"
        assert result[0].description == "Analysis of..."
        assert result[0].prompt == "Create a detailed..."


class TestAddSourceDrive:
    """Tests for add_source_drive method."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock NotebookLMClient."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="test_csrf",
            session_id="test_session",
        )
        client = NotebookLMClient(auth)
        client._core._http_client = MagicMock()
        client._core.rpc_call = AsyncMock(return_value=[["source_id_123"]])
        return client

    @pytest.mark.asyncio
    async def test_add_source_drive_payload_structure(self, mock_client):
        """Test add_source_drive creates correct payload."""
        await mock_client.sources.add_drive(
            "notebook_123",
            file_id="drive_file_abc",
            title="My Document",
            mime_type=DriveMimeType.GOOGLE_DOC.value,
        )

        call_args = mock_client._core.rpc_call.call_args
        params = call_args[0][1]

        # Verify source data structure
        source_data = params[0][0][0]
        assert source_data[0] == [
            "drive_file_abc",
            "application/vnd.google-apps.document",
            1,
            "My Document",
        ]
        assert source_data[10] == 1  # Trailing 1


class TestGetNotebookDescription:
    """Tests for get_notebook_description method."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock NotebookLMClient."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="test_csrf",
            session_id="test_session",
        )
        client = NotebookLMClient(auth)
        client._core._http_client = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_get_notebook_description_parses_response(self, mock_client):
        """Test get_notebook_description parses full response."""
        mock_response = [
            ["This notebook explores **AI** and **machine learning**."],
            [
                [
                    ["What is the future of AI?", "Create a detailed briefing..."],
                    ["How does ML work?", "Explain the fundamentals..."],
                ]
            ],
        ]
        mock_client._core.rpc_call = AsyncMock(return_value=mock_response)

        result = await mock_client.notebooks.get_description("notebook_123")

        assert "AI" in result.summary
        assert len(result.suggested_topics) == 2
        assert result.suggested_topics[0].question == "What is the future of AI?"
        assert "briefing" in result.suggested_topics[0].prompt


class TestPayloadFixes:
    """Tests for fixed payload structures."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock NotebookLMClient."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="test_csrf",
            session_id="test_session",
        )
        client = NotebookLMClient(auth)
        client._core._http_client = MagicMock()
        client._core.rpc_call = AsyncMock(return_value=True)
        return client

    @pytest.mark.asyncio
    async def test_check_source_freshness_payload(self, mock_client):
        """Test check_source_freshness uses correct payload structure."""
        await mock_client.sources.check_freshness("notebook_123", "source_456")

        call_args = mock_client._core.rpc_call.call_args
        params = call_args[0][1]

        # Verify reference payload: [null, ["source_id"], [2]]
        assert params[0] is None
        assert params[1] == ["source_456"]
        assert params[2] == [2]

    @pytest.mark.asyncio
    async def test_refresh_source_payload(self, mock_client):
        """Test refresh_source uses correct payload structure."""
        await mock_client.sources.refresh("notebook_123", "source_456")

        call_args = mock_client._core.rpc_call.call_args
        params = call_args[0][1]

        # Verify reference payload: [null, ["source_id"], [2]]
        assert params[0] is None
        assert params[1] == ["source_456"]
        assert params[2] == [2]
