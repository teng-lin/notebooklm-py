"""E2E tests for chat functionality.

These tests require valid NotebookLM authentication.
Run with: pytest tests/e2e/test_chat.py -m e2e
"""

from copy import deepcopy

import pytest

from notebooklm import AskResult, ChatReference

from .conftest import (
    requires_auth,
    reset_current_chat_conversation,
    skip_or_fail_missing_reference,
)


@pytest.fixture(scope="module")
def _cited_chat_samples():
    # Store response values only, never an async client tied to another test's loop.
    return {}


@pytest.fixture
async def cited_chat_sample(client, multi_source_notebook_id, _cited_chat_samples, request):
    """One real cited answer for independent response-shape assertions.

    These consumers inspect the returned value, not current conversation state.
    Mutation/follow-up tests keep their own fresh conversations and live calls.
    A rerun gets a fresh sample, as does a different notebook or backend.
    """
    key = (
        client.backends["chat"],
        multi_source_notebook_id,
        getattr(request.node, "execution_count", 1),
    )
    if key not in _cited_chat_samples:
        await reset_current_chat_conversation(client, multi_source_notebook_id)
        sources = await client.sources.list(multi_source_notebook_id)
        result = await client.chat.ask(
            multi_source_notebook_id,
            "Summarize the main concepts with specific citations and quote a passage from the sources.",
        )
        _cited_chat_samples[key] = (result, {source.id for source in sources})
    return deepcopy(_cited_chat_samples[key])


@pytest.mark.e2e
@pytest.mark.live_chat_ask
@pytest.mark.timeout(300)
@requires_auth
class TestChatE2E:
    """E2E tests for chat API."""

    @pytest.fixture(autouse=True)
    async def _start_with_fresh_conversation(self, client, multi_source_notebook_id, request):
        if "cited_chat_sample" not in request.fixturenames:
            await reset_current_chat_conversation(client, multi_source_notebook_id)

    @pytest.mark.asyncio
    async def test_ask_question_returns_answer(self, cited_chat_sample):
        """Test asking a question returns a valid answer."""
        result, _source_ids = cited_chat_sample

        assert isinstance(result, AskResult)
        assert result.answer
        assert len(result.answer) > 10
        assert result.conversation_id
        assert result.turn_number >= 1

    @pytest.mark.asyncio
    async def test_ask_returns_references_with_source_ids(self, cited_chat_sample):
        """Test that ask returns references with valid source IDs."""
        result, _source_ids = cited_chat_sample

        assert isinstance(result, AskResult)
        assert result.answer
        assert result.references, "The shared cited answer must exercise citation decoding"

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
    async def test_ask_returns_references_with_cited_text(self, cited_chat_sample):
        """Test that references include cited text when available."""
        result, _source_ids = cited_chat_sample

        assert isinstance(result, AskResult)

        # Check if any references have cited_text
        refs_with_text = [ref for ref in result.references if ref.cited_text]
        assert refs_with_text, "The shared quoted answer must exercise cited-text decoding"

        # Individual structural citations may still omit their text.
        for ref in refs_with_text:
            assert isinstance(ref.cited_text, str)
            assert len(ref.cited_text) > 0

    @pytest.mark.asyncio
    @pytest.mark.timeout(480)
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
    @pytest.mark.timeout(480)
    async def test_delete_conversation_forces_fresh_next_ask(
        self, client, multi_source_notebook_id
    ):
        """``delete_conversation`` is the supported way to force a fresh conversation.

        Without an explicit delete, a null-``conversation_id`` ``ask()`` extends
        the most-recent server conversation (see ``ChatAPI.ask`` Note). After
        deleting, the next null-conv ask starts a brand-new turn-1 conversation.

        Note on conversation_id: ``hPTbtc`` (GET_LAST_CONVERSATION_ID) returns a
        notebook-scoped "current conversation" slot that the server reuses for
        the next turn-1 after a delete. So ``result2.conversation_id`` may equal
        ``result1.conversation_id`` even though result2 is a genuinely fresh
        conversation. The freshness signal is ``turn_number == 1`` /
        ``is_follow_up is False``, plus the server-side turn count.
        """
        result1 = await client.chat.ask(
            multi_source_notebook_id,
            "What is covered in these sources?",
        )
        assert result1.conversation_id

        await client.chat.delete_conversation(multi_source_notebook_id, result1.conversation_id)

        result2 = await client.chat.ask(
            multi_source_notebook_id,
            "Start fresh - what are the main themes?",
        )

        assert result2.is_follow_up is False
        assert result2.turn_number == 1

        # Server-side confirmation that result2 really is a fresh conversation
        # and not a follow-up to result1: the conversation should hold only the
        # one Q&A pair we just posted. Pin ``result2.conversation_id`` first so
        # we don't silently fall through to the implicit "current conversation"
        # that ``get_history`` would resolve on a ``None`` argument.
        assert result2.conversation_id
        history = await client.chat.get_history(
            multi_source_notebook_id, conversation_id=result2.conversation_id
        )
        assert len(history) == 1

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
    async def test_references_have_citation_numbers(self, cited_chat_sample):
        """Test that references have sequential citation numbers."""
        result, _source_ids = cited_chat_sample

        if result.references:
            # Citation numbers should be assigned sequentially
            citation_numbers = [ref.citation_number for ref in result.references]
            assert all(n is not None for n in citation_numbers)
            assert citation_numbers == list(range(1, len(citation_numbers) + 1))


