"""Characterization tests for ``notebooklm`` session/auth CLI commands.

These tests pin observable CLI behavior across the session command surface
BEFORE the P3.T3 service extraction. They are end-to-end at the ``CliRunner``
level so they capture exit codes, stdout/stderr structure, and JSON envelope
shape that lower-level service-unit tests do not cover holistically.

They MUST pass identically on ``main`` HEAD before the extraction commit
lands, and they MUST continue to pass byte-for-byte after the extraction.
Any divergence is a behavior regression — not an opportunity to "fix" the
old behavior.

Coverage matrix (commands x output modes from the P3.T3 spec):

| Command                  | text | json |
|--------------------------|------|------|
| login --browser chrome   | yes  |  -   |
| login --no-browser path  | yes  |  -   |
| status                   | yes  | yes  |
| use                      | yes  | yes  |
| clear                    | yes  |  -   |
| auth check               | yes  | yes  |
| auth refresh             | yes  |  -   |
| auth inspect             | yes  | yes  |

Each test pins one (command, mode) cell of the matrix to a specific
expected-text fragment or JSON envelope. The fragments are intentionally
narrow (one or two structural lines) so the snapshots survive cosmetic
changes (e.g. Rich color codes) while still catching shape regressions.

P1.T3 regression coverage (must survive the extraction unchanged):
- ``use --json`` exit code matches text mode on error
- ``auth check`` OSError handling (storage unreadable)
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli.context as context_module
import notebooklm.cli.helpers as helpers_module
import notebooklm.cli.resolve as resolve_module
import notebooklm.cli.services.auth_source as auth_source_module
import notebooklm.cli.services.playwright_login as playwright_login_module
import notebooklm.cli.services.session_context as session_context_module
import notebooklm.cli.session_cmd as session_cmd
import notebooklm.paths as paths_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import Notebook
from tests._fixtures import patch_session_login_dual

from .conftest import create_mock_client, inject_client

pytestmark = pytest.mark.characterization


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
def char_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def char_context_file(tmp_path, monkeypatch):
    """Provide a temporary context file path with full ``get_context_path`` patching.

    Patches every binding of ``get_context_path`` the CLI surface uses,
    including the P3.T3 service-layer consumer
    ``cli.services.session_context.get_context_path`` so ``read_status``
    reads from the test tmp file rather than the real
    ``~/.notebooklm/context.json``.

    The ``session_context`` binding is patched in object form
    (``monkeypatch.setattr(_session_context, ...)``) because that module is
    the real consumer ``read_status`` / ``verify_and_set_notebook`` look the
    symbol up in; the old ``notebooklm.cli.session_cmd.get_context_path``
    string-patch was a dead pure-surface re-export (#1367) — nothing in
    ``session_cmd``'s body resolves ``get_context_path`` anymore, so that
    patch never bit. Removing it and repointing onto the live consumer keeps
    the ``status`` snapshots load-bearing (verified disable->red).
    """
    import notebooklm.cli.services.session_context as _session_context

    context_file = tmp_path / "context.json"

    def _return_context_file(*_args, **_kwargs):
        return context_file

    monkeypatch.setattr(_session_context, "get_context_path", _return_context_file)
    with (
        patch.object(helpers_module, "get_context_path", return_value=context_file),
        patch.object(context_module, "get_context_path", return_value=context_file),
        patch.object(resolve_module, "get_context_path", return_value=context_file),
    ):
        yield context_file


@pytest.fixture
def char_storage_file(tmp_path, monkeypatch):
    """Provide a storage_state.json path and patch get_storage_path to return it.

    Patches every storage-path resolution seam used by the auth commands:
    the canonical ``notebooklm.paths.get_storage_path`` source, the
    session_cmd legacy binding, and the P3.T3 service-layer bindings.
    """
    storage_file = tmp_path / "storage_state.json"
    with (
        patch.object(paths_module, "get_storage_path", return_value=storage_file),
        patch.object(session_cmd, "get_storage_path", return_value=storage_file),
        patch.object(
            auth_source_module,
            "get_storage_path",
            return_value=storage_file,
        ),
        patch.object(
            session_context_module,
            "get_storage_path",
            return_value=storage_file,
            create=True,
        ),
        patch.object(
            playwright_login_module,
            "get_storage_path",
            return_value=storage_file,
        ),
    ):
        yield storage_file


# ----------------------------------------------------------------------------
# login
# ----------------------------------------------------------------------------


class TestLoginCharacterization:
    """Golden snapshots for ``notebooklm login`` paths."""

    def test_login_rejects_when_auth_json_env_set(self, char_runner, monkeypatch):
        """``login`` refuses to run when NOTEBOOKLM_AUTH_JSON is set."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies": []}')
        result = char_runner.invoke(cli, ["login"])
        assert result.exit_code != 0
        assert "NOTEBOOKLM_AUTH_JSON" in result.output
        assert "Cannot run 'login'" in result.output

    def test_login_browser_chrome_invokes_playwright(self, char_runner, tmp_path):
        """``login --browser chrome`` reaches the Playwright entry point."""
        with (
            patch.object(session_cmd, "run_login") as mock_run_login,
            patch_session_login_dual("_sync_server_language_to_config") as mock_sync,
            patch.object(
                session_cmd,
                "prepare_paths_or_exit",
                return_value=(
                    tmp_path / "storage_state.json",
                    tmp_path / "browser_profile",
                ),
            ),
        ):
            result = char_runner.invoke(cli, ["login", "--browser", "chrome"])

        assert result.exit_code == 0
        mock_run_login.assert_called_once()
        plan = mock_run_login.call_args.args[0]
        assert plan.browser == "chrome"
        assert plan.storage_path == tmp_path / "storage_state.json"
        assert plan.browser_profile == tmp_path / "browser_profile"
        mock_sync.assert_called_once_with(
            storage_path=tmp_path / "storage_state.json",
            profile=None,
        )
        # The "Authentication saved to:" footer is the characterization signal
        assert "Authentication saved to:" in result.output

    def test_login_no_browser_via_browser_cookies(self, char_runner):
        """``login --browser-cookies`` skips Playwright (no-browser path)."""
        with (
            patch_session_login_dual("_login_browser_cookies_single") as mock_single,
            patch_session_login_dual("_warn_missing_optional_domains"),
        ):
            result = char_runner.invoke(cli, ["login", "--browser-cookies", "chrome"])

        assert result.exit_code == 0
        mock_single.assert_called_once()
        # When --browser-cookies is used, no Playwright "Opening ..." banner
        assert "Opening" not in result.output

    def test_login_account_without_browser_cookies_errors(self, char_runner):
        """``login --account email`` without ``--browser-cookies`` is a usage error."""
        result = char_runner.invoke(cli, ["login", "--account", "user@example.com"])
        assert result.exit_code != 0
        assert "--browser-cookies" in result.output


