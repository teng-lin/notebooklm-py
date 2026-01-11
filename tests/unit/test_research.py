"""Tests for research functionality."""

import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod


@pytest.fixture
def auth_tokens():
    """Create test authentication tokens."""
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


class TestResearch:
    @pytest.mark.asyncio
    async def test_start_fast_research(self, auth_tokens, httpx_mock, build_rpc_response):
        response_body = build_rpc_response(RPCMethod.START_FAST_RESEARCH, ["task_123", None])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.start(
                notebook_id="nb_123", query="Quantum computing", mode="fast"
            )

        assert result["task_id"] == "task_123"
        assert result["mode"] == "fast"

    @pytest.mark.asyncio
    async def test_poll_research_completed(self, auth_tokens, httpx_mock, build_rpc_response):
        sources = [["http://example.com", "Example Title", "Description", 1]]
        task_info = [
            None,
            ["query", 1],
            1,
            [sources, "Summary text"],
            2,  # status: completed
        ]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "completed"
        assert len(result["sources"]) == 1
        assert result["sources"][0]["url"] == "http://example.com"
        assert result["summary"] == "Summary text"

    @pytest.mark.asyncio
    async def test_import_research(self, auth_tokens, httpx_mock, build_rpc_response):
        response_body = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH, [[[["src_new"], "Imported Title"]]]
        )
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            sources = [{"url": "http://example.com", "title": "Example"}]
            result = await client.research.import_sources(
                notebook_id="nb_123", task_id="task_123", sources=sources
            )

        assert len(result) == 1
        assert result[0]["id"] == "src_new"

    @pytest.mark.asyncio
    async def test_start_deep_research(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test starting deep web research."""
        response_body = build_rpc_response(
            RPCMethod.START_DEEP_RESEARCH, ["task_456", "report_123"]
        )
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.start(
                notebook_id="nb_123", query="AI research", mode="deep"
            )

        assert result["task_id"] == "task_456"
        assert result["report_id"] == "report_123"
        assert result["mode"] == "deep"

    @pytest.mark.asyncio
    async def test_start_research_invalid_source(self, auth_tokens):
        """Test that invalid source raises ValueError."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match="Invalid source"):
                await client.research.start(notebook_id="nb_123", query="test", source="invalid")

    @pytest.mark.asyncio
    async def test_start_research_invalid_mode(self, auth_tokens):
        """Test that invalid mode raises ValueError."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match="Invalid mode"):
                await client.research.start(notebook_id="nb_123", query="test", mode="invalid")

    @pytest.mark.asyncio
    async def test_start_deep_drive_invalid(self, auth_tokens):
        """Test that deep research with drive source raises ValueError."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match="Deep Research only supports Web"):
                await client.research.start(
                    notebook_id="nb_123", query="test", source="drive", mode="deep"
                )

    @pytest.mark.asyncio
    async def test_start_research_returns_none(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test start returns None on empty response."""
        response_body = build_rpc_response(RPCMethod.START_FAST_RESEARCH, [])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.start(notebook_id="nb_123", query="test", mode="fast")

        assert result is None

    @pytest.mark.asyncio
    async def test_poll_no_research(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test poll returns no_research on empty response."""
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "no_research"

    @pytest.mark.asyncio
    async def test_poll_in_progress(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test poll returns in_progress status."""
        task_info = [
            None,
            ["research query", 1],
            1,
            [[], ""],
            1,  # status: in_progress
        ]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "in_progress"
        assert result["query"] == "research query"

    @pytest.mark.asyncio
    async def test_poll_deep_research_sources(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test poll parses deep research sources (title only, no URL)."""
        sources = [[None, "Deep Research Finding", None, 2]]
        task_info = [None, ["deep query", 1], 1, [sources, "Deep summary"], 2]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "completed"
        assert len(result["sources"]) == 1
        assert result["sources"][0]["title"] == "Deep Research Finding"
        assert result["sources"][0]["url"] == ""

    @pytest.mark.asyncio
    async def test_import_empty_sources(self, auth_tokens):
        """Test import_sources with empty list returns empty list."""
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                notebook_id="nb_123", task_id="task_123", sources=[]
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_import_sources_missing_url(self, auth_tokens):
        """Test import_sources filters out sources without URL.

        Sources without URLs cause the entire batch to fail, so they are
        filtered out before making the RPC call.
        """
        async with NotebookLMClient(auth_tokens) as client:
            sources = [{"title": "Title Only"}]  # No URL
            result = await client.research.import_sources(
                notebook_id="nb_123", task_id="task_123", sources=sources
            )

        # Sources without URLs are filtered out, no RPC call made
        assert result == []

    @pytest.mark.asyncio
    async def test_import_sources_empty_response(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test import_sources handles empty API response."""
        response_body = build_rpc_response(RPCMethod.IMPORT_RESEARCH, [])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            sources = [{"url": "http://example.com", "title": "Example"}]
            result = await client.research.import_sources(
                notebook_id="nb_123", task_id="task_123", sources=sources
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_import_sources_malformed_response(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Test import_sources handles malformed response gracefully."""
        response_body = build_rpc_response(RPCMethod.IMPORT_RESEARCH, [[["not_a_list", "Title"]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            sources = [{"url": "http://example.com", "title": "Example"}]
            result = await client.research.import_sources(
                notebook_id="nb_123", task_id="task_123", sources=sources
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_full_workflow_poll_to_import(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test complete workflow: start -> poll -> import.

        Validates that poll() output format is compatible with import_sources() input.
        """
        # Build mock responses
        poll_sources = [
            ["http://example.com/article1", "First Article", "Description 1", 1],
            ["http://example.com/article2", "Second Article", "Description 2", 1],
            ["http://example.com/article3", "Third Article", "Description 3", 1],
        ]
        task_info = [None, ["AI research query", 1], 1, [poll_sources, "Summary"], 2]

        httpx_mock.add_response(
            content=build_rpc_response(RPCMethod.START_FAST_RESEARCH, ["task_123", None]).encode(),
            method="POST",
        )
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]]
            ).encode(),
            method="POST",
        )
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.IMPORT_RESEARCH,
                [[[["src_001"], "First Article"], [["src_002"], "Second Article"]]],
            ).encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            start_result = await client.research.start(
                notebook_id="nb_123", query="AI research query", mode="fast"
            )
            assert start_result is not None
            task_id = start_result["task_id"]

            poll_result = await client.research.poll("nb_123")
            assert poll_result["status"] == "completed"
            sources = poll_result["sources"]
            assert len(sources) == 3

            for src in sources:
                assert "url" in src
                assert "title" in src

            imported = await client.research.import_sources(
                notebook_id="nb_123", task_id=task_id, sources=sources[:2]
            )

            assert len(imported) == 2
            assert imported[0]["id"] == "src_001"
            assert imported[1]["id"] == "src_002"

    @pytest.mark.asyncio
    async def test_deep_research_workflow_poll_to_import(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Test deep research workflow: poll() sources work with import_sources().

        Deep research sources typically have URLs. Sources without URLs are
        filtered out before import (they cause batch failures).
        """
        # Deep research format: [url, title, description, type]
        poll_sources = [
            ["https://example.com/ai-ethics", "Deep Finding: AI Ethics", "Description", 2],
            ["https://example.com/ml-trends", "Deep Finding: ML Trends", "Description", 2],
            [None, "Synthetic Summary", "No URL", 2],  # This will be filtered out
        ]
        task_info = [None, ["deep AI research", 1], 1, [poll_sources, "Summary"], 2]

        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.START_DEEP_RESEARCH, ["task_deep_456", "report_789"]
            ).encode(),
            method="POST",
        )
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.POLL_RESEARCH, [[["task_deep_456", task_info]]]
            ).encode(),
            method="POST",
        )
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.IMPORT_RESEARCH,
                [
                    [
                        [["deep_src_001"], "Deep Finding: AI Ethics"],
                        [["deep_src_002"], "Deep Finding: ML Trends"],
                    ]
                ],
            ).encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            start_result = await client.research.start(
                notebook_id="nb_123", query="deep AI research", mode="deep"
            )
            assert start_result is not None
            assert start_result["mode"] == "deep"
            task_id = start_result["task_id"]

            poll_result = await client.research.poll("nb_123")
            assert poll_result["status"] == "completed"
            sources = poll_result["sources"]
            assert len(sources) == 3

            # Sources with URLs can be imported; sources without URLs are filtered
            sources_with_urls = [s for s in sources if s.get("url")]
            assert len(sources_with_urls) == 2

            imported = await client.research.import_sources(
                notebook_id="nb_123",
                task_id=task_id,
                sources=sources,  # Pass all, filtering happens internally
            )

            assert len(imported) == 2
            assert imported[0]["id"] == "deep_src_001"
            assert imported[1]["id"] == "deep_src_002"
