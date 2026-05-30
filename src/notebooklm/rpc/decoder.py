"""Decode RPC responses from NotebookLM batchexecute API."""

import json
import logging
import re
import threading
from enum import IntEnum
from typing import Any

# Import exceptions from centralized module
from ..exceptions import (
    AuthError,
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    UnknownRPCMethodError,
    _truncate_response_preview,
)
from ._safe_index import safe_index

# Re-export for backward compatibility (imports from notebooklm.rpc.decoder still work)
__all__ = [
    "RPCError",
    "AuthError",
    "NetworkError",
    "RPCTimeoutError",
    "RateLimitError",
    "ServerError",
    "ClientError",
    "UnknownRPCMethodError",
    "RPCErrorCode",
    "get_error_message_for_code",
    "strip_anti_xssi",
    "parse_chunked_response",
    "collect_rpc_ids",
    "extract_rpc_result",
    "decode_response",
    "safe_index",
    "byte_count_mismatch_total",
    "reset_byte_count_mismatch_total",
]

logger = logging.getLogger(__name__)

# Number of times ``parse_chunked_response`` has observed a declared byte-count
# that did not match the UTF-8 byte length of the following payload. The
# tolerant parse is *not* affected (mismatches are expected on live multi-chunk
# streams; see ``parse_chunked_response``'s Note: block), but a monotonically
# rising counter is the cheapest drift signal: a sudden jump means Google
# changed the framing unit or the proxy stopped preserving counts. Read-only
# for callers; reset only via ``reset_byte_count_mismatch_total`` (tests).
_BYTE_COUNT_MISMATCH_TOTAL = 0

# Guards every read/increment/reset of ``_BYTE_COUNT_MISMATCH_TOTAL``. The
# counter is mutated from ``parse_chunked_response``, which can run on worker
# threads (``run_in_executor`` / ``ThreadPoolExecutor``) or from several
# per-thread ``NotebookLMClient`` instances at once. ``x += 1`` is a
# read-modify-write over multiple bytecodes, so the GIL does not make it
# atomic — without this lock increments could be lost and the "warn every N"
# cadence could fire on a stale count.
_BYTE_COUNT_MISMATCH_LOCK = threading.Lock()

# Emit at most one byte-count-mismatch WARNING per this many observed
# mismatches so a live multi-chunk response cannot flood CI logs while a real
# drift event still surfaces above DEBUG. The counter is post-incremented
# before the modulo test, so the first mismatch of a process always warns
# (1 % interval == 1) and subsequent warnings land at interval + 1.
_BYTE_COUNT_MISMATCH_WARN_INTERVAL = 500


def byte_count_mismatch_total() -> int:
    """Return the process-wide byte-count-mismatch counter.

    Exposed for drift dashboards / telemetry probes that want to alert on a
    sudden rise without re-parsing logs. The value only ever increases within
    a process; ``reset_byte_count_mismatch_total`` exists for test isolation.
    """
    with _BYTE_COUNT_MISMATCH_LOCK:
        return _BYTE_COUNT_MISMATCH_TOTAL


def reset_byte_count_mismatch_total() -> None:
    """Reset the byte-count-mismatch counter (test isolation only)."""
    global _BYTE_COUNT_MISMATCH_TOTAL
    with _BYTE_COUNT_MISMATCH_LOCK:
        _BYTE_COUNT_MISMATCH_TOTAL = 0


class RPCErrorCode(IntEnum):
    """Known RPC error codes from the batchexecute API.

    These codes are discovered through network traffic analysis and may not be
    exhaustive. Unknown codes will still be reported but without specific handling.
    """

    # Common error codes (discovered through testing)
    UNKNOWN = 0  # Generic/unspecified error
    INVALID_REQUEST = 400  # Malformed request
    UNAUTHORIZED = 401  # Authentication required
    FORBIDDEN = 403  # Insufficient permissions
    NOT_FOUND = 404  # Resource not found
    RATE_LIMITED = 429  # Too many requests
    SERVER_ERROR = 500  # Internal server error


