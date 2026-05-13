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


def nest_source_ids(ids: list[str] | None, depth: int) -> list:
    """Wrap each source ID in ``depth`` inner lists, then collect.

    The outer list is always present; ``depth`` is the number of inner
    wrapping levels per ID.

    - depth=1: ``[[id1], [id2]]``
    - depth=2: ``[[[id1]], [[id2]]]``
    - depth=3: ``[[[[id1]]], [[[id2]]]]``

    Args:
        ids: Source IDs, or ``None`` (treated as empty).
        depth: Inner wrap levels per ID. Must be ``>= 1``.

    Returns:
        Empty list when ``ids`` is ``None`` or empty.
    """
    if depth < 1:
        raise ValueError(f"depth must be >= 1, got {depth}")
    if not ids:
        return []
    result: list = list(ids)
    for _ in range(depth):
        result = [[item] for item in result]
    return result


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
