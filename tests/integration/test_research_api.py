"""Integration tests for ResearchAPI."""

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient


class TestResearchAPI:
    """Integration tests for the ResearchAPI."""

    @pytest.mark.asyncio
    async def test_start_fast_web_research(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test starting fast web research."""
        response = build_rpc_response("Ljjv0c", ["task_123", "report_456"])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.start(
                "nb_123", "quantum computing", source="web", mode="fast"
            )

        assert result is not None
        assert result["task_id"] == "task_123"
        assert result["report_id"] == "report_456"
        assert result["mode"] == "fast"

        request = httpx_mock.get_request()
        assert "Ljjv0c" in str(request.url)

    @pytest.mark.asyncio
    async def test_start_fast_drive_research(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test starting fast drive research."""
        response = build_rpc_response("Ljjv0c", ["task_789", None])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.start(
                "nb_123", "project docs", source="drive", mode="fast"
            )

        assert result is not None
        assert result["task_id"] == "task_789"
        assert result["mode"] == "fast"

    @pytest.mark.asyncio
    async def test_start_deep_web_research(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test starting deep web research."""
        response = build_rpc_response("QA9ei", ["task_deep", "report_deep"])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.start("nb_123", "AI ethics", source="web", mode="deep")

        assert result is not None
        assert result["mode"] == "deep"

        request = httpx_mock.get_request()
        assert "QA9ei" in str(request.url)

    @pytest.mark.asyncio
    async def test_start_deep_drive_research_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that deep research on drive raises ValueError."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match="Deep Research only supports Web"):
                await client.research.start("nb_123", "query", source="drive", mode="deep")

    @pytest.mark.asyncio
    async def test_start_invalid_source_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that invalid source raises ValueError."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match="Invalid source"):
                await client.research.start("nb_123", "query", source="invalid")

    @pytest.mark.asyncio
    async def test_start_invalid_mode_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that invalid mode raises ValueError."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match="Invalid mode"):
                await client.research.start("nb_123", "query", mode="invalid")

    @pytest.mark.asyncio
    async def test_poll_completed(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test polling completed research."""
        response = build_rpc_response(
            "e3bVqc",
            [
                [
                    "task_123",
                    [
                        None,
                        ["quantum computing"],
                        None,
                        [
                            [
                                ["https://example.com", "Quantum Guide", "Description"],
                                ["https://another.com", "More Info", "Desc 2"],
                            ],
                            "Summary of quantum computing research...",
                        ],
                        2,
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "completed"
        assert result["task_id"] == "task_123"
        assert len(result["sources"]) == 2
        assert result["sources"][0]["url"] == "https://example.com"
        assert result["sources"][0]["title"] == "Quantum Guide"
        assert "Summary" in result["summary"]

    @pytest.mark.asyncio
    async def test_poll_in_progress(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test polling research that's still in progress."""
        response = build_rpc_response(
            "e3bVqc",
            [
                [
                    "task_456",
                    [
                        None,
                        ["machine learning"],
                        None,
                        [],
                        1,
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "in_progress"
        assert result["task_id"] == "task_456"

    @pytest.mark.asyncio
    async def test_poll_no_research(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test polling when no research exists."""
        response = build_rpc_response("e3bVqc", [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "no_research"

    @pytest.mark.asyncio
    async def test_import_sources(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test importing research sources."""
        response = build_rpc_response(
            "LBwxtb",
            [
                [
                    [["src_001"], "Quantum Computing Guide"],
                    [["src_002"], "AI Research Paper"],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources_to_import = [
                {"url": "https://example.com/quantum", "title": "Quantum Computing Guide"},
                {"url": "https://example.com/ai", "title": "AI Research Paper"},
            ]
            result = await client.research.import_sources("nb_123", "task_123", sources_to_import)

        assert len(result) == 2
        assert result[0]["id"] == "src_001"
        assert result[0]["title"] == "Quantum Computing Guide"

        request = httpx_mock.get_request()
        assert "LBwxtb" in str(request.url)

    @pytest.mark.asyncio
    async def test_import_sources_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test importing empty sources list."""
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources("nb_123", "task_123", [])

        assert result == []
