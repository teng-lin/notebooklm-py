"""Focused exit-code tests for the ``source stale`` freshness predicate.

The ``source stale`` command previously used an inverted-predicate exit
convention (0 = stale, 1 = fresh) so the shell idiom
``if notebooklm source stale ID; then refresh; fi`` worked naturally.
That convention has been flipped: the default now follows the canonical
CLI exit code policy (0 = success, 1 = error). The inverted behavior is
preserved as an opt-in via ``--exit-on-stale`` for callers that depended
on the prior semantics.

This file pins the new default + the back-compat flag across both text
and JSON output modes. The companion documentation lives in
``docs/cli-exit-codes.md`` under the ``source stale`` section and the
``Exit code semantics`` summary.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import Source

from .conftest import create_mock_client, inject_client


@pytest.fixture
def runner():
    return CliRunner()


@contextmanager
def _stale_command_environment(*, is_fresh: bool):
    """Build the injected client env for ``source stale`` tests.

    Yields the ``ctx.obj`` payload that injects the mock client; callers thread
    it into ``runner.invoke(cli, args, obj=obj)``. The ``fetch_tokens`` auth
    seam is still patched here.
    """
    with patch.object(
        auth_module,
        "fetch_tokens_with_domains",
        new_callable=AsyncMock,
        return_value=("csrf", "session"),
    ):
        mock_client = create_mock_client()
        mock_client.sources.list = AsyncMock(
            return_value=[Source(id="src_123", title="Test Source")]
        )
        mock_client.sources.check_freshness = AsyncMock(return_value=is_fresh)
        yield inject_client(mock_client)


# ---------------------------------------------------------------------------
# Default (standard) exit-code semantics: 0 on success regardless of freshness
# ---------------------------------------------------------------------------


class TestSourceStaleDefaultExitCodes:
    """Default behavior: exit 0 when the check completes, 1 only on error."""

    @pytest.mark.parametrize(
        ("is_fresh", "expected_verdict_text"),
        [
            (False, "stale"),
            (True, "fresh"),
        ],
    )
    def test_default_text_mode_exits_zero_regardless_of_verdict(
        self, runner, mock_auth, is_fresh, expected_verdict_text
    ):
        """Default policy: exit 0 once the check succeeds, verdict on stdout."""
        with _stale_command_environment(is_fresh=is_fresh) as obj:
            result = runner.invoke(cli, ["source", "stale", "src_123", "-n", "nb_123"], obj=obj)

        assert result.exit_code == 0, result.output
        assert expected_verdict_text in result.output.lower()

    def test_default_text_mode_stale_branch_mentions_refresh(self, runner, mock_auth):
        """The stale branch should point callers at ``source refresh`` on stdout."""
        with _stale_command_environment(is_fresh=False) as obj:
            result = runner.invoke(cli, ["source", "stale", "src_123", "-n", "nb_123"], obj=obj)

        assert result.exit_code == 0, result.output
        assert "refresh" in result.output.lower()

    @pytest.mark.parametrize(
        ("is_fresh", "expected_stale"),
        [
            (False, True),
            (True, False),
        ],
    )
    def test_default_json_mode_exits_zero_with_verdict_in_payload(
        self, runner, mock_auth, is_fresh, expected_stale
    ):
        """JSON mode mirrors text mode: exit 0, verdict in payload fields."""
        with _stale_command_environment(is_fresh=is_fresh) as obj:
            result = runner.invoke(
                cli, ["source", "stale", "src_123", "-n", "nb_123", "--json"], obj=obj
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["stale"] is expected_stale
        assert data["fresh"] is is_fresh
        assert data["source_id"] == "src_123"


# ---------------------------------------------------------------------------
# --exit-on-stale: back-compat inverted-predicate semantics
# ---------------------------------------------------------------------------


class TestSourceStaleExitOnStaleFlag:
    """With --exit-on-stale, the legacy inverted predicate is preserved."""

    @pytest.mark.parametrize(
        ("is_fresh", "expected_exit", "expected_verdict_text"),
        [
            (False, 0, "stale"),  # predicate true → exit 0
            (True, 1, "fresh"),  # predicate false → exit 1
        ],
    )
    def test_exit_on_stale_text_mode_inverts_exit_codes(
        self, runner, mock_auth, is_fresh, expected_exit, expected_verdict_text
    ):
        with _stale_command_environment(is_fresh=is_fresh) as obj:
            result = runner.invoke(
                cli,
                ["source", "stale", "src_123", "-n", "nb_123", "--exit-on-stale"],
                obj=obj,
            )

        assert result.exit_code == expected_exit, result.output
        assert expected_verdict_text in result.output.lower()

    @pytest.mark.parametrize(
        ("is_fresh", "expected_exit", "expected_stale"),
        [
            (False, 0, True),  # predicate true → exit 0
            (True, 1, False),  # predicate false → exit 1
        ],
    )
    def test_exit_on_stale_json_mode_inverts_exit_codes_with_payload_intact(
        self, runner, mock_auth, is_fresh, expected_exit, expected_stale
    ):
        with _stale_command_environment(is_fresh=is_fresh) as obj:
            result = runner.invoke(
                cli,
                [
                    "source",
                    "stale",
                    "src_123",
                    "-n",
                    "nb_123",
                    "--exit-on-stale",
                    "--json",
                ],
                obj=obj,
            )

        assert result.exit_code == expected_exit, result.output
        data = json.loads(result.output)
        assert data["stale"] is expected_stale
        assert data["fresh"] is is_fresh


# ---------------------------------------------------------------------------
# Help text surface — the flag is discoverable
# ---------------------------------------------------------------------------


class TestSourceStaleHelp:
    """The ``--exit-on-stale`` flag must be discoverable through ``--help``."""

    def test_help_advertises_exit_on_stale_flag(self, runner):
        result = runner.invoke(cli, ["source", "stale", "--help"])
        assert result.exit_code == 0
        assert "--exit-on-stale" in result.output

    def test_help_advertises_default_standard_convention(self, runner):
        result = runner.invoke(cli, ["source", "stale", "--help"])
        assert result.exit_code == 0
        # The help must signal that the default follows the standard
        # convention; phrasing may evolve, but the canonical doc link must
        # remain reachable.
        assert "cli-exit-codes.md" in result.output or "exit-code" in result.output.lower()
