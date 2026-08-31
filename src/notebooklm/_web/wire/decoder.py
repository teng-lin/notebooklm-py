"""Decode RPC responses from NotebookLM batchexecute API."""

import json
import logging
import re
import threading
from enum import IntEnum
from typing import Any

from ..._logging import _truncate_response_preview
from ..._types.enums import GrpcStatusCode

# Import exceptions from centralized module
from ...exceptions import (
    AuthError,
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    UnknownRPCMethodError,
)
from ...rpc.types import RPCMethod
from .safe_index import safe_index

# Re-export through ``notebooklm.rpc`` for backward compatibility.
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

# Preserve the established logging category across the private module move.
logger = logging.getLogger("notebooklm.rpc.decoder")

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
# atomic — without this lock increments could be lost and the counter read by
# ``byte_count_mismatch_total`` could be stale.
_BYTE_COUNT_MISMATCH_LOCK = threading.Lock()


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


# Human-readable labels for the gRPC canonical status codes the batchexecute
# backend embeds at index 5 of a `wrb.fr` response when the RPC returns null
# result data. The bare single-element form `[code]` is what issues #114 and
# #294 observed on the wire.
#
# The codes themselves live in the transport-neutral ``_types/enums.py``
# ``GrpcStatusCode`` definition and are compatibility-re-exported by
# ``rpc/types.py``. This table only supplies the wording, and is keyed by the
# enum so a member added there without a label fails the guardrail test rather
# than silently degrading to an unlabelled status.
_GRPC_STATUS_MESSAGES: dict[int, str] = {
    GrpcStatusCode.OK: "OK",
    GrpcStatusCode.CANCELLED: "Cancelled",
    GrpcStatusCode.UNKNOWN: "Unknown",
    GrpcStatusCode.INVALID_ARGUMENT: "Invalid argument",
    GrpcStatusCode.DEADLINE_EXCEEDED: "Deadline exceeded",
    GrpcStatusCode.NOT_FOUND: "Not found",
    GrpcStatusCode.ALREADY_EXISTS: "Already exists",
    GrpcStatusCode.PERMISSION_DENIED: "Permission denied",
    GrpcStatusCode.RESOURCE_EXHAUSTED: "Resource exhausted",
    GrpcStatusCode.FAILED_PRECONDITION: "Failed precondition",
    GrpcStatusCode.ABORTED: "Aborted",
    GrpcStatusCode.OUT_OF_RANGE: "Out of range",
    GrpcStatusCode.UNIMPLEMENTED: "Not implemented",
    GrpcStatusCode.INTERNAL: "Internal",
    GrpcStatusCode.UNAVAILABLE: "Unavailable",
    GrpcStatusCode.DATA_LOSS: "Data loss",
    GrpcStatusCode.UNAUTHENTICATED: "Unauthenticated",
}

# Index 5 of a ``wrb.fr`` frame is a JSON-array-encoded ``google.rpc.Status``
# (the PUBLIC google/rpc/status.proto envelope, not a Tailwind message), so the
# array positions are the proto tags minus one:
#
#   ===== ================== ==================================================
#   Index Field              Evidence
#   ===== ================== ==================================================
#   0     ``code`` (tag 1)   Live: ``[3]`` INVALID_ARGUMENT from CREATE_ARTIFACT
#                            on a source-less notebook and ``[5]`` NOT_FOUND for
#                            an unknown notebook id (2026-08-13); ``[13]`` in
#                            tests/cassettes/web/notebooks_remove_from_recent.yaml;
#                            ``8`` (RESOURCE_EXHAUSTED) leading the recorded
#                            ``UserDisplayableError`` shape.
#   1     ``message`` (tag 2) NEVER OBSERVED POPULATED. Null in the recorded
#                            rate-limit sample and absent from every captured
#                            bare-status frame (the array is length 1). Read
#                            defensively below so a server-authored reason is
#                            surfaced *if* one ever arrives, but the
#                            client-authored wording remains the observed
#                            ceiling — see docs/rpc-reference.md and #2188.
#   2     ``details``(tag 3) The ``[["type.googleapis.com/…UserDisplayableError",
#                            …]]`` block; ``ErrorPayloadRow._ENTRIES_POS``
#                            models the same slot for streamed chat.
#   ===== ================== ==================================================
#
# ``_web/rows/chat.py`` models the same envelope for streamed chat. Its
# POSITIONS are declared separately because it imports ``notebooklm.rpc``, so a
# decoder-side import of it would close a cycle — this table is the shared
# rationale for both. The normalising rule is NOT duplicated: both layers call
# :func:`sanitize_status_message` below, so they cannot drift in what users see.
_STATUS_MESSAGE_POS = 1

