"""Offline checks for live E2E sample reuse and preserved assertions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from notebooklm import AskResult, ChatReference, RateLimitError
from tests.e2e import test_chat as chat_tests
from tests.e2e import test_source_selection as selection_tests

SOURCE_ID = "11111111-1111-1111-1111-111111111111"


def _client():
    return SimpleNamespace(
        backends={"chat": "web", "sources": "web"},
        sources=SimpleNamespace(list=AsyncMock(return_value=[SimpleNamespace(id=SOURCE_ID)])),
        chat=SimpleNamespace(
            get_conversation_id=AsyncMock(return_value="old-conversation"),
            delete_conversation=AsyncMock(),
            ask=AsyncMock(
                return_value=AskResult(
                    answer="The source explains a useful concept [1].",
                    conversation_id="new-conversation",
                    turn_number=1,
                    is_follow_up=False,
                    references=[
                        ChatReference(source_id=SOURCE_ID, citation_number=1, cited_text="concept")
                    ],
                )
            ),
        ),
    )


def _request(attempt=1):
    return SimpleNamespace(node=SimpleNamespace(execution_count=attempt))


@pytest.mark.asyncio
async def test_six_chat_assertion_tests_share_one_live_ask_and_reset():
    client = _client()
    cache = {}
    consumers = [
        chat_tests.TestChatE2E().test_ask_question_returns_answer,
        chat_tests.TestChatE2E().test_ask_returns_references_with_source_ids,
        chat_tests.TestChatE2E().test_ask_returns_references_with_cited_text,
        chat_tests.TestChatE2E().test_references_have_citation_numbers,
        chat_tests.TestChatReferencesE2E().test_reference_source_ids_exist_in_notebook,
        chat_tests.TestChatReferencesE2E().test_cited_text_matches_source_content,
    ]
    for consumer in consumers:
        sample = await chat_tests.cited_chat_sample.__wrapped__(
            client, "notebook", cache, _request()
        )
        await consumer(sample)
    client.chat.ask.assert_awaited_once()
    client.sources.list.assert_awaited_once_with("notebook")
    client.chat.get_conversation_id.assert_awaited_once_with("notebook")
    client.chat.delete_conversation.assert_awaited_once_with("notebook", "old-conversation")


@pytest.mark.parametrize("missing", ["references", "cited_text"])
@pytest.mark.asyncio
async def test_shared_sample_cannot_make_citation_coverage_vacuously_pass(missing):
    sample = await chat_tests.cited_chat_sample.__wrapped__(_client(), "notebook", {}, _request())
    if missing == "references":
        sample[0].references.clear()
        consumer = chat_tests.TestChatE2E().test_ask_returns_references_with_source_ids
    else:
        sample[0].references[0].cited_text = None
        consumer = chat_tests.TestChatE2E().test_ask_returns_references_with_cited_text
    with pytest.raises(AssertionError, match="must exercise"):
        await consumer(sample)


@pytest.mark.parametrize("fixture_name", ["chat", "sources"])
@pytest.mark.asyncio
async def test_samples_are_isolated_by_notebook_backend_attempt_and_consumer(fixture_name):
    client = _client()
    cache = {}
    fixture = (
        chat_tests.cited_chat_sample
        if fixture_name == "chat"
        else selection_tests.multi_source_sources
    ).__wrapped__
    first = await fixture(client, "notebook", cache, _request())
    if fixture_name == "chat":
        first[0].references.clear()
        first[1].clear()
    else:
        first[0].id = "mutated"
        first.clear()
    second = await fixture(client, "notebook", cache, _request())
    if fixture_name == "chat":
        assert second[0].references[0].source_id == SOURCE_ID
        assert second[1] == {SOURCE_ID}
    else:
        assert second[0].id == SOURCE_ID
    assert client.sources.list.await_count == 1

    await fixture(client, "different-notebook", cache, _request())
    client.backends = {"chat": "android", "sources": "android"}
    await fixture(client, "notebook", cache, _request())
    await fixture(client, "notebook", cache, _request(attempt=2))
    assert client.sources.list.await_count == 4
    if fixture_name == "chat":
        assert client.chat.ask.await_count == 4


@pytest.mark.parametrize("fixture_name", ["chat", "sources"])
@pytest.mark.asyncio
async def test_failed_live_reads_are_not_cached_as_success(fixture_name):
    client = _client()
    cache = {}
    if fixture_name == "chat":
        fixture = chat_tests.cited_chat_sample.__wrapped__
        operation = client.chat.ask
    else:
        fixture = selection_tests.multi_source_sources.__wrapped__
        operation = client.sources.list
    operation.side_effect = RateLimitError("quota")
    with pytest.raises(RateLimitError):
        await fixture(client, "notebook", cache, _request())
    assert not cache
    operation.side_effect = None
    await fixture(client, "notebook", cache, _request(attempt=2))
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_only_response_consumers_bypass_per_test_conversation_reset():
    client = _client()
    reset = chat_tests.TestChatE2E()._start_with_fresh_conversation.__wrapped__
    await reset(None, client, "notebook", SimpleNamespace(fixturenames=["cited_chat_sample"]))
    client.chat.delete_conversation.assert_not_awaited()
    await reset(None, client, "notebook", SimpleNamespace(fixturenames=["client"]))
    client.chat.delete_conversation.assert_awaited_once_with("notebook", "old-conversation")
