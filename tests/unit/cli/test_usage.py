"""Usage CLI output, account scope, and failure contracts."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

import notebooklm.cli.helpers as helpers_module
import notebooklm.client as client_module
from notebooklm.exceptions import AuthError, DecodingError, NetworkError, ServerError
from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    UsageAction,
    UsageActionCostTier,
    UsageActionKind,
    UsageSummary,
    UsageSummaryStatus,
    UsageWindow,
    UsageWindowKind,
)

from .conftest import create_mock_client, inject_client


@pytest.fixture
def summary():
    """A snapshot with non-complementary percentages and a future action."""
    return UsageSummary(
        status=UsageSummaryStatus.READY,
        windows=(
            UsageWindow(
                UsageWindowKind.FIVE_HOUR,
                12.345,
                80.125,
                datetime(2026, 9, 5, 18, 30, 1, 123456, tzinfo=timezone.utc),
            ),
            UsageWindow(
                UsageWindowKind.WEEKLY,
                0.0,
                100.0,
                datetime(2026, 9, 12, 18, 30, tzinfo=timezone.utc),
            ),
        ),
        actions=(
            UsageAction(1, UsageActionKind.AUDIO_OVERVIEW, True, UsageActionCostTier.LOW, 0, 0.0),
            UsageAction(23, None, False, None, None, None),
        ),
    )


@pytest.fixture
def usage_client(summary, mock_auth, mock_fetch_tokens):
    """Inject a usage-only response through the shared client factory seam."""
    client = create_mock_client()
    client.settings.get_usage = AsyncMock(return_value=summary)
    return client


def test_json_preserves_snapshot_and_unknown_fields(runner, usage_client):
    """JSON keeps precision, zero versus null, enum names, and reset offsets."""
    result = runner.invoke(cli, ["usage", "--json"], obj=inject_client(usage_client))

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "ready",
        "enabled": True,
        "available": True,
        "is_exhausted": False,
        "active_window": "five_hour",
        "windows": [
            {
                "kind": "five_hour",
                "used_percent": 12.345,
                "remaining_percent": 80.125,
                "resets_at": "2026-09-05T18:30:01.123456+00:00",
            },
            {
                "kind": "weekly",
                "used_percent": 0.0,
                "remaining_percent": 100.0,
                "resets_at": "2026-09-12T18:30:00+00:00",
            },
        ],
        "actions": [
            {
                "code": 1,
                "kind": "audio_overview",
                "has_sufficient_quota": True,
                "cost_tier": "low",
                "remaining_deferred_artifact_generations": 0,
                "estimated_cost_percent": 0.0,
            },
            {
                "code": 23,
                "kind": None,
                "has_sufficient_quota": False,
                "cost_tier": None,
                "remaining_deferred_artifact_generations": None,
                "estimated_cost_percent": None,
            },
        ],
    }
    usage_client.settings.get_usage.assert_awaited_once_with()
    usage_client.notebooks.list.assert_not_called()
    usage_client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("actions", [False, True])
def test_text_windows_and_optional_actions(runner, usage_client, actions):
    """Text shows both windows and only expands action rows on request."""
    args = ["usage", "--actions"] if actions else ["usage"]
    result = runner.invoke(cli, args, obj=inject_client(usage_client), env={"COLUMNS": "140"})

    assert result.exit_code == 0, result.output
    assert "Five-hour" in result.stdout
    assert "Weekly" in result.stdout
    assert "12.35%" in result.stdout
    assert "80.12%" in result.stdout
    assert "2026-09-05T18:30:01.123456+00:00" in result.stdout
    if actions:
        for text in ("audio overview", "Sufficient", "Insufficient", "Unknown", "23", "0.00%"):
            assert text in result.stdout
    else:
        assert "--actions" in result.stdout
        assert "audio overview" not in result.stdout


@pytest.mark.parametrize("status", [UsageSummaryStatus.DISABLED, UsageSummaryStatus.SKIPPED])
@pytest.mark.parametrize("json_output", [False, True])
def test_unavailable_meter_is_not_zero_usage(runner, usage_client, status, json_output):
    """Unavailable meters exit successfully without fabricated window data."""
    usage_client.settings.get_usage.return_value = UsageSummary(status=status)
    args = ["usage", "--json"] if json_output else ["usage", "--actions"]
    result = runner.invoke(cli, args, obj=inject_client(usage_client))

    assert result.exit_code == 0, result.output
    if json_output:
        assert json.loads(result.stdout) == {
            "status": status.value,
            "enabled": status is UsageSummaryStatus.SKIPPED,
            "available": False,
            "is_exhausted": None,
            "active_window": None,
            "windows": [],
            "actions": [],
        }
    else:
        assert status.value in result.stdout
        assert "%" not in result.stdout


@pytest.mark.parametrize("window_index", [0, 1])
def test_exhaustion_uses_public_active_window(runner, usage_client, summary, window_index):
    """Exhaustion and weekly precedence come from the public snapshot model."""
    windows = list(summary.windows)
    windows[window_index] = replace(
        windows[window_index], used_percent=105.0, remaining_percent=-5.0
    )
    usage_client.settings.get_usage.return_value = replace(summary, windows=tuple(windows))
    result = runner.invoke(cli, ["usage", "--json"], obj=inject_client(usage_client))
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["is_exhausted"] is True
    assert data["active_window"] == ("five_hour" if window_index == 0 else "weekly")
    assert data["windows"][window_index]["remaining_percent"] == -5.0

    result = runner.invoke(cli, ["usage"], obj=inject_client(usage_client))
    assert "exhausted" in result.stdout
    assert "105.00%" in result.stdout
    assert "-5.00%" in result.stdout


def test_empty_actions(runner, usage_client, summary):
    """A ready meter can legitimately omit action details."""
    usage_client.settings.get_usage.return_value = replace(summary, actions=())
    result = runner.invoke(cli, ["usage", "--actions"], obj=inject_client(usage_client))
    assert result.exit_code == 0, result.output
    assert "No action usage details" in result.stdout


@pytest.mark.parametrize("json_output", [False, True])
def test_quiet_preserves_json(runner, usage_client, json_output):
    """Quiet suppresses prose while keeping machine-readable data."""
    args = ["--quiet", "usage", "--actions"] + (["--json"] if json_output else [])
    result = runner.invoke(cli, args, obj=inject_client(usage_client))
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if json_output:
        assert json.loads(result.stdout)["status"] == "ready"
    else:
        assert result.stdout == ""


@pytest.mark.parametrize("error", [AuthError, NetworkError, ServerError, DecodingError])
@pytest.mark.parametrize("json_output", [False, True])
def test_errors_stay_errors(runner, usage_client, error, json_output):
    """Transport/auth/schema failures never turn into disabled or skipped meters."""
    usage_client.settings.get_usage.side_effect = error("usage read failed")
    args = ["--quiet", "usage"] + (["--json"] if json_output else [])
    result = runner.invoke(cli, args, obj=inject_client(usage_client))
    assert result.exit_code == 1, result.output
    if json_output:
        data = json.loads(result.stdout)
        assert data["error"] is True
        assert data["code"]
        assert "usage read failed" in data["message"]
        assert "status" not in data
        assert result.stderr == ""
    else:
        assert "usage read failed" in result.stderr
    usage_client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("backend", ["web", "android"])
def test_selected_profile_storage_and_backend_reach_runtime(
    runner, usage_client, backend, tmp_path
):
    """Account usage honors global auth selectors without notebook resolution."""
    storage = tmp_path / "custom" / "storage_state.json"
    with (
        patch.object(helpers_module, "get_auth_tokens", return_value=object()) as auth,
        patch.object(client_module, "NotebookLMClient", return_value=usage_client) as factory,
    ):
        result = runner.invoke(
            cli,
            ["-p", "work", "--storage", str(storage), "--backend", backend, "usage", "--json"],
        )
    assert result.exit_code == 0, result.output
    ctx = auth.call_args.args[0]
    assert ctx.obj["profile"] == "work"
    assert ctx.obj["storage_path"] == storage
    assert factory.call_args.kwargs["backend"] == backend
    usage_client.settings.get_usage.assert_awaited_once_with()
