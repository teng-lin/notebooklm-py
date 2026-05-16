"""Integration tests for NotebooksAPI."""

import json

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import Notebook, NotebookLMClient
from notebooklm.rpc import RPCError, RPCMethod


class TestListNotebooks:
    @pytest.mark.asyncio
    async def test_list_notebooks_returns_notebooks(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        mock_list_notebooks_response,
    ):
        httpx_mock.add_response(content=mock_list_notebooks_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notebooks = await client.notebooks.list()

        assert len(notebooks) == 2
        assert all(isinstance(nb, Notebook) for nb in notebooks)
        assert notebooks[0].title == "My First Notebook"
        assert notebooks[0].id == "nb_001"
        assert notebooks[0].sources_count == 2
        assert notebooks[1].sources_count == 0

    @pytest.mark.asyncio
    async def test_list_notebooks_request_format(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        mock_list_notebooks_response,
    ):
        httpx_mock.add_response(content=mock_list_notebooks_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.notebooks.list()

        request = httpx_mock.get_request()
        assert request.method == "POST"
        assert RPCMethod.LIST_NOTEBOOKS.value in str(request.url)
        assert b"f.req=" in request.content

    @pytest.mark.asyncio
    async def test_request_includes_cookies(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        mock_list_notebooks_response,
    ):
        httpx_mock.add_response(content=mock_list_notebooks_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.notebooks.list()

        request = httpx_mock.get_request()
        cookie_header = request.headers.get("cookie", "")
        assert "SID=test_sid" in cookie_header
        assert "HSID=test_hsid" in cookie_header

    @pytest.mark.asyncio
    async def test_request_includes_csrf(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        mock_list_notebooks_response,
    ):
        httpx_mock.add_response(content=mock_list_notebooks_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.notebooks.list()

        request = httpx_mock.get_request()
        body = request.content.decode()
        assert "at=test_csrf_token" in body


class TestCreateNotebook:
    @pytest.mark.asyncio
    async def test_create_notebook(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        # ``create`` snapshots the notebook list before issuing
        # CREATE_NOTEBOOK so the probe-then-retry wrapper can detect a
        # server-side commit on a transport failure. Stub the baseline
        # list response first; then the create response.
        baseline_list = build_rpc_response(RPCMethod.LIST_NOTEBOOKS, [[]])
        httpx_mock.add_response(content=baseline_list.encode())
        response = build_rpc_response(
            RPCMethod.CREATE_NOTEBOOK,
            [
                "My Notebook",
                [],
                "new_nb_id",
                "📓",
                None,
                [None, None, None, None, None, [1704067200, 0]],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notebook = await client.notebooks.create("My Notebook")

        assert isinstance(notebook, Notebook)
        assert notebook.id == "new_nb_id"
        assert notebook.title == "My Notebook"

    @pytest.mark.asyncio
    async def test_create_notebook_request_contains_title(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        # see ``test_create_notebook`` for baseline-list rationale.
        baseline_list = build_rpc_response(RPCMethod.LIST_NOTEBOOKS, [[]])
        httpx_mock.add_response(content=baseline_list.encode())
        response = build_rpc_response(
            RPCMethod.CREATE_NOTEBOOK,
            ["Test Title", [], "id", "📓", None, [None, None, None, None, None, [1704067200, 0]]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.notebooks.create("Test Title")

        # The create request is the second one; assert it carries the
        # CREATE_NOTEBOOK rpcid AND the title we passed in.
        requests = httpx_mock.get_requests()
        create_requests = [r for r in requests if RPCMethod.CREATE_NOTEBOOK.value in str(r.url)]
        assert len(create_requests) == 1
        body = create_requests[0].content.decode()
        assert "Test+Title" in body or "Test%20Title" in body, (
            f"CREATE_NOTEBOOK request body did not contain url-encoded 'Test Title': {body[:200]}"
        )


class TestGetNotebook:
    @pytest.mark.asyncio
    async def test_get_notebook(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [["source1"], ["source2"]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notebook = await client.notebooks.get("nb_123")

        assert isinstance(notebook, Notebook)
        assert notebook.id == "nb_123"
        assert notebook.title == "Test Notebook"
        # data[1] holds the source list in the GET_NOTEBOOK shape, same as LIST.
        assert notebook.sources_count == 2

    @pytest.mark.asyncio
    async def test_get_notebook_sources_count_matches_real_payload(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Regression: ``Notebook.sources_count`` is derived from ``data[1]``.

        Pinned to the shape captured in ``tests/cassettes/notebooks_get.yaml``
        (a real GET_NOTEBOOK response) — two PDF source entries at index 1.
        If Google ever moves the source list, this test fails before any
        downstream code that depends on ``sources_count`` (notably the
        divergence warning in ``_notebooks.py``) silently produces a wrong
        count.
        """
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "TypeScript Fundamentals",
                    [
                        [
                            ["fdfc8ac4-3237-4f2a-8a79-3e24297a7040"],
                            "Programming TypeScript.pdf",
                            [None, 87289, [1767921640, 565022000], None, 3, None, 1],
                            [None, 2],
                        ],
                        [
                            ["ddd31154-74a0-484a-a24c-aff796acae2f"],
                            "typescript-book.pdf",
                            [None, 22183, [1767921620, 707149000], None, 3, None, 1],
                            [None, 2],
                        ],
                    ],
                    "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e",
                    "📘",
                    None,
                    [None, None, None, None, None, [1768963937, 237838000]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notebook = await client.notebooks.get("c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e")

        assert notebook.sources_count == 2

    @pytest.mark.asyncio
    async def test_get_notebook_uses_source_path(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [["Name", [], "nb_123", "📘", None, [None, None, None, None, None, [1704067200, 0]]]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.notebooks.get("nb_123")

        request = httpx_mock.get_request()
        assert "source-path=%2Fnotebook%2Fnb_123" in str(request.url)


class TestDeleteNotebook:
    @pytest.mark.asyncio
    async def test_delete_notebook(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(RPCMethod.DELETE_NOTEBOOK, [True])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notebooks.delete("nb_123")

        assert result is True


class TestSummary:
    @pytest.mark.asyncio
    async def test_get_summary(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(
            RPCMethod.SUMMARIZE, [[["Summary of the notebook content..."]]]
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notebooks.get_summary("nb_123")

        assert "Summary" in result


class TestRenameNotebook:
    @pytest.mark.asyncio
    async def test_rename_notebook(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        # First response for rename (returns null)
        rename_response = build_rpc_response(RPCMethod.RENAME_NOTEBOOK, None)
        httpx_mock.add_response(content=rename_response.encode())
        # Second response for get_notebook call after rename
        get_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "New Title",
                    [],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=get_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notebook = await client.notebooks.rename("nb_123", "New Title")

        assert isinstance(notebook, Notebook)
        assert notebook.id == "nb_123"
        assert notebook.title == "New Title"

    @pytest.mark.asyncio
    async def test_rename_notebook_request_format(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        # Rename response (returns null)
        rename_response = build_rpc_response(RPCMethod.RENAME_NOTEBOOK, None)
        httpx_mock.add_response(content=rename_response.encode())
        # Get notebook response after rename
        get_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Renamed",
                    [],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=get_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.notebooks.rename("nb_123", "Renamed")

        request = httpx_mock.get_requests()[0]
        assert RPCMethod.RENAME_NOTEBOOK.value in str(request.url)
        assert "source-path=%2F" in str(request.url)


class TestNotebooksAPIAdditional:
    """Additional integration tests for NotebooksAPI."""

    @pytest.mark.asyncio
    async def test_share_notebook(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test sharing a notebook."""
        response = build_rpc_response(
            RPCMethod.SHARE_ARTIFACT,
            None,  # Share returns null, we build the URL
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notebooks.share("nb_123", public=True)

        assert result["public"] is True
        assert "nb_123" in result["url"]
        request = httpx_mock.get_request()
        assert RPCMethod.SHARE_ARTIFACT.value in str(request.url)

    @pytest.mark.asyncio
    async def test_get_summary_additional(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting notebook summary."""
        response = build_rpc_response(
            RPCMethod.SUMMARIZE,
            [[["This is a comprehensive summary of the notebook content..."]]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notebooks.get_summary("nb_123")

        assert "summary" in result.lower()

    @pytest.mark.asyncio
    async def test_remove_from_recent(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test removing notebook from recent list."""
        response = build_rpc_response("fejl7e", None)  # REMOVE_RECENTLY_VIEWED
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.notebooks.remove_from_recent("nb_123")

        request = httpx_mock.get_request()
        assert "fejl7e" in str(request.url)

    @pytest.mark.asyncio
    async def test_get_raw(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting raw notebook data."""
        raw_data = [
            ["Test Notebook", [["src1"], ["src2"]], "nb_123", "📘"],
            ["extra", "metadata"],
        ]
        response = build_rpc_response(RPCMethod.GET_NOTEBOOK, raw_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notebooks.get_raw("nb_123")

        assert result == raw_data
        request = httpx_mock.get_request()
        assert "source-path=%2Fnotebook%2Fnb_123" in str(request.url)

    @pytest.mark.asyncio
    async def test_get_description(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting notebook description with summary and topics."""
        response = build_rpc_response(
            RPCMethod.SUMMARIZE,
            [
                [
                    ["This notebook covers AI research."],
                    [
                        [
                            ["What are the main findings?", "Explain the key findings"],
                            ["How was the study conducted?", "Describe methodology"],
                        ]
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            description = await client.notebooks.get_description("nb_123")

        assert description.summary == "This notebook covers AI research."
        assert len(description.suggested_topics) == 2
        assert description.suggested_topics[0].question == "What are the main findings?"
        assert description.suggested_topics[0].prompt == "Explain the key findings"


class TestGetNotebookFailures:
    """Integration tests reproducing Issue #114 GET_NOTEBOOK failures.

    Exercises the full NotebookLMClient → rpc_call → decode_response pipeline
    with injected HTTP responses matching each failure scenario from Issue #114.
    """

    @pytest.mark.asyncio
    async def test_empty_response_body(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Scenario A: Server returns empty body after anti-XSSI prefix."""
        raw = b")]}'\n"
        httpx_mock.add_response(content=raw)

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(RPCError, match="response contained no RPC data"):
                await client.notebooks.get("nb_123")

    @pytest.mark.asyncio
    async def test_non_rpc_json_response(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Scenario B: Server returns JSON chunks but no RPC data."""
        chunk = json.dumps({"error": "something"})
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n".encode()
        httpx_mock.add_response(content=raw)

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(RPCError, match="response contained no RPC data"):
                await client.notebooks.get("nb_123")

    @pytest.mark.asyncio
    async def test_null_result_data(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Scenario C: wrb.fr matches GET_NOTEBOOK ID but result_data is None."""
        rpc_id = RPCMethod.GET_NOTEBOOK.value
        chunk = json.dumps(["wrb.fr", rpc_id, None, None, None, None])
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n".encode()
        httpx_mock.add_response(content=raw)

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(RPCError, match="returned null result data"):
                await client.notebooks.get("nb_123")

    @pytest.mark.asyncio
    async def test_short_wrb_fr_item(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Scenario D: wrb.fr item has only 2 elements (skipped by extract)."""
        rpc_id = RPCMethod.GET_NOTEBOOK.value
        chunk = json.dumps(["wrb.fr", rpc_id])
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n".encode()
        httpx_mock.add_response(content=raw)

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(RPCError, match="returned null result data"):
                await client.notebooks.get("nb_123")


class TestNotebookEdgeCases:
    """Test edge cases for NotebooksAPI."""

    @pytest.mark.asyncio
    async def test_list_notebooks_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing notebooks when none exist."""
        response = build_rpc_response(RPCMethod.LIST_NOTEBOOKS, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notebooks = await client.notebooks.list()

        assert notebooks == []

    @pytest.mark.asyncio
    async def test_list_notebooks_nested_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing notebooks with nested empty array."""
        response = build_rpc_response(RPCMethod.LIST_NOTEBOOKS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notebooks = await client.notebooks.list()

        assert notebooks == []

    @pytest.mark.asyncio
    async def test_get_summary_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting summary when empty."""
        response = build_rpc_response(RPCMethod.SUMMARIZE, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notebooks.get_summary("nb_123")

        assert result == ""

    @pytest.mark.asyncio
    async def test_get_description_empty_topics(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting description with no suggested topics."""
        response = build_rpc_response(
            RPCMethod.SUMMARIZE,
            [[["Summary text"], []]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            description = await client.notebooks.get_description("nb_123")

        assert description.summary == "Summary text"
        assert description.suggested_topics == []

    @pytest.mark.asyncio
    async def test_get_description_malformed_topics(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting description with malformed topic data."""
        response = build_rpc_response(
            RPCMethod.SUMMARIZE,
            [
                [
                    ["Summary"],
                    [
                        [
                            ["Valid question", "Valid prompt"],
                            ["Only question"],  # Missing prompt
                            "not a list",  # Not a list
                        ]
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            description = await client.notebooks.get_description("nb_123")

        assert description.summary == "Summary"
        # Should only include valid topics
        assert len(description.suggested_topics) == 1
        assert description.suggested_topics[0].question == "Valid question"


class TestDescribeEdgeCases:
    """Tests for get_description() branch edge cases."""

    @pytest.mark.asyncio
    async def test_get_description_no_topics_key(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """result has only outer[0] (no outer[1]) so topics stay empty."""
        # result = [[["A summary"]]] — outer[0] has summary, no outer[1] for topics
        response = build_rpc_response(
            RPCMethod.SUMMARIZE,
            [[["A summary"]]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            description = await client.notebooks.get_description("nb_123")

        assert description.summary == "A summary"
        assert description.suggested_topics == []

    @pytest.mark.asyncio
    async def test_get_description_result_1_is_empty_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """outer[1] exists but is an empty list, so topics block is skipped."""
        # result = [[["A summary"], []]] — outer[1] is empty, so topics are skipped
        response = build_rpc_response(
            RPCMethod.SUMMARIZE,
            [[["A summary"], []]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            description = await client.notebooks.get_description("nb_123")

        assert description.summary == "A summary"
        assert description.suggested_topics == []

    @pytest.mark.asyncio
    async def test_get_description_result_1_not_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """outer[1] is present but not a list, so topics block is skipped."""
        # result = [[["A summary"], "not-a-list"]] — outer[1] is not a list, topics skipped
        response = build_rpc_response(
            RPCMethod.SUMMARIZE,
            [[["A summary"], "not-a-list"]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            description = await client.notebooks.get_description("nb_123")

        assert description.summary == "A summary"
        assert description.suggested_topics == []


class TestShareEdgeCases:
    """Tests for share() and get_share_url() branch edge cases."""

    @pytest.mark.asyncio
    async def test_share_with_artifact_id(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Line 260: share() public=True with artifact_id builds deep-link URL."""
        response = build_rpc_response(RPCMethod.SHARE_ARTIFACT, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notebooks.share("nb_123", public=True, artifact_id="art_456")

        assert result["public"] is True
        assert result["url"] == "https://notebooklm.google.com/notebook/nb_123?artifactId=art_456"
        assert result["artifact_id"] == "art_456"

    @pytest.mark.asyncio
    async def test_share_public_false_returns_none_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Line 264: share() public=False sets url to None."""
        response = build_rpc_response(RPCMethod.SHARE_ARTIFACT, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notebooks.share("nb_123", public=False)

        assert result["public"] is False
        assert result["url"] is None

    @pytest.mark.asyncio
    async def test_get_share_url_without_artifact(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Line 288: get_share_url() without artifact_id returns base URL."""
        async with NotebookLMClient(auth_tokens) as client:
            url = client.notebooks.get_share_url("nb_123")

        assert url == "https://notebooklm.google.com/notebook/nb_123"

    @pytest.mark.asyncio
    async def test_get_share_url_with_artifact(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Lines 285-287: get_share_url() with artifact_id appends query param."""
        async with NotebookLMClient(auth_tokens) as client:
            url = client.notebooks.get_share_url("nb_123", artifact_id="art_789")

        assert url == "https://notebooklm.google.com/notebook/nb_123?artifactId=art_789"
