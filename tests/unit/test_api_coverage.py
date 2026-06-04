"""Unit tests for new API coverage features."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from _fixtures.fake_core import make_fake_core
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._chat import ChatAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._note_service import NoteService
from notebooklm._notebooks import NotebooksAPI
from notebooklm._runtime.contracts import LoopGuard
from notebooklm._sources import SourcesAPI
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
    def rpc_call(self):
        """RPC seam injected at construction (ADR-0007)."""
        return AsyncMock(return_value=None)

    @pytest.fixture
    def chat(self, rpc_call):
        """Create a ``ChatAPI`` wired to the injected RPC seam.

        The ``rpc_call`` mock is injected via ``make_fake_core(rpc_call=...)``
        / the ``ChatAPI`` constructor rather than assigned onto an instance
        attribute after the fact, satisfying the ADR-0007 monkeypatch policy.
        ``configure`` only exercises the ``rpc`` collaborator; the remaining
        constructor collaborators are inert ``MagicMock`` stand-ins.
        """
        core = make_fake_core(rpc_call=rpc_call)
        return ChatAPI(
            rpc=core.rpc_executor,
            transport=MagicMock(),
            reqid=MagicMock(),
            loop_guard=MagicMock(spec=LoopGuard),
        )

    @pytest.mark.asyncio
    async def test_configure_chat_default(self, chat, rpc_call):
        """Test configure_chat with default settings."""
        await chat.configure("notebook_123")

        rpc_call.assert_called_once()
        call_args = rpc_call.call_args
        params = call_args[0][1]

        # Verify payload structure
        assert params[0] == "notebook_123"
        assert params[1][0][7] == [[1], [1]]  # Default goal, default length

    @pytest.mark.asyncio
    async def test_configure_chat_custom_prompt(self, chat, rpc_call):
        """Test configure_chat with custom prompt."""
        await chat.configure(
            "notebook_123",
            goal=ChatGoal.CUSTOM,
            custom_prompt="Be an expert analyst",
        )

        call_args = rpc_call.call_args
        params = call_args[0][1]

        # Verify custom prompt is included
        assert params[1][0][7][0] == [2, "Be an expert analyst"]

    @pytest.mark.asyncio
    async def test_configure_chat_custom_requires_prompt(self, chat):
        """Test configure_chat raises error when CUSTOM goal without prompt."""
        from notebooklm.exceptions import ValidationError

        with pytest.raises(ValidationError, match="custom_prompt is required"):
            await chat.configure(
                "notebook_123",
                goal=ChatGoal.CUSTOM,
            )

    @pytest.mark.asyncio
    async def test_configure_chat_learning_guide(self, chat, rpc_call):
        """Test configure_chat with learning guide mode."""
        await chat.configure(
            "notebook_123",
            goal=ChatGoal.LEARNING_GUIDE,
            response_length=ChatResponseLength.LONGER,
        )

        call_args = rpc_call.call_args
        params = call_args[0][1]

        assert params[1][0][7] == [[3], [4]]  # Learning guide, longer


class TestGetSourceGuide:
    """Tests for get_source_guide method."""

    def _make_sources(self, return_value):
        """Build a ``SourcesAPI`` with the RPC seam injected at construction.

        Returns ``(sources, rpc_call)`` so tests can assert the seam was
        exercised. ``make_fake_core(rpc_call=...)`` is the sanctioned
        constructor-injection substrate (ADR-0007); ``get_guide`` routes
        through the injected ``rpc`` collaborator only.
        """
        rpc_call = AsyncMock(return_value=return_value)
        core = make_fake_core(rpc_call=rpc_call)
        sources = SourcesAPI(core.rpc_executor, uploader=MagicMock())
        return sources, rpc_call

    @pytest.mark.asyncio
    async def test_get_source_guide_parses_response(self):
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
        sources, rpc_call = self._make_sources(mock_response)

        result = await sources.get_guide("notebook_123", "source_456")

        rpc_call.assert_called_once()
        assert result.summary == "This is a **summary** of the document."
        assert result.keywords == ("Topic 1", "Topic 2", "Topic 3")

    @pytest.mark.asyncio
    async def test_get_source_guide_handles_empty(self):
        """Test get_source_guide handles empty response."""
        sources, rpc_call = self._make_sources(None)

        result = await sources.get_guide("notebook_123", "source_456")

        rpc_call.assert_called_once()
        assert result.summary == ""
        assert result.keywords == ()


class TestGetSuggestedReportFormats:
    """Tests for get_suggested_report_formats method."""

    @pytest.mark.asyncio
    async def test_get_suggested_report_formats_parses_response(self):
        """Test get_suggested_report_formats correctly parses API response."""
        # Response format: [[[title, description, null, null, prompt, audience_level], ...]]
        mock_response = [
            [
                ["Strategy Report", "Analysis of...", None, None, "Create a detailed...", 2],
                ["Summary Brief", "Quick overview...", None, None, "Summarize the...", 1],
            ]
        ]
        rpc_call = AsyncMock(return_value=mock_response)
        core = make_fake_core(rpc_call=rpc_call)
        artifacts = ArtifactsAPI(
            rpc=core.rpc_executor,
            drain=core,
            lifecycle=core,
            notebooks=MagicMock(),
            mind_maps=MagicMock(spec=NoteBackedMindMapService),
            note_service=MagicMock(spec=NoteService),
        )

        result = await artifacts.suggest_reports("notebook_123")

        rpc_call.assert_called_once()
        assert len(result) == 2
        assert result[0].title == "Strategy Report"
        assert result[0].description == "Analysis of..."
        assert result[0].prompt == "Create a detailed..."


class TestAddSourceDrive:
    """Tests for add_source_drive method."""

    @pytest.mark.asyncio
    async def test_add_source_drive_payload_structure(self):
        """Test add_source_drive creates correct payload."""
        rpc_call = AsyncMock(return_value=[["source_id_123"]])
        core = make_fake_core(rpc_call=rpc_call)
        sources = SourcesAPI(core.rpc_executor, uploader=MagicMock())

        await sources.add_drive(
            "notebook_123",
            file_id="drive_file_abc",
            title="My Document",
            mime_type=DriveMimeType.GOOGLE_DOC.value,
        )

        rpc_call.assert_called_once()
        call_args = rpc_call.call_args
        params = call_args[0][1]

        # Verify source data structure - params[0] is [source_data] (single wrap)
        source_data = params[0][0]
        assert source_data[0] == [
            "drive_file_abc",
            "application/vnd.google-apps.document",
            1,
            "My Document",
        ]
        assert source_data[10] == 1  # Trailing 1


class TestGetNotebookDescription:
    """Tests for get_notebook_description method."""

    @pytest.mark.asyncio
    async def test_get_notebook_description_parses_response(self):
        """Test get_notebook_description parses full response."""
        mock_response = [
            [
                ["This notebook explores **AI** and **machine learning**."],
                [
                    [
                        ["What is the future of AI?", "Create a detailed briefing..."],
                        ["How does ML work?", "Explain the fundamentals..."],
                    ]
                ],
            ]
        ]
        rpc_call = AsyncMock(return_value=mock_response)
        core = make_fake_core(rpc_call=rpc_call)
        notebooks = NotebooksAPI(core.rpc_executor, sources_api=MagicMock())

        result = await notebooks.get_description("notebook_123")

        rpc_call.assert_called_once()
        assert "AI" in result.summary
        assert len(result.suggested_topics) == 2
        assert result.suggested_topics[0].question == "What is the future of AI?"
        assert "briefing" in result.suggested_topics[0].prompt


class TestPayloadFixes:
    """Tests for fixed payload structures."""

    def _make_sources(self):
        """Build a ``SourcesAPI`` with the RPC seam injected at construction.

        Returns ``(sources, rpc_call)``; the ``True`` return value mimics
        the freshness/refresh RPC acknowledgements. Constructor injection
        via ``make_fake_core(rpc_call=...)`` keeps the test ADR-0007-clean.
        """
        rpc_call = AsyncMock(return_value=True)
        core = make_fake_core(rpc_call=rpc_call)
        sources = SourcesAPI(core.rpc_executor, uploader=MagicMock())
        return sources, rpc_call

    @pytest.mark.asyncio
    async def test_check_source_freshness_payload(self):
        """Test check_source_freshness uses correct payload structure."""
        sources, rpc_call = self._make_sources()

        await sources.check_freshness("notebook_123", "source_456")

        rpc_call.assert_called_once()
        call_args = rpc_call.call_args
        params = call_args[0][1]

        # Verify reference payload: [null, ["source_id"], [2]]
        assert params[0] is None
        assert params[1] == ["source_456"]
        assert params[2] == [2]

    @pytest.mark.asyncio
    async def test_refresh_source_payload(self):
        """Test refresh_source uses correct payload structure."""
        sources, rpc_call = self._make_sources()

        await sources.refresh("notebook_123", "source_456")

        rpc_call.assert_called_once()
        call_args = rpc_call.call_args
        params = call_args[0][1]

        # Verify reference payload: [null, ["source_id"], [2]]
        assert params[0] is None
        assert params[1] == ["source_456"]
        assert params[2] == [2]