#: Ceiling on how much server-authored status text is echoed into an exception
#: message. The field is server-controlled and unbounded; a runaway payload must
#: not become a multi-kilobyte exception string.
_MAX_STATUS_MESSAGE_CHARS = 300

#: RPCs observed answering a null result with a non-OK ``google.rpc.Status`` on
#: a flow this client currently treats as SUCCESSFUL. A sweep of all 141
#: cassettes finds 397 ``wrb.fr`` frames, 5 of them null-result. FOUR of those
#: five reach this decoder, across three RPCs:
#:
#:   ``REMOVE_RECENTLY_VIEWED``  ``[13]``  notebooks_remove_from_recent.yaml
#:   ``SHARE_NOTEBOOK``          ``[3]``   cli_share_add.yaml, cli_share_remove.yaml
#:   ``SHARE_ARTIFACT``          ``[3]``   notebooks_share.yaml
#:
#: The fifth is chat_ask_oversized_rejection.yaml's ``[3]`` (#1472). It carries
#: no rpc id at all because streamed chat is not a ``batchexecute`` RPC, so it
#: never passes through ``decode_response`` — it is handled by
#: ``_web/rows/chat_stream.py``
#: and is not part of this list.
#:
#: Recorded here as a *finding*, not a blessing: only the first has ever been
#: reasoned about (a cosmetic no-op), and whether the two share rejections are
#: benign or a silently-dropped refusal is an open question this change does not
#: answer. That unresolved-ness is exactly why the swallow below logs at DEBUG
#: rather than WARNING — warning would fire on ordinary ``share add`` traffic and
#: assert a judgement the evidence does not support (#2188).
_RPCS_OBSERVED_SWALLOWING_A_STATUS = (
    RPCMethod.REMOVE_RECENTLY_VIEWED.value,
    RPCMethod.SHARE_NOTEBOOK.value,
    RPCMethod.SHARE_ARTIFACT.value,
)

#: Statuses the decoder routes through ``ClientError`` rather than the generic
#: ``RPCError``, so ``is_auth_error`` cannot misclassify them and fire a
#: spurious token refresh. Both also carry the account-routing hint below.
_ACCOUNT_ROUTED_STATUSES = (GrpcStatusCode.NOT_FOUND, GrpcStatusCode.PERMISSION_DENIED)

