"""Shared fixtures for integration tests.

This module provides VCR cassette utilities and authentication helpers
for integration tests. Fixtures like build_rpc_response and mock_list_notebooks_response
are inherited from tests/conftest.py.
"""

import os
from pathlib import Path

import pytest

from notebooklm.auth import AuthTokens

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
