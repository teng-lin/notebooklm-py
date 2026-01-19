"""Decode RPC responses from NotebookLM batchexecute API."""

import json
import logging
import re
from enum import IntEnum
from typing import Any

logger = logging.getLogger(__name__)


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


class RPCError(Exception):
    """Raised when RPC call returns an error.

    Attributes:
        rpc_id: The RPC method ID that failed.
        code: Error code from API response (if available).
        found_ids: List of RPC IDs found in the response.
        raw_response_preview: First 500 chars of response (for debugging).
    """

    def __init__(
        self,
        message: str,
        rpc_id: str | None = None,
        code: Any | None = None,
        found_ids: list[str] | None = None,
        raw_response_preview: str | None = None,
    ):
        self.rpc_id = rpc_id
        self.code = code
        self.found_ids = found_ids or []
        self.raw_response_preview = raw_response_preview
        super().__init__(message)


class AuthError(RPCError):
    """Raised when RPC call fails due to authentication issues.

    This is a subclass of RPCError for backwards compatibility.
    Catching RPCError will also catch AuthError.
    """


class NetworkError(RPCError):
    """Raised when RPC call fails due to network issues.

    This includes connection failures, DNS errors, and other transport-level problems.
    These errors are typically transient and may succeed on retry.

    Attributes:
        rpc_id: The RPC method ID that failed.
        original_error: The underlying network exception.
    """

    def __init__(
        self, message: str, rpc_id: str | None = None, original_error: Exception | None = None
    ):
        super().__init__(message, rpc_id=rpc_id)
        self.original_error = original_error


