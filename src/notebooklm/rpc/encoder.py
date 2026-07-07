"""Encode RPC requests for NotebookLM batchexecute API."""

import json
import logging
from typing import Any
from urllib.parse import quote

from .types import RPCMethod

logger = logging.getLogger(__name__)


def encode_rpc_request(
    method: RPCMethod,
    params: list[Any],
    rpc_id_override: str | None = None,
) -> list:
    """
    Encode an RPC request into batchexecute format.

    The batchexecute API expects a triple-nested array structure:
    [[[rpc_id, json_params, null, "generic"]]]

    Args:
        method: The RPC method ID enum
        params: Parameters for the RPC call
        rpc_id_override: Optional resolved RPC id string. When provided, this
            value is embedded in the request body instead of ``method.value``.
            Callers must pass the SAME string to the URL builder so the
            ``rpcids=`` query param and the ``f.req`` body stay in sync —
            mismatched IDs reach the wire as malformed requests. Used by
            ``NotebookLMClient`` to thread ``NOTEBOOKLM_RPC_OVERRIDES`` through.

    Returns:
        Triple-nested array structure for batchexecute
    """
    rpc_id = rpc_id_override if rpc_id_override is not None else method.value
    # JSON-encode params without spaces (compact format matching Chrome)
    params_json = json.dumps(params, separators=(",", ":"))
    logger.debug("Encoding RPC: method=%s, param_count=%d", rpc_id, len(params))

    # Build inner request: [rpc_id, json_params, null, "generic"]
    inner = [rpc_id, params_json, None, "generic"]

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
        session_id: Ignored compatibility parameter; session IDs are passed in
            URL query params, not the form body.

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

    # ``session_id`` is accepted for call compatibility; batchexecute session
    # IDs stay in URL query params.

    # Join with & and add trailing &
    body = "&".join(body_parts) + "&"
    logger.debug("Built request body: size=%d bytes", len(body))
    return body


def adapt_enterprise_params(
    method: RPCMethod,
    params: list[Any],
    project_id: str,
    region: str,
    source_path: str | None = None,
) -> list[Any]:
    """Adapt standard RPC parameters to the NotebookLM Enterprise schema."""
    parent = f"projects/{project_id}/locations/{region}"

    def to_notebook_path(nb_id: str) -> str:
        if nb_id.startswith("projects/"):
            return nb_id
        return f"projects/{project_id}/locations/{region}/notebooks/{nb_id}"

    adapted_params = list(params)

    if method == RPCMethod.GET_USER_SETTINGS:
        return [parent]
    elif method == RPCMethod.LIST_NOTEBOOKS:
        return [parent, None, None, 1]
    elif method == RPCMethod.GET_NOTEBOOK:
        if adapted_params:
            notebook_id = adapted_params[0]
            return [to_notebook_path(notebook_id)]
    elif method == RPCMethod.CREATE_NOTEBOOK:
        if adapted_params:
            title = adapted_params[0]
            return [
                parent,
                [title, None, None, None, None, [None, None, None, None, None, None, 1]],
            ]
    elif method == RPCMethod.DELETE_NOTEBOOK:
        if adapted_params and isinstance(adapted_params[0], list) and adapted_params[0]:
            notebook_id = adapted_params[0][0]
            return [to_notebook_path(notebook_id)]
    elif method == RPCMethod.RENAME_NOTEBOOK:
        if len(adapted_params) > 1:
            notebook_id = adapted_params[0]
            title = ""
            try:
                title = adapted_params[1][0][3][1]
            except (IndexError, TypeError):
                title = str(adapted_params[1])
            return [[title, {"70000": to_notebook_path(notebook_id)}], [["title", "emoji"]]]
    elif method == RPCMethod.ADD_SOURCE:
        if len(adapted_params) > 1:
            notebook_id = adapted_params[1]
            return [adapted_params[0], notebook_id, {"70000": to_notebook_path(notebook_id)}]
    elif method == RPCMethod.GET_SOURCE:
        import re

        if not source_path:
            raise ValueError("source_path is required for GET_SOURCE adaptation")
        m = re.search(r"/notebook/([^/]+)", source_path)
        if not m:
            raise ValueError(f"Could not extract notebook_id from source_path: {source_path}")
        notebook_id = m.group(1)
        if len(adapted_params) <= 1:
            raise ValueError(
                f"Invalid adapted_params for GET_SOURCE (expected length > 1, "
                f"got {len(adapted_params)})"
            )
        try:
            source_id = adapted_params[0][0]
            format_code = adapted_params[1][0]
        except (IndexError, TypeError) as exc:
            raise ValueError(
                f"Could not extract source_id or format_code from adapted_params "
                f"for GET_SOURCE: {adapted_params}"
            ) from exc
        ent_source_path = (
            f"projects/{project_id}/locations/{region}/"
            f"notebooks/{notebook_id}/sources/{source_id}"
        )
        return [ent_source_path, [format_code]]
    elif method == RPCMethod.ADD_SOURCE_FILE:
        import re

        if not source_path:
            raise ValueError("source_path is required for ADD_SOURCE_FILE adaptation")
        m = re.match(r"/notebook/([^/]+)/sources/([^/]+)", source_path)
        if not m:
            raise ValueError(
                f"Could not extract notebook_id and source_id from source_path: {source_path}"
            )
        notebook_id = m.group(1)
        source_id = m.group(2)
        ent_source_path = (
            f"projects/{project_id}/locations/{region}/"
            f"notebooks/{notebook_id}/sources/{source_id}"
        )
        return [ent_source_path, [[source_id]]]

    return adapted_params
