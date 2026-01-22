"""Shared fixtures for integration tests."""

import json
import os
from pathlib import Path

import pytest

from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod

# =============================================================================
# VCR Cassette Availability Check
# =============================================================================

CASSETTES_DIR = Path(__file__).parent.parent / "cassettes"

# Check if cassettes are available (more than just example files)
_real_cassettes = (
    [f for f in CASSETTES_DIR.glob("*.yaml") if not f.name.startswith("example_")]
    if CASSETTES_DIR.exists()
    else []
)

# Skip VCR tests if no real cassettes exist (unless in record mode)
_vcr_record_mode = os.environ.get("NOTEBOOKLM_VCR_RECORD", "").lower() in ("1", "true", "yes")
_cassettes_available = bool(_real_cassettes) or _vcr_record_mode

# Marker for skipping VCR tests when cassettes are not available
skip_no_cassettes = pytest.mark.skipif(
    not _cassettes_available,
    reason="VCR cassettes not available. Set NOTEBOOKLM_VCR_RECORD=1 to record.",
)


def requires_cassette(cassette_name: str):
    """Skip test if specific cassette file doesn't exist (unless in record mode)."""
    cassette_path = CASSETTES_DIR / cassette_name
    return pytest.mark.skipif(
        not cassette_path.exists() and not _vcr_record_mode,
        reason=f"Cassette '{cassette_name}' not found. Set NOTEBOOKLM_VCR_RECORD=1 to record.",
    )


async def get_vcr_auth() -> AuthTokens:
    """Get auth tokens for VCR tests.

    In record mode: loads real auth from storage (required for recording).
    In replay mode: returns mock auth (cassettes have recorded responses).
    """
    if _vcr_record_mode:
        return await AuthTokens.from_storage()
    else:
        # Mock auth for replay - values don't matter, VCR replays recorded responses
        return AuthTokens(
            cookies={
                "SID": "mock_sid",
                "HSID": "mock_hsid",
                "SSID": "mock_ssid",
                "APISID": "mock_apisid",
                "SAPISID": "mock_sapisid",
            },
            csrf_token="mock_csrf_token",
            session_id="mock_session_id",
        )


@pytest.fixture
def auth_tokens():
    """Create test authentication tokens."""
    return AuthTokens(
        cookies={
            "SID": "test_sid",
            "HSID": "test_hsid",
            "SSID": "test_ssid",
            "APISID": "test_apisid",
            "SAPISID": "test_sapisid",
        },
        csrf_token="test_csrf_token",
        session_id="test_session_id",
    )


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


@pytest.fixture
def mock_list_notebooks_response():
    """Mock response for listing notebooks."""
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
