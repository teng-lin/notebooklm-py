"""CLI tests for local-only session commands — status / clear / auth logout.

These commands never make network calls, so they need no VCR cassette.

Commands covered:

* ``notebooklm status`` — text mode, JSON mode, ``--paths`` mode (no context).
* ``notebooklm clear`` — local state mutation (removes notebook context).
* ``notebooklm auth logout`` — clears storage_state.json + browser_profile + context.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli


@pytest.fixture
def runner() -> CliRunner:
    """Click test runner."""
    return CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect NOTEBOOKLM_HOME into ``tmp_path`` so tests touch no real state.

    Also clears any NOTEBOOKLM_PROFILE / NOTEBOOKLM_AUTH_JSON inherited from
    the test runner's environment — those would otherwise short-circuit the
    profile machinery and surface state we did not set up here.
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    return tmp_path


def _seed_storage_state(home: Path) -> Path:
    """Write a minimal storage_state.json so ``auth logout`` has something to remove."""
    profile_dir = home / "profiles" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage_path = profile_dir / "storage_state.json"
    storage_path.write_text(
        json.dumps({"cookies": [{"name": "SID", "value": "fake", "domain": ".google.com"}]})
    )
    return storage_path


def _seed_context(home: Path, notebook_id: str = "test_nb_id", **extra) -> Path:
    """Write context.json under the default profile so ``status``/``clear`` find it."""
    profile_dir = home / "profiles" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    context_path = profile_dir / "context.json"
    payload = {"notebook_id": notebook_id, **extra}
    context_path.write_text(json.dumps(payload))
    return context_path


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatusCommand:
    """``notebooklm status`` — local-only inspection of context.json."""

    def test_status_no_context(self, runner: CliRunner, isolated_home: Path) -> None:
        """With no context.json, status prints the 'no notebook selected' hint."""
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        assert "No notebook selected" in result.output

    def test_status_no_context_json(self, runner: CliRunner, isolated_home: Path) -> None:
        """``--json`` returns a structured envelope with ``has_context: false``."""
        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {"has_context": False, "notebook": None, "conversation_id": None}

    def test_status_with_context(self, runner: CliRunner, isolated_home: Path) -> None:
        """With context.json, status renders the notebook ID/title in a table."""
        _seed_context(isolated_home, notebook_id="abc123", title="Demo NB", is_owner=True)

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0, result.output
        assert "abc123" in result.output
        assert "Demo NB" in result.output

    def test_status_with_context_json(self, runner: CliRunner, isolated_home: Path) -> None:
        """``--json`` envelope echoes notebook id/title/is_owner + conversation_id."""
        _seed_context(
            isolated_home,
            notebook_id="abc123",
            title="Demo NB",
            is_owner=True,
            conversation_id="conv-99",
        )

        result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["has_context"] is True
        assert data["notebook"] == {"id": "abc123", "title": "Demo NB", "is_owner": True}
        assert data["conversation_id"] == "conv-99"

    def test_status_paths_json(self, runner: CliRunner, isolated_home: Path) -> None:
        """``status --paths --json`` returns the path-info envelope."""
        result = runner.invoke(cli, ["status", "--paths", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "paths" in data
        # Both home_dir and storage_path keys must be present and live inside
        # our sandbox — guards against the env var being silently ignored.
        paths = data["paths"]
        assert str(isolated_home) in paths["home_dir"]
        assert str(isolated_home) in paths["storage_path"]


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClearCommand:
    """``notebooklm clear`` — local state mutation."""

    def test_clear_with_context(self, runner: CliRunner, isolated_home: Path) -> None:
        """``clear`` wipes the notebook id from context.json."""
        ctx_path = _seed_context(isolated_home, notebook_id="abc123", title="Demo", is_owner=True)

        result = runner.invoke(cli, ["clear"])

        assert result.exit_code == 0, result.output
        assert "Context cleared" in result.output
        # context.json should be removed (no surviving non-context fields).
        assert not ctx_path.exists()

    def test_clear_no_context_is_noop(self, runner: CliRunner, isolated_home: Path) -> None:
        """``clear`` with no existing context still succeeds (idempotent)."""
        result = runner.invoke(cli, ["clear"])
        assert result.exit_code == 0, result.output
        assert "Context cleared" in result.output

    def test_clear_json(self, runner: CliRunner, isolated_home: Path) -> None:
        """``clear --json`` reports an actual clear (cleared=True)."""
        _seed_context(isolated_home, notebook_id="abc123", title="Demo", is_owner=True)
        result = runner.invoke(cli, ["clear", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"status": "cleared", "cleared": True}

    def test_clear_json_noop(self, runner: CliRunner, isolated_home: Path) -> None:
        """``clear --json`` with nothing to clear reports cleared=False (no-op)."""
        result = runner.invoke(cli, ["clear", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"status": "already_clear", "cleared": False}


# ---------------------------------------------------------------------------
# auth logout
# ---------------------------------------------------------------------------


class TestAuthLogoutCommand:
    """``notebooklm auth logout`` — local-only auth file removal."""

    def test_auth_logout_clears_storage(self, runner: CliRunner, isolated_home: Path) -> None:
        """``auth logout`` deletes the storage_state.json for the active profile."""
        storage = _seed_storage_state(isolated_home)
        assert storage.exists()  # pre-condition

        result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0, result.output
        assert "Logged out" in result.output
        assert not storage.exists()

    def test_auth_logout_no_session(self, runner: CliRunner, isolated_home: Path) -> None:
        """With no storage/profile/context, logout reports 'Already logged out'."""
        result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0, result.output
        assert "Already logged out" in result.output

    def test_auth_logout_json(self, runner: CliRunner, isolated_home: Path) -> None:
        """``auth logout --json`` emits a structured logged-out document."""
        _seed_storage_state(isolated_home)
        result = runner.invoke(cli, ["auth", "logout", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "logged_out"
        assert payload["removed"] is True

    def test_auth_logout_json_no_session(self, runner: CliRunner, isolated_home: Path) -> None:
        """``auth logout --json`` with nothing to remove reports already-logged-out."""
        result = runner.invoke(cli, ["auth", "logout", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "already_logged_out"
        assert payload["removed"] is False

    def test_render_logout_outcome_json_failure_is_single_document(self) -> None:
        """A per-step failure under ``--json`` emits exactly ONE error envelope
        (exit 1) — never both the error and the success payload. Exercises the
        render helper directly (no monkeypatch) since it owns the exit policy."""
        import contextlib
        import io

        from notebooklm.cli._session_render import _render_logout_outcome
        from notebooklm.cli.services.session_context import LogoutFailure, LogoutOutcome

        outcome = LogoutOutcome(
            removed_any=False,
            env_auth_remains=False,
            failure=LogoutFailure(
                kind="storage",
                path=Path("/tmp/x/storage_state.json"),
                error_message="permission denied",
                partial_storage_removed=False,
            ),
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            _render_logout_outcome(outcome, json_output=True)
        assert exc.value.code == 1
        payload = json.loads(buf.getvalue())  # raises if stdout isn't a single document
        assert payload["error"] is True
        assert payload["code"] == "logout_storage_failed"

    def test_auth_logout_also_clears_context(self, runner: CliRunner, isolated_home: Path) -> None:
        """``auth logout`` removes context.json so post-logout commands start fresh.

        Regression guard for the account-switch flow: leaving a stale notebook
        id in context.json caused mismatched 'not found' / permission errors
        after the user logged into a different Google account (see
        ``_ACCOUNT_MISMATCH_HINT`` in ``rpc/decoder.py``).
        """
        _seed_storage_state(isolated_home)
        ctx_path = _seed_context(isolated_home, notebook_id="abc123")

        result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0, result.output
        assert not ctx_path.exists()

    def test_auth_logout_warns_on_env_auth(
        self, runner: CliRunner, isolated_home: Path, monkeypatch
    ) -> None:
        """``NOTEBOOKLM_AUTH_JSON`` users get a heads-up that env auth persists."""
        _seed_storage_state(isolated_home)
        # The fixture clears this env var; set it back specifically for this test.
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":{}}')

        result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0, result.output
        assert "NOTEBOOKLM_AUTH_JSON is set" in result.output
