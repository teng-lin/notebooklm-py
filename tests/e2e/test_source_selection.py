"""E2E tests for multi-source selection in chat and artifact generation.

These tests verify that:
1. Source selection works correctly with a subset of sources
2. The source_ids parameter is properly encoded in API requests
3. Operations complete successfully with partial source selection

Tests use multi_source_notebook_id fixture which provides a notebook with 3 sources.

Notebook lifecycle:
- Auto-created on first run if NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID not set
- Artifacts cleaned BEFORE tests to ensure clean state
- Sources preserved (tests need them)
- In CI (CI=true): notebook deleted after tests
- Locally: notebook persists, ID stored in NOTEBOOKLM_HOME
"""

import random

import pytest

from .conftest import assert_generation_started, requires_auth


@requires_auth
class TestChatWithSourceSelection:
    """Tests for chat.ask() with explicit source selection."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_ask_with_all_sources(self, client, multi_source_notebook_id):
        """Test asking a question using all sources (source_ids=None)."""
        result = await client.chat.ask(
            multi_source_notebook_id,
            "What topics are covered in this notebook?",
            source_ids=None,  # Uses all sources
        )
        assert result.answer is not None
        assert len(result.answer) > 20
        assert result.conversation_id is not None

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_ask_with_single_source(self, client, multi_source_notebook_id):
        """Test asking a question using only one source."""
        # Get sources and pick just one
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 1, "Expected at least 1 source in test notebook"

        single_source = [sources[0].id]

        result = await client.chat.ask(
            multi_source_notebook_id,
            "What is this source about?",
            source_ids=single_source,
        )
        assert result.answer is not None
        assert len(result.answer) > 20
        assert result.conversation_id is not None

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_ask_with_random_subset_of_sources(self, client, multi_source_notebook_id):
        """Test asking a question using a random subset of sources."""
        # Get all sources
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 2, "Expected at least 2 sources in test notebook"

        # Randomly pick 2 sources (if 3 available, this tests partial selection)
        num_to_pick = min(2, len(sources))
        selected_sources = random.sample(sources, num_to_pick)
        source_ids = [s.id for s in selected_sources]

        result = await client.chat.ask(
            multi_source_notebook_id,
            "Summarize the key points from these sources.",
            source_ids=source_ids,
        )
        assert result.answer is not None
        assert len(result.answer) > 20
        assert result.conversation_id is not None

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_ask_follow_up_with_different_sources(self, client, multi_source_notebook_id):
        """Test follow-up question can use different source selection."""
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 2, "Expected at least 2 sources"

        # First question with first source
        result1 = await client.chat.ask(
            multi_source_notebook_id,
            "What is covered here?",
            source_ids=[sources[0].id],
        )
        assert result1.answer is not None

        # Follow-up using second source in same conversation
        result2 = await client.chat.ask(
            multi_source_notebook_id,
            "What about this topic?",
            source_ids=[sources[1].id] if len(sources) > 1 else [sources[0].id],
            conversation_id=result1.conversation_id,
        )
        assert result2.answer is not None
        assert result2.is_follow_up is True
        assert result2.turn_number == 2


@requires_auth
class TestArtifactGenerationWithSourceSelection:
    """Tests for artifact generation with explicit source selection.

    These tests verify that generation works with:
    1. All sources (None)
    2. Single source
    3. Random subset of sources

    Only tests a few artifact types to conserve API quota.
    """

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_generate_report_with_all_sources(self, client, multi_source_notebook_id):
        """Test report generation using all sources."""
        result = await client.artifacts.generate_report(
            multi_source_notebook_id,
            source_ids=None,
        )
        assert_generation_started(result, "Report")

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_generate_report_with_single_source(self, client, multi_source_notebook_id):
        """Test report generation using only one source."""
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 1

        result = await client.artifacts.generate_report(
            multi_source_notebook_id,
            source_ids=[sources[0].id],
        )
        assert_generation_started(result, "Report")

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_generate_report_with_subset_of_sources(self, client, multi_source_notebook_id):
        """Test report generation using a random subset of sources."""
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 2

        # Pick 2 random sources
        selected = random.sample(sources, 2)
        source_ids = [s.id for s in selected]

        result = await client.artifacts.generate_report(
            multi_source_notebook_id,
            source_ids=source_ids,
        )
        assert_generation_started(result, "Report")

    @pytest.mark.asyncio
    @pytest.mark.e2e
    @pytest.mark.variants
    async def test_generate_quiz_with_single_source(self, client, multi_source_notebook_id):
        """Test quiz generation using only one source."""
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 1

        result = await client.artifacts.generate_quiz(
            multi_source_notebook_id,
            source_ids=[sources[0].id],
        )
        assert_generation_started(result, "Quiz")

    @pytest.mark.asyncio
    @pytest.mark.e2e
    @pytest.mark.variants
    async def test_generate_flashcards_with_subset(self, client, multi_source_notebook_id):
        """Test flashcard generation using a subset of sources."""
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 2

        selected = random.sample(sources, 2)
        source_ids = [s.id for s in selected]

        result = await client.artifacts.generate_flashcards(
            multi_source_notebook_id,
            source_ids=source_ids,
        )
        assert_generation_started(result, "Flashcards")

    @pytest.mark.asyncio
    @pytest.mark.e2e
    @pytest.mark.variants
    async def test_generate_audio_with_single_source(self, client, multi_source_notebook_id):
        """Test audio generation using only one source."""
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 1

        result = await client.artifacts.generate_audio(
            multi_source_notebook_id,
            source_ids=[sources[0].id],
        )
        assert_generation_started(result, "Audio")


@requires_auth
class TestSourceListingAndSelection:
    """Tests for source listing to ensure test setup is correct."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_notebook_has_multiple_sources(self, client, multi_source_notebook_id):
        """Verify the test notebook has at least 3 sources."""
        sources = await client.sources.list(multi_source_notebook_id)

        assert len(sources) >= 3, (
            f"Expected at least 3 sources for multi-source tests, got {len(sources)}"
        )

        # Verify each source has an ID
        for source in sources:
            assert source.id is not None
            assert len(source.id) > 0

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_source_ids_are_unique(self, client, multi_source_notebook_id):
        """Verify all source IDs are unique."""
        sources = await client.sources.list(multi_source_notebook_id)

        source_ids = [s.id for s in sources]
        unique_ids = set(source_ids)

        assert len(source_ids) == len(unique_ids), "Source IDs should be unique"


