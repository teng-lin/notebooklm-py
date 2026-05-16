"""Unit tests for RPC response decoder."""

import json
import logging

import pytest

from notebooklm.exceptions import DecodingError
from notebooklm.rpc.decoder import (
    AuthError,
    ClientError,
    RateLimitError,
    RPCError,
    RPCErrorCode,
    UnknownRPCMethodError,
    collect_rpc_ids,
    decode_response,
    extract_rpc_result,
    get_error_message_for_code,
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
    @staticmethod
    def _chunk_record(data):
        chunk_json = json.dumps(data)
        return f"{len(chunk_json.encode('utf-8'))}\n{chunk_json}"

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
        # Add 10 valid chunk records and 1 malformed record, keeping the record-based
        # error rate below the 10% threshold.
        valid_chunks = [json.dumps([f"valid{i}"]) for i in range(10)]
        valid_parts = "\n".join([f"{len(c)}\n{c}" for c in valid_chunks])
        response = f"{valid_parts}\n99\nnot-json\n"

        chunks = parse_chunked_response(response)

        assert len(chunks) == 10
        assert chunks[0] == ["valid0"]
        assert chunks[9] == ["valid9"]

    def test_logs_debug_but_parses_mismatched_byte_count_with_valid_json(self, caplog):
        """Mismatched byte counts are tolerated when the payload is valid JSON.

        The mismatch is logged at DEBUG (not WARNING) because Google's
        batchexecute declares a count in a different unit than UTF-8 bytes, so
        every multi-chunk live response would otherwise flood CI logs.
        """
        valid_parts = "\n".join(self._chunk_record([f"valid{i}"]) for i in range(10))
        payload = json.dumps(["wrong-size"])
        response = f"{valid_parts}\n{len(payload) + 1}\n{payload}\n"

        with caplog.at_level(logging.DEBUG, logger="notebooklm.rpc.decoder"):
            chunks = parse_chunked_response(response)

        assert chunks == [[f"valid{i}"] for i in range(10)] + [["wrong-size"]]
        mismatch_records = [
            r
            for r in caplog.records
            if r.name == "notebooklm.rpc.decoder" and "declares" in r.message
        ]
        assert len(mismatch_records) == 1
        assert mismatch_records[0].levelno == logging.DEBUG
        assert "payload is" in mismatch_records[0].message

    def test_skips_byte_count_without_payload_below_threshold(self, caplog):
        """A trailing byte-count line without a payload is malformed and skipped."""
        valid_parts = "\n".join(self._chunk_record([f"valid{i}"]) for i in range(10))
        response = f"{valid_parts}\n42\n"

        chunks = parse_chunked_response(response)

        assert chunks == [[f"valid{i}"] for i in range(10)]
        assert "without payload" in caplog.text

    def test_skips_payload_split_across_lines_below_threshold(self):
        """A payload split across lines is treated as truncated malformed input."""
        valid_parts = "\n".join(self._chunk_record([f"valid{i}"]) for i in range(20))
        payload = json.dumps(["split"])
        first_part, second_part = payload[:4], payload[4:]
        response = f"{valid_parts}\n{len(payload)}\n{first_part}\n{second_part}\n"

        chunks = parse_chunked_response(response)

        assert chunks == [[f"valid{i}"] for i in range(20)]

    def test_skips_extra_non_json_lines_before_and_after_valid_chunk(self):
        """Standalone non-JSON lines are skipped while valid chunks are preserved."""
        valid_parts = "\n".join(self._chunk_record([f"valid{i}"]) for i in range(20))
        response = f"noise-before\n{valid_parts}\nnoise-after\n"

        chunks = parse_chunked_response(response)

        assert chunks == [[f"valid{i}"] for i in range(20)]

    def test_error_rate_exactly_ten_percent_is_tolerated(self):
        """The malformed-record threshold is exclusive: exactly 10% does not raise."""
        valid_lines = "\n".join(json.dumps([f"valid{i}"]) for i in range(9))
        response = f"{valid_lines}\nnot-json\n"

        chunks = parse_chunked_response(response)

        assert chunks == [[f"valid{i}"] for i in range(9)]

    def test_error_rate_just_above_ten_percent_raises(self):
        """The parser raises once malformed records exceed 10%."""
        valid_lines = "\n".join(json.dumps([f"valid{i}"]) for i in range(8))
        response = f"{valid_lines}\nnot-json\n"

        with pytest.raises(RPCError, match="Response parsing failed"):
            parse_chunked_response(response)


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

        assert exc_info.value.rpc_code == "USER_DISPLAYABLE_ERROR"

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

    def test_allow_null_preserves_passthrough_for_nonzero_codes(self):
        """allow_null=True callers must not see status-code enrichment.

        REMOVE_RECENTLY_VIEWED legitimately returns `[13]` at index 5 as part
        of a successful no-op response the caller opts into with
        allow_null=True (see tests/cassettes/notebooks_remove_from_recent.yaml).
        Don't raise in that case.
        """
        chunk = json.dumps(
            [
                "wrb.fr",
                RPCMethod.REMOVE_RECENTLY_VIEWED.value,
                None,
                None,
                None,
                [13],
                "generic",
            ]
        )
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"

        result = decode_response(raw, RPCMethod.REMOVE_RECENTLY_VIEWED.value, allow_null=True)
        assert result is None


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
        error = RPCError("message", method_id="Id1", found_ids=["Id2", "Id3"])

        assert error.found_ids == ["Id2", "Id3"]
        assert error.method_id == "Id1"

    def test_found_ids_defaults_to_empty_list(self):
        """Test found_ids defaults to empty list when not provided."""
        error = RPCError("message")

        assert error.found_ids == []

    def test_found_ids_none_becomes_empty_list(self):
        """Test found_ids=None becomes empty list."""
        error = RPCError("message", found_ids=None)

        assert error.found_ids == []


class TestIssue114Reproduction:
    """Reproduce Issue #114: GET_NOTEBOOK 'No result found' scenarios.

    The user reported `notebooklm use` and `notebooklm ask` fail with
    'No result found for RPC ID: rLM1Ne' while `notebooklm list` works.
    These tests prove each distinct server response scenario that can
    trigger this error, and verify improved diagnostic messages.
    """

    RPC_ID = RPCMethod.GET_NOTEBOOK.value

    def _build_raw(self, body: str) -> str:
        """Wrap body in anti-XSSI prefix."""
        return f")]}}'\n{body}"

    # Scenario A: Empty response — no chunks at all
    def test_scenario_a_empty_response(self):
        """Empty response body after anti-XSSI prefix."""
        raw = self._build_raw("")
        with pytest.raises(RPCError, match="response contained no RPC data — 0 chunks parsed"):
            decode_response(raw, self.RPC_ID)

    # Scenario B: Non-RPC JSON — chunks exist but no wrb.fr/er items
    def test_scenario_b_non_rpc_json(self):
        """Response has JSON chunks but none contain RPC data."""
        chunk = json.dumps({"error": "something"})
        body = f"{len(chunk)}\n{chunk}\n"
        raw = self._build_raw(body)
        with pytest.raises(RPCError, match="response contained no RPC data — 1 chunks parsed"):
            decode_response(raw, self.RPC_ID)

    # Scenario C: Null result data — wrb.fr found with matching ID but result is None
    def test_scenario_c_null_result_data(self):
        """wrb.fr item has matching RPC ID but null result data."""
        chunk = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, None])
        body = f"{len(chunk)}\n{chunk}\n"
        raw = self._build_raw(body)
        with pytest.raises(RPCError, match="returned null result data"):
            decode_response(raw, self.RPC_ID)

    # Scenario D: Short item (2 elements) — wrb.fr found but skipped by extract_rpc_result
    def test_scenario_d_short_item(self):
        """wrb.fr item has only 2 elements, skipped by extract_rpc_result."""
        chunk = json.dumps(["wrb.fr", self.RPC_ID])
        body = f"{len(chunk)}\n{chunk}\n"
        raw = self._build_raw(body)
        # Short items are skipped by extract_rpc_result (len < 3),
        # but collect_rpc_ids still finds the ID (len >= 2)
        with pytest.raises(RPCError, match="returned null result data"):
            decode_response(raw, self.RPC_ID)

    def test_all_scenarios_include_method_id(self):
        """All failure scenarios set method_id on the exception."""
        raw_empty = self._build_raw("")
        with pytest.raises(RPCError) as exc_info:
            decode_response(raw_empty, self.RPC_ID)
        assert exc_info.value.method_id == self.RPC_ID

    def test_null_result_includes_found_ids(self):
        """Null result scenario includes found_ids for debugging."""
        chunk = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, None])
        body = f"{len(chunk)}\n{chunk}\n"
        raw = self._build_raw(body)
        with pytest.raises(RPCError) as exc_info:
            decode_response(raw, self.RPC_ID)
        assert self.RPC_ID in exc_info.value.found_ids


