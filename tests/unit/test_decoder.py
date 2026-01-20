"""Unit tests for RPC response decoder."""

import json

import pytest

from notebooklm.rpc.decoder import (
    RateLimitError,
    RPCError,
    collect_rpc_ids,
    decode_response,
    extract_rpc_result,
    parse_chunked_response,
    strip_anti_xssi,
)
from notebooklm.rpc.types import RPCMethod


class TestStripAntiXSSI:
    def test_strips_prefix(self):
        """Test removal of anti-XSSI prefix."""
        response = ')]}\'\n{"data": "test"}'
        result = strip_anti_xssi(response)
        assert result == '{"data": "test"}'

    def test_no_prefix_unchanged(self):
        """Test response without prefix is unchanged."""
        response = '{"data": "test"}'
        result = strip_anti_xssi(response)
        assert result == response

    def test_handles_windows_newlines(self):
        """Test handles CRLF."""
        response = ')]}\'\r\n{"data": "test"}'
        result = strip_anti_xssi(response)
        assert result == '{"data": "test"}'

    def test_handles_double_newline(self):
        """Test handles double newline after prefix."""
        response = ')]}\'\n\n{"data": "test"}'
        result = strip_anti_xssi(response)
        assert result.startswith("\n{") or result == '{"data": "test"}'


class TestParseChunkedResponse:
    def test_parses_single_chunk(self):
        """Test parsing response with single chunk."""
        chunk_data = ["chunk", "data"]
        chunk_json = json.dumps(chunk_data)
        response = f"{len(chunk_json)}\n{chunk_json}\n"

        chunks = parse_chunked_response(response)

        assert len(chunks) == 1
        assert chunks[0] == ["chunk", "data"]

    def test_parses_multiple_chunks(self):
        """Test parsing response with multiple chunks."""
        chunk1 = json.dumps(["one"])
        chunk2 = json.dumps(["two"])
        response = f"{len(chunk1)}\n{chunk1}\n{len(chunk2)}\n{chunk2}\n"

        chunks = parse_chunked_response(response)

        assert len(chunks) == 2
        assert chunks[0] == ["one"]
        assert chunks[1] == ["two"]

    def test_handles_nested_json(self):
        """Test parsing chunks with nested JSON."""
        inner = json.dumps([["nested", "data"]])
        chunk = ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, inner]
        chunk_json = json.dumps(chunk)
        response = f"{len(chunk_json)}\n{chunk_json}\n"

        chunks = parse_chunked_response(response)

        assert len(chunks) == 1
        assert chunks[0][0] == "wrb.fr"
        assert chunks[0][1] == RPCMethod.LIST_NOTEBOOKS.value

    def test_empty_response(self):
        """Test empty response returns empty list."""
        chunks = parse_chunked_response("")
        assert chunks == []

    def test_whitespace_only_response(self):
        """Test whitespace-only response returns empty list."""
        chunks = parse_chunked_response("   \n\n  ")
        assert chunks == []

    def test_ignores_malformed_chunks(self):
        """Test malformed chunks are ignored when below 10% threshold."""
        # Add 10 valid chunks and 1 malformed = 9% error rate (below 10% threshold)
        valid_chunks = [json.dumps([f"valid{i}"]) for i in range(10)]
        valid_parts = "\n".join([f"{len(c)}\n{c}" for c in valid_chunks])
        response = f"{valid_parts}\n99\nnot-json\n"

        chunks = parse_chunked_response(response)

        assert len(chunks) == 10
        assert chunks[0] == ["valid0"]
        assert chunks[9] == ["valid9"]


