"""Comprehensive VCR tests for all NotebookLM API operations.

This file records cassettes for ALL API operations.
Run with NOTEBOOKLM_VCR_RECORD=1 to record new cassettes.

Recording requires the same env vars as e2e tests:
- NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID: For read-only operations
- NOTEBOOKLM_GENERATION_NOTEBOOK_ID: For mutable operations

Note: Notebook IDs only matter when RECORDING. During replay, VCR uses
recorded responses regardless of notebook ID.

Note: These tests are automatically skipped if cassettes are not available.
"""

import csv
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

# Add tests directory to path for vcr_config import
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import get_vcr_auth, skip_no_cassettes
from notebooklm import (
    Artifact,
    ChatReference,
    GenerationStatus,
    Note,
    Notebook,
    NotebookLMClient,
    ReportFormat,
    Source,
)
from vcr_config import notebooklm_vcr

# Skip all tests in this module if cassettes are not available
pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Use same env vars as e2e tests for consistency
# These only matter during recording - replay uses recorded responses
READONLY_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", "")
MUTABLE_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_GENERATION_NOTEBOOK_ID", "")


# =============================================================================
# Helper for reducing boilerplate
# =============================================================================


@asynccontextmanager
async def vcr_client():
    """Context manager for creating authenticated VCR client."""
    auth = await get_vcr_auth()
    async with NotebookLMClient(auth) as client:
        yield client


# =============================================================================
# Notebooks API
# =============================================================================


class TestNotebooksAPI:
    """Notebooks API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_list.yaml")
    async def test_list(self):
        """List all notebooks."""
        async with vcr_client() as client:
            notebooks = await client.notebooks.list()
        assert isinstance(notebooks, list)
        assert all(isinstance(nb, Notebook) for nb in notebooks)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get.yaml")
    async def test_get(self):
        """Get a specific notebook."""
        async with vcr_client() as client:
            notebook = await client.notebooks.get(READONLY_NOTEBOOK_ID)
        assert notebook is not None
        assert isinstance(notebook, Notebook)
        if READONLY_NOTEBOOK_ID:
            assert notebook.id == READONLY_NOTEBOOK_ID

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_summary.yaml")
    async def test_get_summary(self):
        """Get notebook summary."""
        async with vcr_client() as client:
            summary = await client.notebooks.get_summary(READONLY_NOTEBOOK_ID)
        assert summary is not None
        assert isinstance(summary, str), "Summary should be a string"
        # Summary may be empty for notebooks without sources, but type must be correct

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_description.yaml")
    async def test_get_description(self):
        """Get notebook description."""
        async with vcr_client() as client:
            description = await client.notebooks.get_description(READONLY_NOTEBOOK_ID)
        assert description is not None
        # Verify NotebookDescription structure
        assert hasattr(description, "summary"), "Description should have summary attribute"
        assert hasattr(description, "suggested_topics"), (
            "Description should have suggested_topics attribute"
        )
        assert isinstance(description.suggested_topics, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_raw.yaml")
    async def test_get_raw(self):
        """Get raw notebook data."""
        async with vcr_client() as client:
            raw = await client.notebooks.get_raw(READONLY_NOTEBOOK_ID)
        assert raw is not None
        assert isinstance(raw, list), "Raw notebook data should be a list"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_rename.yaml")
    async def test_rename(self):
        """Rename a notebook (then rename back)."""
        async with vcr_client() as client:
            notebook = await client.notebooks.get(MUTABLE_NOTEBOOK_ID)
            original_name = notebook.title
            await client.notebooks.rename(MUTABLE_NOTEBOOK_ID, "VCR Test Renamed")
            await client.notebooks.rename(MUTABLE_NOTEBOOK_ID, original_name)


# =============================================================================
# Sources API
# =============================================================================


class TestSourcesAPI:
    """Sources API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_list.yaml")
    async def test_list(self):
        """List sources in a notebook."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
        assert isinstance(sources, list)
        assert all(isinstance(src, Source) for src in sources)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_get_guide.yaml")
    async def test_get_guide(self):
        """Get source guide for a specific source."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            if not sources:
                pytest.skip("No sources available")
            guide = await client.sources.get_guide(READONLY_NOTEBOOK_ID, sources[0].id)
        assert guide is not None
        # Verify values are actually populated (catches parsing bugs like issue #70)
        assert guide["summary"], "Expected non-empty summary from source guide"
        assert isinstance(guide["keywords"], list)
        assert len(guide["keywords"]) > 0, "Expected non-empty keywords from source guide"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_get_fulltext.yaml")
    async def test_get_fulltext(self):
        """Get source fulltext content."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            if not sources:
                pytest.skip("No sources available")
            fulltext = await client.sources.get_fulltext(READONLY_NOTEBOOK_ID, sources[0].id)
        assert fulltext is not None
        assert fulltext.source_id == sources[0].id
        # Verify content is actually populated (catches parsing bugs like issue #70)
        assert fulltext.content, "Expected non-empty content from fulltext"
        assert fulltext.title, "Expected non-empty title from fulltext"
        assert fulltext.char_count > 0, "Expected positive char_count"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_text.yaml")
    async def test_add_text(self):
        """Add a text source."""
        async with vcr_client() as client:
            source = await client.sources.add_text(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Test Source",
                content="This is a test source created by VCR recording.",
            )
        assert source is not None
        assert isinstance(source, Source)
        assert source.title == "VCR Test Source"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_url.yaml")
    async def test_add_url(self):
        """Add a URL source."""
        async with vcr_client() as client:
            source = await client.sources.add_url(
                MUTABLE_NOTEBOOK_ID,
                url="https://en.wikipedia.org/wiki/Artificial_intelligence",
            )
        assert source is not None
        assert isinstance(source, Source)
        assert source.id, "Expected non-empty source ID"
        # Title may be extracted from the page
        assert source.title is not None


# =============================================================================
# Notes API
# =============================================================================


class TestNotesAPI:
    """Notes API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_list.yaml")
    async def test_list(self):
        """List notes in a notebook."""
        async with vcr_client() as client:
            notes = await client.notes.list(READONLY_NOTEBOOK_ID)
        assert isinstance(notes, list)
        assert all(isinstance(note, Note) for note in notes)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_list_mind_maps.yaml")
    async def test_list_mind_maps(self):
        """List mind maps in a notebook.

        Note: list_mind_maps returns raw mind map data (nested lists), not Artifact objects.
        For Artifact objects, use client.artifacts.list() instead.
        """
        async with vcr_client() as client:
            mind_maps = await client.notes.list_mind_maps(READONLY_NOTEBOOK_ID)
        assert isinstance(mind_maps, list)
        # Raw mind map data are nested lists, not Artifact objects
        assert all(isinstance(mm, list) for mm in mind_maps)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_create.yaml")
    async def test_create(self):
        """Create a note."""
        async with vcr_client() as client:
            note = await client.notes.create(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Test Note",
                content="This is a test note created by VCR recording.",
            )
        assert note is not None
        assert isinstance(note, Note)
        assert note.id, "Note should have a non-empty ID"
        assert note.title == "VCR Test Note"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_create_and_update.yaml")
    async def test_create_and_update(self):
        """Create and update a note."""
        async with vcr_client() as client:
            note = await client.notes.create(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Update Test",
                content="Original content.",
            )
            assert note is not None
            await client.notes.update(
                MUTABLE_NOTEBOOK_ID,
                note.id,
                title="VCR Update Test - Updated",
                content="Updated content.",
            )


