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
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

import notebooklm.cli.auth_runtime as auth_runtime
import notebooklm.cli.error_handler as error_handler_module
import notebooklm.cli.helpers as helpers_module
from notebooklm.cli.helpers import with_auth_and_errors, with_client
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
    with patch.object(helpers_module, "get_auth_tokens", return_value=fake_auth):
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


def _make_click_context(verbose: int = 0) -> click.Context:
    @click.group()
    def cli():
        pass

    @click.command()
    def run():
        pass

    root_ctx = click.Context(cli)
    root_ctx.params["verbose"] = verbose
    return click.Context(run, parent=root_ctx)


class _FakeClientManager:
    def __init__(self, client):
        self.client = client
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return None


# ---------------------------------------------------------------------------
# Shared primitive behavior
# ---------------------------------------------------------------------------


def test_with_auth_and_errors_uses_custom_auth_loader() -> None:
    sentinel_auth = MagicMock(name="custom-auth")
    seen: dict[str, object] = {}
    ctx = _make_click_context()

    def _loader(loader_ctx: click.Context):
        seen["ctx"] = loader_ctx
        return sentinel_auth

    async def _body(auth):
        seen["auth"] = auth
        return "ok"

    result = with_auth_and_errors(
        ctx,
        command_name="run",
        json_output=False,
        body=_body,
        auth_loader=_loader,
    )

    assert result == "ok"
    assert seen == {"ctx": ctx, "auth": sentinel_auth}


def test_with_auth_and_errors_default_auth_loader_is_looked_up_at_call_time() -> None:
    sentinel_auth = MagicMock(name="default-auth")
    ctx = _make_click_context()

    async def _body(auth):
        return auth

    with patch.object(helpers_module, "get_auth_tokens", return_value=sentinel_auth) as loader:
        result = with_auth_and_errors(
            ctx,
            command_name="run",
            json_output=False,
            body=_body,
        )

    loader.assert_called_once_with(ctx)
    assert result is sentinel_auth


def test_auth_runtime_default_auth_loader_preserves_helpers_patch_seam() -> None:
    sentinel_auth = MagicMock(name="default-auth")
    ctx = _make_click_context()

    async def _body(auth):
        return auth

    with patch.object(helpers_module, "get_auth_tokens", return_value=sentinel_auth) as loader:
        result = auth_runtime.with_auth_and_errors(
            ctx,
            command_name="run",
            json_output=False,
            body=_body,
        )

    loader.assert_called_once_with(ctx)
    assert result is sentinel_auth


def test_run_client_workflow_opens_client_with_loaded_auth() -> None:
    sentinel_auth = MagicMock(name="auth")
    sentinel_client = MagicMock(name="client")
    manager = _FakeClientManager(sentinel_client)
    seen: dict[str, object] = {}
    ctx = _make_click_context()

    def _loader(loader_ctx: click.Context):
        seen["ctx"] = loader_ctx
        return sentinel_auth

    def _client_factory(auth):
        seen["factory_auth"] = auth
        return manager

    async def _body(client):
        seen["client"] = client
        return "ok"

    result = auth_runtime.run_client_workflow(
        ctx,
        command_name="run",
        json_output=False,
        body=_body,
        auth_loader=_loader,
        client_factory=_client_factory,
    )

    assert result == "ok"
    assert seen == {
        "ctx": ctx,
        "factory_auth": sentinel_auth,
        "client": sentinel_client,
    }
    assert manager.entered is True
    assert manager.exited is True


def test_run_client_workflow_body_error_handler_can_handle_body_exception() -> None:
    sentinel_auth = MagicMock(name="auth")
    sentinel_client = MagicMock(name="client")
    manager = _FakeClientManager(sentinel_client)
    ctx = _make_click_context()

    async def _body(_client):
        raise RuntimeError("body failed")

    def _handle(exc: Exception) -> str:
        return f"handled: {exc}"

    result = auth_runtime.run_client_workflow(
        ctx,
        command_name="run",
        json_output=False,
        body=_body,
        auth_loader=lambda _ctx: sentinel_auth,
        client_factory=lambda _auth: manager,
        body_error_handler=_handle,
    )

    assert result == "handled: body failed"
    assert manager.entered is True
    assert manager.exited is True