class TestNullResultStatusCodeEnrichment:
    """Verify decode_response enriches null-result errors with status codes.

    Issues #114 / #294 saw GET_NOTEBOOK return a wrb.fr entry where result_data
    is null and index 5 carries a bare `[code]`. These tests pin the new
    enrichment path: NOT_FOUND / PERMISSION_DENIED must surface as ClientError
    (so _core.is_auth_error does not misclassify them and retry on a token
    refresh), other codes stay as RPCError, and the hint never contains any
    AUTH_ERROR_PATTERNS substring.
    """

    RPC_ID = RPCMethod.GET_NOTEBOOK.value

    # Must stay in sync with _core.AUTH_ERROR_PATTERNS.
    _AUTH_PATTERNS = ("authentication", "expired", "unauthorized", "login", "re-authenticate")

    def _build_raw(self, error_info: list | None) -> str:
        chunk = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, error_info, "generic"])
        return f")]}}'\n{len(chunk)}\n{chunk}\n"

    def _assert_no_auth_patterns(self, message: str) -> None:
        lower = message.lower()
        for pattern in self._AUTH_PATTERNS:
            assert pattern not in lower, (
                f"Message contains AUTH_ERROR_PATTERN {pattern!r}: would trigger "
                f"spurious auth-refresh retry in _core.is_auth_error: {message!r}"
            )

    def test_not_found_raises_client_error(self):
        """[5] → ClientError with rpc_code=5, 'Not found', authuser hint."""
        with pytest.raises(ClientError) as exc_info:
            decode_response(self._build_raw([5]), self.RPC_ID)

        assert exc_info.value.rpc_code == 5
        assert exc_info.value.method_id == self.RPC_ID
        assert self.RPC_ID in exc_info.value.found_ids
        message = str(exc_info.value)
        assert "Not found" in message
        assert "status code 5" in message
        assert "authuser" in message.lower()
        assert "#114" in message and "#294" in message
        self._assert_no_auth_patterns(message)

    def test_permission_denied_raises_client_error(self):
        """[7] → ClientError with rpc_code=7, 'Permission denied'."""
        with pytest.raises(ClientError) as exc_info:
            decode_response(self._build_raw([7]), self.RPC_ID)

        assert exc_info.value.rpc_code == 7
        message = str(exc_info.value)
        assert "Permission denied" in message
        assert "status code 7" in message
        self._assert_no_auth_patterns(message)

    def test_internal_code_raises_plain_rpc_error(self):
        """[13] with allow_null=False → RPCError (not ClientError) with rpc_code=13.

        The account-routing hint (mentioning authuser / issues #114, #294) is
        only meaningful for NOT_FOUND / PERMISSION_DENIED. Other codes like
        INTERNAL must not carry it — it would mislead users about the cause.
        """
        with pytest.raises(RPCError) as exc_info:
            decode_response(self._build_raw([13]), self.RPC_ID)

        assert not isinstance(exc_info.value, ClientError)
        assert exc_info.value.rpc_code == 13
        message = str(exc_info.value)
        assert "status code 13" in message
        assert "Internal" in message
        assert "authuser" not in message.lower()
        assert "#114" not in message and "#294" not in message
        self._assert_no_auth_patterns(message)

    def test_unauthenticated_code_does_not_become_auth_error(self):
        """[16] Unauthenticated stays plain RPCError — we do not infer auth.

        The bare code is too ambiguous (see the REMOVE_RECENTLY_VIEWED [13]
        success cassette) to auto-route into auth-refresh. Stay conservative.
        """
        with pytest.raises(RPCError) as exc_info:
            decode_response(self._build_raw([16]), self.RPC_ID)

        assert not isinstance(exc_info.value, ClientError)
        assert not isinstance(exc_info.value, AuthError)
        assert exc_info.value.rpc_code == 16
        message = str(exc_info.value)
        # The label itself contains no AUTH_ERROR_PATTERNS substring; guard it.
        self._assert_no_auth_patterns(message)

    def test_out_of_range_code_falls_through_to_generic_error(self):
        """[99] is outside 0-16 gRPC range → no enrichment, generic message."""
        with pytest.raises(RPCError) as exc_info:
            decode_response(self._build_raw([99]), self.RPC_ID)

        assert not isinstance(exc_info.value, ClientError)
        assert exc_info.value.rpc_code is None
        message = str(exc_info.value)
        assert "returned null result data" in message
        assert "status code" not in message

    def test_multi_element_error_info_falls_through(self):
        """[5, null, 'x'] is not the bare form — no enrichment."""
        with pytest.raises(RPCError) as exc_info:
            decode_response(self._build_raw([5, None, "x"]), self.RPC_ID)

        assert not isinstance(exc_info.value, ClientError)
        assert exc_info.value.rpc_code is None
        message = str(exc_info.value)
        assert "returned null result data" in message
        assert "status code" not in message

    def test_allow_null_suppresses_enrichment_for_client_error_codes(self):
        """allow_null=True must short-circuit even for [5] / [7].

        Fire-and-forget callers (REMOVE_RECENTLY_VIEWED, RENAME_NOTEBOOK, share)
        opt into null results. They must not trip on enrichment.
        """
        for code in (5, 7, 13, 16, 99):
            result = decode_response(self._build_raw([code]), self.RPC_ID, allow_null=True)
            assert result is None, f"allow_null=True leaked for code {code}"

    def test_boolean_error_info_is_not_treated_as_status_code(self):
        """[true] must not be accepted as code 1 — bool is a subclass of int.

        ``json.loads('[true]')`` yields ``[True]`` and ``isinstance(True, int)``
        is ``True`` in Python, so a lax type check would misread a boolean as
        status code 1 (CANCELLED). Guard with ``type(...) is int``.
        """
        with pytest.raises(RPCError) as exc_info:
            decode_response(self._build_raw([True]), self.RPC_ID)

        assert not isinstance(exc_info.value, ClientError)
        assert exc_info.value.rpc_code is None
        message = str(exc_info.value)
        assert "returned null result data" in message
        assert "status code" not in message


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

        error = AuthError("Token expired", method_id="abc123")
        assert str(error) == "Token expired"
        assert error.method_id == "abc123"