# gRPC canonical status codes (google.rpc.Code) embedded by the batchexecute
# backend at index 5 of a `wrb.fr` response when the RPC returns null result
# data. The bare single-element form `[code]` is what issues #114 and #294
# observed on the wire.
_GRPC_STATUS_MESSAGES: dict[int, str] = {
    0: "OK",
    1: "Cancelled",
    2: "Unknown",
    3: "Invalid argument",
    4: "Deadline exceeded",
    5: "Not found",
    6: "Already exists",
    7: "Permission denied",
    8: "Resource exhausted",
    9: "Failed precondition",
    10: "Aborted",
    11: "Out of range",
    12: "Not implemented",
    13: "Internal",
    14: "Unavailable",
    15: "Data loss",
    16: "Unauthenticated",
}

# Hint appended to NOT_FOUND / PERMISSION_DENIED messages. Deliberately avoids
# the substrings checked by AUTH_ERROR_PATTERNS in _runtime_helpers.py so these errors
# don't incorrectly trigger the auth-refresh retry path.
_ACCOUNT_MISMATCH_HINT = (
    " If you have multiple Google accounts signed in, this is commonly an "
    "account-routing mismatch — the request defaults to account index 0 when "
    "no authuser is set. See issues #114 and #294 for context."
)


# Error code to human-readable message mapping
_ERROR_CODE_MESSAGES: dict[int, tuple[str, bool]] = {
    # (message, is_retryable)
    RPCErrorCode.INVALID_REQUEST: (
        "Invalid request parameters. Check your input and try again.",
        False,
    ),
    RPCErrorCode.UNAUTHORIZED: (
        "Authentication required. Run 'notebooklm login' to re-authenticate.",
        False,
    ),
    RPCErrorCode.FORBIDDEN: (
        "Insufficient permissions for this operation.",
        False,
    ),
    RPCErrorCode.NOT_FOUND: (
        "Requested resource not found.",
        False,
    ),
    RPCErrorCode.RATE_LIMITED: (
        "API rate limit exceeded. Please wait before retrying.",
        True,
    ),
    RPCErrorCode.SERVER_ERROR: (
        "Server error occurred. This is usually temporary - try again later.",
        True,
    ),
}


def get_error_message_for_code(code: int | None) -> tuple[str, bool]:
    """Get human-readable error message and retryability for an error code.

    Args:
        code: Integer error code from API response.

    Returns:
        Tuple of (error_message, is_retryable).
        Returns generic message for unknown codes.
    """
    if code is None:
        return ("Unknown error occurred.", False)

    if code in _ERROR_CODE_MESSAGES:
        return _ERROR_CODE_MESSAGES[code]

    # Unknown code - provide generic guidance based on HTTP status code ranges
    if 400 <= code < 500:
        return (f"Client error {code}. Check your request parameters.", False)
    if 500 <= code < 600:
        return (f"Server error {code}. This is usually temporary - try again later.", True)
    return (f"Error code: {code}", False)


def strip_anti_xssi(response: str) -> str:
    """
    Remove anti-XSSI prefix from response.

    Google APIs prefix responses with )]}' to prevent XSSI attacks.
    This must be stripped before parsing JSON.

    Args:
        response: Raw response text

    Returns:
        Response with prefix removed
    """
    # Handle both Unix (\n) and Windows (\r\n) newlines
    if response.startswith(")]}'"):
        # Find first newline after prefix
        match = re.match(r"\)]\}'\r?\n", response)
        if match:
            return response[match.end() :]
    return response


