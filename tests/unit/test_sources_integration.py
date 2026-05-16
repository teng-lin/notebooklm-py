"""Integration tests for SourcesAPI."""

import re
import urllib.parse
import zipfile
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient, Source, SourceType
from notebooklm.exceptions import RPCError
from notebooklm.rpc import RPCMethod
from notebooklm.types import SourceAddError, SourceNotFoundError


class TestAddSource:
    @pytest.mark.asyncio
    async def test_add_source_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(
            RPCMethod.ADD_SOURCE,
            [
                [
                    [
                        ["source_id"],
                        "Example Site",
                        [None, 11, None, None, 5, None, 1, ["https://example.com"]],
                        [None, 2],
                    ]
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.add_url("nb_123", "https://example.com")

        assert isinstance(source, Source)
        assert source.id == "source_id"
        assert source.url == "https://example.com"

    @pytest.mark.asyncio
    async def test_add_source_text(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(
            RPCMethod.ADD_SOURCE, [[[["source_id"], "My Document", [None, 11], [None, 2]]]]
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.add_text("nb_123", "My Document", "This is the content")

        assert isinstance(source, Source)
        assert source.id == "source_id"
        assert source.title == "My Document"


class TestDeleteSource:
    @pytest.mark.asyncio
    async def test_delete_source(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(RPCMethod.DELETE_SOURCE, [True])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.sources.delete("nb_123", "source_456")

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_source_request_format(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(RPCMethod.DELETE_SOURCE, [True])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.sources.delete("nb_123", "source_456")

        request = httpx_mock.get_request()
        assert RPCMethod.DELETE_SOURCE in str(request.url)
        assert "source-path=%2Fnotebook%2Fnb_123" in str(request.url)


class TestGetSource:
    @pytest.mark.asyncio
    async def test_get_source(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        # get() filters from get_notebook, so mock GET_NOTEBOOK response
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [
                        [
                            ["source_456"],
                            "Source Title",
                            [
                                None,
                                None,
                                None,
                                None,
                                5,  # SourceType.WEB_PAGE
                                None,
                                None,
                                ["https://example.com"],
                            ],
                            [None, 2],  # Status.READY
                        ]
                    ],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.get("nb_123", "source_456")

        assert isinstance(source, Source)
        assert source.id == "source_456"
        assert source.title == "Source Title"
        assert source.kind == SourceType.WEB_PAGE
        assert source.kind == "web_page"

    @pytest.mark.asyncio
    async def test_get_source_uses_public_list_hook(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            client.sources.list = AsyncMock(
                return_value=[
                    Source(id="source_123", title="Other"),
                    Source(id="source_456", title="Source Title"),
                ]
            )

            source = await client.sources.get("nb_123", "source_456")

        client.sources.list.assert_awaited_once_with("nb_123")
        assert source is not None
        assert source.id == "source_456"
        assert source.title == "Source Title"

    @pytest.mark.asyncio
    async def test_list_uses_current_core_rpc_call(self, auth_tokens):
        first_rpc = AsyncMock(return_value=[["Notebook", []]])
        second_rpc = AsyncMock(
            return_value=[
                [
                    "Notebook",
                    [
                        [
                            ["source_456"],
                            "Source Title",
                            [None, 11, None, None, 5, None, None, ["https://example.com"]],
                            [None, 2],
                        ]
                    ],
                ]
            ]
        )

        async with NotebookLMClient(auth_tokens) as client:
            client._core.rpc_call = first_rpc
            assert await client.sources.list("nb_123") == []

            client._core.rpc_call = second_rpc
            sources = await client.sources.list("nb_123")

        first_rpc.assert_awaited_once()
        second_rpc.assert_awaited_once()
        assert [source.id for source in sources] == ["source_456"]


class TestSourcesAPI:
    """Integration tests for SourcesAPI methods."""

    @pytest.mark.asyncio
    async def test_list_sources(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing sources with various types."""
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [
                        [
                            ["src_001"],
                            "My Article",
                            [
                                None,
                                11,
                                [1704067200, 0],
                                None,
                                5,  # WEB_PAGE type code
                                None,
                                None,
                                ["https://example.com"],
                            ],
                            [None, 2],
                        ],
                        [["src_002"], "My Text", [None, 0, [1704153600, 0]], [None, 2]],
                        [
                            ["src_003"],
                            "YouTube Video",
                            [
                                None,
                                11,
                                [1704240000, 0],
                                None,
                                9,  # YOUTUBE type code
                                None,
                                None,
                                ["https://youtube.com/watch?v=abc"],
                            ],
                            [None, 2],
                        ],
                    ],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 3
        assert sources[0].id == "src_001"
        assert sources[0].kind == "web_page"
        assert sources[0].url == "https://example.com"
        assert sources[2].kind == "youtube"

    @pytest.mark.asyncio
    async def test_list_sources_youtube_url_at_index_5(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Regression test for issue #265: YouTube URLs are stored at src[2][5][0].

        The real NotebookLM API stores YouTube metadata at src[2][5] as
        [url, video_id, channel_name], with src[2][7] = None. The list() method
        must extract the URL from [5] when [7] is not populated.
        """
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [
                        [
                            ["src_yt"],
                            "YouTube Video",
                            [
                                None,
                                11,
                                [1704240000, 0],
                                None,
                                9,  # YOUTUBE type code
                                [
                                    "https://www.youtube.com/watch?v=dcWU-qD8ISQ",
                                    "dcWU-qD8ISQ",
                                    "john newquist",
                                ],
                                None,
                                None,  # [7] is None for YouTube sources
                            ],
                            [None, 2],
                        ],
                    ],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        assert sources[0].id == "src_yt"
        assert sources[0].kind == "youtube"
        assert sources[0].url == "https://www.youtube.com/watch?v=dcWU-qD8ISQ"

    @pytest.mark.asyncio
    async def test_list_sources_ignores_bare_http_at_index_0(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """metadata[0] must not be used as a URL fallback in list().

        GET_NOTEBOOK source entries use the same medium-nested shape that
        Source.from_api_response handles with allow_bare_http=False —
        metadata[0] in that shape can pack unrelated http-like data. If the
        bare-[0] fallback fires, list() would surface a bogus URL. Keep
        allow_bare_http=False here so list() and from_api_response agree.
        """
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [
                        [
                            ["src_bare"],
                            "Source with bare http at [0]",
                            [
                                # metadata[0] is an http-like string that must
                                # be ignored — it is not a source URL here.
                                "http://unrelated.example.com/not-a-source-url",
                                11,
                                [1704240000, 0],
                                None,
                                5,
                                None,
                                None,
                                None,  # metadata[7] empty → no web-page URL
                            ],
                            [None, 2],
                        ],
                    ],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        assert sources[0].id == "src_bare"
        assert sources[0].url is None

    @pytest.mark.asyncio
    async def test_list_sources_index_7_wins_over_5(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """When both src[2][7] and src[2][5] are populated, [7] wins."""
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [
                        [
                            ["src_both"],
                            "Hybrid",
                            [
                                None,
                                11,
                                [1704240000, 0],
                                None,
                                5,
                                ["https://shouldnt.win/5"],
                                None,
                                ["https://should.win/7"],
                            ],
                            [None, 2],
                        ],
                    ],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        assert sources[0].url == "https://should.win/7"

    @pytest.mark.asyncio
    async def test_list_sources_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing sources from empty notebook."""
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Empty Notebook",
                    [],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert sources == []

    @pytest.mark.asyncio
    async def test_get_source_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting a non-existent source."""
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.get("nb_123", "nonexistent")

        assert source is None

    @pytest.mark.asyncio
    async def test_add_drive_source(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test adding a Google Drive source."""
        response = build_rpc_response(
            RPCMethod.ADD_SOURCE,
            [[[["drive_001"], "My Doc", [None, 0], [None, 2]]]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.add_drive(
                "nb_123",
                file_id="abc123xyz",
                title="My Doc",
                mime_type="application/vnd.google-apps.document",
            )

        assert source is not None
        request = httpx_mock.get_request()
        assert RPCMethod.ADD_SOURCE in str(request.url)

    @pytest.mark.asyncio
    async def test_refresh_source(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test refreshing a source."""
        response = build_rpc_response(RPCMethod.REFRESH_SOURCE, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.sources.refresh("nb_123", "src_001")

        assert result is True
        request = httpx_mock.get_request()
        assert RPCMethod.REFRESH_SOURCE in str(request.url)

    @pytest.mark.asyncio
    async def test_check_freshness_fresh(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test checking freshness - source is fresh (explicit True)."""
        response = build_rpc_response("yR9Yof", True)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is True

    @pytest.mark.asyncio
    async def test_check_freshness_fresh_empty_array(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test checking freshness - source is fresh (empty array response).

        The real API returns [] (empty array) when source is fresh,
        not True. This test ensures we handle the actual API response.
        """
        # Real API returns empty array for fresh sources
        response = build_rpc_response("yR9Yof", [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is True, "Empty array should mean source is fresh"

    @pytest.mark.asyncio
    async def test_check_freshness_fresh_drive_nested(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test checking freshness - Drive source is fresh (nested response).

        Drive sources return [[null, true, [source_id]]] when fresh,
        not an empty array like URL sources.
        """
        # Real API returns nested structure for Drive sources
        response = build_rpc_response("yR9Yof", [[None, True, ["src_001"]]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is True, "Nested [null, true, ...] should mean source is fresh"

    @pytest.mark.asyncio
    async def test_check_freshness_stale(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test checking freshness - source is stale."""
        response = build_rpc_response("yR9Yof", False)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is False

    @pytest.mark.asyncio
    async def test_get_guide(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting source guide."""
        # Real API returns 3 levels of nesting: [[[null, [summary], [[keywords]], []]]]
        response = build_rpc_response(
            RPCMethod.GET_SOURCE_GUIDE,
            [
                [
                    [
                        None,
                        ["This is a **summary** of the source content..."],
                        [["keyword1", "keyword2", "keyword3"]],
                        [],
                    ]
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            guide = await client.sources.get_guide("nb_123", "src_001")

        assert "summary" in guide
        assert "keywords" in guide
        assert "**summary**" in guide["summary"]
        assert guide["keywords"] == ["keyword1", "keyword2", "keyword3"]

    @pytest.mark.asyncio
    async def test_get_guide_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting guide for source with no AI analysis."""
        # Real API returns 3 levels of nesting even for empty responses
        response = build_rpc_response(RPCMethod.GET_SOURCE_GUIDE, [[[None, [], [], []]]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            guide = await client.sources.get_guide("nb_123", "src_001")

        assert guide["summary"] == ""
        assert guide["keywords"] == []

    @pytest.mark.asyncio
    async def test_rename_source(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test renaming a source."""
        response = build_rpc_response("b7Wfje", None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.rename("nb_123", "src_001", "New Title")

        assert source.title == "New Title"

        request = httpx_mock.get_request()
        assert "b7Wfje" in str(request.url)


class TestAddFileSource:
    """Integration tests for file upload functionality."""

    @pytest.mark.asyncio
    async def test_add_file_success(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test successful file upload with 3-step protocol."""
        # Create test file
        test_file = tmp_path / "test_document.txt"
        test_file.write_text("This is test content for upload.")

        # Step 1: Mock RPC registration response (o4cbdc)
        rpc_response = build_rpc_response(
            RPCMethod.ADD_SOURCE_FILE,
            [[[["file_source_123"], "test_document.txt", [None, None, None, None, 0]]]],
        )
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*"),
            content=rpc_response.encode(),
        )

        # Step 2: Mock upload session start response
        httpx_mock.add_response(
            url=re.compile(r".*upload/_/\?authuser=0$"),
            headers={
                "x-goog-upload-url": "https://notebooklm.google.com/upload/_/?authuser=0&upload_id=test_upload_id",
                "x-goog-upload-status": "active",
            },
            content=b"",
        )

        # Step 3: Mock upload finalize response
        httpx_mock.add_response(
            url=re.compile(r".*upload/_/\?authuser=0&upload_id=.*"),
            content=b"OK: Enqueued blob bytes to spanner queue for processing.",
        )

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.add_file("nb_123", test_file)

        assert source is not None
        assert source.id == "file_source_123"
        assert source.title == "test_document.txt"
        assert source.kind == "unknown"

        # Verify all 3 requests were made
        requests = httpx_mock.get_requests()
        assert len(requests) == 3

        # Verify Step 1: RPC call
        assert RPCMethod.ADD_SOURCE_FILE in str(requests[0].url)

        # Verify Step 2: Upload start
        assert "x-goog-upload-command" in requests[1].headers
        assert requests[1].headers["x-goog-upload-command"] == "start"

        # Verify Step 3: Upload finalize
        assert "x-goog-upload-command" in requests[2].headers
        assert requests[2].headers["x-goog-upload-command"] == "upload, finalize"

    @pytest.mark.asyncio
    async def test_add_file_rpc_params_format(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test that file registration uses correct parameter nesting."""
        test_file = tmp_path / "my_file.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake pdf content")

        # Mock all 3 responses
        rpc_response = build_rpc_response(
            RPCMethod.ADD_SOURCE_FILE,
            [[[[" src_id"], "my_file.pdf", [None, None, None, None, 0]]]],
        )
        httpx_mock.add_response(url=re.compile(r".*batchexecute.*"), content=rpc_response.encode())
        httpx_mock.add_response(
            url=re.compile(r".*upload/_/\?authuser=0$"),
            headers={"x-goog-upload-url": "https://notebooklm.google.com/upload/_/?upload_id=x"},
        )
        httpx_mock.add_response(url=re.compile(r".*upload_id=.*"), content=b"OK")

        async with NotebookLMClient(auth_tokens) as client:
            await client.sources.add_file("nb_123", test_file)

        # Check the RPC request body contains correct nesting
        # params[0] should be [[filename]] (double-nested within the param)
        # In the full params array JSON: [[[filename]], nb_id, ...] (3 brackets total)
        # NOT [[[[filename]]], ...] (4 brackets - the old bug)
        rpc_request = httpx_mock.get_requests()[0]
        body = urllib.parse.unquote(rpc_request.content.decode())
        # The params are JSON-encoded inside the RPC wrapper, so quotes are escaped
        # Verify 3 brackets (correct) not 4 brackets (bug)
        assert '[[[\\"my_file.pdf\\"]]' in body, f"Expected 3 brackets, got: {body}"
        assert '[[[[\\"my_file.pdf\\"]]' not in body, "Should not have 4 brackets (old bug)"

    @pytest.mark.asyncio
    async def test_add_file_not_found(
        self,
        auth_tokens,
        tmp_path,
    ):
        """Test file upload with non-existent file."""
        nonexistent = tmp_path / "does_not_exist.txt"

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(FileNotFoundError):
                await client.sources.add_file("nb_123", nonexistent)

    @pytest.mark.asyncio
    async def test_add_file_upload_metadata(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test that upload session includes correct metadata."""
        test_file = tmp_path / "document.txt"
        content = "Test content " * 100
        test_file.write_text(content)

        rpc_response = build_rpc_response(
            RPCMethod.ADD_SOURCE_FILE,
            [[[["src_abc"], "document.txt", [None, None, None, None, 0]]]],
        )
        httpx_mock.add_response(url=re.compile(r".*batchexecute.*"), content=rpc_response.encode())
        httpx_mock.add_response(
            url=re.compile(r".*upload/_/\?authuser=0$"),
            headers={"x-goog-upload-url": "https://notebooklm.google.com/upload/_/?upload_id=y"},
        )
        httpx_mock.add_response(url=re.compile(r".*upload_id=.*"), content=b"OK")

        async with NotebookLMClient(auth_tokens) as client:
            await client.sources.add_file("nb_123", test_file)

        # Check upload start request (Step 2)
        start_request = httpx_mock.get_requests()[1]

        # Verify headers
        assert start_request.headers["x-goog-upload-protocol"] == "resumable"
        assert start_request.headers["x-goog-upload-header-content-length"] == str(len(content))

        # Verify body contains metadata
        import json

        body = json.loads(start_request.content.decode())
        assert body["PROJECT_ID"] == "nb_123"
        assert body["SOURCE_NAME"] == "document.txt"
        assert body["SOURCE_ID"] == "src_abc"

    @pytest.mark.asyncio
    async def test_add_file_content_upload(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test that file content is correctly uploaded."""
        test_file = tmp_path / "binary_file.bin"
        binary_content = b"\x00\x01\x02\x03\xff\xfe\xfd"
        test_file.write_bytes(binary_content)

        rpc_response = build_rpc_response(
            RPCMethod.ADD_SOURCE_FILE,
            [[[["src_bin"], "binary_file.bin", [None, None, None, None, 0]]]],
        )
        httpx_mock.add_response(url=re.compile(r".*batchexecute.*"), content=rpc_response.encode())
        httpx_mock.add_response(
            url=re.compile(r".*upload/_/\?authuser=0$"),
            headers={"x-goog-upload-url": "https://notebooklm.google.com/upload/_/?upload_id=z"},
        )
        httpx_mock.add_response(url=re.compile(r".*upload_id=.*"), content=b"OK")

        async with NotebookLMClient(auth_tokens) as client:
            await client.sources.add_file("nb_123", test_file)

        # Check upload content request (Step 3)
        upload_request = httpx_mock.get_requests()[2]

        # Verify the actual content was sent
        assert upload_request.content == binary_content
        assert upload_request.headers["x-goog-upload-offset"] == "0"


class TestAddEpubFileSource:
    """Integration tests for EPUB file upload."""

    @pytest.mark.asyncio
    async def test_add_epub_file_upload(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test EPUB file upload through the 3-step protocol."""
        # Minimal EPUB ZIP — only needs valid ZIP structure since the server
        # is mocked and never processes the content.
        test_epub = tmp_path / "test_book.epub"
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            zf.writestr("OEBPS/chapter1.xhtml", "<html><body><p>Test</p></body></html>")
        test_epub.write_bytes(buffer.getvalue())

        # Step 1: Mock RPC registration
        rpc_response = build_rpc_response(
            RPCMethod.ADD_SOURCE_FILE,
            [[[["epub_source_123"], "test_book.epub", [None, None, None, None, 0]]]],
        )
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*"),
            content=rpc_response.encode(),
        )

        # Step 2: Mock upload session start
        httpx_mock.add_response(
            url=re.compile(r".*upload/_/\?authuser=0$"),
            headers={
                "x-goog-upload-url": "https://notebooklm.google.com/upload/_/?authuser=0&upload_id=epub_upload",
                "x-goog-upload-status": "active",
            },
            content=b"",
        )

        # Step 3: Mock upload finalize
        httpx_mock.add_response(
            url=re.compile(r".*upload/_/\?authuser=0&upload_id=.*"),
            content=b"OK: Enqueued blob bytes to spanner queue for processing.",
        )

        async with NotebookLMClient(auth_tokens) as client:
            # ``mime_type`` is deprecated for ``add_file``; the upload
            # pipeline derives the MIME type from the filename extension. The
            # legacy call site is preserved-by-omission here to avoid leaking
            # a DeprecationWarning into the integration suite.
            source = await client.sources.add_file(
                "nb_123",
                test_epub,
            )

        assert source.id == "epub_source_123"
        assert source.title == "test_book.epub"

        # Verify all 3 requests were made
        requests = httpx_mock.get_requests()
        assert len(requests) == 3

        # Verify uploaded content is the EPUB ZIP bytes
        upload_request = requests[2]
        assert upload_request.content == test_epub.read_bytes()


class TestGetFulltext:
    """Tests for sources.get_fulltext() method."""

    @pytest.mark.asyncio
    async def test_get_fulltext_basic(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting fulltext content of a source."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                [
                    "source_123",
                    "My Article",
                    [None, None, None, None, 5, None, None, ["https://example.com"]],
                ],
                None,
                None,
                [
                    [
                        [0, 100, "This is the first paragraph of the article."],
                        [100, 200, "This is the second paragraph."],
                    ]
                ],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "source_123")

        from notebooklm import SourceFulltext

        assert isinstance(fulltext, SourceFulltext)
        assert fulltext.source_id == "source_123"
        assert fulltext.title == "My Article"
        assert fulltext.kind == SourceType.WEB_PAGE
        assert fulltext.url == "https://example.com"
        assert "first paragraph" in fulltext.content
        assert "second paragraph" in fulltext.content
        assert fulltext.char_count > 0

    @pytest.mark.asyncio
    async def test_get_fulltext_request_format(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test that get_fulltext sends correct RPC request."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [["src_456", "Title", []], None, None, [[["Content here"]]]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.sources.get_fulltext("nb_123", "src_456")

        request = httpx_mock.get_request()
        # Verify RPC method in URL
        assert RPCMethod.GET_SOURCE in str(request.url)
        # Verify source_path includes notebook_id
        assert "source-path=%2Fnotebook%2Fnb_123" in str(request.url)
        # Verify params format: [[source_id], [2], [2]]
        body = urllib.parse.unquote(request.content.decode())
        assert "src_456" in body
        # Check for the [2], [2] structure
        assert "[2]" in body

    @pytest.mark.asyncio
    async def test_get_fulltext_empty_content(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext with empty content."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [["src_empty", "Empty Source", []], None, None, None],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_empty")

        assert fulltext.source_id == "src_empty"
        assert fulltext.title == "Empty Source"
        assert fulltext.content == ""
        assert fulltext.char_count == 0

    @pytest.mark.asyncio
    async def test_get_fulltext_markdown_basic(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Markdown mode converts HTML at result[4][1] via markdownify."""
        pytest.importorskip("markdownify")
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                ["source_md", "MD Article", []],
                None,
                None,
                None,
                [None, "<h1>Title</h1><p>Hello <strong>world</strong>.</p>"],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext(
                "nb_123", "source_md", output_format="markdown"
            )

        assert fulltext.source_id == "source_md"
        assert "# Title" in fulltext.content
        assert "**world**" in fulltext.content

    @pytest.mark.asyncio
    async def test_get_fulltext_markdown_sends_params_3_3(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Markdown mode requests HTML rendition via params [3],[3]."""
        pytest.importorskip("markdownify")
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [["src_x", "T", []], None, None, None, [None, "<p>x</p>"]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.sources.get_fulltext("nb_123", "src_x", output_format="markdown")

        body = urllib.parse.unquote(httpx_mock.get_request().content.decode())
        assert "[3]" in body
        assert "[2]" not in body or body.count("[3]") >= 2

    @pytest.mark.asyncio
    async def test_get_fulltext_invalid_format_raises(self, auth_tokens):
        """Unknown output_format must fail fast before any RPC call."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match=r"text.*markdown"):
                await client.sources.get_fulltext(
                    "nb_123",
                    "src_x",
                    output_format="html",  # type: ignore[arg-type]
                )

    @pytest.mark.asyncio
    async def test_get_fulltext_markdown_missing_dep_raises(self, monkeypatch, auth_tokens):
        """When markdownify is unavailable, ImportError surfaces before the RPC."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "markdownify":
                raise ImportError("No module named 'markdownify'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ImportError, match="markdownify"):
                await client.sources.get_fulltext("nb_123", "src_x", output_format="markdown")

    @pytest.mark.asyncio
    async def test_get_fulltext_markdown_no_html_returns_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        caplog,
    ):
        """Source with no HTML rendition logs a warning and returns empty content."""
        pytest.importorskip("markdownify")
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [["src_yt", "YouTube vid", []], None, None, None],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with caplog.at_level("WARNING"):
                fulltext = await client.sources.get_fulltext(
                    "nb_123", "src_yt", output_format="markdown"
                )

        assert fulltext.content == ""
        assert any("no HTML rendition" in r.message for r in caplog.records)


class TestListSourcesMalformedResponse:
    """Tests for list() warning paths when API response is malformed (lines 71-95)."""

    @pytest.mark.asyncio
    async def test_list_sources_empty_response(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() returns [] when notebook response is empty (lines 71-76)."""
        response = build_rpc_response(RPCMethod.GET_NOTEBOOK, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert sources == []

    @pytest.mark.asyncio
    async def test_list_sources_non_list_notebook_entry(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() returns [] when nb_info is not a list (lines 80-85)."""
        # notebook[0] is a string, not a list - fails isinstance(nb_info, list)
        response = build_rpc_response(RPCMethod.GET_NOTEBOOK, ["just_a_string"])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert sources == []

    @pytest.mark.asyncio
    async def test_list_sources_nb_info_missing_index_1(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() returns [] when nb_info list has no index 1 (lines 80-85)."""
        # notebook[0] is a list with only 1 element - len(nb_info) <= 1
        response = build_rpc_response(RPCMethod.GET_NOTEBOOK, [["just_a_title"]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert sources == []

    @pytest.mark.asyncio
    async def test_list_sources_sources_list_not_a_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() returns [] when sources_list is not a list (lines 89-95)."""
        # nb_info[1] is a string - fails isinstance(sources_list, list)
        response = build_rpc_response(RPCMethod.GET_NOTEBOOK, [["Notebook Title", "not_a_list"]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert sources == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("rpc_result", "message"),
        [
            ([], "API response structure changed"),
            (["just_a_string"], "API response structure changed"),
            ([["just_a_title"]], "API response structure changed"),
            ([["Notebook Title", None]], "sources data is NoneType, not list"),
        ],
    )
    async def test_list_sources_strict_raises_for_malformed_response(
        self,
        rpc_result,
        message,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Strict mode exposes malformed source-list responses to callers
        that must distinguish API-structure failures from empty notebooks.
        """
        response = build_rpc_response(RPCMethod.GET_NOTEBOOK, rpc_result)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(RPCError, match=message):
                await client.sources.list("nb_123", strict=True)


class TestListSourcesParsingEdgeCases:
    """Tests for list() per-source parsing edge cases (lines 100-143)."""

    @pytest.mark.asyncio
    async def test_list_sources_src_id_not_nested(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() uses src[0] directly when src[0] is not a list (line 102)."""
        # src[0] is a plain string (not a list), so src_id = src[0] directly
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook Title",
                    [["plain_src_id", "Source Title", [None, 0], [None, 2]]],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        assert sources[0].id == "plain_src_id"

    @pytest.mark.asyncio
    async def test_list_sources_url_list_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() handles empty URL list at src[2][7] - url stays None (lines 109->113)."""
        # src[2][7] is an empty list - URL should remain None
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook Title",
                    [
                        [
                            ["src_001"],
                            "Source Title",
                            [None, 11, [1704067200, 0], None, 5, None, None, []],
                            [None, 2],
                        ]
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        assert sources[0].url is None

    @pytest.mark.asyncio
    async def test_list_sources_timestamp_invalid(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() handles invalid timestamp gracefully (lines 119-120)."""
        # timestamp_list[0] is None, causing TypeError in datetime.fromtimestamp
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook Title",
                    [
                        [
                            ["src_001"],
                            "Source Title",
                            [None, 11, [None], None, 5],
                            [None, 2],
                        ]
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        assert sources[0].created_at is None

    @pytest.mark.asyncio
    async def test_list_sources_invalid_status_code(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() uses default READY when status_code is unknown (lines 125->137, 127->137)."""
        # status_code 999 is not in the SourceStatus enum values
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook Title",
                    [
                        [
                            ["src_001"],
                            "Source Title",
                            [None, 11, [1704067200, 0], None, 5],
                            [None, 999],
                        ]
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        from notebooklm.rpc.types import SourceStatus

        assert sources[0].status == SourceStatus.READY

    @pytest.mark.asyncio
    async def test_list_sources_type_code_not_int(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() skips type_code when src[2][4] is not an int (lines 140->143)."""
        # src[2][4] is a string - type_code should stay None
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook Title",
                    [
                        [
                            ["src_001"],
                            "Source Title",
                            [None, 11, [1704067200, 0], None, "not_an_int"],
                            [None, 2],
                        ]
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        assert sources[0]._type_code is None


class TestWaitForSources:
    """Tests for wait_for_sources() parallel waiting (lines 278-281, 455)."""

    @pytest.mark.asyncio
    async def test_wait_for_sources_parallel(
        self,
        auth_tokens,
    ):
        """Test wait_for_sources() calls wait_until_ready for each source in parallel."""
        ready_source_1 = Source(id="src_1", title="Source 1")
        ready_source_2 = Source(id="src_2", title="Source 2")

        async with NotebookLMClient(auth_tokens) as client:
            call_count = 0

            async def mock_wait(notebook_id, source_id, **kwargs):
                nonlocal call_count
                call_count += 1
                if source_id == "src_1":
                    return ready_source_1
                return ready_source_2

            with patch.object(client.sources, "wait_until_ready", side_effect=mock_wait):
                results = await client.sources.wait_for_sources("nb_123", ["src_1", "src_2"])

        assert len(results) == 2
        assert call_count == 2
        assert results[0].id == "src_1"
        assert results[1].id == "src_2"


class TestAddUrlErrorPaths:
    """Tests for add_url() error paths (lines 320, 327-329, 332, 336)."""

    @pytest.mark.asyncio
    async def test_add_url_rpc_error_raises_source_add_error(
        self,
        auth_tokens,
    ):
        """Test add_url() wraps RPCError from _add_url_source in SourceAddError (lines 327-329)."""
        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources,
                "_add_url_source",
                side_effect=RPCError("RPC call failed"),
            ):
                with pytest.raises(SourceAddError):
                    await client.sources.add_url("nb_123", "https://example.com")

    @pytest.mark.asyncio
    async def test_add_url_youtube_rpc_error_raises_source_add_error(
        self,
        auth_tokens,
    ):
        """Test add_url() wraps RPCError from _add_youtube_source in SourceAddError (lines 327-329)."""
        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources,
                "_add_youtube_source",
                side_effect=RPCError("YouTube RPC failed"),
            ):
                with pytest.raises(SourceAddError):
                    await client.sources.add_url(
                        "nb_123", "https://youtube.com/watch?v=dQw4w9WgXcQ"
                    )

    @pytest.mark.asyncio
    async def test_add_url_none_result_raises_source_add_error(
        self,
        auth_tokens,
    ):
        """Test add_url() raises SourceAddError when API returns None (line 332)."""
        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources,
                "_add_url_source",
                new_callable=AsyncMock,
                return_value=None,
            ):
                with pytest.raises(SourceAddError, match="API returned no data"):
                    await client.sources.add_url("nb_123", "https://example.com")

    @pytest.mark.asyncio
    async def test_add_url_wait_true(
        self,
        auth_tokens,
    ):
        """Test add_url() with wait=True calls wait_until_ready (lines 335-336)."""
        source_data = [[[["src_wait_url"], "Example", [None, 11], [None, 2]]]]
        ready_source = Source(id="src_wait_url", title="Example")

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources,
                "_add_url_source",
                new_callable=AsyncMock,
                return_value=source_data,
            ):
                with patch.object(
                    client.sources,
                    "wait_until_ready",
                    new_callable=AsyncMock,
                    return_value=ready_source,
                ) as mock_wait:
                    result = await client.sources.add_url(
                        "nb_123", "https://example.com", wait=True
                    )

        mock_wait.assert_called_once()
        assert result.id == "src_wait_url"

    @pytest.mark.asyncio
    async def test_add_url_youtube_like_no_id_warning(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test add_url() warns when URL looks like YouTube but has no video ID (line 320)."""
        response = build_rpc_response(
            RPCMethod.ADD_SOURCE,
            [[[["src_channel"], "YouTube Channel", [None, 11], [None, 2]]]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with patch("notebooklm._sources.is_youtube_url", return_value=True) as mock_is_yt:
                with patch.object(
                    client.sources,
                    "_extract_youtube_video_id",
                    return_value=None,
                ):
                    source = await client.sources.add_url(
                        "nb_123", "https://youtube.com/channel/UCxxxxxxx"
                    )

        assert source is not None
        mock_is_yt.assert_called_once()


class TestAddTextErrorPaths:
    """Tests for add_text() error paths (lines 374-375, 382, 387)."""

    @pytest.mark.asyncio
    async def test_add_text_rpc_error_raises_source_add_error(
        self,
        auth_tokens,
    ):
        """Test add_text() wraps RPCError in SourceAddError (lines 374-375)."""
        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources._core,
                "rpc_call",
                side_effect=RPCError("Text RPC failed"),
            ):
                with pytest.raises(SourceAddError, match="Failed to add text source"):
                    await client.sources.add_text("nb_123", "My Title", "content")

    @pytest.mark.asyncio
    async def test_add_text_none_result_raises_source_add_error(
        self,
        auth_tokens,
    ):
        """Test add_text() raises SourceAddError when API returns None (line 382)."""
        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources._core,
                "rpc_call",
                new_callable=AsyncMock,
                return_value=None,
            ):
                with pytest.raises(SourceAddError, match="API returned no data"):
                    await client.sources.add_text("nb_123", "My Title", "content")

    @pytest.mark.asyncio
    async def test_add_text_wait_true(
        self,
        auth_tokens,
    ):
        """Test add_text() with wait=True calls wait_until_ready (line 387)."""
        source_data = [[[["src_wait_text"], "My Title", [None, 0], [None, 2]]]]
        ready_source = Source(id="src_wait_text", title="My Title")

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources._core,
                "rpc_call",
                new_callable=AsyncMock,
                return_value=source_data,
            ):
                with patch.object(
                    client.sources,
                    "wait_until_ready",
                    new_callable=AsyncMock,
                    return_value=ready_source,
                ) as mock_wait:
                    result = await client.sources.add_text(
                        "nb_123", "My Title", "content", wait=True
                    )

        mock_wait.assert_called_once()
        assert result.id == "src_wait_text"


class TestAddFileWait:
    """Tests for add_file() with wait=True (lines 454-455)."""

    @pytest.mark.asyncio
    async def test_add_file_wait_true(
        self,
        auth_tokens,
        tmp_path,
    ):
        """Test add_file() with wait=True calls wait_until_ready (lines 454-455)."""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake content")

        ready_source = Source(id="file_src_001", title="test.pdf")

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources,
                "_register_file_source",
                new_callable=AsyncMock,
                return_value="file_src_001",
            ):
                with patch.object(
                    client.sources,
                    "_start_resumable_upload",
                    new_callable=AsyncMock,
                    return_value="https://upload.example.com/upload_id=abc",
                ):
                    with patch.object(
                        client.sources,
                        "_upload_file_streaming",
                        new_callable=AsyncMock,
                    ):
                        with patch.object(
                            client.sources,
                            "wait_until_ready",
                            new_callable=AsyncMock,
                            return_value=ready_source,
                        ) as mock_wait:
                            result = await client.sources.add_file("nb_123", test_file, wait=True)

        mock_wait.assert_called_once_with("nb_123", "file_src_001", timeout=120.0)
        assert result.id == "file_src_001"


class TestAddDriveWait:
    """Tests for add_drive() with wait=True (line 526)."""

    @pytest.mark.asyncio
    async def test_add_drive_wait_true(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test add_drive() with wait=True calls wait_until_ready (line 526)."""
        response = build_rpc_response(
            RPCMethod.ADD_SOURCE,
            [[[["drive_src_wait"], "My Drive Doc", [None, 0], [None, 2]]]],
        )
        httpx_mock.add_response(content=response.encode())

        ready_source = Source(id="drive_src_wait", title="My Drive Doc")

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources,
                "wait_until_ready",
                new_callable=AsyncMock,
                return_value=ready_source,
            ) as mock_wait:
                result = await client.sources.add_drive(
                    "nb_123",
                    file_id="drive_file_abc",
                    title="My Drive Doc",
                    wait=True,
                )

        mock_wait.assert_called_once()
        assert result.id == "drive_src_wait"


class TestCheckFreshnessEdgeCases:
    """Tests for check_freshness() edge cases (lines 624, 657->667, 659->667)."""

    @pytest.mark.asyncio
    async def test_check_freshness_none_result_returns_false(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test check_freshness() returns False when result is None (line 624)."""
        # None is not True, not False, not a list - falls through to return False
        response = build_rpc_response(RPCMethod.CHECK_SOURCE_FRESHNESS, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is False

    @pytest.mark.asyncio
    async def test_check_freshness_list_first_element_not_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test check_freshness() returns False when result list's first item is not a list (line 624)."""
        # result is ["some_string"] - first is a string, isinstance(first, list) is False
        response = build_rpc_response(RPCMethod.CHECK_SOURCE_FRESHNESS, ["some_value"])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is False

    @pytest.mark.asyncio
    async def test_check_freshness_drive_nested_false_value(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test check_freshness() returns False when nested Drive structure has first[1] != True (lines 657->667, 659->667)."""
        # result is [[None, False, ...]] - first[1] is False, not True
        response = build_rpc_response(
            RPCMethod.CHECK_SOURCE_FRESHNESS, [[None, False, ["src_001"]]]
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is False

    @pytest.mark.asyncio
    async def test_check_freshness_drive_nested_list_too_short(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test check_freshness() returns False when nested Drive list has only one element."""
        # result is [[None]] - first has only 1 element, so len(first) > 1 check fails
        response = build_rpc_response(RPCMethod.CHECK_SOURCE_FRESHNESS, [[None]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is False


class TestGetFulltextEdgeCases:
    """Tests for get_fulltext() URL/type parsing and empty content (lines 700, 708->732, 764-765)."""

    @pytest.mark.asyncio
    async def test_get_fulltext_with_source_type_and_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() parses source_type and url from result[0][2] (lines 708-732)."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                [
                    "src_123",
                    "Web Article",
                    # result[0][2]: index 4=source_type(5), index 7=[url]
                    [None, 0, None, None, 5, None, 1, ["https://example.com/article"]],
                ],
                None,
                None,
                [[["The main content of the article."]]],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_123")

        assert fulltext.title == "Web Article"
        assert fulltext.url == "https://example.com/article"
        assert fulltext._type_code == 5
        assert "main content" in fulltext.content

    @pytest.mark.asyncio
    async def test_get_fulltext_youtube_url_at_index_5(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Regression: YouTube fulltext metadata stores URL at result[0][2][5][0]."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                [
                    "src_yt",
                    "YouTube Video",
                    [
                        None,
                        0,
                        None,
                        None,
                        9,
                        [
                            "https://www.youtube.com/watch?v=dcWU-qD8ISQ",
                            "dcWU-qD8ISQ",
                            "john newquist",
                        ],
                        None,
                        None,
                    ],
                ],
                None,
                None,
                [[["Transcript content."]]],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_yt")

        assert fulltext._type_code == 9
        assert fulltext.url == "https://www.youtube.com/watch?v=dcWU-qD8ISQ"

    @pytest.mark.asyncio
    async def test_get_fulltext_source_type_only_empty_url_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() parses type_code when result[0][2][7] is empty list (lines 714->725, 720->725)."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                [
                    "src_pdf",
                    "PDF Document",
                    # result[0][2]: index 4=3 (PDF), index 7=[] (empty url list)
                    [None, 0, None, None, 3, None, None, []],
                ],
                None,
                None,
                [[["PDF text content here."]]],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_pdf")

        assert fulltext._type_code == 3
        assert fulltext.url is None
        assert "PDF text content" in fulltext.content

    @pytest.mark.asyncio
    async def test_get_fulltext_no_content_logs_warning(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() logs warning when content is empty (lines 764-765 via 731-738)."""
        # result[3][0] is an empty list -> texts = [] -> content = ""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                ["src_no_content", "No Content Source", [None, 0, None, None, 4]],
                None,
                None,
                [[]],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        import logging

        with patch.object(
            logging.getLogger("notebooklm._sources"),
            "warning",
        ) as mock_warn:
            async with NotebookLMClient(auth_tokens) as client:
                fulltext = await client.sources.get_fulltext("nb_123", "src_no_content")

        assert fulltext.content == ""
        assert fulltext.char_count == 0
        mock_warn.assert_called()

    @pytest.mark.asyncio
    async def test_get_fulltext_result_not_list_raises_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() raises SourceNotFoundError when result is None (line 700)."""
        response = build_rpc_response(RPCMethod.GET_SOURCE, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(SourceNotFoundError):
                await client.sources.get_fulltext("nb_123", "missing_src")

    @pytest.mark.asyncio
    async def test_get_fulltext_result_0_2_short_no_type(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() when result[0][2] has fewer than 5 elements - no type_code (lines 710->725, 714->725)."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                # result[0][2] has only 3 elements - no index 4 for type_code
                ["src_short", "Short Meta", [None, 0, None]],
                None,
                None,
                [[["Some content."]]],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_short")

        assert fulltext._type_code is None
        assert fulltext.url is None
        assert "Some content" in fulltext.content

    @pytest.mark.asyncio
    async def test_get_fulltext_result_0_2_has_type_no_url_field(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() when result[0][2] has type at [4] but no [7] (lines 719-720 branch)."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                # result[0][2] has 6 elements - type at [4] but no [7]
                ["src_no_url", "No URL Source", [None, 0, None, None, 9, None]],
                None,
                None,
                [[["YouTube content here."]]],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_no_url")

        assert fulltext._type_code == 9
        assert fulltext.url is None

    @pytest.mark.asyncio
    async def test_get_fulltext_result3_missing(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() when result has fewer than 4 elements - no content (lines 727->732 branch not taken)."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            # Only 2 elements - no result[3]
            [
                ["src_no_r3", "No Result3", [None, 0, None, None, 4]],
                None,
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_no_r3")

        assert fulltext.content == ""


class TestExtractAllText:
    """Tests for _extract_all_text() max_depth guard (lines 763-765)."""

    @pytest.mark.asyncio
    async def test_extract_all_text_max_depth_zero_returns_empty(
        self,
        auth_tokens,
    ):
        """Test _extract_all_text() returns [] when max_depth=0 (lines 763-765)."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_all_text(["some", "text"], max_depth=0)

        assert result == []

    @pytest.mark.asyncio
    async def test_extract_all_text_nested_lists(
        self,
        auth_tokens,
    ):
        """Test _extract_all_text() recursively extracts text from nested arrays."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_all_text([["hello", ["world"]], "foo", [[], "bar"]])

        assert result == ["hello", "world", "foo", "bar"]


class TestExtractYoutubeVideoId:
    """Tests for _extract_youtube_video_id() edge cases (lines 810-819, 832-852)."""

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_youtu_be_no_path(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() returns None for youtu.be with empty path (line 836)."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id("https://youtu.be/")

        assert result is None

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_non_youtube_domain(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() returns None for non-YouTube URL."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id("https://example.com/video")

        assert result is None

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_invalid_id_chars(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() returns None when video ID has invalid chars (line 812->815)."""
        # URL with invalid video ID characters that fail _is_valid_video_id
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id(
                "https://youtube.com/watch?v=invalid id!"
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_parse_error(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() handles exceptions gracefully (lines 817-819)."""
        async with NotebookLMClient(auth_tokens) as client:
            # Patching urlparse to raise ValueError covers the except block
            with patch("notebooklm._sources.urlparse", side_effect=ValueError("parse error")):
                result = client.sources._extract_youtube_video_id(
                    "https://youtube.com/watch?v=abc123"
                )

        assert result is None

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_watch_url(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() extracts ID from standard watch URL (lines 845-850)."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id(
                "https://youtube.com/watch?v=dQw4w9WgXcQ"
            )

        assert result == "dQw4w9WgXcQ"

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_no_query_param(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() returns None for youtube.com URL without v param (line 852)."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id(
                "https://youtube.com/playlist?list=PLxxxxxxxx"
            )

        assert result is None


class TestListSourcesSkippedEntries:
    """Tests for list() skipping non-list source entries (line 100->99)."""

    @pytest.mark.asyncio
    async def test_list_sources_skips_non_list_entry(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() skips source entries that are not lists (line 100->99)."""
        # Mix of valid and invalid source entries
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook Title",
                    [
                        # Valid source entry
                        [["src_001"], "Valid Source", [None, 0], [None, 2]],
                        # Non-list entry - should be skipped
                        "not_a_list",
                        # Another non-list - should be skipped
                        None,
                        # Valid source entry
                        [["src_002"], "Also Valid", [None, 0], [None, 2]],
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 2
        assert sources[0].id == "src_001"
        assert sources[1].id == "src_002"

    @pytest.mark.asyncio
    async def test_list_sources_no_status_data(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test list() defaults to READY when src has no index 3 (line 125->137 false branch)."""
        from notebooklm.rpc.types import SourceStatus

        # Source with only 2 elements - no status data at index 3
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook Title",
                    [
                        [["src_001"], "No Status Source"],
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            sources = await client.sources.list("nb_123")

        assert len(sources) == 1
        assert sources[0].status == SourceStatus.READY


class TestWaitUntilReady:
    """Tests for wait_until_ready() implementation (lines 214-244)."""

    @pytest.mark.asyncio
    async def test_wait_until_ready_source_ready_immediately(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test wait_until_ready() returns immediately when source is already READY (line 231-232)."""
        # Mock GET_NOTEBOOK to return a source with READY status (status_code=2)
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook",
                    [
                        [
                            ["src_ready"],
                            "Ready Source",
                            [None, 0],
                            [None, 2],  # status=READY
                        ]
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.wait_until_ready("nb_123", "src_ready")

        assert source.id == "src_ready"

    @pytest.mark.asyncio
    async def test_wait_until_ready_source_not_found_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test wait_until_ready() raises SourceNotFoundError when source is None (line 226-227)."""
        # Mock GET_NOTEBOOK to return no sources (so get() returns None)
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [["Notebook", [], "nb_123"]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(SourceNotFoundError):
                await client.sources.wait_until_ready("nb_123", "src_missing")

    @pytest.mark.asyncio
    async def test_wait_until_ready_timeout_raises(
        self,
        auth_tokens,
    ):
        """Test wait_until_ready() raises SourceTimeoutError when timeout=0 (line 221-222)."""
        from notebooklm.types import SourceTimeoutError

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(SourceTimeoutError):
                await client.sources.wait_until_ready("nb_123", "src_timeout", timeout=0.0)


class TestAddFileValidation:
    """Tests for add_file() validation errors (line 429)."""

    @pytest.mark.asyncio
    async def test_add_file_directory_raises_validation_error(
        self,
        auth_tokens,
        tmp_path,
    ):
        """Test add_file() raises ValidationError when path is a directory (line 429)."""
        from notebooklm.exceptions import ValidationError

        # tmp_path is a directory, not a file
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Not a regular file"):
                await client.sources.add_file("nb_123", tmp_path)


class TestGetGuideEdgeCases:
    """Tests for get_guide() inner parsing branches (lines 655->667, 657->667, 659->667)."""

    @pytest.mark.asyncio
    async def test_get_guide_outer_not_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_guide() returns empty when outer result[0] is not a list (line 657->667)."""
        # result[0] is a string, not a list
        response = build_rpc_response(RPCMethod.GET_SOURCE_GUIDE, ["not_a_list"])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            guide = await client.sources.get_guide("nb_123", "src_001")

        assert guide["summary"] == ""
        assert guide["keywords"] == []

    @pytest.mark.asyncio
    async def test_get_guide_inner_not_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_guide() returns empty when inner outer[0] is not a list (line 659->667)."""
        # outer is a list but outer[0] is a string, not a list
        response = build_rpc_response(RPCMethod.GET_SOURCE_GUIDE, [["not_a_list"]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            guide = await client.sources.get_guide("nb_123", "src_001")

        assert guide["summary"] == ""
        assert guide["keywords"] == []

    @pytest.mark.asyncio
    async def test_get_guide_result_false(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_guide() returns empty when result is falsy (line 655->667)."""
        response = build_rpc_response(RPCMethod.GET_SOURCE_GUIDE, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            guide = await client.sources.get_guide("nb_123", "src_001")

        assert guide["summary"] == ""
        assert guide["keywords"] == []


class TestGetFulltextResult0Branches:
    """Tests for get_fulltext() result[0] parsing branches (lines 710->725, 714->725, 727->732)."""

    @pytest.mark.asyncio
    async def test_get_fulltext_result_0_not_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() when result[0] is not a list - title stays empty (line 710->725)."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            # result[0] is a string, not a list
            ["just_a_string"],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_no_list")

        assert fulltext.title == ""
        assert fulltext._type_code is None

    @pytest.mark.asyncio
    async def test_get_fulltext_result_0_2_not_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() when result[0][2] is not a list (line 714->725)."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                # result[0][2] is a string, not a list
                ["src_x", "My Title", "not_a_list"],
                None,
                None,
                [[["Some text content."]]],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_x")

        assert fulltext.title == "My Title"
        assert fulltext._type_code is None
        assert fulltext.url is None
        assert "Some text content" in fulltext.content

    @pytest.mark.asyncio
    async def test_get_fulltext_content_blocks_not_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_fulltext() when content_blocks (result[3][0]) is not a list (line 727->732)."""
        response = build_rpc_response(
            RPCMethod.GET_SOURCE,
            [
                ["src_y", "Source Y", [None, 0, None, None, 4]],
                None,
                None,
                # result[3][0] is a string, not a list
                ["just_a_string"],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_123", "src_y")

        assert fulltext.content == ""


class TestExtractVideoIdFromParsedUrl:
    """Tests for _extract_video_id_from_parsed_url() branches (lines 835, 843, 846->852)."""

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_youtu_be_with_path(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() extracts ID from youtu.be short URL (line 835)."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ")

        assert result == "dQw4w9WgXcQ"

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_shorts_url(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() extracts ID from YouTube Shorts URL (line 843)."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id(
                "https://youtube.com/shorts/dQw4w9WgXcQ"
            )

        assert result == "dQw4w9WgXcQ"

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_embed_url(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() extracts ID from embed URL (line 843)."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id(
                "https://youtube.com/embed/dQw4w9WgXcQ"
            )

        assert result == "dQw4w9WgXcQ"

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_watch_with_valid_id(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() via watch URL hits query param branch (lines 846->850)."""
        async with NotebookLMClient(auth_tokens) as client:
            result = client.sources._extract_youtube_video_id(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s"
            )

        assert result == "dQw4w9WgXcQ"


class TestAddYoutubeSourceDirect:
    """Tests for _add_youtube_source() implementation (lines 870-876)."""

    @pytest.mark.asyncio
    async def test_add_url_youtube_video_success(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test add_url() with YouTube URL calls _add_youtube_source internally (lines 870-876)."""
        response = build_rpc_response(
            RPCMethod.ADD_SOURCE,
            [
                [
                    [
                        ["yt_src_001"],
                        "Rick Astley - Never Gonna Give You Up",
                        [
                            None,
                            11,
                            None,
                            None,
                            9,
                            None,
                            1,
                            ["https://youtube.com/watch?v=dQw4w9WgXcQ"],
                        ],
                        [None, 2],
                    ]
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.add_url(
                "nb_123", "https://youtube.com/watch?v=dQw4w9WgXcQ"
            )

        assert source.id == "yt_src_001"
        assert source.kind == "youtube"


class TestRegisterFileSourceError:
    """Tests for _register_file_source() error paths (lines 925, 931)."""

    @pytest.mark.asyncio
    async def test_register_file_source_none_result_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test _register_file_source() raises when result has no extractable ID (line 931)."""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake")

        # Return a response where extract_id returns None - nested empty list
        rpc_response = build_rpc_response(
            RPCMethod.ADD_SOURCE_FILE,
            # Result is an empty list - extract_id([]) returns None
            [],
        )
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*"),
            content=rpc_response.encode(),
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(SourceAddError, match="Failed to get SOURCE_ID"):
                await client.sources.add_file("nb_123", test_file)

    @pytest.mark.asyncio
    async def test_register_file_source_nested_empty_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test _register_file_source() when extract_id hits base case None (line 925)."""
        test_file = tmp_path / "empty_nested.pdf"
        test_file.write_bytes(b"%PDF-1.4 content")

        # Result is [[[]]] - extract_id([[]]) -> extract_id([]) -> returns None
        rpc_response = build_rpc_response(
            RPCMethod.ADD_SOURCE_FILE,
            [[[]]],
        )
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*"),
            content=rpc_response.encode(),
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(SourceAddError):
                await client.sources.add_file("nb_123", test_file)


class TestStartResumableUploadError:
    """Tests for _start_resumable_upload() missing upload URL (line 971)."""

    @pytest.mark.asyncio
    async def test_start_resumable_upload_missing_url_header(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test _start_resumable_upload() raises when response has no upload URL header (line 971)."""
        test_file = tmp_path / "no_url.pdf"
        test_file.write_bytes(b"%PDF-1.4 content")

        # Step 1: Successful RPC registration
        rpc_response = build_rpc_response(
            RPCMethod.ADD_SOURCE_FILE,
            [[[["file_src_001"], "no_url.pdf", [None, None, None, None, 0]]]],
        )
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*"),
            content=rpc_response.encode(),
        )

        # Step 2: Upload session response WITHOUT the required upload URL header
        httpx_mock.add_response(
            url=re.compile(r".*upload/_/\?authuser=0$"),
            headers={},  # No x-goog-upload-url header
            content=b"",
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(SourceAddError, match="Failed to get upload URL"):
                await client.sources.add_file("nb_123", test_file)


class TestWaitUntilReadyPolling:
    """Tests for wait_until_ready() polling loop (lines 234-244)."""

    @pytest.mark.asyncio
    async def test_wait_until_ready_polls_until_ready(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test wait_until_ready() polls and returns when source transitions to READY (lines 234-244)."""
        # First response: source is PROCESSING (status=1)
        processing_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook",
                    [
                        [
                            ["src_polling"],
                            "Processing Source",
                            [None, 0],
                            [None, 1],  # status=PROCESSING
                        ]
                    ],
                    "nb_123",
                ]
            ],
        )
        # Second response: source is READY (status=2)
        ready_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook",
                    [
                        [
                            ["src_polling"],
                            "Processing Source",
                            [None, 0],
                            [None, 2],  # status=READY
                        ]
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=processing_response.encode())
        httpx_mock.add_response(content=ready_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.wait_until_ready(
                "nb_123", "src_polling", initial_interval=0.01, timeout=30.0
            )

        assert source.id == "src_polling"


class TestExtractVideoIdNoQuery:
    """Tests for _extract_video_id_from_parsed_url() no-query branch (line 846->852)."""

    @pytest.mark.asyncio
    async def test_extract_youtube_video_id_no_query_string(
        self,
        auth_tokens,
    ):
        """Test _extract_youtube_video_id() returns None for youtube.com URL with no query at all (line 846->852)."""
        async with NotebookLMClient(auth_tokens) as client:
            # youtube.com URL with no path prefix and no query string
            result = client.sources._extract_youtube_video_id("https://youtube.com/")

        assert result is None


class TestWaitUntilReadyErrorPaths:
    """Tests for wait_until_ready() error state and mid-loop timeout (lines 234-244)."""

    @pytest.mark.asyncio
    async def test_wait_until_ready_source_error_raises_processing_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test wait_until_ready() raises SourceProcessingError when source is in ERROR state (line 235)."""
        from notebooklm.types import SourceProcessingError

        # Source has ERROR status (status=3). Use a non-audio terminal type
        # (3 = PDF) at metadata[4] so the audio-tolerance gate (#391) does
        # not keep polling. Audio (type 10) and unclassified types can briefly
        # report status=3 transiently; PDF cannot.
        response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Notebook",
                    [
                        [
                            ["src_error"],
                            "Failed Source",
                            [None, 0, None, None, 3],  # metadata[4]=3 (PDF)
                            [None, 3],  # status=ERROR
                        ]
                    ],
                    "nb_123",
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(SourceProcessingError):
                await client.sources.wait_until_ready("nb_123", "src_error")

    @pytest.mark.asyncio
    async def test_wait_until_ready_timeout_mid_loop(
        self,
        auth_tokens,
    ):
        """Test wait_until_ready() raises SourceTimeoutError when timeout expires mid-loop (line 240)."""
        from notebooklm.types import SourceTimeoutError

        # A PROCESSING source that never becomes ready
        processing_source = Source(id="src_slow", title="Slow Source", status=1)

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources,
                "get",
                new_callable=AsyncMock,
                return_value=processing_source,
            ):
                with pytest.raises(SourceTimeoutError):
                    await client.sources.wait_until_ready(
                        "nb_123",
                        "src_slow",
                        timeout=0.05,  # Very short timeout so polling loop hits elapsed > timeout
                        initial_interval=0.001,
                    )


class TestWaitUntilReadyMidLoopTimeout:
    """Test for the mid-loop timeout in wait_until_ready() (line 240)."""

    @pytest.mark.asyncio
    async def test_wait_until_ready_remaining_zero_after_get(
        self,
        auth_tokens,
    ):
        """Test wait_until_ready() raises SourceTimeoutError at remaining<=0 check (line 240).

        This covers the case where elapsed < timeout at the top of the loop,
        but by the time we check remaining after get(), the timeout has expired.
        """
        from notebooklm.types import SourceTimeoutError

        # A PROCESSING source
        processing_source = Source(id="src_race", title="Race Source", status=1)
        timeout_val = 1.0
        # Simulate monotonic times: first call (start), second call (elapsed check in loop),
        # third call (remaining check after get) - make third > timeout
        # start=0.0, loop check: elapsed=0.5 < 1.0 (ok), remaining check: elapsed=1.5 > 1.0
        monotonic_values = [0.0, 0.5, 1.5]
        call_idx = 0

        def fake_monotonic():
            nonlocal call_idx
            val = monotonic_values[min(call_idx, len(monotonic_values) - 1)]
            call_idx += 1
            return val

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.sources,
                "get",
                new_callable=AsyncMock,
                return_value=processing_source,
            ):
                with patch("notebooklm._sources.monotonic", side_effect=fake_monotonic):
                    with pytest.raises(SourceTimeoutError):
                        await client.sources.wait_until_ready(
                            "nb_123", "src_race", timeout=timeout_val
                        )
