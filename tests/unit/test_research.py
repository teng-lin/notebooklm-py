"""Tests for research functionality."""

import json
import re

import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod


@pytest.fixture
def auth_tokens():
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


class TestResearch:
    @pytest.mark.asyncio
    async def test_start_fast_research(self, auth_tokens, httpx_mock):
        response_json = json.dumps(["task_123", None])
        chunk = json.dumps(
            ["wrb.fr", RPCMethod.START_FAST_RESEARCH.value, response_json, None, None]
        )
        response_body = f")]}}'\n{len(chunk)}\n{chunk}\n"

        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*"),
            content=response_body.encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.start(
                notebook_id="nb_123", query="Quantum computing", mode="fast"
            )

        assert result["task_id"] == "task_123"
        assert result["mode"] == "fast"

    @pytest.mark.asyncio
    async def test_poll_research_completed(self, auth_tokens, httpx_mock):
        # Mock poll response with completed status (2)
        sources = [
            ["http://example.com", "Example Title", "Description", 1],
        ]
        task_info = [
            None,
            ["query", 1],  # query info
            1,  # mode
            [sources, "Summary text"],  # sources and summary
            2,  # status: completed
        ]

        response_json = json.dumps([[["task_123", task_info]]])
        chunk = json.dumps(["wrb.fr", RPCMethod.POLL_RESEARCH.value, response_json, None, None])
        response_body = f")]}}'\n{len(chunk)}\n{chunk}\n"

        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "completed"
        assert len(result["sources"]) == 1
        assert result["sources"][0]["url"] == "http://example.com"
        assert result["summary"] == "Summary text"

    @pytest.mark.asyncio
    async def test_import_research(self, auth_tokens, httpx_mock):
        response_json = json.dumps([[[["src_new"], "Imported Title"]]])
        chunk = json.dumps(["wrb.fr", RPCMethod.IMPORT_RESEARCH.value, response_json, None, None])
        response_body = f")]}}'\n{len(chunk)}\n{chunk}\n"

        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            sources = [{"url": "http://example.com", "title": "Example"}]
            result = await client.research.import_sources(
                notebook_id="nb_123", task_id="task_123", sources=sources
            )

        assert len(result) == 1
        assert result[0]["id"] == "src_new"