@pytest.mark.e2e
@requires_auth
class TestChatHistoryE2E:
    """E2E tests for chat history and conversation turns API (khqZz RPC).

    These tests use an existing read-only notebook with pre-existing conversation
    history. They do not ask new questions, since conversation persistence takes
    time and makes tests flaky.
    """

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_conversation_turns_returns_qa(self, client, read_only_notebook_id):
        """get_conversation_turns returns Q&A turns for an existing conversation."""
        conv_id = await client.chat.get_conversation_id(read_only_notebook_id)
        if not conv_id:
            skip_or_fail_missing_reference(
                "No conversation history available in read-only notebook"
            )

        turns_data = await client.chat.get_conversation_turns(
            read_only_notebook_id,
            conv_id,
            limit=2,
        )

        assert turns_data is not None
        if client.backends["chat"] == "android":
            from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
                chat_pb2,
            )

            assert isinstance(turns_data, chat_pb2.ListChatTurnsResponse)
            turns = list(turns_data.chat_turns)
            turn_types = [turn.observed_event_type for turn in turns]
        else:
            if not turns_data:
                skip_or_fail_missing_reference(
                    "Read-only notebook has a conversation but no chat turns — "
                    "cannot verify turn structure. Seed the notebook with chat messages to enable this test."
                )
            assert isinstance(turns_data[0], list)
            turns = turns_data[0]
            turn_types = [turn[2] for turn in turns if isinstance(turn, list) and len(turn) > 2]
        if not turns and client.backends["chat"] == "android":
            pytest.fail("Seeded conversation exists but Android ListChatTurns decoded no turns")
        if not turns:
            skip_or_fail_missing_reference(
                "Read-only notebook has a conversation but no chat turns — "
                "cannot verify turn structure. Seed the notebook with chat messages to enable this test."
            )
        assert len(turns) >= 1
        assert any(t in (1, 2) for t in turn_types), "Expected question or answer turns"

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_conversation_turns_question_text(self, client, read_only_notebook_id):
        """get_conversation_turns includes question text in an existing conversation."""
        conv_id = await client.chat.get_conversation_id(read_only_notebook_id)
        if not conv_id:
            skip_or_fail_missing_reference(
                "No conversation history available in read-only notebook"
            )

        turns_data = await client.chat.get_conversation_turns(
            read_only_notebook_id,
            conv_id,
            limit=2,
        )

        assert turns_data is not None
        if client.backends["chat"] == "android":
            from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
                chat_pb2,
            )

            assert isinstance(turns_data, chat_pb2.ListChatTurnsResponse)
            turns = list(turns_data.chat_turns)
            questions = [turn.user_query_text for turn in turns if turn.user_query_text]
        else:
            if not turns_data:
                skip_or_fail_missing_reference(
                    "Read-only notebook has a conversation but no chat turns — "
                    "cannot verify question text. Seed the notebook with chat messages to enable this test."
                )
            turns = turns_data[0]
            questions = [
                turn[3]
                for turn in turns
                if isinstance(turn, list) and len(turn) > 3 and turn[2] == 1
            ]
        if not turns and client.backends["chat"] == "android":
            pytest.fail("Seeded conversation exists but Android ListChatTurns decoded no turns")
        if not turns:
            skip_or_fail_missing_reference(
                "Read-only notebook has a conversation but no chat turns — "
                "cannot verify question text. Seed the notebook with chat messages to enable this test."
            )
        assert questions, "No question turn found in response"
        assert isinstance(questions[0], str)
        assert len(questions[0]) > 0

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_conversation_turns_answer_text(self, client, read_only_notebook_id):
        """get_conversation_turns includes AI answer text in an existing conversation."""
        conv_id = await client.chat.get_conversation_id(read_only_notebook_id)
        if not conv_id:
            skip_or_fail_missing_reference(
                "No conversation history available in read-only notebook"
            )

        turns_data = await client.chat.get_conversation_turns(
            read_only_notebook_id,
            conv_id,
            limit=20,
        )

        assert turns_data is not None
        if client.backends["chat"] == "android":
            from notebooklm._android.codecs.documents import tailwind_doc_plain_text
            from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
                chat_pb2,
            )

            assert isinstance(turns_data, chat_pb2.ListChatTurnsResponse)
            turns = list(turns_data.chat_turns)
            answers = [
                turn.act_on_sources_response.response.response
                or tailwind_doc_plain_text(turn.act_on_sources_response.response.response_doc)
                for turn in turns
                if turn.HasField("act_on_sources_response")
                and turn.act_on_sources_response.HasField("response")
            ]
        else:
            if not turns_data:
                skip_or_fail_missing_reference(
                    "Read-only notebook has a conversation but no chat turns — "
                    "cannot verify answer text. Seed the notebook with chat messages to enable this test."
                )
            turns = turns_data[0]
            answer_turns = [
                turn for turn in turns if isinstance(turn, list) and len(turn) > 4 and turn[2] == 2
            ]
            answers = [turn[4][0][0] for turn in answer_turns]
        if not turns and client.backends["chat"] == "android":
            pytest.fail("Seeded conversation exists but Android ListChatTurns decoded no turns")
        if not turns:
            skip_or_fail_missing_reference(
                "Read-only notebook has a conversation but no chat turns — "
                "cannot verify answer text. Seed the notebook with chat messages to enable this test."
            )
        assert answers, "No answer turn found in response"
        answer_text = next((answer for answer in answers if answer), "")
        if not answer_text:
            skip_or_fail_missing_reference(
                "Conversation history has answer turns but no completed answer text — "
                "cannot verify answer content. Quota-rejected asks can leave this partial "
                "record; seed a completed chat response to enable this test."
            )
        assert isinstance(answer_text, str)
        assert len(answer_text) > 0

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_conversation_id(self, client, read_only_notebook_id):
        """get_conversation_id returns an existing conversation ID."""
        conv_id = await client.chat.get_conversation_id(read_only_notebook_id)
        if not conv_id:
            skip_or_fail_missing_reference(
                "No conversation history available in read-only notebook"
            )

        assert isinstance(conv_id, str)
        assert len(conv_id) > 0

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_history_returns_qa_pairs(self, client, read_only_notebook_id):
        """get_history returns Q&A pairs from existing conversation history."""
        qa_pairs = await client.chat.get_history(read_only_notebook_id)
        if not qa_pairs and client.backends["chat"] == "android":
            pytest.fail("Seeded Android conversation decoded no Q&A pairs")
        if not qa_pairs:
            skip_or_fail_missing_reference(
                "No conversation history available in read-only notebook"
            )

        # Quota-rejected asks can leave an incomplete question with an empty
        # answer in server history. Verify the most recent completed pair
        # instead of treating that expected partial record as decoder drift.
        completed = next(((q, a) for q, a in reversed(qa_pairs) if q and a), None)
        if completed is None:
            skip_or_fail_missing_reference("Conversation history has no completed Q&A pair")
        q, a = completed
        assert isinstance(q, str) and q, "Question should be non-empty string"
        assert isinstance(a, str) and a, "Answer should be non-empty string"


@pytest.mark.e2e
@pytest.mark.live_chat_ask
@pytest.mark.timeout(300)
@requires_auth
class TestChatReferencesE2E:
    """E2E tests specifically for chat references and citations."""

    @pytest.mark.asyncio
    async def test_reference_source_ids_exist_in_notebook(self, cited_chat_sample):
        """Test that reference source IDs correspond to actual sources."""
        result, source_ids = cited_chat_sample

        # All reference source IDs should exist in the notebook
        for ref in result.references:
            assert ref.source_id in source_ids, (
                f"Reference source_id {ref.source_id} not found in notebook sources"
            )

    @pytest.mark.asyncio
    async def test_cited_text_matches_source_content(self, cited_chat_sample):
        """Test that cited text comes from the actual source content."""
        result, _source_ids = cited_chat_sample

        # For references with cited_text, verify it's non-empty
        for ref in result.references:
            if ref.cited_text:
                assert len(ref.cited_text) > 0

                # Could optionally verify against source fulltext:
                # fulltext = await client.sources.get_fulltext(
                #     multi_source_notebook_id, ref.source_id
                # )
                # assert ref.cited_text in fulltext.content