# Hint appended to NOT_FOUND / PERMISSION_DENIED messages. Deliberately avoids
# the substrings checked by AUTH_ERROR_PATTERNS in _runtime/helpers.py so these errors
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
        RPCError: If more than 10% of payload, framing, or aggregate response
            records are malformed, indicating API issues.

    Note:
        Malformed chunks are skipped with a warning logged. A byte-count line
        without a following payload is malformed. A byte-count mismatch is
        logged at DEBUG and tolerated when the following payload is still
        valid JSON, because recorded and proxy-transformed streams may not
        preserve Google's original byte count and live Google responses use a
        different unit (likely UTF-16 code units) than ``len(s.encode("utf-8"))``.
        Because a mismatch is the *expected* case on healthy live streams, it is
        deliberately NOT a WARNING: it only increments the process-wide
        ``byte_count_mismatch_total`` counter, which is the honest drift signal
        (telemetry alerts on a sudden *rate-of-change*, not on the existence of
        mismatches). A JSONDecodeError on the payload still emits a WARNING on the
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
                # Surface the mismatch as a drift signal without escalating to a
                # WARNING (which would fire on essentially every multi-chunk
                # response): bump only the process-wide counter. Telemetry probes
                # alert on a sudden *rate-of-change* via
                # ``byte_count_mismatch_total()``.
                with _BYTE_COUNT_MISMATCH_LOCK:
                    _BYTE_COUNT_MISMATCH_TOTAL += 1

            try:
                chunk = json.loads(json_str)
                chunks.append(chunk)
            except (json.JSONDecodeError, RecursionError) as e:
                # Skip malformed chunks but warn. RecursionError covers
                # pathologically deep JSON that overflows the interpreter
                # stack inside ``json.loads`` — treat it as malformed rather
                # than letting it escape the decode pipeline raw (#2107).
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
            except (json.JSONDecodeError, RecursionError) as e:
                # Skip non-JSON lines but warn (RecursionError: same
                # deep-JSON hardening as the byte-count branch above, #2107)
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
    """Extract the leading status code from a wrb.fr error_info block.

    ``error_info`` is a ``google.rpc.Status`` array, so the code is at index 0
    whatever the array's length: the bare ``[code]`` form issues #114 / #294
    observed, and the longer ``[code, message, details]` form the proto allows.
    Only the leading value is inspected, and only when it is an ``int`` in the
    gRPC canonical range; anything else returns ``None``.

    The arity restriction this used to carry (``len(error_info) != 1``) made
    every longer status invisible — including the ``message`` slot #2188 is
    about, which can only ever appear in a longer array. The
    ``[8, None, [[UserDisplayableError, ...]]]`` rate-limit shape still raises
    on the ``UserDisplayableError`` path *before* this helper is consulted; when
    that path does not fire (e.g. the marker scan hit its depth cap), reporting
    the leading status is strictly better than the unqualified
    "empty result" fallback.

    Note: we do not claim these codes are unambiguously gRPC — REMOVE_RECENTLY_VIEWED
    returns ``[13]`` on what the client treats as a successful no-op (see
    tests/cassettes/web/notebooks_remove_from_recent.yaml). Callers must respect
    ``allow_null`` semantics before treating the code as an error.

    Args:
        error_info: Value at index 5 of a ``wrb.fr`` response item.

    Returns:
        ``(code, label)`` tuple for a recognized status, else ``None``.
    """
    if not isinstance(error_info, list) or not error_info:
        return None
    code = error_info[0]
    # type(code) is int (not isinstance) — bool is a subclass of int, so
    # isinstance(True, int) is True and would accept [true] as code 1.
    # Gate on _GRPC_STATUS_MESSAGES membership so this auto-tracks the table.
    if type(code) is not int or code not in _GRPC_STATUS_MESSAGES:
        return None
    return code, _GRPC_STATUS_MESSAGES[code]


def _null_result_payloads(chunks: list[Any], rpc_id: str) -> list[Any]:
    """Every non-null index-5 payload on a null-result frame for ``rpc_id``.

    Returns ALL of them rather than the first: in ``rt=c`` streamed mode the
    backend can emit more than one frame per rpc id (``extract_rpc_result``
    handles the same case), so stopping at the first would let a placeholder
    frame's unclassifiable payload hide a real status on a later frame — which
    is what the pre-#2188 loop avoided by continuing until a code parsed.

    Shared walk behind :func:`_find_wrb_status` and
    :func:`_has_null_result_payload` so the two agree on which frames they mean.
    """
    source = "decoder._null_result_payloads"
    payloads: list[Any] = []
    for chunk in chunks:
        if not isinstance(chunk, list) or not chunk:
            # Skip empty chunks before safe_index, which raises on shape drift
            # under strict decoding on an out-of-bounds descent.
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
            payloads.append(error_info)
    return payloads


def _has_null_result_payload(chunks: list[Any], rpc_id: str) -> bool:
    """Whether the server attached anything at index 5 of a null-result frame.

    Distinguishes "no payload at all" (a genuinely empty result — nothing to
    report) from "a payload is there". The only caller consults this AFTER
    :func:`_find_wrb_status` has already returned ``None``, so at that point
    "a payload is there" means "there, and this decoder could not name it" —
    which is drift worth logging. Re-checking classifiability here would be
    dead logic, so it is not re-checked.
    """
    return bool(_null_result_payloads(chunks, rpc_id))


