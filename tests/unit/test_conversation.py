"""Tests for conversation functionality."""

import json

import pytest

from notebooklm import AskResult, NotebookLMClient
from notebooklm.auth import AuthTokens


@pytest.fixture
def auth_tokens():
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


class TestAsk:
    @pytest.mark.asyncio
    async def test_ask_new_conversation(self, auth_tokens, httpx_mock):
        import re

        # Mock ask response (streaming chunks)
        inner_json = json.dumps(
            [
                [
                    "This is the answer. It is now long enough to be valid.",
                    None,
                    None,
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
        inner_json = json.dumps(
            [
                [
                    "Follow-up answer. This also needs to be longer than twenty characters.",
                    None,
                    None,
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
            client._core._conversation_cache["conv_123"] = [
                {"query": "Q1", "answer": "A1", "turn_number": 1}
            ]

            result = await client.chat.ask(
                notebook_id="nb_123",
                question="Follow up?",
                conversation_id="conv_123",
                source_ids=["test_source"],
            )

        assert isinstance(result, AskResult)
        assert (
            result.answer
            == "Follow-up answer. This also needs to be longer than twenty characters."
        )
        assert result.is_follow_up is True
        assert result.turn_number == 2
