"""Tests for conversation functionality."""

import json
import re

import pytest

from notebooklm import AskResult, NotebookLMClient
from notebooklm.exceptions import ChatError


class TestAsk:
    @pytest.mark.asyncio
    async def test_ask_new_conversation(self, auth_tokens, httpx_mock):
        import re

        # Mock ask response (streaming chunks). ``first[2][0]`` is the
        # server-assigned conversation_id (issue #659).
        inner_json = json.dumps(
            [
                [
                    "This is the answer. It is now long enough to be valid.",
                    None,
                    ["server-new-conv", 12345],
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
    async def test_ask_returns_server_conversation_id(self, auth_tokens, httpx_mock):
        """ask() uses the conversation_id from the server response, not a local UUID."""
        server_conv_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        inner_json = json.dumps(
            [
                [
                    "Server answer text that is long enough to be valid.",
                    None,
                    [server_conv_id, "hash123"],
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

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask("nb_123", "What is this?", source_ids=["test_source"])

        assert result.conversation_id == server_conv_id
