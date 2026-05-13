"""Tests for research functionality."""

import json
import logging
from urllib.parse import parse_qs

import pytest

from notebooklm import NotebookLMClient
from notebooklm._research import ResearchAPI
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod


def _extract_request_params(request) -> list:
    """Decode the nested batchexecute request params from a mocked request."""
    body = parse_qs(request.content.decode())
    f_req = json.loads(body["f.req"][0])
    return json.loads(f_req[0][0][1])


@pytest.fixture
def auth_tokens():
    """Create test authentication tokens."""
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


class TestParseResultType:
    """Tests for ResearchAPI._parse_result_type static method."""

    def test_int_passthrough(self):
        assert ResearchAPI._parse_result_type(5) == 5

    def test_known_string_alias(self):
        assert ResearchAPI._parse_result_type("web") == 1
        assert ResearchAPI._parse_result_type("drive") == 2
        assert ResearchAPI._parse_result_type("report") == 5

    def test_case_insensitive(self):
        assert ResearchAPI._parse_result_type("WEB") == 1
        assert ResearchAPI._parse_result_type("Drive") == 2

    def test_unknown_string_preserved(self):
        assert ResearchAPI._parse_result_type("video") == "video"

    def test_none_defaults_to_1(self):
        assert ResearchAPI._parse_result_type(None) == 1

    def test_float_defaults_to_1(self):
        assert ResearchAPI._parse_result_type(3.14) == 1

    def test_list_defaults_to_1(self):
        assert ResearchAPI._parse_result_type([]) == 1


class TestBuildImportEntries:
    """Tests for import entry builder static methods."""

    def test_build_report_import_entry(self):
        entry = ResearchAPI._build_report_import_entry("Title", "# Markdown")
        assert entry[1] == ["Title", "# Markdown"]
        assert entry[3] == 3
        assert entry[10] == 3
        assert entry[0] is None

    def test_build_web_import_entry(self):
        entry = ResearchAPI._build_web_import_entry("https://example.com", "Example")
        assert entry[2] == ["https://example.com", "Example"]
        assert entry[10] == 2
        assert entry[0] is None
        assert entry[1] is None


class TestCitedSourceSelection:
    def test_extract_report_urls_normalizes_markdown_and_bare_urls(self):
        urls = ResearchAPI.extract_report_urls(
            "See [Example](https://Example.com/a/) and https://example.com/b."
        )

        assert urls == {"https://example.com/a", "https://example.com/b"}

    def test_extract_report_urls_keeps_balanced_parentheses(self):
        urls = ResearchAPI.extract_report_urls(
            "See [Function](https://en.wikipedia.org/wiki/Function_(mathematics)) "
            "and https://example.com/Topic_(research)."
        )

        assert urls == {
            "https://en.wikipedia.org/wiki/Function_(mathematics)",
            "https://example.com/Topic_(research)",
        }

    def test_extract_report_urls_ignores_markdown_images(self):
        urls = ResearchAPI.extract_report_urls(
            "![chart](https://example.com/chart_(v2).png) and "
            '![titled](https://example.com/titled.png "Chart title") '
            "![](https://example.com/empty.png) "
            "cite [Article](https://example.com/a)"
        )

        assert urls == {"https://example.com/a"}

    def test_select_cited_sources_filters_urls_and_preserves_report_entry(self):
        sources = [
            {
                "title": "Deep Research Report",
                "result_type": 5,
                "report_markdown": "# Report",
            },
            {"title": "Cited", "url": "https://example.com/cited/"},
            {"title": "Uncited", "url": "https://example.com/uncited"},
            {"title": "No URL"},
        ]

        selection = ResearchAPI.select_cited_sources(
            sources,
            "Final report cites [the source](https://example.com/cited).",
        )

        assert selection.used_fallback is False
        assert selection.cited_url_count == 1
        assert selection.matched_url_source_count == 1
        assert [source["title"] for source in selection.sources] == [
            "Deep Research Report",
            "Cited",
        ]

    def test_select_cited_sources_deduplicates_report_entries_with_urls(self):
        report_source = {
            "title": "Deep Research Report",
            "result_type": 5,
            "report_markdown": "# Report",
            "url": "https://example.com/report",
        }

        selection = ResearchAPI.select_cited_sources(
            [report_source],
            "Final report cites https://example.com/report",
        )

        assert selection.used_fallback is True
        assert selection.sources == [report_source]

    def test_select_cited_sources_falls_back_when_no_urls_found(self, caplog):
        sources = [{"title": "Source", "url": "https://example.com/source"}]

        with caplog.at_level(logging.WARNING, logger="notebooklm._research"):
            selection = ResearchAPI.select_cited_sources(sources, "# Report without links")

        assert selection.used_fallback is True
        assert selection.sources == sources
        assert "falling back" in caplog.text

    def test_select_cited_sources_falls_back_when_no_sources_match(self, caplog):
        sources = [{"title": "Source", "url": "https://example.com/source"}]

        with caplog.at_level(logging.WARNING, logger="notebooklm._research"):
            selection = ResearchAPI.select_cited_sources(
                sources,
                "Report cites https://example.com/other",
            )

        assert selection.used_fallback is True
        assert selection.cited_url_count == 1
        assert selection.matched_url_source_count == 0
        assert selection.sources == sources
        assert "none of the report URLs matched" in caplog.text