def _find_wrb_status(chunks: list[Any], rpc_id: str) -> tuple[int, str, Any] | None:
    """Locate the status code at index 5 of a null-result frame for ``rpc_id``.

    Used by ``decode_response`` to enrich the null-result error message when
    the server explicitly flagged the RPC with a status code.

    Returns ``(code, label, payload)`` where ``payload`` is the raw
    ``google.rpc.Status`` array, so the caller can also read the server's
    ``message`` field without re-walking the chunks.
    """
    for payload in _null_result_payloads(chunks, rpc_id):
        status = _extract_status_code(payload)
        if status is not None:
            code, label = status
            return code, label, payload
    return None


def _contains_user_displayable_error(obj: Any, max_depth: int = 20) -> bool:
    """Check if object contains a UserDisplayableError marker.

    Google's API embeds error information in index 5 of wrb.fr responses
    when the operation fails due to rate limiting, quota, or other
    user-facing restrictions.

    Recursion is bounded because ``obj`` is server-controlled: a maliciously
    or accidentally deep payload must not raise ``RecursionError`` (#2107).
    Real markers live a handful of levels deep, so the cap is generous; at
    the cap we treat the payload as carrying no marker, which lets
    :func:`decode_response` fall through to its regular null-result handling
    (mapped ``RPCError``) instead of a false rate-limit signal.

    Args:
        obj: Object to search (typically index 5 of response item)
        max_depth: Maximum nesting depth to descend before giving up

    Returns:
        True if UserDisplayableError pattern is found
    """
    if max_depth <= 0:
        return False
    if isinstance(obj, str):
        return "UserDisplayableError" in obj
    if isinstance(obj, (list, dict)) and max_depth == 1:
        # Stop at the container itself so a wide list/dict at the boundary
        # logs a single warning instead of one per child.
        logger.warning("Max recursion depth reached in UserDisplayableError detection")
        return False
    if isinstance(obj, list):
        return any(_contains_user_displayable_error(item, max_depth - 1) for item in obj)
    if isinstance(obj, dict):
        return any(_contains_user_displayable_error(v, max_depth - 1) for v in obj.values())
    return False


