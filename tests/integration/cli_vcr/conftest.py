"""Shared fixtures for CLI integration tests.

These tests use VCR cassettes with real NotebookLMClient instances,
exercising the full CLI → Client → RPC path without mocking the client.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# Add tests directory to path for vcr_config import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from integration.conftest import skip_no_cassettes  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

# Re-export for use by test files
__all__ = [
    "runner",
    "mock_context",
    "skip_no_cassettes",
    "notebooklm_vcr",
    "assert_command_success",
    "parse_json_output",
]


@pytest.fixture
def runner() -> CliRunner:
    """Create a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_context(tmp_path: Path):
    """Mock context file with a test notebook ID.

    CLI commands that require a notebook ID will use this context.
    The notebook ID doesn't matter for VCR replay - cassettes have recorded responses.
    """
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps({"notebook_id": "test_notebook_id"}))

    with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
        yield context_file


@pytest.fixture
def mock_auth_for_vcr():
    """Mock authentication that works with VCR cassettes.

    VCR replays recorded responses regardless of auth tokens,
    so we use mock auth to avoid requiring real credentials.
    """
    mock_cookies = {
        "SID": "vcr_mock_sid",
        "HSID": "vcr_mock_hsid",
        "SSID": "vcr_mock_ssid",
        "APISID": "vcr_mock_apisid",
        "SAPISID": "vcr_mock_sapisid",
    }
    with (
        patch("notebooklm.cli.helpers.load_auth_from_storage", return_value=mock_cookies),
        patch(
            "notebooklm.cli.helpers.fetch_tokens",
            return_value=("vcr_mock_csrf", "vcr_mock_session"),
        ),
    ):
        yield


def assert_command_success(result, *, allow_no_context: bool = True) -> None:
    """Assert a CLI command completed without crashing.

    Args:
        result: The CliRunner result object.
        allow_no_context: If True, exit code 1 (no notebook context) is acceptable.
    """
    acceptable_codes = (0, 1) if allow_no_context else (0,)
    assert result.exit_code in acceptable_codes, f"Command failed: {result.output}"


def parse_json_output(output: str) -> list | dict | None:
    """Parse JSON from CLI output, handling potential non-JSON prefixes.

    Returns the parsed JSON or None if no valid JSON found.
    """
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass

    # If whole output is not JSON, try finding the start of a JSON object.
    # This handles multi-line JSON with a prefix.
    brace_pos = output.find("{")
    bracket_pos = output.find("[")
    start_positions = [p for p in (brace_pos, bracket_pos) if p != -1]
    if start_positions:
        start_pos = min(start_positions)
        try:
            return json.loads(output[start_pos:])
        except json.JSONDecodeError:
            pass

    # Try each line (some output may have single-line JSON prefix)
    for line in output.strip().split("\n"):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    return None