# =============================================================================
# Artifacts API - Read Operations
# =============================================================================


# Artifact list method configurations: (method_name, cassette_name)
ARTIFACT_LIST_METHODS = [
    ("list", "artifacts_list.yaml"),
    ("list_audio", "artifacts_list_audio.yaml"),
    ("list_video", "artifacts_list_video.yaml"),
    ("list_reports", "artifacts_list_reports.yaml"),
    ("list_quizzes", "artifacts_list_quizzes.yaml"),
    ("list_flashcards", "artifacts_list_flashcards.yaml"),
    ("list_infographics", "artifacts_list_infographics.yaml"),
    ("list_slide_decks", "artifacts_list_slide_decks.yaml"),
    ("list_data_tables", "artifacts_list_data_tables.yaml"),
]


class TestArtifactsListAPI:
    """Artifacts API list operations - parametrized to reduce duplication."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method_name,cassette", ARTIFACT_LIST_METHODS)
    async def test_list_artifacts(self, method_name, cassette):
        """Test artifact list methods."""
        with notebooklm_vcr.use_cassette(cassette):
            async with vcr_client() as client:
                method = getattr(client.artifacts, method_name)
                result = await method(READONLY_NOTEBOOK_ID)
                assert isinstance(result, list)
                assert all(isinstance(art, Artifact) for art in result)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_suggest_reports.yaml")
    async def test_suggest_reports(self):
        """Get report suggestions."""
        async with vcr_client() as client:
            suggestions = await client.artifacts.suggest_reports(READONLY_NOTEBOOK_ID)
        assert isinstance(suggestions, list)
        # Verify structure if suggestions exist
        for suggestion in suggestions:
            assert suggestion.title, "Suggestion should have a non-empty title"
            assert suggestion.description, "Suggestion should have a non-empty description"
            assert suggestion.prompt, "Suggestion should have a non-empty prompt"


class TestArtifactsDownloadAPI:
    """Artifacts API download operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_download_report.yaml")
    async def test_download_report(self, tmp_path):
        """Download a report as markdown."""
        async with vcr_client() as client:
            output_path = tmp_path / "report.md"
            try:
                path = await client.artifacts.download_report(
                    READONLY_NOTEBOOK_ID, str(output_path)
                )
                assert os.path.exists(path)
                content = output_path.read_text(encoding="utf-8")
                assert len(content) > 0 and "#" in content
            except ValueError as e:
                if "No completed report" in str(e):
                    pytest.skip("No completed report artifact available")
                raise

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_download_mind_map.yaml")
    async def test_download_mind_map(self, tmp_path):
        """Download a mind map as JSON."""
        async with vcr_client() as client:
            output_path = tmp_path / "mindmap.json"
            try:
                path = await client.artifacts.download_mind_map(
                    READONLY_NOTEBOOK_ID, str(output_path)
                )
                assert os.path.exists(path)
                data = json.loads(output_path.read_text(encoding="utf-8"))
                assert "name" in data
            except ValueError as e:
                if "No mind maps found" in str(e):
                    pytest.skip("No mind map artifact available")
                raise

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_download_data_table.yaml")
    async def test_download_data_table(self, tmp_path):
        """Download a data table as CSV."""
        async with vcr_client() as client:
            output_path = tmp_path / "data.csv"
            try:
                path = await client.artifacts.download_data_table(
                    READONLY_NOTEBOOK_ID, str(output_path)
                )
                assert os.path.exists(path)
                with open(output_path, encoding="utf-8-sig") as f:
                    rows = list(csv.reader(f))
                assert len(rows) >= 1
            except ValueError as e:
                if "No completed data table" in str(e):
                    pytest.skip("No completed data table artifact available")
                raise

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_download_quiz.yaml")
    async def test_download_quiz(self, tmp_path):
        """Download a quiz as JSON."""
        async with vcr_client() as client:
            output_path = tmp_path / "quiz.json"
            try:
                path = await client.artifacts.download_quiz(READONLY_NOTEBOOK_ID, str(output_path))
                assert os.path.exists(path)
                data = json.loads(output_path.read_text(encoding="utf-8"))
                assert "title" in data
                assert "questions" in data
            except ValueError as e:
                if "No completed quiz" in str(e):
                    pytest.skip("No completed quiz artifact available")
                raise

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_download_quiz_markdown.yaml")
    async def test_download_quiz_markdown(self, tmp_path):
        """Download a quiz as markdown."""
        async with vcr_client() as client:
            output_path = tmp_path / "quiz.md"
            try:
                path = await client.artifacts.download_quiz(
                    READONLY_NOTEBOOK_ID, str(output_path), output_format="markdown"
                )
                assert os.path.exists(path)
                content = output_path.read_text(encoding="utf-8")
                assert "# " in content  # Should have a heading
                assert "Question" in content or "##" in content
            except ValueError as e:
                if "No completed quiz" in str(e):
                    pytest.skip("No completed quiz artifact available")
                raise

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_download_flashcards.yaml")
    async def test_download_flashcards(self, tmp_path):
        """Download flashcards as JSON."""
        async with vcr_client() as client:
            output_path = tmp_path / "flashcards.json"
            try:
                path = await client.artifacts.download_flashcards(
                    READONLY_NOTEBOOK_ID, str(output_path)
                )
                assert os.path.exists(path)
                data = json.loads(output_path.read_text(encoding="utf-8"))
                assert "title" in data
                assert "cards" in data
                # Verify normalized format (front/back, not f/b)
                if data["cards"]:
                    assert "front" in data["cards"][0]
                    assert "back" in data["cards"][0]
            except ValueError as e:
                if "No completed flashcard" in str(e):
                    pytest.skip("No completed flashcard artifact available")
                raise

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_download_flashcards_markdown.yaml")
    async def test_download_flashcards_markdown(self, tmp_path):
        """Download flashcards as markdown."""
        async with vcr_client() as client:
            output_path = tmp_path / "flashcards.md"
            try:
                path = await client.artifacts.download_flashcards(
                    READONLY_NOTEBOOK_ID, str(output_path), output_format="markdown"
                )
                assert os.path.exists(path)
                content = output_path.read_text(encoding="utf-8")
                assert "# " in content  # Should have a heading
                assert "**Q:**" in content or "Card" in content
            except ValueError as e:
                if "No completed flashcard" in str(e):
                    pytest.skip("No completed flashcard artifact available")
                raise


