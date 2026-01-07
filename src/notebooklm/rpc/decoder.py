"""Decode RPC responses from NotebookLM batchexecute API."""

import json
import re
from typing import Any, Optional


class RPCError(Exception):
    """Raised when RPC call returns an error."""

    def __init__(
        self,
        message: str,
        rpc_id: Optional[str] = None,
        code: Optional[Any] = None,
        found_ids: Optional[list[str]] = None,
    ):
        self.rpc_id = rpc_id
        self.code = code
        self.found_ids = found_ids or []
        super().__init__(message)


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
            byte_count = int(line)
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

    Useful for debugging when expected RPC ID is not found.
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
                if isinstance(result_data, str):
                    try:
                        return json.loads(result_data)
                    except json.JSONDecodeError:
                        return result_data
                return result_data

    return None


def decode_response(
    raw_response: str, rpc_id: str, allow_null: bool = False, debug: bool = False
) -> Any:
    """
    Complete decode pipeline: strip prefix -> parse chunks -> extract result.

    Args:
        raw_response: Raw response text from batchexecute
        rpc_id: RPC method ID to extract result for
        allow_null: If True, return None instead of raising error when result is null
        debug: If True, print debug information about found RPC IDs

    Returns:
        Decoded result data

    Raises:
        RPCError: If RPC returned an error or result not found (when allow_null=False)
    """
    cleaned = strip_anti_xssi(raw_response)
    chunks = parse_chunked_response(cleaned)

    # Collect all RPC IDs for debugging
    found_ids = collect_rpc_ids(chunks)

    if debug:
        print(f"DEBUG: Looking for RPC ID: {rpc_id}")
        print(f"DEBUG: Found RPC IDs in response: {found_ids}")

    result = extract_rpc_result(chunks, rpc_id)

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
        raise RPCError(f"No result found for RPC ID: {rpc_id}", rpc_id=rpc_id)

    return result
