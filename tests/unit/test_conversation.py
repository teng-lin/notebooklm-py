"""Tests for conversation functionality."""

import json
import re

import pytest

from notebooklm import AskResult, NotebookLMClient
from notebooklm.exceptions import ChatError


class TestAsk:
    @pytest.mark.asyncio
    async def test_ask_new_conversation(self, auth_tokens, httpx_mock, mock_get_conversation_id):
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
        # New conversations now require a hPTbtc round-trip post-ask
        # (issue #659): the SDK fetches the real conversation_id from
        # there because the streamed response only contains a stream id.
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
            # Seed cache via core client
            client._core._conversation_cache[_TEST_CONV_ID] = [
                {"query": "Q1", "answer": "A1", "turn_number": 1}
            ]

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
    async def test_ask_raises_chat_error_on_rate_limit(self, auth_tokens, httpx_mock):
        """ask() raises ChatError when the server returns UserDisplayableError."""
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