# =============================================================================
# Artifacts API - Generation Operations (use mutable notebook)
# =============================================================================


class TestArtifactsGenerateAPI:
    """Artifacts API generation operations.

    These tests generate artifacts which may take time and consume quota.
    They use the mutable notebook to avoid polluting the read-only one.
    """

    def _assert_generation_started(self, result: GenerationStatus, artifact_type: str) -> None:
        """Assert that artifact generation started successfully."""
        assert result is not None, f"{artifact_type} generation returned None"
        assert isinstance(result, GenerationStatus)
        # Check for rate limiting
        if result.is_rate_limited:
            pytest.skip("Rate limited by API")
        assert result.task_id, f"{artifact_type} generation should have a non-empty task_id"
        assert result.status in ("pending", "in_progress"), (
            f"Unexpected {artifact_type.lower()} status: {result.status}"
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_report.yaml")
    async def test_generate_report(self):
        """Generate a briefing doc report."""
        async with vcr_client() as client:
            result = await client.artifacts.generate_report(
                MUTABLE_NOTEBOOK_ID,
                report_format=ReportFormat.BRIEFING_DOC,
            )
        self._assert_generation_started(result, "report")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_study_guide.yaml")
    async def test_generate_study_guide(self):
        """Generate a study guide."""
        async with vcr_client() as client:
            result = await client.artifacts.generate_study_guide(MUTABLE_NOTEBOOK_ID)
        self._assert_generation_started(result, "study_guide")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_quiz.yaml")
    async def test_generate_quiz(self):
        """Generate a quiz."""
        async with vcr_client() as client:
            result = await client.artifacts.generate_quiz(MUTABLE_NOTEBOOK_ID)
        self._assert_generation_started(result, "quiz")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_flashcards.yaml")
    async def test_generate_flashcards(self):
        """Generate flashcards."""
        async with vcr_client() as client:
            result = await client.artifacts.generate_flashcards(MUTABLE_NOTEBOOK_ID)
        self._assert_generation_started(result, "flashcards")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_mind_map.yaml")
    async def test_generate_mind_map(self):
        """Generate a mind map from notebook sources."""
        async with vcr_client() as client:
            result = await client.artifacts.generate_mind_map(MUTABLE_NOTEBOOK_ID)
        assert result is not None
        # Mind map returns a dict with mind_map and note_id
        assert isinstance(result, dict)
        assert "mind_map" in result
        assert "note_id" in result
        # The mind_map has the tree structure with name
        assert "name" in result["mind_map"]


# =============================================================================
# Chat API
# =============================================================================


class TestChatAPI:
    """Chat API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_ask.yaml")
    async def test_ask(self):
        """Ask a question."""
        async with vcr_client() as client:
            result = await client.chat.ask(
                MUTABLE_NOTEBOOK_ID,
                "What is this notebook about?",
            )
        assert result is not None
        assert result.answer, "Answer should be a non-empty string"
        assert result.conversation_id, "Conversation ID should be non-empty"
        assert isinstance(result.references, list), "References should be a list"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_ask_with_references.yaml")
    async def test_ask_with_references(self):
        """Ask a question that generates references."""
        async with vcr_client() as client:
            result = await client.chat.ask(
                MUTABLE_NOTEBOOK_ID,
                "Summarize the key points with specific citations from the sources.",
            )
        assert result is not None
        assert result.answer is not None
        # References may or may not be present depending on the answer
        assert isinstance(result.references, list)
        # If references exist, verify structure and type
        for ref in result.references:
            assert isinstance(ref, ChatReference)
            assert ref.source_id is not None
            assert ref.citation_number is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_get_history.yaml")
    async def test_get_history(self):
        """Get chat history."""
        async with vcr_client() as client:
            history = await client.chat.get_history(MUTABLE_NOTEBOOK_ID)
        assert isinstance(history, list)


# =============================================================================
# Settings API
# =============================================================================


class TestSettingsAPI:
    """Settings API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("settings_get_output_language.yaml")
    async def test_get_output_language(self):
        """Get current output language setting."""
        async with vcr_client() as client:
            language = await client.settings.get_output_language()
        # Language may be None if not set, or a string like "en", "ja", "zh_Hans"
        assert language is None or isinstance(language, str)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("settings_set_output_language.yaml")
    async def test_set_output_language(self):
        """Set output language (then restore original)."""
        async with vcr_client() as client:
            # Get current language to restore later
            original = await client.settings.get_output_language()
            # Set to English
            result = await client.settings.set_output_language("en")
            assert result == "en" or result is None
            # Restore original if it was set
            if original:
                await client.settings.set_output_language(original)


# =============================================================================
# Sharing API
# =============================================================================


class TestSharingAPI:
    """Sharing API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sharing_get_status.yaml")
    async def test_get_status(self):
        """Get sharing status for a notebook."""
        async with vcr_client() as client:
            status = await client.sharing.get_status(READONLY_NOTEBOOK_ID)
        assert status is not None
        assert status.notebook_id == READONLY_NOTEBOOK_ID

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sharing_set_public.yaml")
    async def test_set_public(self):
        """Toggle public sharing (restore original state)."""
        async with vcr_client() as client:
            # Get current status
            original = await client.sharing.get_status(MUTABLE_NOTEBOOK_ID)
            # Toggle to opposite
            new_status = await client.sharing.set_public(
                MUTABLE_NOTEBOOK_ID, not original.is_public
            )
            assert new_status.is_public != original.is_public
            # Restore original state
            await client.sharing.set_public(MUTABLE_NOTEBOOK_ID, original.is_public)


# =============================================================================
# Sources API - Additional Operations
# =============================================================================


class TestSourcesAdditionalAPI:
    """Additional sources API operations not covered in main TestSourcesAPI."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_file.yaml")
    async def test_add_file(self, tmp_path):
        """Add a file source."""
        # Create a test file
        test_file = tmp_path / "vcr_test_document.txt"
        test_file.write_text("This is a test document for VCR cassette recording.")

        async with vcr_client() as client:
            source = await client.sources.add_file(
                MUTABLE_NOTEBOOK_ID,
                str(test_file),
            )
        assert source is not None
        assert isinstance(source, Source)
        assert source.id, "Source should have a non-empty ID"
        assert source.title, "Source should have a non-empty title"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_drive.yaml")
    async def test_add_drive(self):
        """Add a Google Drive source (Google Doc)."""
        from notebooklm import SourceType
        from notebooklm.rpc.types import DriveMimeType

        async with vcr_client() as client:
            # Note: This test requires a real Google Doc file_id during recording
            # The file_id below is a placeholder - replace when recording
            # Use: NOTEBOOKLM_VCR_RECORD=1 pytest -k test_add_drive
            source = await client.sources.add_drive(
                MUTABLE_NOTEBOOK_ID,
                file_id="1bAgBGlybk82LZfbz6IPCwpQ12E4hlDQsuWTVWJVEHfM",
                title="VCR Test Google Doc",
                mime_type=DriveMimeType.GOOGLE_DOC.value,
            )
        assert source is not None
        assert isinstance(source, Source)
        assert source.id is not None
        assert source.kind == SourceType.GOOGLE_DOCS

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_youtube.yaml")
    async def test_add_youtube(self):
        """Add a YouTube source via add_url (auto-detects YouTube URLs)."""
        from notebooklm import SourceType

        async with vcr_client() as client:
            # YouTube URLs are added via add_url which auto-detects and uses
            # the YouTube-specific RPC internally
            # Use: NOTEBOOKLM_VCR_RECORD=1 pytest -k test_add_youtube
            source = await client.sources.add_url(
                MUTABLE_NOTEBOOK_ID,
                url="https://www.youtube.com/watch?v=JMUxmLyrhSk",
            )
        assert source is not None
        assert isinstance(source, Source)
        assert source.id is not None
        assert source.kind == SourceType.YOUTUBE

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_check_freshness.yaml")
    async def test_check_freshness(self):
        """Check source freshness."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            if not sources:
                pytest.skip("No sources available")
            is_fresh = await client.sources.check_freshness(READONLY_NOTEBOOK_ID, sources[0].id)
        assert isinstance(is_fresh, bool)
        # The cassette shows API returns [] which should be interpreted as fresh
        assert is_fresh is True, "Source in cassette should be fresh (API returned [])"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_check_freshness_drive.yaml")
    async def test_check_freshness_drive(self):
        """Check freshness for Drive source (different response format)."""
        from notebooklm import SourceType

        async with vcr_client() as client:
            sources = await client.sources.list(MUTABLE_NOTEBOOK_ID)
            if not sources:
                pytest.skip("No sources available")
            # Find a GOOGLE_DOCS source
            drive_source = next((s for s in sources if s.kind == SourceType.GOOGLE_DOCS), None)
            if not drive_source:
                pytest.skip("No GOOGLE_DOCS source available")
            is_fresh = await client.sources.check_freshness(MUTABLE_NOTEBOOK_ID, drive_source.id)
        assert isinstance(is_fresh, bool)
        # Drive sources return [[null, true, [source_id]]] when fresh
        assert is_fresh is True, "Drive source should be fresh"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_refresh.yaml")
    async def test_refresh(self):
        """Refresh a source."""
        from notebooklm import SourceType

        async with vcr_client() as client:
            sources = await client.sources.list(MUTABLE_NOTEBOOK_ID)
            if not sources:
                pytest.skip("No sources available")
            # Find a WEB_PAGE source (text sources can't be refreshed)
            url_source = next((s for s in sources if s.kind == SourceType.WEB_PAGE), None)
            if not url_source:
                pytest.skip("No WEB_PAGE source available for refresh")
            result = await client.sources.refresh(MUTABLE_NOTEBOOK_ID, url_source.id)
        # refresh() returns True if initiated successfully (no exception)
        assert result is True, "refresh() should return True on success"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_rename.yaml")
    async def test_rename(self):
        """Rename a source (then restore original name)."""
        async with vcr_client() as client:
            sources = await client.sources.list(MUTABLE_NOTEBOOK_ID)
            if not sources:
                pytest.skip("No sources available")
            source = sources[0]
            original_title = source.title
            # Rename
            renamed = await client.sources.rename(
                MUTABLE_NOTEBOOK_ID, source.id, "VCR Test Renamed Source"
            )
            assert isinstance(renamed, Source)
            assert renamed.title == "VCR Test Renamed Source"
            # Restore
            await client.sources.rename(MUTABLE_NOTEBOOK_ID, source.id, original_title)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_delete.yaml")
    async def test_delete(self):
        """Delete a source (creates one first to delete)."""
        async with vcr_client() as client:
            # Create a source to delete
            source = await client.sources.add_text(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Delete Test Source",
                content="This source will be deleted.",
            )
            assert source is not None
            # Delete it
            result = await client.sources.delete(MUTABLE_NOTEBOOK_ID, source.id)
        assert result is True


# =============================================================================
# Notebooks API - Additional Operations
# =============================================================================


class TestNotebooksAdditionalAPI:
    """Additional notebooks API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_create.yaml")
    async def test_create(self):
        """Create a new notebook."""
        async with vcr_client() as client:
            notebook = await client.notebooks.create("VCR Test Notebook")
        assert notebook is not None
        assert isinstance(notebook, Notebook)
        assert notebook.title == "VCR Test Notebook"
        # Note: We don't delete it here to keep the cassette simple
        # A separate delete test will clean up

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_delete.yaml")
    async def test_delete(self):
        """Delete a notebook (creates one first)."""
        async with vcr_client() as client:
            # Create a notebook to delete
            notebook = await client.notebooks.create("VCR Delete Test Notebook")
            assert notebook is not None
            # Delete it
            result = await client.notebooks.delete(notebook.id)
        assert result is True

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_remove_from_recent.yaml")
    async def test_remove_from_recent(self):
        """Remove a notebook from recently viewed."""
        async with vcr_client() as client:
            # This just removes from the recent list, doesn't delete
            await client.notebooks.remove_from_recent(MUTABLE_NOTEBOOK_ID)
        # No return value to check - if it doesn't raise, it worked

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_share.yaml")
    async def test_share(self):
        """Test sharing a notebook (toggle on then off)."""
        async with vcr_client() as client:
            # Enable sharing
            result = await client.notebooks.share(MUTABLE_NOTEBOOK_ID, public=True)
            assert result["public"] is True
            assert "notebooklm.google.com" in result["url"]

            # Disable sharing (restore original state)
            result = await client.notebooks.share(MUTABLE_NOTEBOOK_ID, public=False)
            assert result["public"] is False
            assert result["url"] is None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_share_with_artifact.yaml")
    async def test_share_with_artifact(self):
        """Test sharing with artifact deep-link."""
        async with vcr_client() as client:
            # List artifacts to find one
            artifacts = await client.artifacts.list(MUTABLE_NOTEBOOK_ID)
            if not artifacts:
                pytest.skip("No artifacts available for share test")

            artifact_id = artifacts[0].id

            try:
                # Share with artifact ID
                result = await client.notebooks.share(
                    MUTABLE_NOTEBOOK_ID, public=True, artifact_id=artifact_id
                )
                assert result["public"] is True
                assert artifact_id in result["url"]
            finally:
                # Disable sharing (cleanup even if assertion fails)
                await client.notebooks.share(MUTABLE_NOTEBOOK_ID, public=False)