def parse_chunked_response(response: str) -> list[Any]:
    """
    Parse chunked response format (rt=c mode).

    Format is alternating lines of:
    - byte_count (integer)
    - json_payload

    Args:
        response: Response text after anti-XSSI removal

    Returns:
        List of parsed JSON chunks

    Raises:
        RPCError: If more than 10% of attempted payload records are malformed,
            indicating API issues.

    Note:
        Malformed chunks are skipped with a warning logged. A byte-count line
        without a following payload is malformed. A byte-count mismatch is
        logged at DEBUG and tolerated when the following payload is still
        valid JSON, because recorded and proxy-transformed streams may not
        preserve Google's original byte count and live Google responses use a
        different unit (likely UTF-16 code units) than ``len(s.encode("utf-8"))``.
        Each mismatch also increments the process-wide
        ``byte_count_mismatch_total`` counter and emits a rate-limited WARNING
        (one per ``_BYTE_COUNT_MISMATCH_WARN_INTERVAL`` mismatches) so a real
        framing-unit change is a visible drift signal without the tolerant
        parse changing or per-chunk DEBUG noise flooding CI logs.
        A JSONDecodeError on the payload still emits a WARNING on the
        subsequent parse-failure path. If the malformed-payload rate exceeds
        10%, raises RPCError as this likely indicates API changes. Framing and
        mixed payload/framing corruption keep their own strict guards without
        letting byte-count records dilute the payload-specific threshold.
    """
    global _BYTE_COUNT_MISMATCH_TOTAL

    if not response or not response.strip():
        return []

    chunks = []
    malformed_payload_records = 0
    payload_records = 0
    malformed_framing_records = 0
    framing_records = 0
    response_records = 0
    lines = [line.removesuffix("\r") for line in response.strip().split("\n")]

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Try to parse as byte count
        try:
            byte_count = int(line)
            framing_records += 1
            response_records += 1
            i += 1

            # Next line should be JSON payload
            if i >= len(lines):
                malformed_framing_records += 1
                logger.warning("Skipping byte-count line %d without payload", i)
                continue

            json_str = lines[i]
            payload_records += 1
            actual_byte_count = len(json_str.encode("utf-8"))
            if actual_byte_count != byte_count:
                # DEBUG (not WARNING): live multi-chunk responses trip this on
                # every chunk; see the Note: block in this function's docstring.
                logger.debug(
                    "Chunk at line %d declares %d bytes but payload is %d bytes; "
                    "parsing valid JSON payload anyway. Preview: %s",
                    i + 1,
                    byte_count,
                    actual_byte_count,
                    _truncate_response_preview(json_str),
                )
                # Surface the mismatch as a drift signal WITHOUT changing the
                # tolerant parse: bump a process-wide counter (telemetry probes
                # alert on a sudden rise) and emit a rate-limited WARNING so a
                # real framing-unit change rises above the per-chunk DEBUG noise
                # without flooding CI logs on every live multi-chunk response.
                # Snapshot the count under the lock so the modulo decision and
                # the logged total are consistent even under concurrent parses.
                with _BYTE_COUNT_MISMATCH_LOCK:
                    _BYTE_COUNT_MISMATCH_TOTAL += 1
                    mismatch_total = _BYTE_COUNT_MISMATCH_TOTAL
                if mismatch_total % _BYTE_COUNT_MISMATCH_WARN_INTERVAL == 1:
                    logger.warning(
                        "Byte-count mismatch in chunked response (declared %d, "
                        "actual %d bytes); tolerated. Total mismatches this "
                        "process: %d — a sudden rise may indicate the "
                        "batchexecute framing unit changed.",
                        byte_count,
                        actual_byte_count,
                        mismatch_total,
                    )

            try:
                chunk = json.loads(json_str)
                chunks.append(chunk)
            except json.JSONDecodeError as e:
                # Skip malformed chunks but warn
                malformed_payload_records += 1
                logger.warning(
                    "Skipping malformed chunk at line %d: %s. Preview: %s",
                    i + 1,
                    e,
                    _truncate_response_preview(json_str),
                )
            i += 1
        except ValueError:
            # Not a byte count, try to parse as JSON directly
            payload_records += 1
            response_records += 1
            try:
                chunk = json.loads(line)
                chunks.append(chunk)
            except json.JSONDecodeError as e:
                # Skip non-JSON lines but warn
                malformed_payload_records += 1
                logger.warning(
                    "Skipping non-JSON line at %d: %s. Preview: %s",
                    i + 1,
                    e,
                    _truncate_response_preview(line),
                )
            i += 1

    payload_error_rate = malformed_payload_records / payload_records if payload_records else 0
    framing_error_rate = malformed_framing_records / framing_records if framing_records else 0
    malformed_records = malformed_payload_records + malformed_framing_records
    response_error_rate = malformed_records / response_records if response_records else 0

    # Fail if error rate is too high (indicates API problems)
    if payload_error_rate > 0.1:  # More than 10% malformed
        raise RPCError(
            f"Response parsing failed: {malformed_payload_records} of "
            f"{payload_records} payload records "
            f"malformed. "
            f"This may indicate API changes or data corruption.",
            raw_response=response,
        )

    if framing_error_rate > 0.1:  # More than 10% malformed
        raise RPCError(
            f"Response parsing failed: {malformed_framing_records} of "
            f"{framing_records} framing records malformed. "
            f"This may indicate API changes or data corruption.",
            raw_response=response,
        )

    # Preserve the legacy aggregate strictness after payload/framing-specific
    # checks so mixed corruption does not become more permissive.
    if response_error_rate > 0.1:  # More than 10% malformed
        raise RPCError(
            f"Response parsing failed: {malformed_records} of "
            f"{response_records} response records malformed. "
            f"This may indicate API changes or data corruption.",
            raw_response=response,
        )

    if malformed_payload_records > 0:
        logger.warning(
            "Parsed response but skipped %d malformed payload chunks (%d%%). "
            "Results may be incomplete.",
            malformed_payload_records,
            int(payload_error_rate * 100),
        )

    if malformed_framing_records > 0:
        logger.warning(
            "Parsed response but skipped %d malformed framing records (%d%%). "
            "Results may be incomplete.",
            malformed_framing_records,
            int(framing_error_rate * 100),
        )

    return chunks


