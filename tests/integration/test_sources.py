"""Integration tests for SourcesAPI."""

import re
import urllib.parse

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient, Source, SourceType
from notebooklm.rpc import RPCMethod


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
        """Test checking freshness - source is fresh."""
        response = build_rpc_response("yR9Yof", True)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            is_fresh = await client.sources.check_freshness("nb_123", "src_001")

        assert is_fresh is True

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
        response = build_rpc_response(
            RPCMethod.GET_SOURCE_GUIDE,
            [
                [
                    None,
                    ["This is a **summary** of the source content..."],
                    [["keyword1", "keyword2", "keyword3"]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            guide = await client.sources.get_guide("nb_123", "src_001")

        assert "summary" in guide
        assert "keywords" in guide
        assert "**summary**" in guide["summary"]

    @pytest.mark.asyncio
    async def test_get_guide_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting guide for source with no AI analysis."""
        response = build_rpc_response(RPCMethod.GET_SOURCE_GUIDE, [[None, [], []]])
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
