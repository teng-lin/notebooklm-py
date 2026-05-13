"""Unit tests for RPC request encoder."""

import json

from notebooklm.rpc.encoder import build_request_body, encode_rpc_request
from notebooklm.rpc.types import RPCMethod


class TestEncodeRPCRequest:
    def test_encode_list_notebooks(self):
        """Test encoding list notebooks request."""
        params = [None, 1, None, [2]]
        result = encode_rpc_request(RPCMethod.LIST_NOTEBOOKS, params)

        # Result should be triple-nested array
        assert isinstance(result, list)
        assert len(result) == 1
        assert len(result[0]) == 1

        inner = result[0][0]
        assert inner[0] == RPCMethod.LIST_NOTEBOOKS.value  # RPC ID
        assert inner[2] is None
        assert inner[3] == "generic"

        # Second element is JSON-encoded params
        decoded_params = json.loads(inner[1])
        assert decoded_params == [None, 1, None, [2]]

    def test_encode_create_notebook(self):
        """Test encoding create notebook request."""
        params = ["Test Notebook", None, None, [2], [1]]
        result = encode_rpc_request(RPCMethod.CREATE_NOTEBOOK, params)

        inner = result[0][0]
        assert inner[0] == RPCMethod.CREATE_NOTEBOOK.value
        decoded_params = json.loads(inner[1])
        assert decoded_params[0] == "Test Notebook"

    def test_encode_with_nested_params(self):
        """Test encoding with deeply nested parameters."""
        params = [[[["source_id"]], "text"], "notebook_id", [2]]
        result = encode_rpc_request(RPCMethod.ADD_SOURCE, params)

        inner = result[0][0]
        decoded_params = json.loads(inner[1])
        assert decoded_params[0][0][0][0] == "source_id"

    def test_params_json_no_spaces(self):
        """Ensure params are JSON-encoded without spaces (compact)."""
        params = [{"key": "value"}, [1, 2, 3]]
        result = encode_rpc_request(RPCMethod.LIST_NOTEBOOKS, params)

        json_str = result[0][0][1]
        # Should be compact: no spaces after colons or commas
        assert ": " not in json_str
        assert ", " not in json_str

    def test_encode_empty_params(self):
        """Test encoding with empty params."""
        params = []
        result = encode_rpc_request(RPCMethod.LIST_NOTEBOOKS, params)

        inner = result[0][0]
        assert inner[1] == "[]"


class TestBuildRequestBody:
    def test_body_is_form_encoded(self):
        """Test that body is properly form-encoded."""
        rpc_request = [[[RPCMethod.LIST_NOTEBOOKS.value, "[]", None, "generic"]]]
        csrf_token = "test_token_123"

        body = build_request_body(rpc_request, csrf_token)

        assert "f.req=" in body
        assert "at=test_token_123" in body
        assert body.endswith("&")

    def test_body_url_encodes_json(self):
        """Test that JSON in f.req is URL-encoded."""
        rpc_request = [[[RPCMethod.LIST_NOTEBOOKS.value, '["test"]', None, "generic"]]]
        csrf_token = "token"

        body = build_request_body(rpc_request, csrf_token)

        # Brackets should be percent-encoded
        f_req_part = body.split("&")[0]
        assert "%5B" in f_req_part  # [ encoded
        assert "%5D" in f_req_part  # ] encoded

    def test_csrf_token_encoded(self):
        """Test CSRF token with special chars is encoded."""
        rpc_request = [[[RPCMethod.LIST_NOTEBOOKS.value, "[]", None, "generic"]]]
        csrf_token = "token:with/special=chars"

        body = build_request_body(rpc_request, csrf_token)

        # Colon and slash should be encoded
        at_part = body.split("at=")[1].split("&")[0]
        assert "%3A" in at_part or "%2F" in at_part

    def test_body_without_csrf(self):
        """Test body can be built without CSRF token."""
        rpc_request = [[[RPCMethod.LIST_NOTEBOOKS.value, "[]", None, "generic"]]]

        body = build_request_body(rpc_request, csrf_token=None)

        assert "f.req=" in body
        assert "at=" not in body

    def test_body_with_session_id(self):
        """Test body with session ID parameter."""
        rpc_request = [[[RPCMethod.LIST_NOTEBOOKS.value, "[]", None, "generic"]]]

        body = build_request_body(rpc_request, csrf_token="token", session_id="sess123")

        assert "f.req=" in body
        assert "at=token" in body
