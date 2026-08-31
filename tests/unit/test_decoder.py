"""Unit tests for RPC response decoder."""

import json
import logging

import pytest

from notebooklm._web.wire.decoder import (
    _RPCS_OBSERVED_SWALLOWING_A_STATUS,
    AuthError,
    ClientError,
    RateLimitError,
    RPCError,
    RPCErrorCode,
    UnknownRPCMethodError,
    _contains_user_displayable_error,
    _server_status_message,
    byte_count_mismatch_total,
    collect_rpc_ids,
    decode_response,
    extract_rpc_result,
    get_error_message_for_code,
    parse_chunked_response,
    reset_byte_count_mismatch_total,
    strip_anti_xssi,
)
from notebooklm.exceptions import DecodingError
from notebooklm.rpc.types import RPCMethod
from tests._fixtures.rpc_error_frames import (
    CREATE_ARTIFACT_METHOD_ID,
    LIVE_CREATE_ARTIFACT_INVALID_ARGUMENT_BODY,
    LIVE_RETRY_ARTIFACT_NOT_FOUND_BODY,
    LIVE_REVISE_SLIDE_NOT_FOUND_BODY,
    USER_DISPLAYABLE_RATE_LIMIT_STATUS,
)


def test_decoder_keeps_legacy_logger_name() -> None:
    from notebooklm._web.wire import decoder as decoder_module

    assert decoder_module.logger.name == "notebooklm.rpc.decoder"


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
        # Add 10 valid payload records and 1 malformed payload record, keeping
        # the payload error rate below the 10% threshold.
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

    def test_byte_count_mismatch_bumps_counter_without_warning(self, caplog):
        """A byte-count mismatch is the expected case: counter only, no WARNING.

        The tolerant parse is unchanged (valid JSON payloads are still
        returned) and each mismatch increments the process-wide counter, but no
        WARNING is emitted: a mismatch trips on essentially every healthy live
        multi-chunk response, so it is tracked silently and surfaced only via
        ``byte_count_mismatch_total()`` (telemetry alerts on its rate-of-change).
        """
        reset_byte_count_mismatch_total()
        try:
            payload = json.dumps(["wrong-size"])
            response = f"{len(payload) + 1}\n{payload}\n"

            assert byte_count_mismatch_total() == 0
            with caplog.at_level(logging.DEBUG, logger="notebooklm.rpc.decoder"):
                chunks = parse_chunked_response(response)

            # Tolerant parse preserved: valid JSON still returned.
            assert chunks == [["wrong-size"]]
            assert byte_count_mismatch_total() == 1

            # No WARNING for the expected mismatch; the DEBUG line still fires.
            assert not [
                r
                for r in caplog.records
                if r.name == "notebooklm.rpc.decoder" and r.levelno == logging.WARNING
            ]
            assert any(
                r.levelno == logging.DEBUG and "declares" in r.message
                for r in caplog.records
                if r.name == "notebooklm.rpc.decoder"
            )
        finally:
            reset_byte_count_mismatch_total()

    def test_byte_count_mismatch_counts_silently_across_records(self, caplog):
        """Repeated mismatches keep counting without ever emitting a WARNING."""
        reset_byte_count_mismatch_total()
        try:
            payload = json.dumps(["wrong-size"])
            record = f"{len(payload) + 1}\n{payload}"
            response = "\n".join(record for _ in range(50)) + "\n"

            with caplog.at_level(logging.WARNING, logger="notebooklm.rpc.decoder"):
                chunks = parse_chunked_response(response)

            assert chunks == [["wrong-size"]] * 50
            assert byte_count_mismatch_total() == 50
            # Mismatches are counted silently; none escalate to WARNING.
            assert not [
                r
                for r in caplog.records
                if r.name == "notebooklm.rpc.decoder" and r.levelno == logging.WARNING
            ]
        finally:
            reset_byte_count_mismatch_total()

    def test_matching_byte_count_does_not_bump_counter(self):
        """A correct byte count is not a mismatch and must not bump the counter."""
        reset_byte_count_mismatch_total()
        try:
            payload = json.dumps(["right-size"])
            response = f"{len(payload.encode('utf-8'))}\n{payload}\n"

            chunks = parse_chunked_response(response)

            assert chunks == [["right-size"]]
            assert byte_count_mismatch_total() == 0
        finally:
            reset_byte_count_mismatch_total()

    def test_concurrent_mismatches_do_not_lose_increments(self):
        """The counter is lock-guarded, so concurrent parses must not race.

        ``x += 1`` on a module global is a non-atomic read-modify-write in
        CPython, so without the lock concurrent ``parse_chunked_response``
        calls (worker threads / multiple per-thread clients) could lose
        increments. Drive many threads at one mismatch each and assert the
        total equals the number of parses exactly.
        """
        import threading

        reset_byte_count_mismatch_total()
        try:
            payload = json.dumps(["wrong-size"])
            response = f"{len(payload) + 1}\n{payload}\n"
            threads = 16
            per_thread = 50
            barrier = threading.Barrier(threads)

            def worker() -> None:
                barrier.wait()
                for _ in range(per_thread):
                    parse_chunked_response(response)

            workers = [threading.Thread(target=worker) for _ in range(threads)]
            for t in workers:
                t.start()
            for t in workers:
                t.join()

            assert byte_count_mismatch_total() == threads * per_thread
        finally:
            reset_byte_count_mismatch_total()

    def test_skips_byte_count_without_payload_below_threshold(self, caplog):
        """A trailing byte-count line without a payload is malformed and skipped."""
        valid_parts = "\n".join(self._chunk_record([f"valid{i}"]) for i in range(10))
        response = f"{valid_parts}\n42\n"

        chunks = parse_chunked_response(response)

        assert chunks == [[f"valid{i}"] for i in range(10)]
        assert "without payload" in caplog.text

    def test_trailing_byte_count_above_framing_threshold_raises(self):
        """A framing-only response fails strict decoding."""
        with pytest.raises(RPCError, match="1 of 1 framing records"):
            parse_chunked_response("42\n")

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

    def test_payload_error_rate_exactly_ten_percent_is_tolerated(self):
        """The payload threshold is exclusive: exactly 10% does not raise."""
        valid_parts = "\n".join(self._chunk_record([f"valid{i}"]) for i in range(9))
        response = f"{valid_parts}\n99\nnot-json\n"

        chunks = parse_chunked_response(response)

        assert chunks == [[f"valid{i}"] for i in range(9)]

    def test_byte_count_frames_do_not_dilute_malformed_payload_rate(self):
        """The parser raises when payload errors exceed 10%, excluding byte-count frames."""
        valid_parts = "\n".join(self._chunk_record([f"valid{i}"]) for i in range(8))
        response = f"{valid_parts}\n99\nnot-json\n"

        with pytest.raises(RPCError) as exc_info:
            parse_chunked_response(response)

        assert "1 of 9 payload records malformed" in str(exc_info.value)
        assert "18 response records" not in str(exc_info.value)

    def test_mixed_payload_and_framing_errors_preserve_strict_threshold(self):
        """Separate payload/framing rates must not loosen mixed malformed streams."""
        valid_parts = "\n".join(self._chunk_record([f"valid{i}"]) for i in range(10))
        response = f"{valid_parts}\n99\nnot-json\n42\n"

        with pytest.raises(RPCError, match="2 of 12 response records"):
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

    def test_prefers_last_non_null_frame_for_same_id(self):
        """rt=c emits a null placeholder then the populated frame; the last wins.

        Multiple ``wrb.fr`` frames for one RPC ID are legal in streamed
        ``rt=c`` mode. The decoder must skip past an earlier null placeholder
        and return the final populated payload instead of the first match.
        """
        placeholder = ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, None]
        final = [
            "wrb.fr",
            RPCMethod.LIST_NOTEBOOKS.value,
            json.dumps([["final-notebook"]]),
            None,
            None,
            None,
        ]
        # Frames may arrive in separate chunks or grouped — cover both.
        chunks = [[placeholder], [final]]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result == [["final-notebook"]]

    def test_does_not_downgrade_populated_frame_to_later_null(self):
        """A trailing null frame must not clobber an earlier populated result."""
        final = [
            "wrb.fr",
            RPCMethod.LIST_NOTEBOOKS.value,
            json.dumps([["real-data"]]),
            None,
            None,
            None,
        ]
        trailing_null = ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, None]
        chunks = [[final], [trailing_null]]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result == [["real-data"]]

    def test_single_null_frame_still_returns_none(self):
        """A lone null frame returns None exactly as before (first==last)."""
        chunks = [["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, None]]

        result = extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)
        assert result is None

    def test_raises_on_error_chunk(self):
        """Test raises RPCError for error chunks."""
        chunks = [
            ["er", RPCMethod.LIST_NOTEBOOKS.value, "Some error message", None, None],
        ]

        with pytest.raises(RPCError, match="Some error message"):
            extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)

    def test_error_frame_after_null_placeholder_still_raises(self):
        """An 'er' frame following a null placeholder is terminal and raises."""
        placeholder = ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, None]
        error = ["er", RPCMethod.LIST_NOTEBOOKS.value, "Terminal failure", None, None]
        chunks = [[placeholder], [error]]

        with pytest.raises(RPCError, match="Terminal failure"):
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
        # Real-world structure from an API rate limit response, shared with
        # the #239 regression suite (tests/_fixtures/rpc_error_frames.py).
        error_info = USER_DISPLAYABLE_RATE_LIMIT_STATUS
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
        # The stable label is surfaced; the raw numeric gRPC code is not (#1921).
        message = str(exc_info.value)
        assert "Resource exhausted" in message
        assert "status code 8" not in message
        assert "Upstream status code" not in message

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
        allow_null=True (see tests/cassettes/web/notebooks_remove_from_recent.yaml).
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
        with pytest.raises(RPCError, match="empty result") as exc_info:
            decode_response(raw, self.RPC_ID)
        # The obfuscated method id must not leak into the human-readable
        # message (#1921), but stays on the exception attributes.
        assert self.RPC_ID not in str(exc_info.value)
        assert exc_info.value.method_id == self.RPC_ID

    # Scenario D: Short item (2 elements) — wrb.fr found but skipped by extract_rpc_result
    def test_scenario_d_short_item(self):
        """wrb.fr item has only 2 elements, skipped by extract_rpc_result."""
        chunk = json.dumps(["wrb.fr", self.RPC_ID])
        body = f"{len(chunk)}\n{chunk}\n"
        raw = self._build_raw(body)
        # Short items are skipped by extract_rpc_result (len < 3),
        # but collect_rpc_ids still finds the ID (len >= 2)
        with pytest.raises(RPCError, match="empty result"):
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
    (which ``notebooklm._runtime.helpers.is_auth_error`` treats as non-auth),
    other codes stay as RPCError, and the account-routing hint avoids legacy
    auth-word substrings.

    The status code and label live on the exception attributes (``rpc_code``),
    and the stable gRPC status *label* is user-facing, but the obfuscated
    method id, the raw numeric code, and the ``Found IDs`` dump must NOT leak
    into the human-readable message (#1921) — they stay on the attributes.
    """

    RPC_ID = RPCMethod.GET_NOTEBOOK.value

    # Keep this legacy auth-word list in sync with
    # ``notebooklm._runtime.helpers.AUTH_ERROR_PATTERNS``.
    _AUTH_PATTERNS = ("authentication", "expired", "unauthorized", "login", "re-authenticate")

    def _build_raw(self, error_info: list | None) -> str:
        chunk = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, error_info, "generic"])
        return f")]}}'\n{len(chunk)}\n{chunk}\n"

    def _assert_no_auth_patterns(self, message: str) -> None:
        lower = message.lower()
        for pattern in self._AUTH_PATTERNS:
            assert pattern not in lower, (
                f"Message contains AUTH_ERROR_PATTERN {pattern!r}: would trigger "
                f"spurious auth-refresh retry in is_auth_error: {message!r}"
            )

    def test_not_found_raises_client_error(self):
        """[5] → ClientError with rpc_code=5, 'not found' label, authuser hint."""
        with pytest.raises(ClientError) as exc_info:
            decode_response(self._build_raw([5]), self.RPC_ID)

        assert exc_info.value.rpc_code == 5
        assert exc_info.value.method_id == self.RPC_ID
        assert self.RPC_ID in exc_info.value.found_ids
        message = str(exc_info.value)
        # Human-readable label is surfaced; the obfuscated method id and the
        # raw numeric code are not (#1921).
        assert "not found" in message.lower()
        assert self.RPC_ID not in message
        assert "status code" not in message
        assert "Found IDs" not in message
        assert "authuser" in message.lower()
        assert "#114" in message and "#294" in message
        self._assert_no_auth_patterns(message)

    def test_permission_denied_raises_client_error(self):
        """[7] → ClientError with rpc_code=7, 'permission denied' label."""
        with pytest.raises(ClientError) as exc_info:
            decode_response(self._build_raw([7]), self.RPC_ID)

        assert exc_info.value.rpc_code == 7
        message = str(exc_info.value)
        assert "permission denied" in message.lower()
        assert self.RPC_ID not in message
        assert "status code" not in message
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
        assert "internal" in message.lower()
        assert self.RPC_ID not in message
        assert "status code" not in message
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
        assert "empty result" in message
        assert "status code" not in message

    def test_multi_element_status_is_still_read_as_a_status(self):
        """``[5, null, 'x']`` is the same google.rpc.Status at a longer arity.

        The old ``len(error_info) != 1`` gate made every non-bare status
        invisible, which also made ``google.rpc.Status.message`` (index 1)
        unreachable by construction — a read that could never fire is the
        #2134 defect, so the arity gate had to go (#2188). The leading code is
        what identifies the rejection at any length.
        """
        with pytest.raises(ClientError) as exc_info:
            decode_response(self._build_raw([5, None, "x"]), self.RPC_ID)

        assert exc_info.value.rpc_code == 5
        assert "not found" in str(exc_info.value).lower()

    def test_allow_null_suppresses_enrichment_for_client_error_codes(self):
        """allow_null=True must short-circuit even for [5] / [7].

        Fire-and-forget callers (REMOVE_RECENTLY_VIEWED, RENAME_NOTEBOOK, share)
        opt into null results. They must not trip on enrichment.
        """
        for code in (5, 7, 13, 16, 99):
            result = decode_response(self._build_raw([code]), self.RPC_ID, allow_null=True)
            assert result is None, f"allow_null=True leaked for code {code}"

    def test_enriched_messages_do_not_leak_method_id_or_codes(self):
        """The human-readable message must not leak the obfuscated id or codes.

        The obfuscated method id is the #1 breakage class (it changes whenever
        Google re-obfuscates a method) and carries no end-user value; the raw
        numeric gRPC code and the ``Found IDs`` dump are likewise volatile
        internal detail. All three must stay OFF the message and ON the
        exception attributes so logs/tracebacks retain the debug signal (#1921).
        """
        for error_info in ([5], [13], [99]):
            with pytest.raises(RPCError) as exc_info:
                decode_response(self._build_raw(error_info), self.RPC_ID)
            message = str(exc_info.value)
            assert self.RPC_ID not in message
            assert "Found IDs" not in message
            assert "status code" not in message
            # The debug signal is preserved on the attribute.
            assert self.RPC_ID in exc_info.value.found_ids

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
        assert "empty result" in message
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


class TestAllowNullDoesNotMaskDrift:
    """Issue #1158: ``allow_null=True`` must not swallow method-ID drift or
    anti-bot/redirect walls as a benign ``None``.

    ``allow_null`` only sanctions a ``wrb.fr`` frame that genuinely carried a
    null payload (the requested ``rpc_id`` *is* present). An *absent* RPC ID
    (drift) or a body with no RPC frames at all (anti-bot wall) is categorically
    different and must still raise, even for opt-in callers.
    """

    def test_allow_null_still_raises_on_method_id_drift(self):
        """Requested ID missing but another ID present → UnknownRPCMethodError,
        even with allow_null=True."""
        requested = "OldMethodId"
        actual = "NewMethodId"
        inner = json.dumps([])
        chunk = json.dumps(["wrb.fr", actual, inner, None, None])
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"

        with pytest.raises(UnknownRPCMethodError) as exc_info:
            decode_response(raw, requested, allow_null=True)
        err = exc_info.value
        assert err.method_id == requested
        assert err.found_ids == [actual]
        assert "may have changed" in str(err)

    def test_allow_null_still_raises_when_no_rpc_data(self):
        """Empty/anti-bot body with no RPC frames → RPCError, even with
        allow_null=True."""
        raw = ")]}'\n"
        with pytest.raises(RPCError) as exc_info:
            decode_response(raw, "AnyId", allow_null=True)
        assert "response contained no RPC data" in str(exc_info.value)
        assert not isinstance(exc_info.value, UnknownRPCMethodError)

    def test_allow_null_still_raises_on_non_rpc_json_body(self):
        """A parseable but non-RPC JSON body (e.g. a redirect/error page) yields
        no found_ids → RPCError, even with allow_null=True."""
        chunk = json.dumps({"redirect": "https://accounts.google.com/"})
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"
        with pytest.raises(RPCError) as exc_info:
            decode_response(raw, RPCMethod.CREATE_ARTIFACT.value, allow_null=True)
        assert "response contained no RPC data" in str(exc_info.value)
        assert not isinstance(exc_info.value, UnknownRPCMethodError)

    def test_allow_null_returns_none_for_present_but_null_frame(self):
        """Regression guard: when the requested ID IS present with a null
        payload, allow_null=True still returns None (the legitimate use case)."""
        rpc_id = RPCMethod.CREATE_ARTIFACT.value
        chunk = json.dumps(["wrb.fr", rpc_id, None, None, None])
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"

        result = decode_response(raw, rpc_id, allow_null=True)
        assert result is None


class TestMalformedChunkResilience:
    """safe_index hot-path traversal must tolerate malformed chunks.

    None of these inputs should raise; the decoder API surface (collect_rpc_ids,
    extract_rpc_result) length-guards each chunk *before* invoking
    ``safe_index``, so structurally-empty/short chunks are skipped rather than
    triggering a strict-decode ``UnknownRPCMethodError``.
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
        with pytest.raises(RPCError, match="empty result"):
            decode_response(raw, rpc_id)


class TestDeepNestingRecursionGuard:
    """Server-controlled nesting depth must never raise RecursionError (#2107).

    ``_contains_user_displayable_error`` recurses over the ``error_info``
    payload at index 5 of ``wrb.fr`` frames, and ``parse_chunked_response``
    feeds raw server JSON to ``json.loads`` — both inputs are entirely
    server-controlled, so pathological nesting depth must degrade gracefully
    instead of overflowing the interpreter stack.
    """

    RPC_ID = RPCMethod.LIST_NOTEBOOKS.value
    MARKER = (
        "type.googleapis.com/google.internal.labs.tailwind.orchestration.v1.UserDisplayableError"
    )

    @staticmethod
    def _nest_list(value, depth: int):
        """Wrap ``value`` in ``depth`` levels of single-element lists."""
        for _ in range(depth):
            value = [value]
        return value

    @staticmethod
    def _frame_body(payload: str) -> str:
        """Wrap a JSON payload in anti-XSSI prefix + chunked framing."""
        return f")]}}'\n{len(payload.encode('utf-8'))}\n{payload}\n"

    def test_marker_found_at_shallow_list_depth(self):
        """Regression guard: marker inside a few list levels is still found."""
        assert _contains_user_displayable_error(self._nest_list(self.MARKER, 3))

    def test_marker_found_at_shallow_dict_depth(self):
        """Regression guard: marker inside nested dicts is still found."""
        obj = {"outer": {"inner": {"type": self.MARKER}}}
        assert _contains_user_displayable_error(obj)

    def test_marker_found_just_under_depth_cap(self):
        """A marker at the deepest reachable level (cap - 1) is detected."""
        assert _contains_user_displayable_error(self._nest_list(self.MARKER, 19))

    def test_nesting_over_depth_cap_returns_false_and_warns(self, caplog):
        """Past the cap the marker is treated as absent and a warning fires."""
        obj = self._nest_list(self.MARKER, 25)

        with caplog.at_level(logging.WARNING, logger="notebooklm.rpc.decoder"):
            assert _contains_user_displayable_error(obj) is False

        assert any(
            r.name == "notebooklm.rpc.decoder"
            and "Max recursion depth reached in UserDisplayableError detection" in r.message
            for r in caplog.records
        )

    def test_wide_container_at_depth_boundary_warns_once(self, caplog):
        """A wide container at the cap logs one warning, not one per child."""
        # 19 wrapper levels put the wide list exactly at the depth boundary
        # (max_depth == 1); its 50 children must not be visited individually.
        obj = self._nest_list([self.MARKER] * 50, 19)

        with caplog.at_level(logging.WARNING, logger="notebooklm.rpc.decoder"):
            assert _contains_user_displayable_error(obj) is False

        depth_warnings = [
            r
            for r in caplog.records
            if r.name == "notebooklm.rpc.decoder"
            and "Max recursion depth reached in UserDisplayableError detection" in r.message
        ]
        assert len(depth_warnings) == 1

    def test_decode_response_deep_error_info_raises_rpc_error(self):
        """~800-deep error_info must map to RPCError, never RecursionError.

        Depth 800 parses fine in ``json.loads`` (one recursion unit per
        level, under the default limit of 1000) but previously overflowed in
        ``_contains_user_displayable_error`` (~2 frames per level). With the
        depth cap, the payload is treated as carrying no displayable-error
        marker and falls through to the regular null-result handling.
        """
        deep = json.dumps(self._nest_list([], 800))
        payload = f'[["wrb.fr","{self.RPC_ID}",null,null,null,{deep}]]'

        with pytest.raises(RPCError, match="empty result"):
            decode_response(self._frame_body(payload), self.RPC_ID)

    def test_decode_response_deep_error_info_allow_null_returns_none(self):
        """Same deep payload with allow_null=True returns None gracefully."""
        deep = json.dumps(self._nest_list([], 800))
        payload = f'[["wrb.fr","{self.RPC_ID}",null,null,null,{deep}]]'

        result = decode_response(self._frame_body(payload), self.RPC_ID, allow_null=True)
        assert result is None

    def test_parse_chunked_response_recursion_error_chunk_treated_as_malformed(
        self, caplog, monkeypatch
    ):
        """A chunk whose json.loads raises RecursionError is skipped as malformed.

        The depth at which ``json.loads`` overflows varies by interpreter
        version (3.12+ raised the effective C recursion headroom), so the
        overflow is injected deterministically instead of relying on a magic
        nesting depth.
        """
        import notebooklm._web.wire.decoder as decoder_module

        poisoned = '[["poisoned"]]'
        real_loads = json.loads

        def fake_loads(s, *args, **kwargs):
            if s == poisoned:
                raise RecursionError("maximum recursion depth exceeded")
            return real_loads(s, *args, **kwargs)

        monkeypatch.setattr(decoder_module.json, "loads", fake_loads)

        valid_chunks = [json.dumps([f"valid{i}"]) for i in range(10)]
        valid_parts = "\n".join(f"{len(c)}\n{c}" for c in valid_chunks)
        response = f"{valid_parts}\n{len(poisoned)}\n{poisoned}\n"

        with caplog.at_level(logging.WARNING, logger="notebooklm.rpc.decoder"):
            chunks = parse_chunked_response(response)

        assert chunks == [[f"valid{i}"] for i in range(10)]
        assert any(
            r.name == "notebooklm.rpc.decoder" and "malformed" in r.message.lower()
            for r in caplog.records
        )

    def test_parse_chunked_response_extreme_depth_never_raises(self):
        """Genuinely deep JSON must never propagate RecursionError.

        Whether a given depth overflows ``json.loads`` is interpreter-
        dependent: it does on 3.10/3.11 (recursion-limit based) but parses
        fine on 3.12+ (larger C recursion headroom). The contract under test
        is only that ``parse_chunked_response`` completes either way — the
        chunk is parsed or skipped as malformed, never raised through.
        """
        # Built textually: json.dumps itself cannot serialize this depth.
        deep_json = "[" * 2000 + "]" * 2000
        valid_chunks = [json.dumps([f"valid{i}"]) for i in range(10)]
        valid_parts = "\n".join(f"{len(c)}\n{c}" for c in valid_chunks)
        response = f"{valid_parts}\n{len(deep_json)}\n{deep_json}\n"

        chunks = parse_chunked_response(response)

        assert chunks[:10] == [[f"valid{i}"] for i in range(10)]
        # Trailing deep chunk is either absent (skipped as malformed) or the
        # parsed nested list — both acceptable; RecursionError is not.
        assert len(chunks) in (10, 11)

    def test_extract_rpc_result_recursion_error_in_result_data_falls_back_to_raw(self, monkeypatch):
        """result_data whose inner json.loads overflows falls back to the raw string.

        ``extract_rpc_result`` re-parses string result_data; a server-
        controlled deeply nested JSON string there must degrade to the
        existing unparseable-payload fallback (raw string) instead of
        propagating RecursionError out of decode_response.
        """
        import notebooklm._web.wire.decoder as decoder_module

        poisoned_inner = "[" * 40 + "]" * 40
        real_loads = json.loads

        def fake_loads(s, *args, **kwargs):
            if s == poisoned_inner:
                raise RecursionError("maximum recursion depth exceeded")
            return real_loads(s, *args, **kwargs)

        monkeypatch.setattr(decoder_module.json, "loads", fake_loads)

        payload = json.dumps([["wrb.fr", self.RPC_ID, poisoned_inner]])
        result = decode_response(self._frame_body(payload), self.RPC_ID)

        assert result == poisoned_inner


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


class TestServerStatusMessage:
    """``google.rpc.Status.message`` (index 1) — the only server-authored text.

    Every other word this client prints for a rejection is client-authored. The
    slot has **never** been observed populated, so the tests below split cleanly
    in two: the *captured* shapes pin that nothing is invented from them, and
    the *populated* cases pin what would happen if the server ever filled it in.
    The populated payloads are the only synthetic fixtures here, and they are
    synthetic by necessity — you cannot capture a field the server has not sent.
    """

    def test_recorded_rate_limit_status_yields_no_server_message(self):
        """The one recorded rich sample carries ``None`` at index 1."""
        assert _server_status_message(USER_DISPLAYABLE_RATE_LIMIT_STATUS) is None

    def test_bare_status_array_yields_no_server_message(self):
        """A live ``[3]`` / ``[13]`` rejection has no message slot at all."""
        assert _server_status_message([3]) is None
        assert _server_status_message([13]) is None

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            "not-a-status",
            [3, None],
            [3, 42],
            [3, ["nested"]],
            [3, ""],
            [3, "   "],
        ],
    )
    def test_non_text_slots_are_ignored(self, payload):
        """Only a non-empty ``str`` counts — never a guess at something else."""
        assert _server_status_message(payload) is None

    def test_populated_message_is_returned(self):
        """A server-sent string is surfaced verbatim."""
        assert _server_status_message([8, "Daily limit reached", []]) == "Daily limit reached"

    def test_whitespace_is_collapsed(self):
        assert _server_status_message([8, "  Daily  \n limit\treached  "]) == "Daily limit reached"

    def test_overlong_message_is_truncated(self):
        """The field is server-controlled; it must not blow up an exception."""
        result = _server_status_message([8, "x" * 5000])

        assert result is not None
        assert len(result) <= 301  # 300 chars + the ellipsis
        assert result.endswith("…")

    def test_rate_limit_error_prefers_server_text_over_the_client_sentence(self):
        """When the server explains itself, its words lead (#2188)."""
        chunks = [
            [
                "wrb.fr",
                RPCMethod.LIST_NOTEBOOKS.value,
                None,
                None,
                None,
                [8, "Daily limit for Video Overviews reached", [["UserDisplayableError", []]]],
            ]
        ]

        with pytest.raises(RateLimitError) as exc_info:
            extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)

        message = str(exc_info.value)
        assert message.startswith("Daily limit for Video Overviews reached")
        assert "API rate limit or quota exceeded" not in message
        # The upstream label is still appended for diagnosis.
        assert "(Upstream: Resource exhausted.)" in message

    def test_rate_limit_error_keeps_the_client_sentence_when_the_server_is_silent(self):
        """The recorded shape still produces exactly today's wording."""
        chunks = [
            [
                "wrb.fr",
                RPCMethod.LIST_NOTEBOOKS.value,
                None,
                None,
                None,
                USER_DISPLAYABLE_RATE_LIMIT_STATUS,
            ]
        ]

        with pytest.raises(RateLimitError) as exc_info:
            extract_rpc_result(chunks, RPCMethod.LIST_NOTEBOOKS.value)

        assert str(exc_info.value) == (
            "API rate limit or quota exceeded. Please wait before retrying. "
            "(Upstream: Resource exhausted.)"
        )

    def test_null_result_rejection_appends_server_text(self):
        """The bare-status path echoes a server message when one is sent."""
        chunk = json.dumps(
            [
                "wrb.fr",
                RPCMethod.GET_NOTEBOOK.value,
                None,
                None,
                None,
                [9, "This notebook has no sources yet"],
                "generic",
            ]
        )
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"

        with pytest.raises(RPCError) as exc_info:
            decode_response(raw, RPCMethod.GET_NOTEBOOK.value)

        message = str(exc_info.value)
        assert "failed precondition" in message.lower()
        assert "This notebook has no sources yet" in message


