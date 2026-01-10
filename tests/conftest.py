"""Shared test fixtures."""

import json

import pytest

from notebooklm.rpc import RPCMethod


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "vcr: marks tests that use VCR cassettes (may be skipped if cassettes unavailable)",
    )


@pytest.fixture
def sample_storage_state():
    """Sample Playwright storage state with valid cookies."""
    return {
        "cookies": [
            {"name": "SID", "value": "test_sid", "domain": ".google.com"},
            {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
            {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
            {"name": "APISID", "value": "test_apisid", "domain": ".google.com"},
            {"name": "SAPISID", "value": "test_sapisid", "domain": ".google.com"},
        ]
    }


@pytest.fixture
def sample_homepage_html():
    """Sample NotebookLM homepage HTML with tokens."""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>NotebookLM</title></head>
    <body>
    <script>window.WIZ_global_data = {
        "SNlM0e": "test_csrf_token_123",
        "FdrFJe": "test_session_id_456"
    }</script>
    </body>
    </html>
    """


@pytest.fixture
def mock_list_notebooks_response():
    inner_data = json.dumps(
        [
            [
                [
                    "My First Notebook",
                    [],
                    "nb_001",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ],
                [
                    "Research Notes",
                    [],
                    "nb_002",
                    "📚",
                    None,
                    [None, None, None, None, None, [1704153600, 0]],
                ],
            ]
        ]
    )
    rpc_id = RPCMethod.LIST_NOTEBOOKS.value
    chunk = json.dumps([["wrb.fr", rpc_id, inner_data, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


@pytest.fixture
def build_rpc_response():
    """Factory for building RPC responses.

    Args:
        rpc_id: Either an RPCMethod enum or string RPC ID.
        data: The response data to encode.
    """

    def _build(rpc_id: RPCMethod | str, data) -> str:
        # Convert RPCMethod to string value if needed
        rpc_id_str = rpc_id.value if isinstance(rpc_id, RPCMethod) else rpc_id
        inner = json.dumps(data)
        chunk = json.dumps(["wrb.fr", rpc_id_str, inner, None, None])
        return f")]}}'\n{len(chunk)}\n{chunk}\n"

    return _build
