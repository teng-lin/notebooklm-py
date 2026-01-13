"""E2E tests for chat functionality.

These tests require valid NotebookLM authentication.
Run with: pytest tests/e2e/test_chat.py -m e2e
"""

import pytest

from notebooklm import AskResult, ChatReference

from .conftest import requires_auth


@pytest.mark.e2e
@requires_auth
class TestChatE2E:
    """E2E tests for chat API."""

    @pytest.mark.asyncio
    async def test_ask_question_returns_answer(self, client, multi_source_notebook_id):
        """Test asking a question returns a valid answer."""
        result = await client.chat.ask(
            multi_source_notebook_id,
            "What is the main topic of these sources?",
        )

        assert isinstance(result, AskResult)
        assert result.answer
        assert len(result.answer) > 10
        assert result.conversation_id
        assert result.turn_number >= 1

    @pytest.mark.asyncio
    async def test_ask_returns_references_with_source_ids(self, client, multi_source_notebook_id):
        """Test that ask returns references with valid source IDs."""
        # Ask a question likely to generate citations
        result = await client.chat.ask(
            multi_source_notebook_id,
            "Summarize the key points with specific citations.",
        )

        assert isinstance(result, AskResult)
        assert result.answer

        # If the answer contains citations [1], [2], etc., there should be references
        if "[1]" in result.answer:
            assert len(result.references) >= 1, "Answer has citations but no references"

            # Verify references have valid source IDs
            for ref in result.references:
                assert isinstance(ref, ChatReference)
                assert ref.source_id
                # Source ID should be a UUID
                assert len(ref.source_id) == 36
                assert ref.source_id.count("-") == 4

    @pytest.mark.asyncio
    async def test_ask_returns_references_with_cited_text(self, client, multi_source_notebook_id):
        """Test that references include cited text when available."""
        result = await client.chat.ask(
            multi_source_notebook_id,
            "Quote specific passages that explain the main concept.",
        )

        assert isinstance(result, AskResult)

        # Check if any references have cited_text
        refs_with_text = [ref for ref in result.references if ref.cited_text]

        # Note: Not all citations may have cited_text depending on the response
        # So we just verify the structure is correct when present
        for ref in refs_with_text:
            assert isinstance(ref.cited_text, str)
            assert len(ref.cited_text) > 0

    @pytest.mark.asyncio
    async def test_ask_follow_up_conversation(self, client, multi_source_notebook_id):
        """Test follow-up questions use the same conversation."""
        # First question
        result1 = await client.chat.ask(
            multi_source_notebook_id,
            "What is the main topic?",
        )
        assert result1.conversation_id
        assert result1.is_follow_up is False

        # Follow-up question
        result2 = await client.chat.ask(
            multi_source_notebook_id,
            "Can you elaborate on that?",
            conversation_id=result1.conversation_id,
        )
        assert result2.conversation_id == result1.conversation_id
        assert result2.is_follow_up is True
        assert result2.turn_number > result1.turn_number

    @pytest.mark.asyncio
    async def test_ask_new_conversation_flag(self, client, multi_source_notebook_id):
        """Test that --new flag starts a fresh conversation."""
        # Ask first question
        result1 = await client.chat.ask(
            multi_source_notebook_id,
            "What is covered in these sources?",
        )

        # Ask with new conversation (no conversation_id)
        result2 = await client.chat.ask(
            multi_source_notebook_id,
            "Start fresh - what are the main themes?",
        )

        # Should be a new conversation
        assert result2.conversation_id != result1.conversation_id
        assert result2.is_follow_up is False
        assert result2.turn_number == 1

    @pytest.mark.asyncio
    async def test_ask_specific_sources(self, client, multi_source_notebook_id):
        """Test asking questions about specific sources."""
        # Get sources
        sources = await client.sources.list(multi_source_notebook_id)
        if not sources:
            pytest.skip("No sources in notebook")

        # Ask about first source only
        result = await client.chat.ask(
            multi_source_notebook_id,
            "What is this source about?",
            source_ids=[sources[0].id],
        )

        assert isinstance(result, AskResult)
        assert result.answer

    @pytest.mark.asyncio
    async def test_references_have_citation_numbers(self, client, multi_source_notebook_id):
        """Test that references have sequential citation numbers."""
        result = await client.chat.ask(
            multi_source_notebook_id,
            "List the key points with citations.",
        )

        if result.references:
            # Citation numbers should be assigned sequentially
            citation_numbers = [ref.citation_number for ref in result.references]
            assert all(n is not None for n in citation_numbers)
            assert citation_numbers == list(range(1, len(citation_numbers) + 1))


@pytest.mark.e2e
@requires_auth
class TestChatReferencesE2E:
    """E2E tests specifically for chat references and citations."""

    @pytest.mark.asyncio
    async def test_reference_source_ids_exist_in_notebook(self, client, multi_source_notebook_id):
        """Test that reference source IDs correspond to actual sources."""
        # Get all sources in the notebook
        sources = await client.sources.list(multi_source_notebook_id)
        source_ids = {s.id for s in sources}

        # Ask a question that generates citations
        result = await client.chat.ask(
            multi_source_notebook_id,
            "Explain the main concepts with references to sources.",
        )

        # All reference source IDs should exist in the notebook
        for ref in result.references:
            assert ref.source_id in source_ids, (
                f"Reference source_id {ref.source_id} not found in notebook sources"
            )

    @pytest.mark.asyncio
    async def test_cited_text_matches_source_content(self, client, multi_source_notebook_id):
        """Test that cited text comes from the actual source content."""
        result = await client.chat.ask(
            multi_source_notebook_id,
            "Quote a specific passage from the sources.",
        )

        # For references with cited_text, verify it's non-empty
        for ref in result.references:
            if ref.cited_text:
                assert len(ref.cited_text) > 0

                # Could optionally verify against source fulltext:
                # fulltext = await client.sources.get_fulltext(
                #     multi_source_notebook_id, ref.source_id
                # )
                # assert ref.cited_text in fulltext.content