class TestLiveCapturedFraming:
    """The one fixture carrying real server framing, pinned as captured.

    ``LIVE_CREATE_ARTIFACT_INVALID_ARGUMENT_BODY`` declares byte counts two
    higher than its chunks are long. That is not a typo: two independent live
    captures (2026-08-13, different notebooks and nonces) declare the same
    pair, and the nonce scrub is width-preserving. This test exists so nobody
    "corrects" the fixture into agreement with ``raw_batchexecute_body`` and
    quietly turns a real capture into a synthetic one — a reviewer proposed
    exactly that.
    """

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(LIVE_CREATE_ARTIFACT_INVALID_ARGUMENT_BODY, id="create_artifact"),
            pytest.param(LIVE_RETRY_ARTIFACT_NOT_FOUND_BODY, id="retry_artifact"),
            pytest.param(LIVE_REVISE_SLIDE_NOT_FOUND_BODY, id="revise_slide"),
        ],
    )
    def test_declared_counts_exceed_the_payloads_by_two(self, body):
        """All three captures show the same +2, on both chunks.

        Parametrized over every live body after a reviewer hand-counted the
        retry and revise chunks as +1 and asked for the comments to be
        "corrected". They are +2; measuring beats counting, so the claim is now
        enforced for each body instead of asserted for one and described for
        the other two.
        """
        lines = body.split("\n")
        # ")]}'", "", <count>, <chunk>, <count>, <trailer>, ""
        assert int(lines[2]) == len(lines[3]) + 2
        assert int(lines[4]) == len(lines[5]) + 2

    def test_decoding_it_records_a_byte_count_mismatch(self):
        """The decoder tolerates the mismatch but counts it as a drift signal."""
        reset_byte_count_mismatch_total()
        try:
            with pytest.raises(RPCError):
                decode_response(
                    LIVE_CREATE_ARTIFACT_INVALID_ARGUMENT_BODY,
                    CREATE_ARTIFACT_METHOD_ID,
                    allow_null=True,
                    raise_on_null_status=True,
                )
            assert byte_count_mismatch_total() == 2
        finally:
            reset_byte_count_mismatch_total()


