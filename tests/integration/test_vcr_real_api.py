"""VCR tests against real NotebookLM API.

These tests record actual API interactions for regression testing.
Run with NOTEBOOKLM_VCR_RECORD=1 to record new cassettes.

Recording requires: NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID (same as e2e tests)

Note: Notebook ID only matters when RECORDING. During replay, VCR uses
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
from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from vcr_config import notebooklm_vcr

# Skip all tests in this module if cassettes are not available
pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Use same env var as e2e tests for consistency
TEST_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", "")


class TestRealAPIWithVCR:
    """Tests that record/replay real NotebookLM API interactions."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("real_api_list_notebooks.yaml")
    async def test_list_notebooks(self):
        """Record listing notebooks from real API."""
        auth = await AuthTokens.from_storage()

        async with NotebookLMClient(auth) as client:
            notebooks = await client.notebooks.list()

        assert isinstance(notebooks, list)
        # Should have at least the test notebook
        assert len(notebooks) >= 1

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("real_api_get_notebook.yaml")
    async def test_get_notebook(self):
        """Record getting a specific notebook from real API."""
        auth = await AuthTokens.from_storage()

        async with NotebookLMClient(auth) as client:
            notebook = await client.notebooks.get(TEST_NOTEBOOK_ID)

        assert notebook is not None
        # Only check ID match when recording (env var set)
        if TEST_NOTEBOOK_ID:
            assert notebook.id == TEST_NOTEBOOK_ID

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("real_api_list_sources.yaml")
    async def test_list_sources(self):
        """Record listing sources from a notebook."""
        auth = await AuthTokens.from_storage()

        async with NotebookLMClient(auth) as client:
            sources = await client.sources.list(TEST_NOTEBOOK_ID)

        assert isinstance(sources, list)


class TestArtifactsWithVCR:
    """Tests for artifact operations (most likely to be rate-limited)."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("real_api_list_artifacts.yaml")
    async def test_list_artifacts(self):
        """Record listing all artifacts from a notebook."""
        auth = await AuthTokens.from_storage()

        async with NotebookLMClient(auth) as client:
            artifacts = await client.artifacts.list(TEST_NOTEBOOK_ID)

        assert isinstance(artifacts, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("real_api_list_audio.yaml")
    async def test_list_audio_artifacts(self):
        """Record listing audio artifacts (podcasts)."""
        auth = await AuthTokens.from_storage()

        async with NotebookLMClient(auth) as client:
            audio = await client.artifacts.list_audio(TEST_NOTEBOOK_ID)

        assert isinstance(audio, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("real_api_list_reports.yaml")
    async def test_list_reports(self):
        """Record listing report artifacts."""
        auth = await AuthTokens.from_storage()

        async with NotebookLMClient(auth) as client:
            reports = await client.artifacts.list_reports(TEST_NOTEBOOK_ID)

        assert isinstance(reports, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("real_api_list_quizzes.yaml")
    async def test_list_quizzes(self):
        """Record listing quiz artifacts."""
        auth = await AuthTokens.from_storage()

        async with NotebookLMClient(auth) as client:
            quizzes = await client.artifacts.list_quizzes(TEST_NOTEBOOK_ID)

        assert isinstance(quizzes, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("real_api_list_mind_maps.yaml")
    async def test_list_mind_maps(self):
        """Record listing mind map artifacts (via notes API)."""
        auth = await AuthTokens.from_storage()

        async with NotebookLMClient(auth) as client:
            mind_maps = await client.notes.list_mind_maps(TEST_NOTEBOOK_ID)

        assert isinstance(mind_maps, list)