def collect_rpc_ids(chunks: list[Any]) -> list[str]:
    """Collect all RPC IDs found in response chunks.

    Collects IDs from both successful (wrb.fr) and error (er) responses.
    Useful for debugging when expected RPC ID is not found.

    Args:
        chunks: Parsed response chunks from parse_chunked_response().

    Returns:
        List of RPC method IDs found in the response.
    """
    source = "decoder.collect_rpc_ids"
    found_ids = []
    for chunk in chunks:
        if not isinstance(chunk, list):
            continue

        # Preserve the truthy short-circuit on an empty chunk so safe_index
        # (which raises on shape drift under strict decoding) is only called
        # when the index is structurally valid.
        if not chunk:
            continue
        first = safe_index(chunk, 0, method_id=None, source=source)
        items = chunk if isinstance(first, list) else [chunk]

        for item in items:
            if not isinstance(item, list) or len(item) < 2:
                continue

            tag = safe_index(item, 0, method_id=None, source=source)
            rpc_id = safe_index(item, 1, method_id=None, source=source)
            if tag in ("wrb.fr", "er") and isinstance(rpc_id, str):
                found_ids.append(rpc_id)

    return found_ids


def _extract_status_code(error_info: Any) -> tuple[int, str] | None:
    """Extract a bare status code from a wrb.fr error_info block.

    Returns ``(code, label)`` only for the bare single-element form ``[code]``
    in the gRPC canonical range (0-16). Longer structures (e.g. the
    ``[8, None, [[UserDisplayableError, ...]]]`` rate-limit shape) are handled
    by the UserDisplayableError path and fall through here by returning
    ``None``.

    Note: we do not claim these codes are unambiguously gRPC — REMOVE_RECENTLY_VIEWED
    returns ``[13]`` on what the client treats as a successful no-op (see
    tests/cassettes/notebooks_remove_from_recent.yaml). Callers must respect
    ``allow_null`` semantics before treating the code as an error.

    Args:
        error_info: Value at index 5 of a ``wrb.fr`` response item.

    Returns:
        ``(code, label)`` tuple for a recognized bare status, else ``None``.
    """
    if not isinstance(error_info, list) or len(error_info) != 1:
        return None
    code = error_info[0]
    # type(code) is int (not isinstance) — bool is a subclass of int, so
    # isinstance(True, int) is True and would accept [true] as code 1.
    # Gate on _GRPC_STATUS_MESSAGES membership so this auto-tracks the table.
    if type(code) is not int or code not in _GRPC_STATUS_MESSAGES:
        return None
    return code, _GRPC_STATUS_MESSAGES[code]