# ----------------------------------------------------------------------------
# use
# ----------------------------------------------------------------------------


class TestUseCharacterization:
    """Golden snapshots for ``notebooklm use``."""

    def test_use_text_success(self, char_runner, char_context_file, mock_auth):
        """``use <id>`` (text) prints the resolved notebook in a table."""
        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(
            return_value=Notebook(
                id="nb_char_001",
                title="Characterization Notebook",
                created_at=datetime(2024, 1, 15),
                is_owner=True,
            )
        )
        with (
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                session_cmd,
                "resolve_notebook_id",
                new_callable=AsyncMock,
            ) as mock_resolve,
        ):
            mock_fetch.return_value = ("csrf", "session")
            mock_resolve.return_value = "nb_char_001"
            result = char_runner.invoke(cli, ["use", "nb_char_001"], obj=inject_client(mock_client))

        assert result.exit_code == 0
        assert "nb_char_001" in result.output
        assert "Characterization Notebook" in result.output

    def test_use_json_success_envelope(self, char_runner, char_context_file, mock_auth):
        """``use --json`` emits the documented envelope shape."""
        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(
            return_value=Notebook(
                id="nb_json_001",
                title="JSON Char Notebook",
                created_at=datetime(2024, 2, 1),
                is_owner=False,
            )
        )
        with (
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                session_cmd,
                "resolve_notebook_id",
                new_callable=AsyncMock,
            ) as mock_resolve,
        ):
            mock_fetch.return_value = ("csrf", "session")
            mock_resolve.return_value = "nb_json_001"
            result = char_runner.invoke(
                cli, ["use", "nb_json_001", "--json"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {
            "active_notebook_id": "nb_json_001",
            "success": True,
            "verified": True,
            "notebook": {
                "id": "nb_json_001",
                "title": "JSON Char Notebook",
                "is_owner": False,
                "created_at": "2024-02-01T00:00:00",
                "modified_at": None,
            },
        }

    def test_use_force_text_marks_not_verified(self, char_runner, char_context_file):
        """``use --force`` persists without verification and prints '(not verified ...)'."""
        result = char_runner.invoke(cli, ["use", "nb_unverif", "--force"])
        assert result.exit_code == 0
        assert "nb_unverif" in result.output
        assert "not verified" in result.output.lower()

    def test_use_force_json_envelope(self, char_runner, char_context_file):
        """``use --force --json`` envelope reports ``verified: false``."""
        result = char_runner.invoke(cli, ["use", "nb_unverif", "--force", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {
            "active_notebook_id": "nb_unverif",
            "success": True,
            "verified": False,
        }


# ----------------------------------------------------------------------------
# status
# ----------------------------------------------------------------------------


class TestStatusCharacterization:
    """Golden snapshots for ``notebooklm status``."""

    def test_status_no_context_text(self, char_runner, char_context_file):
        if char_context_file.exists():
            char_context_file.unlink()
        result = char_runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "No notebook selected" in result.output

    def test_status_no_context_json(self, char_runner, char_context_file):
        if char_context_file.exists():
            char_context_file.unlink()
        result = char_runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {
            "has_context": False,
            "notebook": None,
            "conversation_id": None,
        }

    def test_status_with_context_text(self, char_runner, char_context_file):
        char_context_file.write_text(
            json.dumps(
                {
                    "notebook_id": "nb_status_1",
                    "title": "Status Notebook",
                    "is_owner": True,
                    "created_at": "2024-03-04",
                }
            )
        )
        result = char_runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "nb_status_1" in result.output
        assert "Status Notebook" in result.output
        assert "Current Context" in result.output

    def test_status_with_context_json(self, char_runner, char_context_file):
        char_context_file.write_text(
            json.dumps(
                {
                    "notebook_id": "nb_status_2",
                    "title": "Status Notebook 2",
                    "is_owner": False,
                    "created_at": "2024-04-05",
                    "conversation_id": "conv_xyz",
                }
            )
        )
        result = char_runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {
            "has_context": True,
            "notebook": {
                "id": "nb_status_2",
                "title": "Status Notebook 2",
                "is_owner": False,
            },
            "conversation_id": "conv_xyz",
        }

    def test_status_paths_json(self, char_runner, char_context_file):
        with patch.object(session_context_module, "get_path_info") as mock_path_info:
            mock_path_info.return_value = {
                "home_dir": "/tmp/.notebooklm",
                "home_source": "default",
                "storage_path": "/tmp/.notebooklm/storage_state.json",
                "context_path": "/tmp/.notebooklm/context.json",
                "browser_profile_dir": "/tmp/.notebooklm/browser_profile",
            }
            result = char_runner.invoke(cli, ["status", "--paths", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "paths" in data
        assert data["paths"]["home_dir"] == "/tmp/.notebooklm"


# ----------------------------------------------------------------------------
# clear
# ----------------------------------------------------------------------------


class TestClearCharacterization:
    """Golden snapshots for ``notebooklm clear``."""

    def test_clear_text_when_context_present(self, char_runner, char_context_file):
        char_context_file.write_text(json.dumps({"notebook_id": "nb_clear_1"}))
        result = char_runner.invoke(cli, ["clear"])
        assert result.exit_code == 0
        assert "Context cleared" in result.output

    def test_clear_text_when_no_context(self, char_runner, char_context_file):
        if char_context_file.exists():
            char_context_file.unlink()
        result = char_runner.invoke(cli, ["clear"])
        assert result.exit_code == 0
        # The current behavior prints "Context cleared" unconditionally.
        assert "Context cleared" in result.output


# ----------------------------------------------------------------------------
# auth check (P1.T3 regression coverage included)
# ----------------------------------------------------------------------------


class TestAuthCheckCharacterization:
    """Golden snapshots for ``notebooklm auth check``."""

    def test_auth_check_text_storage_missing(self, char_runner, char_storage_file):
        # storage file does not exist
        result = char_runner.invoke(cli, ["auth", "check"])
        assert result.exit_code == 0  # text mode does not exit non-zero
        assert "Authentication Check" in result.output
        assert "Storage file not found" in result.output

    def test_auth_check_json_storage_missing(self, char_runner, char_storage_file):
        result = char_runner.invoke(cli, ["auth", "check", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["checks"]["storage_exists"] is False

    def test_auth_check_json_valid_storage(self, char_runner, char_storage_file):
        # Minimum cookie set that ``extract_cookies_from_storage`` accepts:
        # both ``SID`` and ``__Secure-1PSIDTS`` are required by
        # ``MINIMUM_REQUIRED_COOKIES``; HSID is the conventional companion.
        char_storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "abc", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "tts",
                            "domain": ".google.com",
                        },
                        {"name": "HSID", "value": "xyz", "domain": ".google.com"},
                    ]
                }
            )
        )
        result = char_runner.invoke(cli, ["auth", "check", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["checks"]["storage_exists"] is True
        assert data["checks"]["json_valid"] is True
        assert data["checks"]["sid_cookie"] is True

    def test_auth_check_json_invalid_json(self, char_runner, char_storage_file):
        char_storage_file.write_text("not valid json {")
        result = char_runner.invoke(cli, ["auth", "check", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["checks"]["storage_exists"] is True
        assert data["checks"]["json_valid"] is False

    def test_auth_check_oserror_text_p1t3(self, char_runner, char_storage_file):
        """P1.T3 regression: OSError on read does not raise; reports error gracefully."""
        # File exists but read raises OSError (permission denied simulation).
        char_storage_file.write_text("{}")
        with patch("pathlib.Path.read_text", side_effect=OSError("Permission denied")):
            result = char_runner.invoke(cli, ["auth", "check"])
        assert result.exit_code == 0
        assert "Storage unreadable" in result.output or "Permission denied" in result.output

    def test_auth_check_oserror_json_p1t3(self, char_runner, char_storage_file):
        """P1.T3 regression: ``auth check --json`` exits 1 on OSError."""
        char_storage_file.write_text("{}")
        with patch("pathlib.Path.read_text", side_effect=OSError("Permission denied")):
            result = char_runner.invoke(cli, ["auth", "check", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["checks"]["json_valid"] is False


# ----------------------------------------------------------------------------
# auth refresh
# ----------------------------------------------------------------------------


class TestAuthRefreshCharacterization:
    """Golden snapshots for ``notebooklm auth refresh``."""

    def test_auth_refresh_rejects_when_auth_json_env_set(self, char_runner, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[]}')
        result = char_runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code != 0
        assert "NOTEBOOKLM_AUTH_JSON" in result.output

    def test_auth_refresh_default_path_success(self, char_runner, char_storage_file):
        """Default path (no --browser-cookies) calls ``fetch_tokens_with_domains``."""
        char_storage_file.write_text(json.dumps({"cookies": [{"name": "SID", "value": "x"}]}))
        with patch.object(
            session_cmd,
            "fetch_tokens_with_domains",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = char_runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 0
        mock_fetch.assert_awaited()
        assert "ok" in result.output
        assert "refreshed" in result.output

    def test_auth_refresh_quiet_suppresses_success_output(self, char_runner, char_storage_file):
        with patch.object(
            session_cmd,
            "fetch_tokens_with_domains",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = char_runner.invoke(cli, ["auth", "refresh", "--quiet"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_auth_refresh_browser_cookies_path(self, char_runner, char_storage_file):
        with patch_session_login_dual("_refresh_from_browser_cookies") as mock_refresh:
            result = char_runner.invoke(cli, ["auth", "refresh", "--browser-cookies", "chrome"])
        assert result.exit_code == 0
        mock_refresh.assert_called_once()

    def test_auth_refresh_include_domains_without_browser_cookies_errors(self, char_runner):
        """``--include-domains`` without ``--browser-cookies`` is a usage error."""
        result = char_runner.invoke(cli, ["auth", "refresh", "--include-domains", "youtube"])
        assert result.exit_code != 0
        assert "--include-domains" in result.output


# ----------------------------------------------------------------------------
# auth inspect
# ----------------------------------------------------------------------------


class TestAuthInspectCharacterization:
    """Golden snapshots for ``notebooklm auth inspect``."""

    def test_auth_inspect_text(self, char_runner):
        """``auth inspect`` (text) lists accounts in a table."""
        fake_account_1 = MagicMock(email="a@example.com", is_default=True, browser_profile=None)
        fake_account_2 = MagicMock(email="b@example.com", is_default=False, browser_profile=None)
        with patch_session_login_dual("_enumerate_browser_accounts") as mock_enum:
            mock_enum.return_value = ("chrome", [fake_account_1, fake_account_2])
            result = char_runner.invoke(cli, ["auth", "inspect", "--browser", "chrome"])
        assert result.exit_code == 0
        assert "a@example.com" in result.output
        assert "b@example.com" in result.output
        assert "Browser:" in result.output

    def test_auth_inspect_json(self, char_runner):
        fake_account = MagicMock(
            email="primary@example.com", is_default=True, browser_profile="Default"
        )
        with patch_session_login_dual("_enumerate_browser_accounts") as mock_enum:
            mock_enum.return_value = ("chrome", [fake_account])
            result = char_runner.invoke(cli, ["auth", "inspect", "--browser", "chrome", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {
            "browser": "chrome",
            "accounts": [
                {
                    "email": "primary@example.com",
                    "is_default": True,
                    "browser_profile": "Default",
                }
            ],
        }
