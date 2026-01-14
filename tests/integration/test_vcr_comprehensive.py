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