def _find_wrb_status(chunks: list[Any], rpc_id: str) -> tuple[int, str] | None:
    """Locate bare status code at index 5 of a wrb.fr entry for ``rpc_id``.

    Used by ``decode_response`` to enrich the null-result error message when
    the server explicitly flagged the RPC with a status code.
    """
    source = "decoder._find_wrb_status"
    for chunk in chunks:
        if not isinstance(chunk, list):
            continue
        # Skip empty chunks before safe_index, which raises on shape drift
        # under strict decoding on an out-of-bounds descent.
        if not chunk:
            continue
        first = safe_index(chunk, 0, method_id=rpc_id, source=source)
        items = chunk if isinstance(first, list) else [chunk]
        for item in items:
            if not isinstance(item, list) or len(item) < 6:
                continue
            tag = safe_index(item, 0, method_id=rpc_id, source=source)
            id_field = safe_index(item, 1, method_id=rpc_id, source=source)
            if tag != "wrb.fr" or id_field != rpc_id:
                continue
            result_data = safe_index(item, 2, method_id=rpc_id, source=source)
            error_info = safe_index(item, 5, method_id=rpc_id, source=source)
            if result_data is not None or error_info is None:
                continue
            status = _extract_status_code(error_info)
            if status is not None:
                return status
    return None


def _contains_user_displayable_error(obj: Any) -> bool:
    """Check if object contains a UserDisplayableError marker.

    Google's API embeds error information in index 5 of wrb.fr responses
    when the operation fails due to rate limiting, quota, or other
    user-facing restrictions.

    Args:
        obj: Object to search (typically index 5 of response item)

    Returns:
        True if UserDisplayableError pattern is found
    """
    if isinstance(obj, str):
        return "UserDisplayableError" in obj
    if isinstance(obj, list):
        return any(_contains_user_displayable_error(item) for item in obj)
    if isinstance(obj, dict):
        return any(_contains_user_displayable_error(v) for v in obj.values())
    return False


def _extract_user_displayable_status(error_info: Any) -> tuple[int, str] | None:
    """Extract the leading gRPC status from a UserDisplayableError block."""
    if not isinstance(error_info, list) or not error_info:
        return None
    code = error_info[0]
    if type(code) is not int or code not in _GRPC_STATUS_MESSAGES:
        return None
    return code, _GRPC_STATUS_MESSAGES[code]


def _user_displayable_error_message(error_info: Any) -> str:
    """Build a non-sensitive diagnostic for a user-displayable rejection."""
    message = "API rate limit or quota exceeded. Please wait before retrying."
    status = _extract_user_displayable_status(error_info)
    if status is None:
        return message
    code, label = status
    return f"{message} Upstream status code {code} ({label})."


_SENTINEL_NO_RESULT = object()