def test_with_auth_and_errors_passes_verbose_json_to_current_handle_errors() -> None:
    calls: list[dict[str, bool]] = []
    ctx = _make_click_context(verbose=1)

    @contextmanager
    def fake_handle_errors(*, verbose: bool, json_output: bool):
        calls.append({"verbose": verbose, "json_output": json_output})
        yield

    async def _body(_auth):
        return "real body result"

    def fake_run_async(coro):
        coro.close()
        return "patched run result"

    # ``with_auth_and_errors`` imports this at call time, so patch the source module.
    with (
        patch.object(error_handler_module, "handle_errors", fake_handle_errors),
        patch.object(helpers_module, "run_async", side_effect=fake_run_async) as run_async,
    ):
        result = with_auth_and_errors(
            ctx,
            command_name="run",
            json_output=True,
            body=_body,
            auth_loader=lambda _ctx: MagicMock(name="auth"),
        )

    assert result == "patched run result"
    assert calls == [{"verbose": True, "json_output": True}]
    assert run_async.call_count == 1


def test_with_auth_and_errors_auth_file_not_found_uses_auth_handler() -> None:
    ctx = _make_click_context()

    async def _never_called(_auth):
        raise AssertionError("body should not run when auth bootstrap fails")

    def _loader(_ctx):
        raise FileNotFoundError("missing storage")

    with (
        patch.object(helpers_module, "handle_auth_error", side_effect=SystemExit(1)) as auth_error,
        pytest.raises(SystemExit) as exc_info,
    ):
        with_auth_and_errors(
            ctx,
            command_name="run",
            json_output=True,
            body=_never_called,
            auth_loader=_loader,
        )

    assert exc_info.value.code == 1
    auth_error.assert_called_once_with(True)


def test_with_auth_and_errors_body_file_not_found_reaches_handle_errors(capsys) -> None:
    ctx = _make_click_context()

    async def _body(_auth):
        raise FileNotFoundError("missing command input")

    with (
        patch.object(helpers_module, "handle_auth_error") as auth_error,
        pytest.raises(SystemExit) as exc_info,
    ):
        with_auth_and_errors(
            ctx,
            command_name="run",
            json_output=True,
            body=_body,
            auth_loader=lambda _ctx: MagicMock(name="auth"),
        )

    assert exc_info.value.code == 2
    auth_error.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] is True
    assert payload["code"] == "UNEXPECTED_ERROR"
    assert "missing command input" in payload["message"]


def test_with_auth_and_errors_non_file_auth_failure_reaches_handle_errors(capsys) -> None:
    ctx = _make_click_context()

    async def _never_called(_auth):
        raise AssertionError("body should not run when auth bootstrap fails")

    def _loader(_ctx):
        raise ValueError("malformed storage JSON")

    with (
        patch.object(helpers_module, "handle_auth_error") as auth_error,
        pytest.raises(SystemExit) as exc_info,
    ):
        with_auth_and_errors(
            ctx,
            command_name="run",
            json_output=True,
            body=_never_called,
            auth_loader=_loader,
        )

    assert exc_info.value.code == 2
    auth_error.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] is True
    assert payload["code"] == "UNEXPECTED_ERROR"
    assert "malformed storage JSON" in payload["message"]


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

    with patch.object(
        helpers_module,
        "get_auth_tokens",
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
    with patch.object(
        helpers_module,
        "get_auth_tokens",
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

    with patch.object(
        helpers_module,
        "get_auth_tokens",
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

    Regression guard for Gemini feedback on PR #455
    (``cli.auth_runtime.with_auth_and_errors``): previously
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
        patch.object(
            helpers_module,
            "get_auth_tokens",
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

    with patch.object(
        helpers_module,
        "get_auth_tokens",
        side_effect=FileNotFoundError("missing storage"),
    ):
        cli = _build_cli(_never_called)
        result = runner.invoke(cli, ["run", "--json"], catch_exceptions=False)

    assert result.exit_code != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "AUTH_REQUIRED"


def test_command_body_file_not_found_is_not_auth_required(runner: CliRunner, stubbed_auth) -> None:
    """Command-body ``FileNotFoundError`` stays on the unexpected-error path."""

    async def _raise(_auth):
        raise FileNotFoundError("missing source-file payload")

    cli = _build_cli(_raise)
    result = runner.invoke(cli, ["run", "--json"], catch_exceptions=False)

    assert result.exit_code == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "UNEXPECTED_ERROR"


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