class TestUnknownRPCMethodErrorRouting:
    """The 'requested rpc_id missing but other IDs found' branch routes to
    ``UnknownRPCMethodError`` (a ``DecodingError`` subclass), not plain
    ``RPCError``. Other null-result branches must keep raising ``RPCError``.
    """

    def _build_raw_missing(self) -> tuple[str, str, list[str]]:
        """Build a response where requested ID is missing but another is present."""
        requested = "OldMethodId"
        actual = "NewMethodId"
        inner = json.dumps([])
        chunk = json.dumps(["wrb.fr", actual, inner, None, None])
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"
        return raw, requested, [actual]

    def test_missing_id_raises_unknown_rpc_method_error(self):
        raw, requested, found = self._build_raw_missing()
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            decode_response(raw, requested)
        err = exc_info.value
        assert err.method_id == requested
        assert err.found_ids == found
        # raw_response must match the legacy RPCError preview semantics.
        assert err.raw_response is not None
        assert isinstance(err.raw_response, str)
        # Message text unchanged.
        assert "may have changed" in str(err)
        assert requested in str(err)

    def test_unknown_method_error_catchable_as_rpc_error(self):
        raw, requested, _ = self._build_raw_missing()
        with pytest.raises(RPCError):
            decode_response(raw, requested)

    def test_unknown_method_error_catchable_as_decoding_error(self):
        raw, requested, _ = self._build_raw_missing()
        with pytest.raises(DecodingError):
            decode_response(raw, requested)

    def test_unknown_method_error_catchable_as_specific_type(self):
        raw, requested, _ = self._build_raw_missing()
        with pytest.raises(UnknownRPCMethodError):
            decode_response(raw, requested)

    def test_status_code_branch_still_plain_rpc_error(self):
        """Negative test: status-code [13] branch must NOT reroute."""
        rpc_id = RPCMethod.GET_NOTEBOOK.value
        chunk = json.dumps(["wrb.fr", rpc_id, None, None, None, [13], "generic"])
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"
        with pytest.raises(RPCError) as exc_info:
            decode_response(raw, rpc_id)
        # Must be plain RPCError (or ClientError for 5/7), but NEVER UnknownRPCMethodError.
        assert not isinstance(exc_info.value, UnknownRPCMethodError)

    def test_null_result_branch_still_plain_rpc_error(self):
        """Negative test: null-result-no-status branch must NOT reroute."""
        rpc_id = RPCMethod.GET_NOTEBOOK.value
        chunk = json.dumps(["wrb.fr", rpc_id, None, None, None, None])
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"
        with pytest.raises(RPCError) as exc_info:
            decode_response(raw, rpc_id)
        assert not isinstance(exc_info.value, UnknownRPCMethodError)

    def test_no_rpc_data_branch_still_plain_rpc_error(self):
        """Negative test: empty-chunks branch must NOT reroute."""
        raw = ")]}'\n"
        with pytest.raises(RPCError) as exc_info:
            decode_response(raw, "AnyId")
        assert not isinstance(exc_info.value, UnknownRPCMethodError)

    def test_preserves_byte_for_byte_payload_of_legacy_branch(self):
        """raw_response, method_id, and found_ids match legacy RPCError shape."""
        raw, requested, found = self._build_raw_missing()
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            decode_response(raw, requested)
        err = exc_info.value
        # Same fields legacy RPCError populated.
        assert err.method_id == requested
        assert err.found_ids == found
        assert err.raw_response is not None
        # raw_response preview cap (80 chars + "..." = 83) preserved by the
        # base RPCError contract (NOTEBOOKLM_DEBUG=1 opts into full body).
        assert len(err.raw_response) <= 83