def extract_rpc_result(chunks: list[Any], rpc_id: str) -> Any:
    """Extract result data for a specific RPC ID from chunks.

    In ``rt=c`` streamed mode the backend can emit more than one ``wrb.fr``
    frame for a single ``rpc_id`` (e.g. a null placeholder frame followed by
    the final populated frame). We iterate every frame and return the result
    of the **last non-null** ``wrb.fr`` frame for ``rpc_id`` so the placeholder
    does not shadow the real payload. ``er`` frames and embedded
    ``UserDisplayableError`` markers still raise immediately — those are
    terminal signals, not placeholders to be superseded.

    For single-frame responses (every existing golden fixture) the first and
    last usable frame are identical, so behaviour is unchanged.
    """
    source = "decoder.extract_rpc_result"
    # Track the last usable result so a later populated frame wins over an
    # earlier null placeholder. ``_SENTINEL_NO_RESULT`` distinguishes "no
    # wrb.fr frame seen yet" from "the last frame genuinely carried null".
    last_result: Any = _SENTINEL_NO_RESULT
    for chunk in chunks:
        if not isinstance(chunk, list):
            continue

        # Skip empty chunks before safe_index, which raises on shape drift
        # under strict decoding on an out-of-bounds descent.
        if not chunk:
            continue
        first = safe_index(chunk, 0, method_id=rpc_id, source=source)
        items = chunk if isinstance(first, list) else [chunk]

        for item in items:
            if not isinstance(item, list) or len(item) < 3:
                continue

            tag = safe_index(item, 0, method_id=rpc_id, source=source)
            id_field = safe_index(item, 1, method_id=rpc_id, source=source)

            if tag == "er" and id_field == rpc_id:
                error_code = safe_index(item, 2, method_id=rpc_id, source=source)

                # Try to get human-readable message for integer error codes
                if isinstance(error_code, int):
                    error_msg, is_retryable = get_error_message_for_code(error_code)
                    logger.debug(
                        "RPC error code %d for %s: %s (retryable: %s)",
                        error_code,
                        rpc_id,
                        error_msg,
                        is_retryable,
                    )
                else:
                    error_msg = str(error_code) if error_code else "Unknown error"

                raise RPCError(
                    error_msg,
                    method_id=rpc_id,
                    rpc_code=error_code,
                )

            if tag == "wrb.fr" and id_field == rpc_id:
                result_data = safe_index(item, 2, method_id=rpc_id, source=source)

                # Check for embedded UserDisplayableError when result is null
                # This indicates rate limiting, quota exceeded, or other API restrictions
                if result_data is None and len(item) > 5:
                    error_info = safe_index(item, 5, method_id=rpc_id, source=source)
                    if error_info is not None and _contains_user_displayable_error(error_info):
                        raise RateLimitError(
                            _user_displayable_error_message(error_info),
                            method_id=rpc_id,
                            rpc_code="USER_DISPLAYABLE_ERROR",
                        )

                if isinstance(result_data, str):
                    try:
                        parsed: Any = json.loads(result_data)
                    except json.JSONDecodeError:
                        parsed = result_data
                else:
                    parsed = result_data

                # Prefer a later populated frame over an earlier null
                # placeholder; only let a null frame overwrite a previous
                # usable result when nothing better followed (it won't, since
                # we never downgrade a non-null result back to null).
                if parsed is not None or last_result is _SENTINEL_NO_RESULT:
                    last_result = parsed

    if last_result is _SENTINEL_NO_RESULT:
        return None
    return last_result


