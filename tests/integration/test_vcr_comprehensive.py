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

import os
import sys
from pathlib import Path

import pytest

# Add tests directory to path for vcr_config import
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import skip_no_cassettes
from notebooklm import NotebookLMClient, ReportFormat
from notebooklm.auth import AuthTokens
from vcr_config import notebooklm_vcr

# Skip all tests in this module if cassettes are not available
pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Use same env vars as e2e tests for consistency
# These only matter during recording - replay uses recorded responses
READONLY_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", "")
MUTABLE_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_GENERATION_NOTEBOOK_ID", "")


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
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            notebooks = await client.notebooks.list()
        assert isinstance(notebooks, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get.yaml")
    async def test_get(self):
        """Get a specific notebook."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            notebook = await client.notebooks.get(READONLY_NOTEBOOK_ID)
        assert notebook is not None
        # Only check ID match when recording (env var set)
        if READONLY_NOTEBOOK_ID:
            assert notebook.id == READONLY_NOTEBOOK_ID

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_summary.yaml")
    async def test_get_summary(self):
        """Get notebook summary."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            summary = await client.notebooks.get_summary(READONLY_NOTEBOOK_ID)
        assert summary is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_description.yaml")
    async def test_get_description(self):
        """Get notebook description."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            description = await client.notebooks.get_description(READONLY_NOTEBOOK_ID)
        assert description is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_raw.yaml")
    async def test_get_raw(self):
        """Get raw notebook data."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            raw = await client.notebooks.get_raw(READONLY_NOTEBOOK_ID)
        assert raw is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_rename.yaml")
    async def test_rename(self):
        """Rename a notebook (then rename back)."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            # Get current name
            notebook = await client.notebooks.get(MUTABLE_NOTEBOOK_ID)
            original_name = notebook.title

            # Rename
            await client.notebooks.rename(MUTABLE_NOTEBOOK_ID, "VCR Test Renamed")

            # Rename back
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
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
        assert isinstance(sources, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_get_guide.yaml")
    async def test_get_guide(self):
        """Get source guide for a specific source."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            # First get a source to test with
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            if sources:
                guide = await client.sources.get_guide(READONLY_NOTEBOOK_ID, sources[0].id)
                assert guide is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_text.yaml")
    async def test_add_text(self):
        """Add a text source."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
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
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
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
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            notes = await client.notes.list(READONLY_NOTEBOOK_ID)
        assert isinstance(notes, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_list_mind_maps.yaml")
    async def test_list_mind_maps(self):
        """List mind maps in a notebook."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            mind_maps = await client.notes.list_mind_maps(READONLY_NOTEBOOK_ID)
        assert isinstance(mind_maps, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_create.yaml")
    async def test_create(self):
        """Create a note."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
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
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            # Create
            note = await client.notes.create(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Update Test",
                content="Original content.",
            )
            assert note is not None

            # Update (returns None on success)
            await client.notes.update(
                MUTABLE_NOTEBOOK_ID,
                note.id,
                title="VCR Update Test - Updated",
                content="Updated content.",
            )


# =============================================================================
# Artifacts API - Read Operations
# =============================================================================


class TestArtifactsReadAPI:
    """Artifacts API read operations."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    async def test_list(self):
        """List all artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            artifacts = await client.artifacts.list(READONLY_NOTEBOOK_ID)
        assert isinstance(artifacts, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_audio.yaml")
    async def test_list_audio(self):
        """List audio artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            audio = await client.artifacts.list_audio(READONLY_NOTEBOOK_ID)
        assert isinstance(audio, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_video.yaml")
    async def test_list_video(self):
        """List video artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            video = await client.artifacts.list_video(READONLY_NOTEBOOK_ID)
        assert isinstance(video, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_reports.yaml")
    async def test_list_reports(self):
        """List report artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            reports = await client.artifacts.list_reports(READONLY_NOTEBOOK_ID)
        assert isinstance(reports, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_quizzes.yaml")
    async def test_list_quizzes(self):
        """List quiz artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            quizzes = await client.artifacts.list_quizzes(READONLY_NOTEBOOK_ID)
        assert isinstance(quizzes, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_flashcards.yaml")
    async def test_list_flashcards(self):
        """List flashcard artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            flashcards = await client.artifacts.list_flashcards(READONLY_NOTEBOOK_ID)
        assert isinstance(flashcards, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_infographics.yaml")
    async def test_list_infographics(self):
        """List infographic artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            infographics = await client.artifacts.list_infographics(READONLY_NOTEBOOK_ID)
        assert isinstance(infographics, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_slide_decks.yaml")
    async def test_list_slide_decks(self):
        """List slide deck artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            slide_decks = await client.artifacts.list_slide_decks(READONLY_NOTEBOOK_ID)
        assert isinstance(slide_decks, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_data_tables.yaml")
    async def test_list_data_tables(self):
        """List data table artifacts."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            data_tables = await client.artifacts.list_data_tables(READONLY_NOTEBOOK_ID)
        assert isinstance(data_tables, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_suggest_reports.yaml")
    async def test_suggest_reports(self):
        """Get report suggestions."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            suggestions = await client.artifacts.suggest_reports(READONLY_NOTEBOOK_ID)
        assert isinstance(suggestions, list)


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
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
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
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            result = await client.artifacts.generate_study_guide(MUTABLE_NOTEBOOK_ID)
        assert result is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_quiz.yaml")
    async def test_generate_quiz(self):
        """Generate a quiz."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            result = await client.artifacts.generate_quiz(MUTABLE_NOTEBOOK_ID)
        assert result is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_flashcards.yaml")
    async def test_generate_flashcards(self):
        """Generate flashcards."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
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
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            result = await client.chat.ask(
                MUTABLE_NOTEBOOK_ID,
                "What is this notebook about?",
            )
        assert result is not None
        assert result.answer is not None

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_get_history.yaml")
    async def test_get_history(self):
        """Get chat history."""
        auth = await AuthTokens.from_storage()
        async with NotebookLMClient(auth) as client:
            history = await client.chat.get_history(MUTABLE_NOTEBOOK_ID)
        assert isinstance(history, list)