def sanitize_status_message(value: Any, *, source: str) -> str | None:
    """Normalize a raw ``google.rpc.Status.message`` slot into display text.

    Shared by the two layers that read the slot — this decoder for
    ``batchexecute`` frames and ``_web.rows.chat.ErrorPayloadRow`` for the
    streamed-chat envelope. Only the *positions* are declared per layer (the
    ``rpc`` → ``_web.rows`` import direction forbids sharing those); the
    sanitising rule is here so the two cannot drift in what users see.

    The rule is deliberately narrow, because **no captured response has ever
    carried a populated message** (#2188): only a non-empty ``str`` counts, so
    the enrichment is inert on all traffic observed to date and cannot invent a
    "reason" out of a slot holding something else — the failure mode #2134 was
    about.

    A non-string, non-null value is the interesting case: it would mean the
    envelope is not the ``google.rpc.Status`` this client believes it is. That
    cannot raise — this runs while *reporting* an error, and replacing a real
    server rejection with a decoder crash is a worse outcome than the
    client-authored fallback — but per ADR-0011's spirit it must not be
    inaudible either, or the only evidence of the drift would be that the
    message silently failed to change. So it warns and degrades.

    Whitespace is collapsed and the text capped at
    ``_MAX_STATUS_MESSAGE_CHARS`` so a server-controlled field cannot blow up
    an exception message.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        logger.warning(
            "google.rpc.Status.message slot held %s, not a string (%s); "
            "ignoring it and keeping the client-authored wording",
            type(value).__name__,
            source,
        )
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > _MAX_STATUS_MESSAGE_CHARS:
        text = text[:_MAX_STATUS_MESSAGE_CHARS].rstrip() + "…"
    return text


def _server_status_message(status_payload: Any) -> str | None:
    """Return the server-authored ``google.rpc.Status.message``, or ``None``.

    ``status_payload`` is the raw value at index 5 of a ``wrb.fr`` frame — a
    JSON-array-encoded ``google.rpc.Status`` whose ``message`` field (tag 2)
    lands at index 1 (see the position table near ``_STATUS_MESSAGE_POS``).
    """
    if not isinstance(status_payload, list) or len(status_payload) <= _STATUS_MESSAGE_POS:
        return None
    return sanitize_status_message(
        status_payload[_STATUS_MESSAGE_POS], source="decoder.wrb.fr[5][1]"
    )


def _user_displayable_error_message(error_info: Any) -> str:
    """Build a diagnostic for a user-displayable rejection.

    The client-authored parts stay free of volatile internal detail — no
    obfuscated method id, no raw numeric gRPC code (#1921). The server's own
    ``message``, when present, is echoed as-is (whitespace-collapsed and capped)
    because it is by definition the text Google chose to *display* to this user;
    that part is outside this client's control, so "non-sensitive" is a
    guarantee only over the wording this client writes.

    Prefers the server's own ``google.rpc.Status.message`` when one is present;
    otherwise falls back to the client-authored sentence below. The two are
    NOT interchangeable and the distinction matters when reading a bug report:
    the fallback is this client's guess at what a ``UserDisplayableError``
    means, and every recorded sample has taken that branch.
    """
    message = _server_status_message(error_info) or (
        "API rate limit or quota exceeded. Please wait before retrying."
    )
    # Same ``google.rpc.Status`` envelope as the bare-status path, so the same
    # leading-code extractor serves both. Before #2188 these were two helpers
    # differing only in their arity guard (``len(error_info) != 1`` here versus
    # ``not error_info`` there); dropping the arity gate collapsed the
    # difference, so the duplicate was removed rather than kept in sync.
    status = _extract_status_code(error_info)
    if status is None:
        return message
    _code, label = status
    # Keep the stable, human-readable status label but not the raw numeric gRPC
    # code — it carries no end-user value and is volatile internal detail
    # (#1921). The numeric code stays available on the exception's ``rpc_code``.
    return f"{message} (Upstream: {label}.)"


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

    For single-frame responses, the first and last usable frame are identical,
    so behaviour is unchanged; multi-frame golden fixtures pin the
    "last non-null frame wins" behavior.
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
                    except (json.JSONDecodeError, RecursionError):
                        # RecursionError: pathologically deep JSON inside the
                        # result_data string — same server-controlled-depth
                        # hardening as parse_chunked_response (#2107). Fall
                        # back to the raw string like any unparseable payload.
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


def decode_response(
    raw_response: str,
    rpc_id: str,
    allow_null: bool = False,
    *,
    raise_on_null_status: bool = False,
) -> Any:
    """
    Complete decode pipeline: strip prefix -> parse chunks -> extract result.

    Args:
        raw_response: Raw response text from batchexecute
        rpc_id: RPC method ID to extract result for
        allow_null: If True, return None instead of raising error when result is null
        raise_on_null_status: Opt-in strictness for ``allow_null`` callers. When
            True, a null result the server tagged with a **recognized** non-OK
            ``google.rpc.Status`` at index 5 (a code this decoder can name — see
            ``_GRPC_STATUS_MESSAGES``; an unrecognized one still decodes to
            ``None``, loudly) raises instead of decoding to ``None``, so the
            caller reports the server's reason rather than inventing one.
            Callers that tolerate a status-tagged null leave it False. This is
            opt-in per call site rather than a blanket change because three RPCs
            are RECORDED doing exactly that on flows the client reports as
            successful — see ``_RPCS_OBSERVED_SWALLOWING_A_STATUS`` (#2188).

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

        # RPC ID was found but extract_rpc_result returned None.
        # This means wrb.fr had null result_data without UserDisplayableError.
        # Enrich the message if the server attached a bare status code at
        # index 5 (issues #114 / #294 showed GET_NOTEBOOK returning [5]).
        #
        # The obfuscated ``rpc_id`` (the #1 breakage class per CLAUDE.md — it
        # changes whenever Google re-obfuscates a method) and the raw numeric
        # gRPC ``code`` carry no value for the end user and, when surfaced
        # through MCP/CLI, leak volatile internal detail (#1921). Keep them on
        # the exception ATTRIBUTES (``method_id`` / ``rpc_code`` / ``found_ids``)
        # for logging and debugging, but keep the human-readable message free of
        # them — only the stable gRPC status label is user-facing.
        status = _find_wrb_status(chunks, rpc_id)

        # ``rpc_id`` is present in ``found_ids`` but ``extract_rpc_result``
        # returned None — the requested frame carried a genuinely null payload.
        # Honor ``allow_null`` and return None for callers that opt in, EXCEPT
        # when the caller asked for strictness and the server tagged that null
        # with a non-OK status. A status-tagged null is the server saying "no",
        # not an empty-but-fine payload; swallowing it made ``generate_*``
        # report "<type> generation is unavailable" for a live INVALID_ARGUMENT
        # rejection (#2188).
        rejected = status is not None and status[0] != GrpcStatusCode.OK
        if allow_null and not (raise_on_null_status and rejected):
            if rejected and status is not None:
                # Still swallowed, but no longer without trace. DEBUG, not
                # WARNING: three RPCs are recorded doing this on flows the
                # client reports as successful (see
                # ``_RPCS_OBSERVED_SWALLOWING_A_STATUS``), so warning would fire
                # on ordinary ``share add`` traffic while asserting a
                # benign-vs-broken judgement nobody has made yet. The trace is
                # what makes such a swallow findable; deciding which of them are
                # real failures needs its own evidence (#2188).
                logger.debug(
                    "Null result for %s carried status %s; swallowed because the "
                    "call site did not pass raise_on_null_status",
                    rpc_id,
                    status[1],
                )
            elif raise_on_null_status and _has_null_result_payload(chunks, rpc_id):
                # The caller asked for strictness and the server DID attach
                # something at index 5, but not a status this decoder can name —
                # a code outside 0-16, a bool, a string, a nested ``[[3]]``. That
                # is a rejection in a shape we do not recognise, so it lands back
                # on the very defect #2188 fixed (the caller invents its own
                # reason) and must not be the quietest branch here.
                logger.warning(
                    "Null result for %s carried an unrecognized index-5 payload; "
                    "treating it as a benign null. If %s is being reported as "
                    "unavailable, this is the drift to look at",
                    rpc_id,
                    rpc_id,
                )
            return None

        if status is not None:
            code, label, payload = status
            message = f"The server rejected this request ({label.lower()})."
            # Echo the server's own ``google.rpc.Status.message`` when it sent
            # one. No captured frame ever has (#2188), so in practice this is
            # the client-authored sentence above — the wording is only
            # server-authored when this suffix appears.
            server_message = _server_status_message(payload)
            if server_message is not None:
                message = f"{message} {server_message}"
            # Route NOT_FOUND / PERMISSION_DENIED through ClientError so
            # is_auth_error does not misclassify them as auth failures and
            # trigger a spurious token-refresh retry. The account-routing hint
            # is only relevant for these two codes — other codes (e.g.
            # INTERNAL 13) get a plain message.
            if code in _ACCOUNT_ROUTED_STATUSES:
                raise ClientError(
                    message + _ACCOUNT_MISMATCH_HINT,
                    method_id=rpc_id,
                    rpc_code=code,
                    found_ids=found_ids,
                    raw_response=response_preview,
                )
            raise RPCError(
                message,
                method_id=rpc_id,
                rpc_code=code,
                found_ids=found_ids,
                raw_response=response_preview,
            )
        raise RPCError(
            "The server returned an empty result (possible server error or parameter mismatch).",
            method_id=rpc_id,
            found_ids=found_ids,
            raw_response=response_preview,
        )

    return result
