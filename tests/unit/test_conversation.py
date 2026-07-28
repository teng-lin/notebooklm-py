"""Tests for conversation functionality."""

import json
import re

import pytest

from notebooklm import AskResult, NotebookLMClient
from notebooklm.exceptions import ChatError
from notebooklm.rpc import RPCMethod


class TestAsk:
    @pytest.mark.asyncio
    async def test_ask_new_conversation(
        self, auth_tokens, httpx_mock, mock_get_conversation_id, build_rpc_response
    ):
        import re

        # Mock the chat-ask streamed response.
        inner_json = json.dumps(
            [
                [
                    "This is the answer. It is now long enough to be valid.",
                    None,
                    ["stream-id-not-conv", 12345],
                    None,
                    [1],
                ]
            ]
        )
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])

        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=response_body.encode(),
            method="POST",
        )
        # First-ever conversation: the pre-POST hPTbtc resolve finds no current
        # conversation (empty envelope → None), so the null ask creates a fresh
        # one. Its real conversation_id is recovered via a second, post-POST
        # hPTbtc round-trip (issue #659). The None-then-real ordering makes this
        # a genuine new conversation, so is_follow_up stays False (#1965).
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*rpcids=hPTbtc.*"),
            content=build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [[[]]]).encode(),
            method="POST",
        )
        mock_get_conversation_id(conv_id="real-conv-id")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask(
                notebook_id="nb_123",
                question="What is this?",
                source_ids=["test_source"],
            )

        assert isinstance(result, AskResult)
        assert result.answer == "This is the answer. It is now long enough to be valid."
        assert result.is_follow_up is False
        assert result.turn_number == 1
        assert result.conversation_id == "real-conv-id"

    @pytest.mark.asyncio
    async def test_ask_implicit_continue_is_follow_up(
        self, auth_tokens, httpx_mock, mock_get_conversation_id, build_rpc_response
    ):
        """Two null asks on one notebook: the second continues the conversation.

        Regression for #1965. When ``ask()`` is called without a
        ``conversation_id``, it appends to the notebook's current conversation.
        The *first* such ask on a fresh notebook starts a new conversation
        (``is_follow_up=False``); the *second* resumes it and must report
        ``is_follow_up=True`` with ``turn_number == 2`` and the same id.
        """
        import re

        def stream_body(answer: str) -> bytes:
            inner_json = json.dumps([[answer, None, ["stream-id-not-conv", 12345], None, [1]]])
            chunk_json = json.dumps([["wrb.fr", None, inner_json]])
            return f")]}}'\n{len(chunk_json)}\n{chunk_json}\n".encode()

        # One streamed answer per ask (reusable — both asks POST the same URL).
        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=stream_body("First answer, long enough to be a valid reply."),
            method="POST",
            is_reusable=True,
        )
        # Ask #1 (fresh notebook): pre-POST hPTbtc resolves to None, so a new
        # conversation is created and its id recovered post-POST.
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*rpcids=hPTbtc.*"),
            content=build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [[[]]]).encode(),
            method="POST",
        )
        # Every subsequent hPTbtc call (post-POST recovery for ask #1, and the
        # pre-POST resolve for ask #2) returns the now-current conversation id.
        mock_get_conversation_id(conv_id="conv-x", reusable=True)

        async with NotebookLMClient(auth_tokens) as client:
            result1 = await client.chat.ask(
                notebook_id="nb_123",
                question="First question?",
                source_ids=["test_source"],
            )
            result2 = await client.chat.ask(
                notebook_id="nb_123",
                question="Second question?",
                source_ids=["test_source"],
            )

        assert result1.is_follow_up is False
        assert result1.turn_number == 1
        assert result1.conversation_id == "conv-x"

        assert result2.is_follow_up is True
        assert result2.turn_number == 2
        assert result2.conversation_id == "conv-x"

    @pytest.mark.asyncio
    async def test_ask_follow_up(self, auth_tokens, httpx_mock):
        _TEST_CONV_ID = "a1b2c3d4-0000-0000-0000-000000000002"
        inner_json = json.dumps(
            [
                [
                    "Follow-up answer. This also needs to be longer than twenty characters.",
                    None,
                    [_TEST_CONV_ID, 12345],
                    None,
                    [1],
                ]
            ]
        )
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            # Seed cache via the public helper (cache moved off Session).
            client.chat._cache.cache_conversation_turn(_TEST_CONV_ID, "Q1", "A1", 1)

            result = await client.chat.ask(
                notebook_id="nb_123",
                question="Follow up?",
                conversation_id=_TEST_CONV_ID,
                source_ids=["test_source"],
            )

        assert isinstance(result, AskResult)
        assert (
            result.answer
            == "Follow-up answer. This also needs to be longer than twenty characters."
        )
        assert result.is_follow_up is True
        assert result.turn_number == 2

    @pytest.mark.asyncio
    async def test_ask_raises_chat_error_on_rate_limit(
        self, auth_tokens, httpx_mock, mock_get_conversation_id
    ):
        """ask() raises ChatError when the server returns UserDisplayableError."""
        # A null ask resolves the notebook's current conversation via hPTbtc
        # before the POST (issue #1875); mock it so the POST is what fails.
        mock_get_conversation_id()
        error_chunk = json.dumps(
            [
                [
                    "wrb.fr",
                    None,
                    None,
                    None,
                    None,
                    [
                        8,
                        None,
                        [
                            [
                                "type.googleapis.com/google.internal.labs.tailwind"
                                ".orchestration.v1.UserDisplayableError",
                                [None, [None, [[1]]]],
                            ]
                        ],
                    ],
                ]
            ]
        )
        response_body = f")]}}'\n{len(error_chunk)}\n{error_chunk}\n"
        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=response_body.encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ChatError, match="rate limited"):
                await client.chat.ask("nb_123", "What is this?", source_ids=["test_source"])

    @pytest.mark.asyncio
    async def test_ask_returns_hptbtc_conversation_id_not_stream_id(
        self, auth_tokens, httpx_mock, mock_get_conversation_id
    ):
        """``AskResult.conversation_id`` is the hPTbtc-fetched real id, NOT
        the stream id at ``first[2][0]`` in the chat response (issue #659).

        Prior to the fix, the SDK extracted ``first[2][0]`` from the
        streaming response and treated it as the conversation_id. Live API
        tests proved that field is a per-stream/per-query id that returns
        0 turns when queried via ``khqZz``. The real id only comes from
        ``hPTbtc`` after the ask.
        """
        stream_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        real_conv_id = "11111111-2222-3333-4444-555555555555"
        inner_json = json.dumps(
            [
                [
                    "Server answer text that is long enough to be valid.",
                    None,
                    [stream_id, "hash123"],
                    None,
                    [1],
                ]
            ]
        )
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"
        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=response_body.encode(),
            method="POST",
        )
        mock_get_conversation_id(conv_id=real_conv_id)

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask("nb_123", "What is this?", source_ids=["test_source"])

        assert result.conversation_id == real_conv_id
        assert result.conversation_id != stream_id