def decode_response(raw_response: str, rpc_id: str, allow_null: bool = False) -> Any:
    """
    Complete decode pipeline: strip prefix -> parse chunks -> extract result.

    Args:
        raw_response: Raw response text from batchexecute
        rpc_id: RPC method ID to extract result for
        allow_null: If True, return None instead of raising error when result is null

    Returns:
        Decoded result data

    Raises:
        RPCError: If RPC returned an error or result not found (when allow_null=False)
    """
    logger.debug("Decoding response: size=%d bytes", len(raw_response))
    cleaned = strip_anti_xssi(raw_response)
    chunks = parse_chunked_response(cleaned)
    logger.debug("Parsed %d chunks from response", len(chunks))

    # Pass the full cleaned body to exception constructors; ``RPCError.__init__``
    # routes ``raw_response`` through ``_truncate_response_preview`` so the
    # truncation contract (and ``NOTEBOOKLM_DEBUG=1`` opt-in) lives in one
    # place. The one branch that bypasses ``__init__`` (direct attribute set
    # on an already-constructed exception) calls the helper explicitly at the
    # call site below.
    response_preview = cleaned

    # Collect all RPC IDs for debugging
    found_ids = collect_rpc_ids(chunks)

    logger.debug("Looking for RPC ID: %s", rpc_id)
    logger.debug("Found RPC IDs in response: %s", found_ids)

    try:
        result = extract_rpc_result(chunks, rpc_id)
    except RPCError as e:
        # Add context to errors from extract_rpc_result. This branch sets
        # ``raw_response`` directly on an already-constructed exception, so
        # ``__init__`` does not run again — apply the helper explicitly here
        # to honor the truncation contract.
        if not e.found_ids:
            e.found_ids = found_ids
        if not e.raw_response:
            e.raw_response = _truncate_response_preview(response_preview)
        raise

    if result is None:
        # An *absent* RPC ID is categorically different from a *present-but-null*
        # result. The drift detector — the strongest signal that Google changed a
        # method ID or served an anti-bot/redirect wall instead of real RPC data —
        # must fire even when ``allow_null=True``; ``allow_null`` only sanctions a
        # ``wrb.fr`` frame that genuinely carried a null payload, not a response
        # that never contained the requested ID at all.
        if found_ids and rpc_id not in found_ids:
            # Method ID likely changed - provide actionable error
            raise UnknownRPCMethodError(
                f"No result found for RPC ID '{rpc_id}'. "
                f"Response contains IDs: {found_ids}. "
                f"The RPC method ID may have changed.",
                method_id=rpc_id,
                found_ids=list(found_ids),
                raw_response=response_preview,
            )

        if not found_ids:
            # No RPC data found at all — the response carried no recognizable RPC
            # frames (e.g. an anti-bot/redirect HTML page). This is never a benign
            # null, so raise regardless of ``allow_null``.
            raise RPCError(
                f"No result found for RPC ID: {rpc_id} "
                f"(response contained no RPC data — {len(chunks)} chunks parsed)",
                method_id=rpc_id,
                raw_response=response_preview,
            )

        # ``rpc_id`` is present in ``found_ids`` but ``extract_rpc_result`` returned
        # None — the requested frame carried a genuinely null payload. Honor
        # ``allow_null`` here and return None for callers that opt in.
        if allow_null:
            return None

        # RPC ID was found but extract_rpc_result returned None.
        # This means wrb.fr had null result_data without UserDisplayableError.
        # Enrich the message if the server attached a bare status code at
        # index 5 (issues #114 / #294 showed GET_NOTEBOOK returning [5]).
        status = _find_wrb_status(chunks, rpc_id)
        # The base ``RPCError.__str__`` does not surface ``found_ids``, so embed
        # it in the message text too — otherwise the strongest debugging signal
        # is silently dropped from plain logs and tracebacks for these branches.
        found_ids_suffix = f" Found IDs: {found_ids}."
        if status is not None:
            code, label = status
            message = f"RPC {rpc_id} returned null result with status code {code} ({label})."
            # Route NOT_FOUND (5) / PERMISSION_DENIED (7) through ClientError
            # so is_auth_error does not misclassify them as auth
            # failures and trigger a spurious token-refresh retry. The
            # account-routing hint is only relevant for these two codes —
            # other codes (e.g. INTERNAL 13) get a plain message.
            if code in (5, 7):
                raise ClientError(
                    message + found_ids_suffix + _ACCOUNT_MISMATCH_HINT,
                    method_id=rpc_id,
                    rpc_code=code,
                    found_ids=found_ids,
                    raw_response=response_preview,
                )
            raise RPCError(
                message + found_ids_suffix,
                method_id=rpc_id,
                rpc_code=code,
                found_ids=found_ids,
                raw_response=response_preview,
            )
        raise RPCError(
            f"RPC {rpc_id} returned null result data "
            f"(possible server error or parameter mismatch).{found_ids_suffix}",
            method_id=rpc_id,
            found_ids=found_ids,
            raw_response=response_preview,
        )

    return result