# =============================================================================
# Notes API - Additional Operations
# =============================================================================


class TestNotesAdditionalAPI:
    """Additional notes API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_delete.yaml")
    async def test_delete(self):
        """Delete a note (creates one first)."""
        async with vcr_client() as client:
            # Create a note to delete
            note = await client.notes.create(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Delete Test Note",
                content="This note will be deleted.",
            )
            assert note is not None
            # Delete it
            result = await client.notes.delete(MUTABLE_NOTEBOOK_ID, note.id)
        assert result is True


# =============================================================================
# Artifacts API - Additional Operations
# =============================================================================


class TestArtifactsAdditionalAPI:
    """Additional artifacts API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_rename.yaml")
    async def test_rename(self):
        """Rename an artifact."""
        async with vcr_client() as client:
            # List artifacts to find one to rename
            artifacts = await client.artifacts.list(MUTABLE_NOTEBOOK_ID)
            if not artifacts:
                pytest.skip("No artifacts available")
            artifact = artifacts[0]
            original_title = artifact.title
            # Rename
            await client.artifacts.rename(MUTABLE_NOTEBOOK_ID, artifact.id, "VCR Renamed Artifact")
            # Restore original name
            await client.artifacts.rename(MUTABLE_NOTEBOOK_ID, artifact.id, original_title)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_delete.yaml")
    async def test_delete(self):
        """Delete an artifact."""
        async with vcr_client() as client:
            # List existing artifacts
            artifacts = await client.artifacts.list(MUTABLE_NOTEBOOK_ID)
            if not artifacts:
                pytest.skip("No artifacts available to delete")
            # Delete the first one
            artifact_id = artifacts[0].id
            deleted = await client.artifacts.delete(MUTABLE_NOTEBOOK_ID, artifact_id)
        assert deleted is True

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_export_report.yaml")
    async def test_export_report(self):
        """Export a report to Google Docs."""
        async with vcr_client() as client:
            # Find a completed report artifact
            reports = await client.artifacts.list_reports(MUTABLE_NOTEBOOK_ID)
            completed_reports = [r for r in reports if r.is_completed]
            if not completed_reports:
                pytest.skip("No completed report artifact available")
            report = completed_reports[0]
            # Export it to Google Docs
            result = await client.artifacts.export_report(
                MUTABLE_NOTEBOOK_ID, report.id, title="VCR Export Test"
            )
        assert result is not None
        # export_report returns a list with Google Docs URL(s)
        assert isinstance(result, list), "Export result should be a list"
        assert len(result) > 0, "Export result should contain at least one URL"
        assert result[0].startswith("https://docs.google.com/"), (
            "Export URL should be a Google Docs URL"
        )


