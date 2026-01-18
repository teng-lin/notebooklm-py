"""Decode RPC responses from NotebookLM batchexecute API."""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class RPCError(Exception):
    """Raised when RPC call returns an error."""

    def __init__(
        self,
        message: str,
        rpc_id: str | None = None,
        code: Any | None = None,
        found_ids: list[str] | None = None,
    ):
        self.rpc_id = rpc_id
        self.code = code
        self.found_ids = found_ids or []
        super().__init__(message)


class AuthError(RPCError):
    """Raised when RPC call fails due to authentication issues.

    This is a subclass of RPCError for backwards compatibility.
    Catching RPCError will also catch AuthError.
    """


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
    """
    if not response or not response.strip():
        return []

    chunks = []
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
                except json.JSONDecodeError:
                    # Skip malformed chunks
                    pass
            i += 1
        except ValueError:
            # Not a byte count, try to parse as JSON directly
            try:
                chunk = json.loads(line)
                chunks.append(chunk)
            except json.JSONDecodeError:
                # Skip non-JSON lines
                pass
            i += 1

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
                error_msg = item[2] if len(item) > 2 else "Unknown error"
                if isinstance(error_msg, int):
                    error_msg = f"Error code: {error_msg}"
                raise RPCError(
                    str(error_msg),
                    rpc_id=rpc_id,
                    code=item[2] if len(item) > 2 else None,
                )

            if item[0] == "wrb.fr" and item[1] == rpc_id:
                result_data = item[2]

                # Check for embedded UserDisplayableError when result is null
                # This indicates rate limiting, quota exceeded, or other API restrictions
                if result_data is None and len(item) > 5 and item[5] is not None:
                    if _contains_user_displayable_error(item[5]):
                        raise RPCError(
                            "Request rejected by API - may indicate rate limiting or quota exceeded",
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

    # Collect all RPC IDs for debugging
    found_ids = collect_rpc_ids(chunks)

    logger.debug("Looking for RPC ID: %s", rpc_id)
    logger.debug("Found RPC IDs in response: %s", found_ids)

    try:
        result = extract_rpc_result(chunks, rpc_id)
    except RPCError as e:
        # Add found_ids context to errors from extract_rpc_result
        if not e.found_ids:
            e.found_ids = found_ids
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
            )
        # Log raw response details at debug level for troubleshooting
        # Show first 500 chars of cleaned response
        response_preview = cleaned[:500] if len(cleaned) > 500 else cleaned
        logger.debug(
            "Empty result for RPC ID '%s'. Chunks parsed: %d. Response preview: %s",
            rpc_id,
            len(chunks),
            response_preview,
        )
        raise RPCError(f"No result found for RPC ID: {rpc_id}", rpc_id=rpc_id)

    return result