class TestRaiseOnNullStatus:
    """``allow_null`` tolerates an empty payload, not an explicit rejection."""

    RPC_ID = RPCMethod.CREATE_ARTIFACT.value

    def _raw(self, status):
        chunk = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, status, "generic"])
        return f")]}}'\n{len(chunk)}\n{chunk}\n"

    def test_default_still_swallows_a_status_tagged_null(self):
        """Unchanged for every caller that did not opt in.

        ``REMOVE_RECENTLY_VIEWED`` answers ``[13]`` on what the client treats as
        a successful no-op (tests/cassettes/web/notebooks_remove_from_recent.yaml),
        so blanket strictness would have broken a recorded interaction.
        """
        for code in (3, 5, 7, 13, 16):
            assert decode_response(self._raw([code]), self.RPC_ID, allow_null=True) is None

    def test_opt_in_raises_for_a_non_ok_status(self):
        with pytest.raises(RPCError) as exc_info:
            decode_response(self._raw([3]), self.RPC_ID, allow_null=True, raise_on_null_status=True)

        assert exc_info.value.rpc_code == 3
        assert "invalid argument" in str(exc_info.value).lower()

    def test_opt_in_still_returns_none_without_a_status(self):
        """No status means no reason to report — the null stays a null."""
        assert (
            decode_response(
                self._raw(None), self.RPC_ID, allow_null=True, raise_on_null_status=True
            )
            is None
        )

    def test_opt_in_still_returns_none_for_an_ok_status(self):
        """``[0]`` is OK — an explicitly fine empty payload, not a rejection."""
        assert (
            decode_response(self._raw([0]), self.RPC_ID, allow_null=True, raise_on_null_status=True)
            is None
        )

    def test_opt_in_still_returns_none_for_an_unrecognized_status(self):
        """Out-of-range codes stay unclassified rather than guessed at."""
        assert (
            decode_response(
                self._raw([99]), self.RPC_ID, allow_null=True, raise_on_null_status=True
            )
            is None
        )

    def test_opt_in_routes_account_scoped_codes_through_client_error(self):
        """NOT_FOUND / PERMISSION_DENIED keep their ``ClientError`` routing."""
        with pytest.raises(ClientError) as exc_info:
            decode_response(self._raw([5]), self.RPC_ID, allow_null=True, raise_on_null_status=True)

        assert exc_info.value.rpc_code == 5

    def test_swallowed_rejection_leaves_a_debug_trace(self, caplog):
        """A swallow leaves a trace instead of vanishing — but only at DEBUG.

        WARNING was considered and rejected: three RPCs are RECORDED swallowing
        a status-tagged null on flows this client reports as successful
        (SHARE_NOTEBOOK / SHARE_ARTIFACT ``[3]``, REMOVE_RECENTLY_VIEWED
        ``[13]``), so warning would fire on ordinary ``share add`` traffic and
        assert a benign-vs-broken judgement the evidence does not support.
        """
        with caplog.at_level(logging.DEBUG, logger="notebooklm.rpc.decoder"):
            assert decode_response(self._raw([13]), self.RPC_ID, allow_null=True) is None

        matching = [
            r
            for r in caplog.records
            if "swallowed because the call site did not pass raise_on_null_status" in r.message
        ]
        assert matching, "the swallow left no trace"
        assert all(r.levelno == logging.DEBUG for r in matching)
        assert "Internal" in matching[0].getMessage()

    def test_recorded_swallowing_rpcs_are_declared(self):
        """The three RPCs observed doing this are on the record, not folklore.

        Re-derived from tests/cassettes/ by the sweep in the #2188 PR: 5
        null-result frames out of 397, FOUR of which carry a batchexecute rpc
        id — on exactly these three. (The fifth is the streamed-chat ``[3]``
        from #1472, which has no rpc id and never reaches this decoder.)
        """
        assert set(_RPCS_OBSERVED_SWALLOWING_A_STATUS) == {
            RPCMethod.REMOVE_RECENTLY_VIEWED.value,
            RPCMethod.SHARE_NOTEBOOK.value,
            RPCMethod.SHARE_ARTIFACT.value,
        }

    def test_unclassifiable_status_under_opt_in_warns(self, caplog):
        """A rejection in a shape we cannot name must not be the quietest branch.

        ``[99]`` is outside the gRPC range, so ``rejected`` is False and the
        caller falls back to inventing its own reason — the exact #2188 defect.
        The opt-in says the caller wanted the server's word, so say so.
        """
        with caplog.at_level(logging.DEBUG, logger="notebooklm.rpc.decoder"):
            assert (
                decode_response(
                    self._raw([99]), self.RPC_ID, allow_null=True, raise_on_null_status=True
                )
                is None
            )

        matching = [r for r in caplog.records if "unrecognized index-5 payload" in r.message]
        assert matching, "an unclassifiable rejection payload left no trace"
        assert matching[0].levelno == logging.WARNING

    def test_a_reasonless_null_under_opt_in_stays_quiet(self, caplog):
        """No payload at all is a genuinely empty result, not drift."""
        with caplog.at_level(logging.DEBUG, logger="notebooklm.rpc.decoder"):
            assert (
                decode_response(
                    self._raw(None), self.RPC_ID, allow_null=True, raise_on_null_status=True
                )
                is None
            )

        assert not any("unrecognized index-5 payload" in r.message for r in caplog.records)

    @pytest.mark.parametrize("status", [None, [0]])
    def test_a_benign_null_logs_nothing(self, caplog, status):
        """A genuinely empty payload is not a rejection, so it stays quiet."""
        with caplog.at_level(logging.DEBUG, logger="notebooklm.rpc.decoder"):
            assert decode_response(self._raw(status), self.RPC_ID, allow_null=True) is None

        assert not any("swallowed because" in r.message for r in caplog.records)

    def test_opt_in_appends_a_server_message_when_one_is_sent(self):
        """The #2188 headline path composes label + server text.

        Synthetic by necessity — no captured rejection has ever populated the
        message slot — so the captured-shape case above is the one that pins
        what users see today.
        """
        with pytest.raises(RPCError) as exc_info:
            decode_response(
                self._raw([9, "This notebook has no sources yet"]),
                self.RPC_ID,
                allow_null=True,
                raise_on_null_status=True,
            )

        message = str(exc_info.value)
        assert message.startswith("The server rejected this request (failed precondition).")
        assert message.endswith("This notebook has no sources yet")

    def test_a_later_frames_status_is_not_hidden_by_an_earlier_one(self):
        """Multi-frame ``rt=c``: an unclassifiable placeholder must not shadow.

        The backend can emit several frames for one rpc id. Scanning only the
        first would let a placeholder's unrecognized payload swallow the real
        status on the frame that follows — the pre-#2188 loop avoided that by
        continuing until a code parsed, and the refactor must keep it.
        """
        placeholder = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, [99], "generic"])
        real = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, [3], "generic"])
        raw = f")]}}'\n{len(placeholder)}\n{placeholder}\n{len(real)}\n{real}\n"

        with pytest.raises(RPCError) as exc_info:
            decode_response(raw, self.RPC_ID, allow_null=True, raise_on_null_status=True)

        assert exc_info.value.rpc_code == 3

    def test_unclassified_warning_needs_every_frame_to_be_unclassifiable(self, caplog):
        """One nameable status among several means it is not 'unrecognized'."""
        placeholder = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, [99], "generic"])
        real = json.dumps(["wrb.fr", self.RPC_ID, None, None, None, [3], "generic"])
        raw = f")]}}'\n{len(placeholder)}\n{placeholder}\n{len(real)}\n{real}\n"

        with (
            caplog.at_level(logging.DEBUG, logger="notebooklm.rpc.decoder"),
            pytest.raises(RPCError),
        ):
            decode_response(raw, self.RPC_ID, allow_null=True, raise_on_null_status=True)

        assert not any("unrecognized index-5 payload" in r.message for r in caplog.records)

    def test_opt_in_does_not_change_a_populated_result(self):
        """Strictness only concerns null results."""
        chunk = json.dumps(["wrb.fr", self.RPC_ID, '["art_1"]', None, None, [3], "generic"])
        raw = f")]}}'\n{len(chunk)}\n{chunk}\n"

        result = decode_response(raw, self.RPC_ID, allow_null=True, raise_on_null_status=True)

        assert result == ["art_1"]