@requires_auth
class TestEdgeCases:
    """Edge case tests for source selection."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_ask_with_explicit_all_sources(self, client, multi_source_notebook_id):
        """Test asking with explicitly listing all source IDs (same as None)."""
        sources = await client.sources.list(multi_source_notebook_id)
        all_source_ids = [s.id for s in sources]

        result = await client.chat.ask(
            multi_source_notebook_id,
            "Give me a summary of all content.",
            source_ids=all_source_ids,
        )
        assert result.answer is not None
        assert len(result.answer) > 20

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_sources_appear_in_different_order(self, client, multi_source_notebook_id):
        """Test that source order doesn't affect results."""
        sources = await client.sources.list(multi_source_notebook_id)
        assert len(sources) >= 2

        source_ids = [s.id for s in sources[:2]]

        # Ask with original order
        result1 = await client.chat.ask(
            multi_source_notebook_id,
            "List the main topics.",
            source_ids=source_ids,
        )

        # Ask with reversed order (new conversation)
        result2 = await client.chat.ask(
            multi_source_notebook_id,
            "List the main topics.",
            source_ids=list(reversed(source_ids)),
        )

        # Both should produce valid answers
        assert result1.answer is not None
        assert result2.answer is not None
        # Answers may differ due to non-determinism, but both should work