# =============================================================================
# Research API
# =============================================================================


class TestResearchAPI:
    """Research API operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("research_start_fast.yaml")
    async def test_start_fast(self):
        """Start fast web research."""
        async with vcr_client() as client:
            result = await client.research.start(
                MUTABLE_NOTEBOOK_ID,
                query="Python programming best practices",
                source="web",
                mode="fast",
            )
        assert result is not None
        assert "task_id" in result
        assert result["mode"] == "fast"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("research_poll.yaml")
    async def test_poll(self):
        """Poll research status."""
        async with vcr_client() as client:
            # Start research first
            await client.research.start(
                MUTABLE_NOTEBOOK_ID,
                query="Machine learning fundamentals",
                source="web",
                mode="fast",
            )
            # Poll for results
            result = await client.research.poll(MUTABLE_NOTEBOOK_ID)
        assert result is not None
        assert "status" in result

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("research_import_sources.yaml")
    async def test_import_sources(self):
        """Import research sources."""
        async with vcr_client() as client:
            # Start research
            start_result = await client.research.start(
                MUTABLE_NOTEBOOK_ID,
                query="Data science tutorials",
                source="web",
                mode="fast",
            )
            if not start_result:
                pytest.skip("Could not start research")

            # Poll until we have sources (with timeout via cassette)
            poll_result = await client.research.poll(MUTABLE_NOTEBOOK_ID)
            if not poll_result.get("sources"):
                pytest.skip("No research sources found")

            # Import first source
            imported = await client.research.import_sources(
                MUTABLE_NOTEBOOK_ID,
                start_result["task_id"],
                poll_result["sources"][:1],
            )
        assert isinstance(imported, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("research_start_deep.yaml")
    async def test_start_deep(self):
        """Start deep web research."""
        async with vcr_client() as client:
            result = await client.research.start(
                MUTABLE_NOTEBOOK_ID,
                query="Artificial intelligence history",
                source="web",
                mode="deep",
            )
        assert result is not None
        assert "task_id" in result
        assert result["mode"] == "deep"


# =============================================================================
# Artifacts API - Binary Downloads (audio, video, infographic, slide_deck)
# =============================================================================


# Magic bytes for file type verification
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PDF_MAGIC = b"%PDF"
MP4_FTYP = b"ftyp"  # At offset 4


def check_file_magic(path: str, file_type: str) -> bool:
    """Check if file matches expected magic bytes for the given type."""
    with open(path, "rb") as f:
        if file_type == "mp4":
            header = f.read(12)
            return len(header) >= 8 and header[4:8] == MP4_FTYP
        elif file_type == "png":
            return f.read(8) == PNG_MAGIC
        elif file_type == "pdf":
            return f.read(4) == PDF_MAGIC
        else:
            raise ValueError(f"Unknown file type: {file_type}")


# Binary download test configurations: (method, cassette, filename, file_type, description)
BINARY_DOWNLOAD_TESTS = [
    ("download_audio", "artifacts_download_audio.yaml", "audio.mp4", "mp4", "audio"),
    ("download_video", "artifacts_download_video.yaml", "video.mp4", "mp4", "video"),
    (
        "download_infographic",
        "artifacts_download_infographic.yaml",
        "infographic.png",
        "png",
        "infographic",
    ),
    (
        "download_slide_deck",
        "artifacts_download_slide_deck.yaml",
        "slides.pdf",
        "pdf",
        "slide deck",
    ),
]


class TestArtifactsBinaryDownloads:
    """VCR tests for binary artifact downloads (audio, video, infographic, slide deck).

    These tests record actual binary downloads to cassettes.
    """

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method_name,cassette,filename,file_type,description", BINARY_DOWNLOAD_TESTS
    )
    async def test_binary_download(
        self, tmp_path, method_name, cassette, filename, file_type, description
    ):
        """Download a binary artifact and verify file format."""
        from notebooklm.exceptions import ArtifactNotReadyError

        with notebooklm_vcr.use_cassette(cassette):
            async with vcr_client() as client:
                output_path = tmp_path / filename
                try:
                    method = getattr(client.artifacts, method_name)
                    path = await method(READONLY_NOTEBOOK_ID, str(output_path))
                    assert os.path.exists(path)
                    assert os.path.getsize(path) > 0
                    assert check_file_magic(path, file_type), (
                        f"Downloaded {description} should be {file_type.upper()} format"
                    )
                except ArtifactNotReadyError:
                    pytest.skip(f"No completed {description} artifact available")


# =============================================================================
# Sources API - Readiness Polling
# =============================================================================


class TestSourcesPolling:
    """VCR tests for source readiness polling.

    These tests verify wait_until_ready and wait_for_sources methods.
    Note: Polling tests require multiple HTTP requests recorded in sequence.
    """

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_wait_until_ready.yaml")
    async def test_wait_until_ready(self):
        """Test waiting for a single source to become ready."""
        async with vcr_client() as client:
            # Add a text source (fast to process)
            source = await client.sources.add_text(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Wait Test Source",
                content="This is test content for the wait_until_ready test. " * 20,
            )
            assert source is not None
            assert source.id is not None

            # Wait for it to be ready
            ready_source = await client.sources.wait_until_ready(
                MUTABLE_NOTEBOOK_ID,
                source.id,
                timeout=60.0,
            )
            assert ready_source.is_ready, "Source should be ready after wait"
            assert ready_source.id == source.id

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_wait_for_sources.yaml")
    async def test_wait_for_sources(self):
        """Test waiting for multiple sources to become ready in parallel."""
        async with vcr_client() as client:
            # Add multiple text sources
            source1 = await client.sources.add_text(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Batch Wait Test 1",
                content="First batch test content for parallel wait. " * 20,
            )
            source2 = await client.sources.add_text(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Batch Wait Test 2",
                content="Second batch test content for parallel wait. " * 20,
            )
            assert source1.id is not None
            assert source2.id is not None

            # Wait for all to be ready
            ready_sources = await client.sources.wait_for_sources(
                MUTABLE_NOTEBOOK_ID,
                [source1.id, source2.id],
                timeout=60.0,
            )
            assert len(ready_sources) == 2
            assert all(s.is_ready for s in ready_sources)


# =============================================================================
# Source Selection Tests (chat and artifact generation with source_ids)
# =============================================================================


class TestChatSourceSelection:
    """VCR tests for chat.ask() with source_ids parameter.

    These tests verify that source selection works correctly when asking
    questions with a subset of sources.
    """

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_ask_with_source_ids.yaml")
    async def test_ask_with_single_source(self):
        """Test asking a question using only one source."""
        async with vcr_client() as client:
            # Get sources from the notebook
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            if len(sources) < 1:
                pytest.skip("No sources available for source selection test")

            # Ask using only the first source
            result = await client.chat.ask(
                READONLY_NOTEBOOK_ID,
                "What is this source about?",
                source_ids=[sources[0].id],
            )
            assert result.answer is not None
            assert len(result.answer) > 10
            assert result.conversation_id is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_ask_with_multiple_source_ids.yaml")
    async def test_ask_with_multiple_sources(self):
        """Test asking a question using multiple sources."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            if len(sources) < 2:
                pytest.skip("Need at least 2 sources for multi-source test")

            # Ask using first two sources
            source_ids = [sources[0].id, sources[1].id]
            result = await client.chat.ask(
                READONLY_NOTEBOOK_ID,
                "Summarize the key points from these sources.",
                source_ids=source_ids,
            )
            assert result.answer is not None
            assert len(result.answer) > 10
            assert result.conversation_id is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_follow_up_different_sources.yaml")
    async def test_follow_up_with_different_sources(self):
        """Test that follow-up can use different source selection."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            if len(sources) < 2:
                pytest.skip("Need at least 2 sources for follow-up test")

            # First question with first source
            result1 = await client.chat.ask(
                READONLY_NOTEBOOK_ID,
                "What is covered here?",
                source_ids=[sources[0].id],
            )
            assert result1.answer is not None
            assert result1.conversation_id is not None

            # Follow-up using second source
            result2 = await client.chat.ask(
                READONLY_NOTEBOOK_ID,
                "What about this topic?",
                source_ids=[sources[1].id],
                conversation_id=result1.conversation_id,
            )
            assert result2.answer is not None
            assert result2.is_follow_up is True


# Artifact generation with source selection test configurations
# (method_name, cassette, min_sources, description)
ARTIFACT_SOURCE_SELECTION_TESTS = [
    ("generate_report", "artifacts_generate_report_with_source_ids.yaml", 1, "report"),
    ("generate_quiz", "artifacts_generate_quiz_with_source_ids.yaml", 2, "quiz"),
    ("generate_flashcards", "artifacts_generate_flashcards_with_source_ids.yaml", 1, "flashcards"),
]


class TestArtifactSourceSelection:
    """VCR tests for artifact generation with source_ids parameter.

    These tests verify that artifacts can be generated using a subset of sources.
    """

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method_name,cassette,min_sources,description", ARTIFACT_SOURCE_SELECTION_TESTS
    )
    async def test_generate_with_source_selection(
        self, method_name, cassette, min_sources, description
    ):
        """Test artifact generation using a subset of sources."""
        with notebooklm_vcr.use_cassette(cassette):
            async with vcr_client() as client:
                sources = await client.sources.list(MUTABLE_NOTEBOOK_ID)
                if len(sources) < min_sources:
                    pytest.skip(f"Need at least {min_sources} source(s) for {description} test")

                source_ids = [s.id for s in sources[:min_sources]]
                method = getattr(client.artifacts, method_name)
                result = await method(MUTABLE_NOTEBOOK_ID, source_ids=source_ids)

                assert result is not None
                assert result.task_id is not None


# =============================================================================
# Sources API - Get Operations (High Priority Gap)
# =============================================================================


class TestSourcesGetAPI:
    """VCR tests for source get operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_get.yaml")
    async def test_get_source(self):
        """Get a specific source by ID."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            if not sources:
                pytest.skip("No sources available")

            source = await client.sources.get(READONLY_NOTEBOOK_ID, sources[0].id)
            assert source is not None
            assert isinstance(source, Source)
            assert source.id == sources[0].id

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_get_not_found.yaml")
    async def test_get_source_not_found(self):
        """Get a non-existent source returns None."""
        async with vcr_client() as client:
            source = await client.sources.get(READONLY_NOTEBOOK_ID, "nonexistent_source_id")
            assert source is None


# =============================================================================
# Notes API - Get and Update Operations (High Priority Gap)
# =============================================================================


class TestNotesGetUpdateAPI:
    """VCR tests for notes get and update operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_get.yaml")
    async def test_get_note(self):
        """Get a specific note by ID."""
        async with vcr_client() as client:
            notes = await client.notes.list(READONLY_NOTEBOOK_ID)
            if not notes:
                pytest.skip("No notes available")

            note = await client.notes.get(READONLY_NOTEBOOK_ID, notes[0].id)
            assert note is not None
            assert isinstance(note, Note)
            assert note.id == notes[0].id

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_get_not_found.yaml")
    async def test_get_note_not_found(self):
        """Get a non-existent note returns None."""
        async with vcr_client() as client:
            note = await client.notes.get(READONLY_NOTEBOOK_ID, "nonexistent_note_id")
            assert note is None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_update.yaml")
    async def test_update_note(self):
        """Update a note's content and title."""
        async with vcr_client() as client:
            # Create a note to update
            note = await client.notes.create(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Update Test Note",
                content="Original content for update test.",
            )
            assert note is not None
            assert isinstance(note, Note)

            # Update it
            await client.notes.update(
                MUTABLE_NOTEBOOK_ID,
                note.id,
                title="VCR Updated Title",
                content="Updated content for VCR test.",
            )

            # Verify the update
            updated = await client.notes.get(MUTABLE_NOTEBOOK_ID, note.id)
            assert updated is not None
            assert isinstance(updated, Note)
            assert updated.title == "VCR Updated Title"
            assert updated.content == "Updated content for VCR test."