class RPCTimeoutError(NetworkError):
    """Raised when RPC call times out.

    Indicates the request took too long to complete. May succeed with a longer timeout
    or during periods of lower API load.

    Note: Named RPCTimeoutError to avoid shadowing Python's built-in TimeoutError.

    Attributes:
        rpc_id: The RPC method ID that timed out.
        timeout_seconds: The timeout duration that was exceeded.
        original_error: The underlying timeout exception.
    """

    def __init__(
        self,
        message: str,
        rpc_id: str | None = None,
        timeout_seconds: float | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(message, rpc_id=rpc_id, original_error=original_error)
        self.timeout_seconds = timeout_seconds


class RateLimitError(RPCError):
    """Raised when API rate limit is exceeded.

    The API has rejected the request due to too many requests in a time window.
    Requests should be retried after a delay.

    Attributes:
        rpc_id: The RPC method ID that was rate limited.
        retry_after: Suggested seconds to wait before retrying (if available).
        code: Error code from API response.
    """

    def __init__(
        self,
        message: str,
        rpc_id: str | None = None,
        retry_after: int | None = None,
        code: Any | None = None,
    ):
        super().__init__(message, rpc_id=rpc_id, code=code)
        self.retry_after = retry_after


class ServerError(RPCError):
    """Raised when API returns a 5xx server error.

    Indicates a problem on Google's servers. These are typically transient
    and may succeed on retry with exponential backoff.

    Attributes:
        rpc_id: The RPC method ID that failed.
        status_code: HTTP status code (500-599).
        code: Error code from response.
    """

    def __init__(
        self,
        message: str,
        rpc_id: str | None = None,
        status_code: int | None = None,
        code: Any | None = None,
    ):
        super().__init__(message, rpc_id=rpc_id, code=code)
        self.status_code = status_code


class ClientError(RPCError):
    """Raised when API returns a 4xx client error.

    Indicates a problem with the request itself (invalid parameters, malformed data, etc.).
    These errors typically should not be retried without fixing the request.

    Attributes:
        rpc_id: The RPC method ID that failed.
        status_code: HTTP status code (400-499, excluding 401/403).
        code: Error code from response.
    """

    def __init__(
        self,
        message: str,
        rpc_id: str | None = None,
        status_code: int | None = None,
        code: Any | None = None,
    ):
        super().__init__(message, rpc_id=rpc_id, code=code)
        self.status_code = status_code


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
        RPCError: If more than 10% of chunks are malformed, indicating API issues.

    Note:
        Malformed chunks are skipped with a warning logged. If the error rate
        exceeds 10%, raises RPCError as this likely indicates API changes.
    """
    if not response or not response.strip():
        return []

    chunks = []
    skipped_count = 0
    lines = response.strip().split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Try to parse as byte count
        try:
            int(line)  # Validate it's a byte count (we don't need the value)
            i += 1

            # Next line should be JSON payload
            if i < len(lines):
                json_str = lines[i]
                try:
                    chunk = json.loads(json_str)
                    chunks.append(chunk)
                except json.JSONDecodeError as e:
                    # Skip malformed chunks but warn
                    skipped_count += 1
                    logger.warning(
                        "Skipping malformed chunk at line %d: %s. Preview: %s",
                        i + 1,
                        e,
                        json_str[:100],
                    )
            i += 1
        except ValueError:
            # Not a byte count, try to parse as JSON directly
            try:
                chunk = json.loads(line)
                chunks.append(chunk)
            except json.JSONDecodeError as e:
                # Skip non-JSON lines but warn
                skipped_count += 1
                logger.warning(
                    "Skipping non-JSON line at %d: %s. Preview: %s",
                    i + 1,
                    e,
                    line[:100],
                )
            i += 1

    # Fail if error rate is too high (indicates API problems)
    if skipped_count > 0:
        error_rate = skipped_count / len(lines) if lines else 0
        if error_rate > 0.1:  # More than 10% malformed
            raise RPCError(
                f"Response parsing failed: {skipped_count} of {len(lines)} chunks malformed. "
                f"This may indicate API changes or data corruption.",
                raw_response_preview=response[:500],
            )
        # Non-critical but warn user results may be incomplete
        logger.warning(
            "Parsed response but skipped %d malformed chunks (%d%%). Results may be incomplete.",
            skipped_count,
            int(error_rate * 100),
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
    found_ids = []
    for chunk in chunks:
        if not isinstance(chunk, list):
            continue

        items = chunk if (chunk and isinstance(chunk[0], list)) else [chunk]

        for item in items:
            if not isinstance(item, list) or len(item) < 2:
                continue

            if item[0] in ("wrb.fr", "er") and isinstance(item[1], str):
                found_ids.append(item[1])

    return found_ids


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


def extract_rpc_result(chunks: list[Any], rpc_id: str) -> Any:
    """Extract result data for a specific RPC ID from chunks."""
    for chunk in chunks:
        if not isinstance(chunk, list):
            continue

        items = chunk if (chunk and isinstance(chunk[0], list)) else [chunk]

        for item in items:
            if not isinstance(item, list) or len(item) < 3:
                continue

            if item[0] == "er" and item[1] == rpc_id:
                error_code = item[2] if len(item) > 2 else None

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
                    rpc_id=rpc_id,
                    code=error_code,
                )

            if item[0] == "wrb.fr" and item[1] == rpc_id:
                result_data = item[2]

                # Check for embedded UserDisplayableError when result is null
                # This indicates rate limiting, quota exceeded, or other API restrictions
                if result_data is None and len(item) > 5 and item[5] is not None:
                    if _contains_user_displayable_error(item[5]):
                        raise RateLimitError(
                            "API rate limit or quota exceeded. Please wait before retrying.",
                            rpc_id=rpc_id,
                            code="USER_DISPLAYABLE_ERROR",
                        )

                if isinstance(result_data, str):
                    try:
                        return json.loads(result_data)
                    except json.JSONDecodeError:
                        return result_data
                return result_data

    return None


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

    # Create response preview for error context (first 500 chars)
    response_preview = cleaned[:500] if len(cleaned) > 500 else cleaned

    # Collect all RPC IDs for debugging
    found_ids = collect_rpc_ids(chunks)

    logger.debug("Looking for RPC ID: %s", rpc_id)
    logger.debug("Found RPC IDs in response: %s", found_ids)

    try:
        result = extract_rpc_result(chunks, rpc_id)
    except RPCError as e:
        # Add context to errors from extract_rpc_result
        if not e.found_ids:
            e.found_ids = found_ids
        if not e.raw_response_preview:
            e.raw_response_preview = response_preview
        raise

    if result is None and not allow_null:
        if found_ids and rpc_id not in found_ids:
            # Method ID likely changed - provide actionable error
            raise RPCError(
                f"No result found for RPC ID '{rpc_id}'. "
                f"Response contains IDs: {found_ids}. "
                f"The RPC method ID may have changed.",
                rpc_id=rpc_id,
                found_ids=found_ids,
                raw_response_preview=response_preview,
            )
        # Log raw response details at debug level for troubleshooting
        logger.debug(
            "Empty result for RPC ID '%s'. Chunks parsed: %d. Response preview: %s",
            rpc_id,
            len(chunks),
            response_preview,
        )
        raise RPCError(
            f"No result found for RPC ID: {rpc_id}",
            rpc_id=rpc_id,
            raw_response_preview=response_preview,
        )

    return result
