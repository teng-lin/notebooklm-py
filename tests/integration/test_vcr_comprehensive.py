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
from notebooklm import NotebookLMClient, ReportFormat
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

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get.yaml")
    async def test_get(self):
        """Get a specific notebook."""
        async with vcr_client() as client:
            notebook = await client.notebooks.get(READONLY_NOTEBOOK_ID)
        assert notebook is not None
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

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_description.yaml")
    async def test_get_description(self):
        """Get notebook description."""
        async with vcr_client() as client:
            description = await client.notebooks.get_description(READONLY_NOTEBOOK_ID)
        assert description is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_raw.yaml")
    async def test_get_raw(self):
        """Get raw notebook data."""
        async with vcr_client() as client:
            raw = await client.notebooks.get_raw(READONLY_NOTEBOOK_ID)
        assert raw is not None

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
        assert len(fulltext.content) > 0

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

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_list_mind_maps.yaml")
    async def test_list_mind_maps(self):
        """List mind maps in a notebook."""
        async with vcr_client() as client:
            mind_maps = await client.notes.list_mind_maps(READONLY_NOTEBOOK_ID)
        assert isinstance(mind_maps, list)

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
                if method_name == "list":
                    result = await method(READONLY_NOTEBOOK_ID)
                else:
                    result = await method(READONLY_NOTEBOOK_ID)
                assert isinstance(result, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_suggest_reports.yaml")
    async def test_suggest_reports(self):
        """Get report suggestions."""
        async with vcr_client() as client:
            suggestions = await client.artifacts.suggest_reports(READONLY_NOTEBOOK_ID)
        assert isinstance(suggestions, list)


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
        assert result is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_study_guide.yaml")
    async def test_generate_study_guide(self):
        """Generate a study guide."""
        async with vcr_client() as client:
            result = await client.artifacts.generate_study_guide(MUTABLE_NOTEBOOK_ID)
        assert result is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_quiz.yaml")
    async def test_generate_quiz(self):
        """Generate a quiz."""
        async with vcr_client() as client:
            result = await client.artifacts.generate_quiz(MUTABLE_NOTEBOOK_ID)
        assert result is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_flashcards.yaml")
    async def test_generate_flashcards(self):
        """Generate flashcards."""
        async with vcr_client() as client:
            result = await client.artifacts.generate_flashcards(MUTABLE_NOTEBOOK_ID)
        assert result is not None


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
        assert result.answer is not None
        assert result.conversation_id is not None

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
        # If references exist, verify structure
        for ref in result.references:
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
        assert source.id is not None

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
        assert isinstance(result, bool)

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