# =============================================================================
# Artifacts API - Get Operations (High Priority Gap)
# =============================================================================


class TestArtifactsGetAPI:
    """VCR tests for artifact get operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_get.yaml")
    async def test_get_artifact(self):
        """Get a specific artifact by ID."""
        async with vcr_client() as client:
            artifacts = await client.artifacts.list(READONLY_NOTEBOOK_ID)
            if not artifacts:
                pytest.skip("No artifacts available")

            artifact = await client.artifacts.get(READONLY_NOTEBOOK_ID, artifacts[0].id)
            assert artifact is not None
            assert isinstance(artifact, Artifact)
            assert artifact.id == artifacts[0].id

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_get_not_found.yaml")
    async def test_get_artifact_not_found(self):
        """Get a non-existent artifact returns None."""
        async with vcr_client() as client:
            artifact = await client.artifacts.get(READONLY_NOTEBOOK_ID, "nonexistent_artifact_id")
            assert artifact is None


# =============================================================================
# Sharing API - Additional Operations (High Priority Gap)
# =============================================================================


class TestSharingAdditionalAPI:
    """VCR tests for sharing API operations not in base TestSharingAPI."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sharing_set_view_level.yaml")
    async def test_set_view_level(self):
        """Set sharing view level."""
        from notebooklm import ShareViewLevel

        async with vcr_client() as client:
            # Set view level to chat only
            await client.sharing.set_view_level(MUTABLE_NOTEBOOK_ID, ShareViewLevel.CHAT_ONLY)
            # Set back to full notebook
            await client.sharing.set_view_level(MUTABLE_NOTEBOOK_ID, ShareViewLevel.FULL_NOTEBOOK)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sharing_workflow.yaml")
    async def test_sharing_workflow(self):
        """Complete sharing workflow: enable, set view level, disable."""
        from notebooklm import ShareViewLevel

        async with vcr_client() as client:
            # 1. Get initial status
            initial = await client.sharing.get_status(MUTABLE_NOTEBOOK_ID)
            was_public = initial.is_public

            # 2. Enable public sharing
            public_status = await client.sharing.set_public(MUTABLE_NOTEBOOK_ID, True)
            assert public_status.is_public is True
            assert public_status.share_url is not None

            # 3. Set view level
            await client.sharing.set_view_level(MUTABLE_NOTEBOOK_ID, ShareViewLevel.CHAT_ONLY)

            # 4. Verify status
            current = await client.sharing.get_status(MUTABLE_NOTEBOOK_ID)
            assert current.is_public is True

            # 5. Restore original state
            await client.sharing.set_public(MUTABLE_NOTEBOOK_ID, was_public)


