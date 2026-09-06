"""Replay the CLI over the separately recorded Standard and Pro usage reads.

These tests reuse API cassettes in replay-only mode. Re-record them with the
profile-specific commands in ``tests/integration/test_usage_vcr.py``.
"""

import json
import math
from datetime import datetime, timedelta

import pytest

from notebooklm.notebooklm_cli import cli
from tests.integration.conftest import _is_vcr_record_mode

from .conftest import notebooklm_vcr, skip_no_cassettes

pytestmark = [
    pytest.mark.vcr,
    skip_no_cassettes,
    pytest.mark.skipif(
        _is_vcr_record_mode(),
        reason="Replay only; record usage cassettes with the profile-specific API tests",
    ),
]


@pytest.mark.parametrize("cassette_name", ["usage_standard.yaml", "usage_pro.yaml"])
@pytest.mark.parametrize("json_output", [False, True])
def test_usage_snapshot(runner, mock_auth_for_vcr, cassette_name, json_output):
    """Real CLI/client decoding produces both windows without notebook context."""
    args = ["usage", "--json"] if json_output else ["usage", "--categories"]
    with notebooklm_vcr.use_cassette(cassette_name, record_mode="none") as cassette:
        result = runner.invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    # Auth bootstrap is mocked; both usage RPCs must still be replayed once.
    assert len(cassette.play_counts) == 2
    assert all(count == 1 for count in cassette.play_counts.values())
    if json_output:
        data = json.loads(result.stdout)
        assert data["status"] == "ready"
        assert data["enabled"] is True
        assert data["available"] is True
        assert {window["kind"] for window in data["windows"]} == {"five_hour", "weekly"}
        for window in data["windows"]:
            assert math.isfinite(window["used_percent"])
            assert math.isfinite(window["remaining_percent"])
            assert datetime.fromisoformat(window["resets_at"]).utcoffset() == timedelta(0)
        assert {"flashcards", "quiz", "deep_research"} <= {
            action["kind"] for action in data["actions"]
        }
    else:
        assert "Five-hour" in result.stdout
        assert "Weekly" in result.stdout
        assert "Usage categories" in result.stdout