class TestMalformedChunkResilience:
    """T8.D1: safe_index hot-path traversal must tolerate malformed chunks.

    None of these inputs should raise; the decoder API surface (collect_rpc_ids,
    extract_rpc_result) must degrade gracefully when Google's response shape
    drifts. This pins the soft-mode contract from rpc._safe_index: drift returns
    None / empty rather than IndexError / TypeError bubbling up.
    """

    RPC_ID = RPCMethod.LIST_NOTEBOOKS.value

    def test_collect_rpc_ids_handles_empty_chunk(self):
        """Empty list chunk is skipped without indexing into it."""
        assert collect_rpc_ids([[]]) == []

    def test_collect_rpc_ids_handles_non_list_chunk(self):
        """Non-list chunks (str / int / dict / None) are skipped silently."""
        assert collect_rpc_ids(["nope", 123, None, {"k": "v"}]) == []

    def test_collect_rpc_ids_handles_short_item(self):
        """Items shorter than 2 elements are skipped before indexing item[1]."""
        chunks: list = [["wrb.fr"]]  # missing rpc_id at index 1
        assert collect_rpc_ids(chunks) == []

    def test_collect_rpc_ids_handles_non_list_inner_item(self):
        """Non-list items inside a nested chunk are skipped."""
        chunks: list = [["string-item", 42, None]]
        # First element is a string, so items becomes [chunk] (the outer list),
        # which has len=3 and item[0]="string-item" doesn't match tags. No raise.
        assert collect_rpc_ids(chunks) == []

    def test_collect_rpc_ids_handles_deeply_nested_malformed(self):
        """Deeply nested malformed structures do not crash the traversal."""
        chunks: list = [[[None]], [[1, 2]], [[]], [[[]]], [["wrb.fr"]]]
        # None of these have a (tag, rpc_id) pair with a valid string rpc_id.
        assert collect_rpc_ids(chunks) == []

    def test_extract_rpc_result_handles_empty_chunk(self):
        """Empty chunks are skipped in extract_rpc_result."""
        assert extract_rpc_result([[]], self.RPC_ID) is None

    def test_extract_rpc_result_handles_non_list_chunk(self):
        """Non-list chunks are skipped in extract_rpc_result."""
        assert extract_rpc_result(["nope", 123, None], self.RPC_ID) is None

    def test_extract_rpc_result_handles_missing_index_5(self):
        """wrb.fr items with len < 6 don't trigger UserDisplayableError lookup."""
        # Item has exactly 3 elements: tag, rpc_id, null result. No index 5.
        chunks: list = [["wrb.fr", self.RPC_ID, None]]
        # Should return None (the null result) without raising IndexError.
        assert extract_rpc_result(chunks, self.RPC_ID) is None

    def test_extract_rpc_result_handles_short_item(self):
        """Items with len < 3 are skipped before any indexing."""
        # Items shorter than 3 elements should be skipped — no IndexError.
        chunks: list = [["wrb.fr", self.RPC_ID]]
        assert extract_rpc_result(chunks, self.RPC_ID) is None

    def test_extract_rpc_result_handles_deeply_nested_malformed(self):
        """Pathological nesting must not raise in extract_rpc_result."""
        chunks: list = [
            [],
            [[]],
            [None, None, None],
            [["wrb.fr"]],
            [["wrb.fr", self.RPC_ID]],  # len < 3 — skipped
        ]
        assert extract_rpc_result(chunks, self.RPC_ID) is None

    def test_decode_response_handles_malformed_chunk_for_status_lookup(self):
        """_find_wrb_status (via decode_response) must not crash on malformed.

        When decode_response hits the "null result data, look up status"
        branch, the chunk list it scans may include malformed entries. None
        of these should raise.
        """
        # First a malformed chunk, then the legitimate null-result entry that
        # decode_response will report on. Use GET_NOTEBOOK to trigger the
        # status-lookup branch.
        rpc_id = RPCMethod.GET_NOTEBOOK.value
        good = json.dumps(["wrb.fr", rpc_id, None, None, None, None])
        bad_short = json.dumps(["wrb.fr"])  # malformed, len < 6
        bad_empty = json.dumps([])
        body = (
            f"{len(bad_short)}\n{bad_short}\n{len(bad_empty)}\n{bad_empty}\n{len(good)}\n{good}\n"
        )
        raw = f")]}}'\n{body}"
        # decode_response will still raise RPCError for the null result, but
        # the malformed-chunk traversal must not raise IndexError on its way
        # there.
        with pytest.raises(RPCError, match="returned null result data"):
            decode_response(raw, rpc_id)