class TestExtractLegacyReportChunks:
    """Tests for _extract_legacy_report_chunks static method."""

    def test_missing_index_6(self):
        assert ResearchAPI._extract_legacy_report_chunks([None, "t", None, 5, None, None]) == ""

    def test_index_6_not_list(self):
        assert (
            ResearchAPI._extract_legacy_report_chunks([None, "t", None, 5, None, None, "str"]) == ""
        )

    def test_single_chunk(self):
        assert (
            ResearchAPI._extract_legacy_report_chunks([None, "t", None, 5, None, None, ["chunk"]])
            == "chunk"
        )

    def test_multiple_chunks_joined(self):
        src = [None, "t", None, 5, None, None, ["a", "b", "c"]]
        assert ResearchAPI._extract_legacy_report_chunks(src) == "a\n\nb\n\nc"

    def test_filters_non_string_and_empty(self):
        src = [None, "t", None, 5, None, None, ["real", None, "", 42, "also_real"]]
        assert ResearchAPI._extract_legacy_report_chunks(src) == "real\n\nalso_real"

    def test_all_empty_returns_empty(self):
        assert (
            ResearchAPI._extract_legacy_report_chunks([None, "t", None, 5, None, None, ["", None]])
            == ""
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
        assert result["sources"][0]["result_type"] == 1
        assert result["summary"] == "Summary text"
        assert result["report"] == ""
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["task_id"] == "task_123"

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
        """Test that invalid source raises ValidationError."""
        from notebooklm.exceptions import ValidationError

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Invalid source"):
                await client.research.start(notebook_id="nb_123", query="test", source="invalid")

    @pytest.mark.asyncio
    async def test_start_research_invalid_mode(self, auth_tokens):
        """Test that invalid mode raises ValidationError."""
        from notebooklm.exceptions import ValidationError

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Invalid mode"):
                await client.research.start(notebook_id="nb_123", query="test", mode="invalid")

    @pytest.mark.asyncio
    async def test_start_deep_drive_invalid(self, auth_tokens):
        """Test that deep research with drive source raises ValidationError."""
        from notebooklm.exceptions import ValidationError

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Deep Research only supports Web"):
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
        sources = [[None, "Deep Research Finding", None, 5, None, None, ["# Report markdown"]]]
        task_info = [None, ["deep query", 1], 1, [sources, "Deep summary"], 2]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "completed"
        assert len(result["sources"]) == 1
        assert result["sources"][0]["title"] == "Deep Research Finding"
        assert result["sources"][0]["url"] == ""
        assert result["sources"][0]["result_type"] == 5
        assert result["sources"][0]["research_task_id"] == "task_123"
        assert result["sources"][0]["report_markdown"] == "# Report markdown"
        assert result["report"] == "# Report markdown"

    @pytest.mark.asyncio
    async def test_poll_returns_all_tasks(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test poll preserves all parsed research tasks in an additive tasks field."""
        latest_sources = [["http://example.com/latest", "Latest", "Description", 1]]
        older_sources = [["http://example.com/older", "Older", "Description", 1]]
        latest_task = [None, ["latest query", 1], 1, [latest_sources, "Latest summary"], 2]
        older_task = [None, ["older query", 1], 1, [older_sources, "Older summary"], 2]
        response_body = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [[["task_latest", latest_task], ["task_older", older_task]]],
        )
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["task_id"] == "task_latest"
        assert result["query"] == "latest query"
        assert len(result["tasks"]) == 2
        assert result["tasks"][0]["task_id"] == "task_latest"
        assert result["tasks"][1]["task_id"] == "task_older"
        assert result["tasks"][1]["query"] == "older query"

    @pytest.mark.asyncio
    async def test_poll_joins_legacy_report_chunks(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Test poll joins multiple legacy report chunks instead of truncating to the first one."""
        sources = [[None, "Deep Research Finding", None, 5, None, None, ["chunk one", "chunk two"]]]
        task_info = [None, ["deep query", 1], 1, [sources, "Deep summary"], 2]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["report"] == "chunk one\n\nchunk two"
        assert result["tasks"][0]["report"] == "chunk one\n\nchunk two"

    @pytest.mark.asyncio
    async def test_poll_deep_research_current_report_shape(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Test poll parses the current report payload shape from deep research."""
        sources = [
            [
                None,
                ["Deep Research Report", "# Current report markdown"],
                None,
                5,
                None,
                None,
                None,
            ]
        ]
        task_info = [None, ["deep query", 1], 1, [sources, "Deep summary"], 6]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["report_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "completed"
        assert result["task_id"] == "report_123"
        assert result["sources"][0]["title"] == "Deep Research Report"
        assert result["sources"][0]["report_markdown"] == "# Current report markdown"
        assert result["sources"][0]["research_task_id"] == "report_123"
        assert result["report"] == "# Current report markdown"

    @pytest.mark.asyncio
    async def test_poll_fast_research_string_drive_result_type(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Test poll preserves legacy string-encoded source types semantically."""
        sources = [["https://drive.example.com/doc", "Drive Doc", "Description", "drive"]]
        task_info = [None, ["drive query", 1], 1, [sources, "Drive summary"], 2]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "completed"
        assert result["sources"][0]["url"] == "https://drive.example.com/doc"
        assert result["sources"][0]["title"] == "Drive Doc"
        assert result["sources"][0]["result_type"] == 2

    @pytest.mark.asyncio
    async def test_poll_status_code_6_completed(self, auth_tokens, httpx_mock, build_rpc_response):
        """Test that status code 6 (deep research) is treated as completed."""
        task_info = [None, ["query", 1], 1, [[], ""], 6]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_import_sources_skips_result_type_5(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Test that import_sources keeps importable report entries and skips the rest."""
        response_body = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH, [[[["src_001"], "Web Source"]]]
        )
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            sources = [
                {"url": "http://example.com", "title": "Web Source", "result_type": 1},
                {"title": "Report Without Body", "result_type": 5},
            ]
            result = await client.research.import_sources(
                notebook_id="nb_123", task_id="task_123", sources=sources
            )

        assert len(result) == 1
        assert result[0]["id"] == "src_001"

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
    async def test_import_sources_includes_deep_research_report_entry(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Test that deep research imports prepend the report entry and use the polled task id."""
        response_body = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            [[[["report_src_001"], "Deep Research Report"], [["src_001"], "Web Source"]]],
        )
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            sources = [
                {
                    "title": "Deep Research Report",
                    "result_type": 5,
                    "report_markdown": "# Deep report body",
                    "research_task_id": "report_123",
                },
                {
                    "url": "http://example.com",
                    "title": "Web Source",
                    "result_type": 1,
                    "research_task_id": "report_123",
                },
            ]
            result = await client.research.import_sources(
                notebook_id="nb_123",
                task_id="task_123",
                sources=sources,
            )

        assert len(result) == 2
        request = httpx_mock.get_request()
        params = _extract_request_params(request)
        assert params[2] == "report_123"
        assert params[3] == "nb_123"
        assert params[4][0] == [
            None,
            ["Deep Research Report", "# Deep report body"],
            None,
            3,
            None,
            None,
            None,
            None,
            None,
            None,
            3,
        ]
        assert params[4][1][2] == ["http://example.com", "Web Source"]

    @pytest.mark.asyncio
    async def test_import_sources_rejects_mixed_research_task_ids(self, auth_tokens):
        """Test that import_sources rejects batches spanning multiple research tasks."""
        from notebooklm.exceptions import ValidationError

        async with NotebookLMClient(auth_tokens) as client:
            sources = [
                {
                    "title": "Deep Research Report",
                    "result_type": 5,
                    "report_markdown": "# Deep report body",
                    "research_task_id": "report_123",
                },
                {
                    "url": "http://example.com",
                    "title": "Web Source",
                    "result_type": 1,
                    "research_task_id": "report_456",
                },
            ]
            with pytest.raises(ValidationError, match="multiple research tasks"):
                await client.research.import_sources(
                    notebook_id="nb_123",
                    task_id="task_123",
                    sources=sources,
                )

    @pytest.mark.asyncio
    async def test_import_sources_includes_multiple_report_entries(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Test that import_sources preserves all valid report entries in one batch."""
        response_body = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            [
                [
                    [["report_src_001"], "Deep Research Report 1"],
                    [["report_src_002"], "Deep Research Report 2"],
                    [["src_001"], "Web Source"],
                ]
            ],
        )
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            sources = [
                {
                    "title": "Deep Research Report 1",
                    "result_type": 5,
                    "report_markdown": "# Deep report body 1",
                    "research_task_id": "report_123",
                },
                {
                    "title": "Deep Research Report 2",
                    "result_type": 5,
                    "report_markdown": "# Deep report body 2",
                    "research_task_id": "report_123",
                },
                {
                    "url": "http://example.com",
                    "title": "Web Source",
                    "result_type": 1,
                    "research_task_id": "report_123",
                },
            ]
            result = await client.research.import_sources(
                notebook_id="nb_123",
                task_id="task_123",
                sources=sources,
            )

        assert len(result) == 3
        request = httpx_mock.get_request()
        params = _extract_request_params(request)
        assert params[2] == "report_123"
        assert params[4][0][1] == ["Deep Research Report 1", "# Deep report body 1"]
        assert params[4][1][1] == ["Deep Research Report 2", "# Deep report body 2"]
        assert params[4][2][2] == ["http://example.com", "Web Source"]

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
                assert "result_type" in src

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
        # Deep research format includes a special report entry and web sources.
        poll_sources = [
            [None, ["Deep Research Report", "# Deep report body"], None, 5, None, None, None],
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
                RPCMethod.POLL_RESEARCH, [[["report_789", task_info]]]
            ).encode(),
            method="POST",
        )
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.IMPORT_RESEARCH,
                [
                    [
                        [["report_src_001"], "Deep Research Report"],
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
            assert poll_result["task_id"] == "report_789"
            sources = poll_result["sources"]
            assert len(sources) == 4

            # Sources with URLs can be imported; sources without URLs are filtered
            sources_with_urls = [s for s in sources if s.get("url")]
            assert len(sources_with_urls) == 2

            imported = await client.research.import_sources(
                notebook_id="nb_123",
                task_id=task_id,
                sources=sources,  # Pass all, filtering happens internally
            )

            assert len(imported) == 3
            assert imported[0]["id"] == "report_src_001"
            assert imported[1]["id"] == "deep_src_001"
            assert imported[2]["id"] == "deep_src_002"

    @pytest.mark.asyncio
    async def test_poll_no_research_returns_tasks_key(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Both no_research return paths include a 'tasks' key for API consistency."""
        # Early return path (empty response)
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "no_research"
        assert result["tasks"] == []

    @pytest.mark.asyncio
    async def test_poll_no_research_all_invalid_returns_tasks_key(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Late no_research return (all tasks invalid) also includes 'tasks' key."""
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[42, "not_a_list"]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["status"] == "no_research"
        assert result["tasks"] == []

    @pytest.mark.asyncio
    async def test_poll_unknown_string_result_type_preserved(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Unknown string result_type tags are preserved as-is in source dicts."""
        sources = [["http://example.com", "Video Source", "desc", "video"]]
        task_info = [None, ["query", 1], 1, [sources, "Summary"], 2]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["sources"][0]["result_type"] == "video"

    @pytest.mark.asyncio
    async def test_poll_legacy_report_mixed_chunks(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Legacy report chunks filter out non-string and empty values."""
        sources = [[None, "Report Title", None, 5, None, None, ["chunk1", None, "", "chunk2"]]]
        task_info = [None, ["query", 1], 1, [sources, ""], 2]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["report"] == "chunk1\n\nchunk2"

    @pytest.mark.asyncio
    async def test_poll_source_single_element_list_title_dropped(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        """Deep source with src[1] as single-element list is correctly dropped."""
        sources = [[None, ["title_only"], None, 5]]
        task_info = [None, ["query", 1], 1, [sources, ""], 2]
        response_body = build_rpc_response(RPCMethod.POLL_RESEARCH, [[["task_123", task_info]]])
        httpx_mock.add_response(content=response_body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")

        assert result["sources"] == []
