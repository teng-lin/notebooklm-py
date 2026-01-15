"""Encode RPC requests for NotebookLM batchexecute API."""

import json
import logging
from typing import Any
from urllib.parse import quote

from .types import RPCMethod

logger = logging.getLogger(__name__)


def encode_rpc_request(method: RPCMethod, params: list[Any]) -> list:
    """
    Encode an RPC request into batchexecute format.

    The batchexecute API expects a triple-nested array structure:
    [[[rpc_id, json_params, null, "generic"]]]

    Args:
        method: The RPC method ID enum
        params: Parameters for the RPC call

    Returns:
        Triple-nested array structure for batchexecute
    """
    # JSON-encode params without spaces (compact format matching Chrome)
    params_json = json.dumps(params, separators=(",", ":"))
    logger.debug("Encoding RPC: method=%s, param_count=%d", method.value, len(params))

    # Build inner request: [rpc_id, json_params, null, "generic"]
    inner = [method.value, params_json, None, "generic"]

    # Triple-nest the request
    return [[inner]]


def build_request_body(
    rpc_request: list,
    csrf_token: str | None = None,
    session_id: str | None = None,
) -> str:
    """
    Build form-encoded request body for batchexecute.

    Args:
        rpc_request: Encoded RPC request from encode_rpc_request
        csrf_token: CSRF token (SNlM0e value) - optional but recommended
        session_id: Session ID (FdrFJe value) - optional

    Returns:
        Form-encoded body string with trailing &
    """
    # JSON-encode the request (compact, no spaces)
    f_req = json.dumps(rpc_request, separators=(",", ":"))

    # URL encode with safe='' to encode all special characters
    body_parts = [f"f.req={quote(f_req, safe='')}"]

    # Add CSRF token if provided
    if csrf_token:
        body_parts.append(f"at={quote(csrf_token, safe='')}")

    # Note: session_id is typically passed in URL query params, not body
    # but we support it here for flexibility

    # Join with & and add trailing &
    body = "&".join(body_parts) + "&"
    logger.debug("Built request body: size=%d bytes", len(body))
    return body


def build_url_params(
    rpc_method: RPCMethod,
    source_path: str = "/",
    session_id: str | None = None,
    bl: str | None = None,
) -> dict[str, str]:
    """
    Build URL query parameters for batchexecute request.

    Args:
        rpc_method: RPC method being called
        source_path: Source path context (e.g., /notebook/{id})
        session_id: Session ID (FdrFJe value)
        bl: Build label (changes periodically, optional)

    Returns:
        Dict of query parameters
    """
    params = {
        "rpcids": rpc_method.value,
        "source-path": source_path,
        "hl": "en",
        "rt": "c",  # Chunked response mode
    }

    if session_id:
        params["f.sid"] = session_id

    if bl:
        params["bl"] = bl

    return params