class TestGetErrorMessageForCode:
    """Parametrized coverage for ``get_error_message_for_code``.

    Covers every known code in ``_ERROR_CODE_MESSAGES``, the ``None``
    sentinel, the 4xx/5xx fallback ranges, and out-of-range codes.
    """

    @pytest.mark.parametrize(
        ("code", "expected_substring", "expected_retryable"),
        [
            pytest.param(
                RPCErrorCode.INVALID_REQUEST,
                "Invalid request parameters",
                False,
                id="invalid_request_400",
            ),
            pytest.param(
                RPCErrorCode.UNAUTHORIZED,
                "Authentication required",
                False,
                id="unauthorized_401",
            ),
            pytest.param(
                RPCErrorCode.FORBIDDEN,
                "Insufficient permissions",
                False,
                id="forbidden_403",
            ),
            pytest.param(
                RPCErrorCode.NOT_FOUND,
                "Requested resource not found",
                False,
                id="not_found_404",
            ),
            pytest.param(
                RPCErrorCode.RATE_LIMITED,
                "rate limit exceeded",
                True,
                id="rate_limited_429",
            ),
            pytest.param(
                RPCErrorCode.SERVER_ERROR,
                "Server error occurred",
                True,
                id="server_error_500",
            ),
        ],
    )
    def test_error_code_known_returns_mapped_message(
        self, code: int, expected_substring: str, expected_retryable: bool
    ) -> None:
        """Every known mapped code returns its tailored message + retry flag."""
        message, is_retryable = get_error_message_for_code(int(code))
        assert expected_substring in message
        assert is_retryable is expected_retryable

    def test_error_code_400_invalid_request(self) -> None:
        """400 maps to the INVALID_REQUEST table entry, not the 4xx fallback."""
        message, is_retryable = get_error_message_for_code(400)
        assert message == "Invalid request parameters. Check your input and try again."
        assert is_retryable is False

    def test_error_code_401_unauthorized(self) -> None:
        message, is_retryable = get_error_message_for_code(401)
        assert "notebooklm login" in message
        assert is_retryable is False

    def test_error_code_403_forbidden(self) -> None:
        message, is_retryable = get_error_message_for_code(403)
        assert message == "Insufficient permissions for this operation."
        assert is_retryable is False

    def test_error_code_404_not_found(self) -> None:
        message, is_retryable = get_error_message_for_code(404)
        assert message == "Requested resource not found."
        assert is_retryable is False

    def test_error_code_429_rate_limit(self) -> None:
        message, is_retryable = get_error_message_for_code(429)
        assert "rate limit" in message.lower()
        assert is_retryable is True

    def test_error_code_500_server_error(self) -> None:
        message, is_retryable = get_error_message_for_code(500)
        assert "Server error" in message
        assert is_retryable is True

    def test_error_code_none_returns_generic_unknown(self) -> None:
        """``None`` sentinel returns the generic non-retryable message."""
        message, is_retryable = get_error_message_for_code(None)
        assert message == "Unknown error occurred."
        assert is_retryable is False

    @pytest.mark.parametrize(
        ("code", "expected_message", "expected_retryable"),
        [
            pytest.param(
                402,
                "Client error 402. Check your request parameters.",
                False,
                id="unmapped_4xx_402",
            ),
            pytest.param(
                418,
                "Client error 418. Check your request parameters.",
                False,
                id="unmapped_4xx_418_teapot",
            ),
            pytest.param(
                422,
                "Client error 422. Check your request parameters.",
                False,
                id="unmapped_4xx_422",
            ),
            pytest.param(
                499,
                "Client error 499. Check your request parameters.",
                False,
                id="unmapped_4xx_499_upper_edge",
            ),
        ],
    )
    def test_error_code_unmapped_4xx_generic_client_error(
        self, code: int, expected_message: str, expected_retryable: bool
    ) -> None:
        """Unknown 400-499 codes get the generic client-error fallback."""
        message, is_retryable = get_error_message_for_code(code)
        assert message == expected_message
        assert is_retryable is expected_retryable

    @pytest.mark.parametrize(
        ("code", "expected_message", "expected_retryable"),
        [
            pytest.param(
                501,
                "Server error 501. This is usually temporary - try again later.",
                True,
                id="unmapped_5xx_501",
            ),
            pytest.param(
                502,
                "Server error 502. This is usually temporary - try again later.",
                True,
                id="unmapped_5xx_502_bad_gateway",
            ),
            pytest.param(
                503,
                "Server error 503. This is usually temporary - try again later.",
                True,
                id="unmapped_5xx_503",
            ),
            pytest.param(
                599,
                "Server error 599. This is usually temporary - try again later.",
                True,
                id="unmapped_5xx_599_upper_edge",
            ),
        ],
    )
    def test_error_code_unmapped_5xx_generic_server_error(
        self, code: int, expected_message: str, expected_retryable: bool
    ) -> None:
        """Unknown 500-599 codes get the generic retryable server-error fallback."""
        message, is_retryable = get_error_message_for_code(code)
        assert message == expected_message
        assert is_retryable is expected_retryable

    @pytest.mark.parametrize(
        ("code", "expected_message"),
        [
            pytest.param(0, "Error code: 0", id="zero_edge_case"),
            pytest.param(1, "Error code: 1", id="positive_below_4xx_grpc_cancelled"),
            pytest.param(13, "Error code: 13", id="grpc_internal_13"),
            pytest.param(200, "Error code: 200", id="success_range_200"),
            pytest.param(399, "Error code: 399", id="just_below_4xx_399"),
            pytest.param(600, "Error code: 600", id="just_above_5xx_600"),
            pytest.param(999, "Error code: 999", id="three_digit_999"),
            pytest.param(123456, "Error code: 123456", id="very_large_code"),
            pytest.param(-1, "Error code: -1", id="negative_code"),
        ],
    )
    def test_error_code_out_of_range_generic_fallback(
        self, code: int, expected_message: str
    ) -> None:
        """Codes outside 400-599 get the bare ``Error code: <n>`` fallback (non-retryable)."""
        message, is_retryable = get_error_message_for_code(code)
        assert message == expected_message
        assert is_retryable is False

    def test_returns_tuple_of_str_and_bool(self) -> None:
        """Return contract: always ``tuple[str, bool]`` regardless of input."""
        for code in (None, 0, 400, 429, 500, 502, 999, -1):
            message, is_retryable = get_error_message_for_code(code)
            assert isinstance(message, str)
            assert isinstance(is_retryable, bool)

    def test_every_rpc_error_code_enum_value_covered_or_fallback(self) -> None:
        """Each ``RPCErrorCode`` enum value resolves to a non-empty message.

        Belt-and-suspenders: future additions to ``RPCErrorCode`` either land
        in ``_ERROR_CODE_MESSAGES`` or fall through to the range-based
        fallback. Either way ``get_error_message_for_code`` must produce a
        non-empty human-readable message.
        """
        for code in RPCErrorCode:
            message, is_retryable = get_error_message_for_code(int(code))
            assert isinstance(message, str) and message
            assert isinstance(is_retryable, bool)
