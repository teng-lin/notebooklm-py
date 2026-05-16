"""``with_client`` routes errors through ``handle_errors``.

Before the fix, ``@with_client`` ran its own ad-hoc ``try/except FileNotFoundError``
+ broad ``except Exception``, so typed library exceptions got squashed into a
generic ``"ERROR"`` payload (or a plain "Error: ..." stderr line) with no
actionable hint.

After the with_client refactor:

* ``AuthError`` → "Run 'notebooklm login' to re-authenticate." hint, exit 1
* ``RateLimitError`` → "Retry after Ns" hint, exit 1
* ``FileNotFoundError`` (missing storage file) → same AUTH_REQUIRED UX, exit 1
* JSON variants emit parseable JSON with the appropriate ``code`` + nonzero exit
* Happy path is unchanged (exit 0, normal output)

The tests use a throwaway Click command registered onto a fresh ``click.Group``
so they exercise the decorator in isolation from the production CLI surface.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from notebooklm.cli.helpers import with_client
from notebooklm.exceptions import AuthError, RateLimitError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    # Click 8.2+ separates stdout/stderr in ``CliRunner`` by default, which is
    # what we need to assert hints went to stderr and JSON to stdout.
    return CliRunner()


@pytest.fixture
def stubbed_auth(monkeypatch) -> Generator[None, None, None]:
    """Replace ``get_auth_tokens`` with a no-op so the body runs.

    Tests that want to force a ``FileNotFoundError`` from the auth bootstrap
    will override this fixture's behavior locally.
    """
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    fake_auth = MagicMock(name="AuthTokens-stub")
    with patch("notebooklm.cli.helpers.get_auth_tokens", return_value=fake_auth):
        yield


def _build_cli(body):
    """Wrap ``body`` (a coroutine factory) in a minimal ``with_client`` command.

    The resulting ``Group`` exposes a single ``run`` subcommand with optional
    ``--json`` flag, matching the calling convention every production CLI
    command uses.
    """

    @click.group()
    @click.option("-v", "--verbose", count=True)
    @click.pass_context
    def cli(ctx, verbose):
        ctx.ensure_object(dict)

    @cli.command("run")
    @click.option("--json", "json_output", is_flag=True)
    @with_client
    def run(ctx, json_output, client_auth):
        return body(client_auth)

    return cli


# ---------------------------------------------------------------------------
# Text-mode failure paths
# ---------------------------------------------------------------------------


def test_auth_error_surfaces_login_hint(runner: CliRunner, stubbed_auth) -> None:
    """``AuthError`` → "run notebooklm login" hint, exit code 1."""

    async def _raise(_auth):
        raise AuthError("Token expired")

    cli = _build_cli(_raise)
    result = runner.invoke(cli, ["run"], catch_exceptions=False)

    assert result.exit_code == 1, result.stderr
    assert "Authentication error" in result.stderr
    assert "notebooklm login" in result.stderr


def test_rate_limit_error_surfaces_retry_hint(runner: CliRunner, stubbed_auth) -> None:
    """``RateLimitError`` → "Retry after Ns" hint, exit code 1."""

    async def _raise(_auth):
        raise RateLimitError("Too many requests", retry_after=42)

    cli = _build_cli(_raise)
    result = runner.invoke(cli, ["run"], catch_exceptions=False)

    assert result.exit_code == 1, result.stderr
    assert "Rate limited" in result.stderr
    assert "42" in result.stderr


def test_file_not_found_routes_to_auth_hint(runner: CliRunner, monkeypatch) -> None:
    """Missing storage file → AUTH_REQUIRED UX (same as no-login path)."""
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)

    async def _never_called(_auth):
        raise AssertionError("body should not run when auth bootstrap fails")

    with patch(
        "notebooklm.cli.helpers.get_auth_tokens",
        side_effect=FileNotFoundError("missing storage"),
    ):
        cli = _build_cli(_never_called)
        result = runner.invoke(cli, ["run"], catch_exceptions=False)

    assert result.exit_code == 1, result.stderr
    # ``handle_auth_error`` prints rich console output; we just need the
    # actionable hint to be reachable somewhere across stdout/stderr/output.
    combined = (result.stdout or "") + (result.stderr or "") + (result.output or "")
    assert "notebooklm login" in combined.lower()


def test_auth_bootstrap_non_filenotfound_routes_through_handle_errors(
    runner: CliRunner, monkeypatch
) -> None:
    """Non-FileNotFoundError exceptions during auth bootstrap reach ``handle_errors``.

    Regression guard for Gemini feedback on PR #454: previously the auth
    bootstrap lived OUTSIDE ``handle_errors``, so a ``ValueError`` from
    malformed storage JSON or an ``AuthError`` during token extraction would
    bubble unhandled instead of getting the centralized hint + typed code.
    """
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)

    async def _never_called(_auth):
        raise AssertionError("body should not run when auth bootstrap fails")

    # AuthError surfaces with the actionable "run notebooklm login" hint and exit 1.
    with patch(
        "notebooklm.cli.helpers.get_auth_tokens",
        side_effect=AuthError("token refresh failed"),
    ):
        cli = _build_cli(_never_called)
        result = runner.invoke(cli, ["run"], catch_exceptions=False)

    assert result.exit_code == 1, result.stderr
    assert "notebooklm login" in result.stderr


def test_auth_bootstrap_malformed_json_routes_through_handle_errors(
    runner: CliRunner, monkeypatch
) -> None:
    """``ValueError`` (e.g., malformed storage JSON) during auth bootstrap exits 2.

    Without ``handle_errors`` wrapping the bootstrap this would bubble as an
    uncaught traceback.
    """
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)

    async def _never_called(_auth):
        raise AssertionError("body should not run when auth bootstrap fails")

    with patch(
        "notebooklm.cli.helpers.get_auth_tokens",
        side_effect=ValueError("malformed storage JSON"),
    ):
        cli = _build_cli(_never_called)
        result = runner.invoke(cli, ["run", "--json"], catch_exceptions=False)

    assert result.exit_code == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "UNEXPECTED_ERROR"


def test_auth_bootstrap_non_filenotfound_logs_failed_result(
    runner: CliRunner, monkeypatch, caplog
) -> None:
    """Bootstrap exceptions other than FileNotFoundError emit ``log_result('failed', ...)``.

    Regression guard for Gemini feedback on PR #455 (helpers.py:1030): previously
    only ``FileNotFoundError`` produced the structured debug log entry, so an
    ``AuthError`` during bootstrap would be handled by ``handle_errors`` but the
    timing/cmd-name pair never reached the debug log — an observability gap when
    triaging auth failures via ``NOTEBOOKLM_DEBUG=1``.
    """
    import logging

    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)

    async def _never_called(_auth):
        raise AssertionError("body should not run when auth bootstrap fails")

    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.cli"),
        patch(
            "notebooklm.cli.helpers.get_auth_tokens",
            side_effect=AuthError("token refresh failed"),
        ),
    ):
        cli = _build_cli(_never_called)
        result = runner.invoke(cli, ["run"], catch_exceptions=False)

    assert result.exit_code == 1, result.stderr
    failed_records = [
        r for r in caplog.records if "failed" in r.getMessage() and "run" in r.getMessage()
    ]
    assert failed_records, (
        f"Expected at least one log_result('failed', ...) record, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# JSON-mode failure paths
# ---------------------------------------------------------------------------


def test_auth_error_json_payload(runner: CliRunner, stubbed_auth) -> None:
    """JSON mode: AuthError → parseable JSON with AUTH_ERROR code, nonzero exit."""

    async def _raise(_auth):
        raise AuthError("Token expired")

    cli = _build_cli(_raise)
    result = runner.invoke(cli, ["run", "--json"], catch_exceptions=False)

    assert result.exit_code != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "AUTH_ERROR"
    assert "Token expired" in payload["message"]


def test_rate_limit_error_json_payload(runner: CliRunner, stubbed_auth) -> None:
    """JSON mode: RateLimitError → parseable JSON with RATE_LIMITED code, retry_after."""

    async def _raise(_auth):
        raise RateLimitError("Too many requests", retry_after=30)

    cli = _build_cli(_raise)
    result = runner.invoke(cli, ["run", "--json"], catch_exceptions=False)

    assert result.exit_code != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "RATE_LIMITED"
    assert payload["retry_after"] == 30


def test_file_not_found_json_payload(runner: CliRunner, monkeypatch) -> None:
    """JSON mode: missing storage → AUTH_REQUIRED JSON, nonzero exit."""
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)

    async def _never_called(_auth):
        raise AssertionError("body should not run when auth bootstrap fails")

    with patch(
        "notebooklm.cli.helpers.get_auth_tokens",
        side_effect=FileNotFoundError("missing storage"),
    ):
        cli = _build_cli(_never_called)
        result = runner.invoke(cli, ["run", "--json"], catch_exceptions=False)

    assert result.exit_code != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "AUTH_REQUIRED"


def test_unexpected_error_json_payload_exits_2(runner: CliRunner, stubbed_auth) -> None:
    """JSON mode: unknown ``RuntimeError`` → UNEXPECTED_ERROR + exit 2 (system error)."""

    async def _raise(_auth):
        raise RuntimeError("something went sideways")

    cli = _build_cli(_raise)
    result = runner.invoke(cli, ["run", "--json"], catch_exceptions=False)

    assert result.exit_code == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "UNEXPECTED_ERROR"


# ---------------------------------------------------------------------------
# Backward-compat: happy path still works
# ---------------------------------------------------------------------------


def test_successful_command_exits_zero(runner: CliRunner, stubbed_auth) -> None:
    """A command that runs cleanly should still exit 0 with no errors."""
    sentinel: dict = {}

    async def _ok(auth):
        sentinel["called"] = True
        click.echo("ok")
        return None

    cli = _build_cli(_ok)
    result = runner.invoke(cli, ["run"], catch_exceptions=False)

    assert result.exit_code == 0, result.stderr
    assert sentinel.get("called") is True
    assert "ok" in result.stdout


def test_successful_command_json_mode_exits_zero(runner: CliRunner, stubbed_auth) -> None:
    """``--json`` happy path also exits 0."""

    async def _ok(auth):
        click.echo(json.dumps({"ok": True}))

    cli = _build_cli(_ok)
    result = runner.invoke(cli, ["run", "--json"], catch_exceptions=False)

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {"ok": True}
