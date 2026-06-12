"""Integration tests for ResearchAPI."""

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod


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
        assert result.task_id == "task_123"
        assert result.report_id == "report_456"
        assert result.mode == "fast"
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
        assert result.task_id == "task_789"
        assert result.mode == "fast"

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
        assert result.mode == "deep"
        request = httpx_mock.get_request()
        assert "QA9ei" in str(request.url)

    @pytest.mark.asyncio
    async def test_start_deep_drive_research_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that deep research on drive raises ValidationError."""
        from notebooklm.exceptions import ValidationError

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Deep Research only supports Web"):
                await client.research.start("nb_123", "query", source="drive", mode="deep")

    @pytest.mark.asyncio
    async def test_start_invalid_source_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that invalid source raises ValidationError."""
        from notebooklm.exceptions import ValidationError

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Invalid source"):
                await client.research.start("nb_123", "query", source="invalid")

    @pytest.mark.asyncio
    async def test_start_invalid_mode_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that invalid mode raises ValidationError."""
        from notebooklm.exceptions import ValidationError

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Invalid mode"):
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
        assert result.status == "completed"
        assert result.task_id == "task_123"
        assert len(result.sources) == 2
        assert result.sources[0].url == "https://example.com"
        assert result.sources[0].title == "Quantum Guide"
        assert result.sources[0].result_type == 1
        assert "Summary" in result.summary
        # The dict-subscript/membership bridge was dropped in v0.8.0 (#1251):
        # ``report`` is now a plain attribute on the attribute-only dataclass.
        assert hasattr(result, "report")

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
        assert result.status == "in_progress"
        assert result.task_id == "task_456"

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
        assert result.status == "no_research"

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


class TestPollEdgeCases:
    """Tests for poll() parsing branch edge cases."""

    @pytest.mark.asyncio
    async def test_poll_unwrap_nested_result(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """result[0] is a list whose first element is also a list — unwrap one level."""
        # Outer list wraps the inner task list: result[0][0] is a list → unwrap
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [
                [
                    [
                        "task_wrap",
                        [None, ["wrapped query"], None, [], 1],
                    ]
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        assert result.task_id == "task_wrap"
        assert result.query == "wrapped query"

    @pytest.mark.asyncio
    async def test_poll_skips_non_list_task_data(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """A too-short task entry is drift and raises under strict decoding.
        Non-list outer items are skipped before ``safe_index`` is reached, but
        a too-short list entry (``["only_one_elem"]``) drifts on the
        ``task_info`` descent. Strict decoding is the only mode (the soft-mode
        opt-out was retired in v0.7.0), so the drift raises rather than being
        silently skipped.
        """
        # Outer list contains a non-list item then a too-short list
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            ["not_a_list", ["only_one_elem"]],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(UnknownRPCMethodError):
                await client.research.poll("nb_123")

    @pytest.mark.asyncio
    async def test_poll_skips_non_string_task_id(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """task_id is not str — continue, eventually return no_research."""
        # task_id is an integer (not str) and task_info is a list
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [[42, [None, ["query"], None, [], 1]]],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        assert result.status == "no_research"

    @pytest.mark.asyncio
    async def test_poll_skips_non_list_task_info(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """task_info is not a list — continue, eventually return no_research."""
        # task_id is str but task_info is a string, not list
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [["task_bad", "not_a_list"]],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        assert result.status == "no_research"

    @pytest.mark.asyncio
    async def test_poll_sources_and_summary_has_only_sources_no_summary(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """sources_and_summary has len 1 (sources only, no summary string)."""
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [
                [
                    "task_nosummary",
                    [
                        None,
                        ["no summary query"],
                        None,
                        [
                            [
                                ["https://example.com", "Title", "desc"],
                            ]
                            # No second element — summary is absent
                        ],
                        2,
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        assert result.status == "completed"
        assert result.summary == ""
        assert len(result.sources) == 1

    @pytest.mark.asyncio
    async def test_poll_skips_short_source_entry(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """a source entry in sources_data is too short (len < 2) — skipped."""
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [
                [
                    "task_shortsrc",
                    [
                        None,
                        ["short src query"],
                        None,
                        [
                            [
                                ["only_one_element"],  # len < 2 → skipped
                                ["https://valid.com", "Valid"],  # kept
                            ],
                            "Summary text",
                        ],
                        2,
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        # Only the valid source is returned
        assert len(result.sources) == 1
        assert result.sources[0].url == "https://valid.com"

    @pytest.mark.asyncio
    async def test_poll_deep_research_source_none_first_element(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """deep research source where src[0] is None — title extracted, url=''.
        Also tests report extraction from src[6]."""
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [
                [
                    "task_deep",
                    [
                        None,
                        ["deep query"],
                        None,
                        [
                            [
                                [
                                    None,
                                    "Deep Research Title",
                                    None,
                                    5,
                                    None,
                                    None,
                                    ["# Deep Report\nContent here"],
                                ],
                            ],
                            "Deep summary",
                        ],
                        2,
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        assert result.status == "completed"
        assert len(result.sources) == 1
        assert result.sources[0].title == "Deep Research Title"
        assert result.sources[0].url == ""
        assert result.sources[0].result_type == 5
        assert result.report == "# Deep Report\nContent here"

    @pytest.mark.asyncio
    async def test_poll_status_code_6_is_completed(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Status code 6 (deep research) should be treated as completed."""
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [
                [
                    "task_deep6",
                    [
                        None,
                        ["deep query"],
                        None,
                        [
                            [
                                ["https://example.com", "Web Source", "desc", 1],
                            ],
                            "Summary",
                        ],
                        6,  # status code 6 = completed (deep research)
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        assert result.status == "completed"
        assert result.task_id == "task_deep6"

    @pytest.mark.asyncio
    async def test_poll_fast_research_source_with_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """fast research source where src[0] is a str URL."""
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [
                [
                    "task_fast",
                    [
                        None,
                        ["fast query"],
                        None,
                        [
                            [
                                ["https://fast.example.com", "Fast Title", "desc", "web"],
                            ],
                            "Fast summary",
                        ],
                        1,
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        assert result.status == "in_progress"
        assert result.sources[0].url == "https://fast.example.com"
        assert result.sources[0].title == "Fast Title"

    @pytest.mark.asyncio
    async def test_poll_source_with_no_title_or_url_skipped(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """src has two elements but neither is title nor url — not appended."""
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [
                [
                    "task_empty_src",
                    [
                        None,
                        ["empty src query"],
                        None,
                        [
                            [
                                # src[0] is not None and not str (e.g. integer), len < 3
                                # so url="", title="" and nothing is appended
                                [42, 99],
                            ],
                            "summary here",
                        ],
                        2,
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.poll("nb_123")
        assert result.status == "completed"
        assert result.sources == ()

    @pytest.mark.asyncio
    async def test_poll_all_tasks_too_short_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """All task entries are too short — the first drift raises.
        Each task_data entry is a short list (len < 2), so the ``task_info``
        descent drifts. Strict decoding is the only mode (the soft-mode
        opt-out was retired in v0.7.0), so the drift raises rather than every
        iteration being silently skipped to a ``no_research`` result.
        """
        # All task_data entries are short lists (len < 2)
        response = build_rpc_response(
            RPCMethod.POLL_RESEARCH,
            [["only_one"], ["also_one"]],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(UnknownRPCMethodError):
                await client.research.poll("nb_123")


class TestImportSourcesEdgeCases:
    """Tests for import_sources() parsing branch edge cases."""

    @pytest.mark.asyncio
    async def test_import_sources_skips_no_url_sources(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """sources without URLs are skipped; if ALL lack URLs, return []."""
        # No HTTP call should be made when all sources lack URLs
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                "nb_123",
                "task_123",
                [{"title": "No URL source"}, {"title": "Also no URL"}],
            )
        assert result == []

    @pytest.mark.asyncio
    async def test_import_sources_skips_deep_report_type(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Sources with result_type=5 (deep research report) are filtered out even with URL."""
        response = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            [
                [
                    [["src_web"], "Web Source"],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                "nb_123",
                "task_123",
                [
                    {"url": "https://web.com", "title": "Web Source", "result_type": 1},
                    {"url": "https://report.com", "title": "Report Entry", "result_type": 5},
                ],
            )
        assert len(result) == 1
        assert result[0]["id"] == "src_web"

    @pytest.mark.asyncio
    async def test_import_sources_filters_some_no_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """sources without URLs are filtered, valid ones are imported."""
        # Double-wrap so the unwrap logic peels one layer: result[0][0] is a list
        response = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            [
                [
                    [["src_good"], "Good Source"],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                "nb_123",
                "task_123",
                [
                    {"url": "https://good.com", "title": "Good Source"},
                    {"title": "No URL source"},  # filtered out
                ],
            )
        assert len(result) == 1
        assert result[0]["id"] == "src_good"

    @pytest.mark.asyncio
    async def test_import_sources_no_double_nesting(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """A non-list head skips envelope unwrapping and still parses later rows."""
        response = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            ["string_not_list", [["src_nw"], "No-Wrap Title"]],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                "nb_123",
                "task_123",
                [{"url": "https://nowrap.example.com", "title": "No-Wrap Title"}],
            )
        # "string_not_list" is not a list → skipped; [["src_nw"], "No-Wrap Title"] is valid
        assert len(result) == 1
        assert result[0]["id"] == "src_nw"
        assert result[0]["title"] == "No-Wrap Title"

    @pytest.mark.asyncio
    async def test_import_sources_flat_single_row_with_id_envelope(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Regression for #1558: flat ``[[id], title]`` rows are not wrappers."""
        response = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            [[["src_flat"], "Flat Title"]],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                "nb_123",
                "task_123",
                [{"url": "https://flat.example.com", "title": "Flat Title"}],
            )
        assert result == [{"id": "src_flat", "title": "Flat Title"}]

    @pytest.mark.asyncio
    async def test_import_sources_wrapped_single_row_still_unwraps(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Matches ``research_import_sources_direct.yaml``'s one-row wrapper."""
        response = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            [[[["src_wrapped"], "Wrapped Title"]]],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                "nb_123",
                "task_123",
                [{"url": "https://wrapped.example.com", "title": "Wrapped Title"}],
            )
        assert result == [{"id": "src_wrapped", "title": "Wrapped Title"}]

    @pytest.mark.asyncio
    async def test_import_sources_src_data_too_short_skipped(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """src_data in result has len < 2 — skipped in loop."""
        # First entry is too short (len 1), second is valid
        response = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            [
                ["short_only"],  # len 1 — skipped
                [["src_valid"], "Valid"],  # len 2 — kept
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Valid"}],
            )
        assert len(result) == 1
        assert result[0]["id"] == "src_valid"

    @pytest.mark.asyncio
    async def test_import_sources_src_id_none_skipped(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """src_data[0] is None (not a list) — src_id is None, entry skipped."""
        # src_data[0] is None — not a list, so src_id = None → skipped
        response = build_rpc_response(
            RPCMethod.IMPORT_RESEARCH,
            [
                [None, "Title with no ID"],  # src_data[0] is None → skipped
                [["src_real"], "Real Title"],  # valid
            ],
        )
        httpx_mock.add_response(content=response.encode())
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.research.import_sources(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "anything"}],
            )
        assert len(result) == 1
        assert result[0]["id"] == "src_real"