# =============================================================================
# Chat API - Configuration and References (High Priority Gap)
# =============================================================================


class TestChatConfigurationAPI:
    """VCR tests for chat configuration operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_set_mode.yaml")
    async def test_set_mode(self):
        """Set chat mode to learning guide then back to default."""
        from notebooklm import ChatMode

        async with vcr_client() as client:
            # Set to learning guide
            await client.chat.set_mode(MUTABLE_NOTEBOOK_ID, ChatMode.LEARNING_GUIDE)
            # Restore to default
            await client.chat.set_mode(MUTABLE_NOTEBOOK_ID, ChatMode.DEFAULT)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_configure.yaml")
    async def test_configure(self):
        """Configure chat with custom persona."""
        from notebooklm import ChatGoal

        async with vcr_client() as client:
            await client.chat.configure(
                MUTABLE_NOTEBOOK_ID,
                goal=ChatGoal.CUSTOM,
                custom_prompt="You are a helpful assistant for VCR testing.",
            )
            # Restore to default
            await client.chat.configure(MUTABLE_NOTEBOOK_ID, goal=ChatGoal.DEFAULT)


class TestChatReferencesAPI:
    """VCR tests for chat reference validation."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_references_with_citations.yaml")
    async def test_ask_returns_references_with_citations(self):
        """Test that ask returns references with proper structure."""
        async with vcr_client() as client:
            result = await client.chat.ask(
                READONLY_NOTEBOOK_ID,
                "Summarize the key points with specific citations.",
            )
            assert result is not None
            assert result.answer is not None
            assert result.conversation_id is not None

            # If references exist, verify structure and type
            for ref in result.references:
                assert isinstance(ref, ChatReference)
                assert ref.source_id is not None
                assert ref.citation_number is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_references_source_validation.yaml")
    async def test_reference_source_ids_exist(self):
        """Test that reference source IDs match actual sources."""
        async with vcr_client() as client:
            # Get sources in the notebook
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            source_ids = {s.id for s in sources}

            # Ask a question that generates citations
            result = await client.chat.ask(
                READONLY_NOTEBOOK_ID,
                "Explain the main concepts with references to sources.",
            )

            # All reference source IDs should exist in the notebook
            for ref in result.references:
                assert isinstance(ref, ChatReference)
                assert ref.source_id in source_ids, (
                    f"Reference source_id {ref.source_id} not found in notebook"
                )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_references_sequential_numbers.yaml")
    async def test_references_have_sequential_numbers(self):
        """Test that references have sequential citation numbers."""
        async with vcr_client() as client:
            result = await client.chat.ask(
                READONLY_NOTEBOOK_ID,
                "List the key points with citations.",
            )

            if result.references:
                # Verify all references are proper ChatReference objects
                assert all(isinstance(ref, ChatReference) for ref in result.references)
                citation_numbers = [ref.citation_number for ref in result.references]
                # All citation numbers should be assigned
                assert all(n is not None for n in citation_numbers)
                # Should be sequential starting from 1
                assert citation_numbers == list(range(1, len(citation_numbers) + 1))