class TestExtractRPCResult:
    def test_extracts_result_for_rpc_id(self):
        """Test extracting result for specific RPC ID."""
        inner_data = json.dumps([["notebook1"]])
        chunks = [
            ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, inner_data, None, None],
            ["di", 123],  # Some other chunk type
        ]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result == [["notebook1"]]

    def test_returns_none_if_not_found(self):
        """Test returns None if RPC ID not in chunks."""
        inner_data = json.dumps([])
        chunks = [
            ["wrb.fr", "other_id", inner_data, None, None],
        ]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result is None

    def test_handles_double_encoded_json(self):
        """Test handles JSON string inside JSON (common pattern)."""
        inner_json = json.dumps([["notebook1", "id1"]])
        chunks = [
            ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, inner_json, None, None],
        ]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result == [["notebook1", "id1"]]

    def test_handles_non_json_string_result(self):
        """Test handles string results that aren't JSON."""
        chunks = [
            ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, "plain string result", None, None],
        ]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result == "plain string result"

    def test_raises_on_error_chunk(self):
        """Test raises RPCError for error chunks."""
        chunks = [
            ["er", RPCMethod.LIST_NOTEBOOKS.value, "Some error message", None, None],
        ]

        with pytest.raises(RPCError, match="Some error message"):
            extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)

    def test_handles_numeric_error_code(self):
        """Test handles numeric error codes."""
        chunks = [
            ["er", RPCMethod.LIST_NOTEBOOKS.value, 403, None, None],
        ]

        with pytest.raises(RPCError):
            extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)

    def test_raises_on_user_displayable_error(self):
        """Test raises RateLimitError when UserDisplayableError is embedded in response.

        Google's API returns this pattern for rate limiting, quota exceeded,
        and other user-facing restrictions.
        """
        # Real-world structure from API rate limit response
        error_info = [
            8,
            None,
            [
                [
                    "type.googleapis.com/google.internal.labs.tailwind.orchestration.v1.UserDisplayableError",
                    [None, None, None, None, [None, [[1]], 2]],
                ]
            ],
        ]
        chunks = [
            [
                "wrb.fr",
                RPCMethod.LIST_NOTEBOOKS.value,
                None,  # null result
                None,
                None,
                error_info,
                "generic",
            ]
        ]

        with pytest.raises(RateLimitError, match="rate limit"):
            extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)

    def test_user_displayable_error_sets_code(self):
        """Test UserDisplayableError sets code to USER_DISPLAYABLE_ERROR."""
        error_info = [8, None, [["UserDisplayableError", []]]]
        chunks = [["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, error_info]]

        with pytest.raises(RateLimitError) as exc_info:
            extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)

        assert exc_info.value.code == "USER_DISPLAYABLE_ERROR"

    def test_null_result_without_error_info_returns_none(self):
        """Test null result without UserDisplayableError returns None normally."""
        chunks = [["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, None]]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result is None

    def test_null_result_with_non_error_info_returns_none(self):
        """Test null result with non-error data at index 5 returns None."""
        chunks = [["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, [1, 2, 3]]]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result is None

    def test_user_displayable_error_in_dict_structure(self):
        """Test UserDisplayableError detection in dictionary structures.

        While the batchexecute protocol typically uses arrays, this ensures
        robustness if dict structures ever appear.
        """
        error_info = {
            "type": "type.googleapis.com/google.internal.labs.tailwind.orchestration.v1.UserDisplayableError",
            "details": {"code": 1},
        }
        chunks = [["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, error_info]]

        with pytest.raises(RateLimitError, match="rate limit"):
            extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)


class TestDecodeResponse:
    def test_full_decode_pipeline(self):
        """Test complete decode from raw response to result."""
        inner_data = json.dumps([["My Notebook", "nb_123"]])
        chunk = json.dumps(["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, inner_data, None, None])
        raw_response = f")]}}'\n{len(chunk)}\n{chunk}\n"

        result = decode_response(raw_response, RPCMethod.LIST_NOTEBOOKS.value)

        assert result == [["My Notebook", "nb_123"]]

    def test_decode_raises_on_missing_result(self):
        """Test decode raises if RPC ID not found."""
        inner_data = json.dumps([])
        chunk = json.dumps(["wrb.fr", "other_id", inner_data, None, None])
        raw_response = f")]}}'\n{len(chunk)}\n{chunk}\n"

        with pytest.raises(RPCError, match="No result found"):
            decode_response(raw_response, RPCMethod.LIST_NOTEBOOKS.value)

    def test_decode_with_error_response(self):
        """Test decode when response contains error."""
        chunk = json.dumps(["er", RPCMethod.LIST_NOTEBOOKS.value, "Authentication failed", None])
        raw_response = f")]}}'\n{len(chunk)}\n{chunk}\n"

        with pytest.raises(RPCError, match="Authentication failed"):
            decode_response(raw_response, RPCMethod.LIST_NOTEBOOKS.value)

    def test_decode_complex_nested_data(self):
        """Test decoding complex nested data structures."""
        data = {"notebooks": [{"id": "nb1", "title": "Test", "sources": [{"id": "s1"}]}]}
        inner = json.dumps(data)
        chunk = json.dumps(["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, inner, None, None])
        raw_response = f")]}}'\n{len(chunk)}\n{chunk}\n"

        result = decode_response(raw_response, RPCMethod.LIST_NOTEBOOKS.value)

        assert result["notebooks"][0]["id"] == "nb1"

    def test_decode_logs_rpc_ids_at_debug_level(self, caplog):
        """Test decode always logs RPC IDs at DEBUG level."""
        import logging

        inner_data = json.dumps([["data"]])
        chunk = json.dumps(["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, inner_data, None, None])
        raw_response = f")]}}'\n{len(chunk)}\n{chunk}\n"

        with caplog.at_level(logging.DEBUG, logger="notebooklm.rpc.decoder"):
            result = decode_response(raw_response, RPCMethod.LIST_NOTEBOOKS.value)

        assert result == [["data"]]
        assert "Looking for RPC ID: wXbhsf" in caplog.text
        assert "Found RPC IDs in response: ['wXbhsf']" in caplog.text

    def test_decode_missing_id_includes_found_ids_in_error(self):
        """Test error includes found_ids when RPC ID not found."""
        inner_data = json.dumps([])
        chunk = json.dumps(["wrb.fr", "NewMethodId", inner_data, None, None])
        raw_response = f")]}}'\n{len(chunk)}\n{chunk}\n"

        with pytest.raises(RPCError) as exc_info:
            decode_response(raw_response, "OldMethodId")

        assert exc_info.value.found_ids == ["NewMethodId"]
        assert "NewMethodId" in str(exc_info.value)
        assert "may have changed" in str(exc_info.value)

    def test_decode_error_response_includes_found_ids(self):
        """Test error response includes found_ids context."""
        chunk = json.dumps(["er", RPCMethod.LIST_NOTEBOOKS.value, "Auth failed", None])
        raw_response = f")]}}'\n{len(chunk)}\n{chunk}\n"

        with pytest.raises(RPCError) as exc_info:
            decode_response(raw_response, RPCMethod.LIST_NOTEBOOKS.value)

        assert exc_info.value.found_ids == [RPCMethod.LIST_NOTEBOOKS.value]


class TestCollectRpcIds:
    def test_collects_single_id(self):
        """Test collecting single RPC ID from chunk."""
        inner_data = json.dumps([])
        chunks = [["wrb.fr", "TestId", inner_data, None, None]]

        ids = collect_rpc_ids(chunks)

        assert ids == ["TestId"]

    def test_collects_multiple_ids(self):
        """Test collecting multiple RPC IDs from chunks."""
        chunk1 = ["wrb.fr", "Id1", json.dumps([]), None, None]
        chunk2 = ["wrb.fr", "Id2", json.dumps([]), None, None]
        chunks = [chunk1, chunk2]

        ids = collect_rpc_ids(chunks)

        assert ids == ["Id1", "Id2"]

    def test_collects_error_ids(self):
        """Test collecting IDs from error chunks."""
        chunks = [["er", "ErrorId", "Error message", None]]

        ids = collect_rpc_ids(chunks)

        assert ids == ["ErrorId"]

    def test_collects_both_success_and_error_ids(self):
        """Test collecting both success and error IDs."""
        chunks = [
            ["wrb.fr", "SuccessId", json.dumps([]), None, None],
            ["er", "ErrorId", "Error", None],
        ]

        ids = collect_rpc_ids(chunks)

        assert ids == ["SuccessId", "ErrorId"]

    def test_empty_chunks(self):
        """Test empty chunks returns empty list."""
        assert collect_rpc_ids([]) == []

    def test_ignores_non_list_chunks(self):
        """Test non-list chunks are ignored."""
        chunks = ["string", 123, None, {"dict": True}]

        ids = collect_rpc_ids(chunks)

        assert ids == []

    def test_ignores_malformed_chunks(self):
        """Test malformed chunks are ignored."""
        chunks = [
            ["wrb.fr"],  # Missing ID
            ["wrb.fr", 123],  # Non-string ID
            [],  # Empty
        ]

        ids = collect_rpc_ids(chunks)

        assert ids == []

    def test_handles_nested_chunks(self):
        """Test handles nested chunk structure."""
        inner_chunk = ["wrb.fr", "NestedId", json.dumps([]), None, None]
        chunks = [[inner_chunk]]

        ids = collect_rpc_ids(chunks)

        assert ids == ["NestedId"]


class TestRPCError:
    def test_found_ids_stored(self):
        """Test found_ids is stored in exception."""
        error = RPCError("message", rpc_id="Id1", found_ids=["Id2", "Id3"])

        assert error.found_ids == ["Id2", "Id3"]
        assert error.rpc_id == "Id1"

    def test_found_ids_defaults_to_empty_list(self):
        """Test found_ids defaults to empty list when not provided."""
        error = RPCError("message")

        assert error.found_ids == []

    def test_found_ids_none_becomes_empty_list(self):
        """Test found_ids=None becomes empty list."""
        error = RPCError("message", found_ids=None)

        assert error.found_ids == []


class TestAuthError:
    def test_auth_error_is_rpc_error_subclass(self):
        """AuthError should be a subclass of RPCError for backwards compatibility."""
        from notebooklm.rpc import AuthError, RPCError

        error = AuthError("Authentication expired")
        assert isinstance(error, RPCError)
        assert isinstance(error, AuthError)

    def test_auth_error_message(self):
        """AuthError should preserve message and attributes."""
        from notebooklm.rpc import AuthError

        error = AuthError("Token expired", rpc_id="abc123")
        assert str(error) == "Token expired"
        assert error.rpc_id == "abc123"
