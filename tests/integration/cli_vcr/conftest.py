"""Shared fixtures for CLI integration tests.

These tests use VCR cassettes with real NotebookLMClient instances,
exercising the full CLI → Client → RPC path without mocking the client.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# Add tests directory to path for vcr_config import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from integration.conftest import get_vcr_auth, skip_no_cassettes

# Re-export for convenience
__all__ = ["runner", "mock_context", "skip_no_cassettes", "get_vcr_auth"]


@pytest.fixture
def runner():
    """Create a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_context(tmp_path):
    """Mock context file with a test notebook ID.

    CLI commands that require a notebook ID will use this context.
    The notebook ID doesn't matter for VCR replay - cassettes have recorded responses.
    """
    context_file = tmp_path / "context.json"
    # Write initial context with test notebook ID
    import json

    context_file.write_text(json.dumps({"notebook_id": "test_notebook_id"}))

    with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
        yield context_file


@pytest.fixture
def mock_auth_for_vcr():
    """Mock authentication that works with VCR cassettes.

    VCR replays recorded responses regardless of auth tokens,
    so we use mock auth to avoid requiring real credentials.
    """
    with (
        patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load,
        patch("notebooklm.cli.helpers.fetch_tokens") as mock_fetch,
    ):
        mock_load.return_value = {
            "SID": "vcr_mock_sid",
            "HSID": "vcr_mock_hsid",
            "SSID": "vcr_mock_ssid",
            "APISID": "vcr_mock_apisid",
            "SAPISID": "vcr_mock_sapisid",
        }
        mock_fetch.return_value = ("vcr_mock_csrf", "vcr_mock_session")
        yield
