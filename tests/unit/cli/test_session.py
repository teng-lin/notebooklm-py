"""Tests for session CLI commands (login, use, status, clear)."""

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import click
import httpx
import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Notebook

from .conftest import create_mock_client, patch_main_cli_client


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


@pytest.fixture
def mock_context_file(tmp_path):
    """Provide a temporary context file for testing context commands."""
    context_file = tmp_path / "context.json"
    with (
        patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
        patch("notebooklm.cli.session.get_context_path", return_value=context_file),
    ):
        yield context_file


# =============================================================================
# LOGIN COMMAND TESTS
# =============================================================================


class TestLoginUrlValidation:
    def test_url_matches_default_base_host(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

        from notebooklm.cli.session import _url_matches_base_host

        assert _url_matches_base_host("https://notebooklm.google.com/notebook/abc")
        assert not _url_matches_base_host(
            "https://example.com/path?next=https://notebooklm.google.com/"
        )

    def test_url_matches_enterprise_base_host(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")

        from notebooklm.cli.session import _url_matches_base_host

        assert _url_matches_base_host("https://notebooklm.cloud.google.com/notebook/abc")
        assert not _url_matches_base_host("https://notebooklm.google.com/notebook/abc")

    def test_connection_error_help_uses_enterprise_base_host(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")

        from notebooklm.cli.session import _connection_error_help

        blocked_host = (
            _connection_error_help().split("Firewall or VPN blocking ", 1)[1].split("\n", 1)[0]
        )
        assert blocked_host == "notebooklm.cloud.google.com"


class TestLoginCommand:
    def test_login_playwright_import_error_handling(self, runner):
        """Test that ImportError for playwright is handled gracefully."""
        # Patch the import inside the login function to raise ImportError
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = runner.invoke(cli, ["login"])

            # Should exit with code 1 and show helpful message
            assert result.exit_code == 1
            assert "Playwright not installed" in result.output or "pip install" in result.output

    def test_login_help_message(self, runner):
        """Test login command shows help information."""
        result = runner.invoke(cli, ["login", "--help"])

        assert result.exit_code == 0
        assert "Log in to NotebookLM" in result.output
        assert "--storage" in result.output

    def test_login_default_storage_path_info(self, runner):
        """Test login command help shows default storage path."""
        result = runner.invoke(cli, ["login", "--help"])

        assert result.exit_code == 0
        assert "storage_state.json" in result.output or "storage" in result.output.lower()

    def test_login_blocked_when_notebooklm_auth_json_set(self, runner, monkeypatch):
        """Test login command blocks when NOTEBOOKLM_AUTH_JSON is set."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[]}')

        result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        assert "Cannot run 'login' when NOTEBOOKLM_AUTH_JSON is set" in result.output

    def test_login_help_shows_browser_option(self, runner):
        """Test login --help shows --browser option with chromium/msedge choices."""
        result = runner.invoke(cli, ["login", "--help"])

        assert result.exit_code == 0
        assert "--browser" in result.output
        assert "chromium" in result.output
        assert "msedge" in result.output

    def test_login_rejects_invalid_browser(self, runner):
        """Test login rejects invalid --browser values."""
        result = runner.invoke(cli, ["login", "--browser", "firefox"])

        assert result.exit_code != 0

    @pytest.fixture
    def mock_login_browser(self, tmp_path):
        """Mock Playwright browser launch for login --browser tests.

        Yields (mock_ensure, mock_launch) for assertions on chromium install
        check and launch_persistent_context kwargs.
        """
        with (
            patch("notebooklm.cli.session._ensure_chromium_installed") as mock_ensure,
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch(
                "notebooklm.cli.session.get_storage_path", return_value=tmp_path / "storage.json"
            ),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=tmp_path / "profile",
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            yield mock_ensure, mock_launch

    def test_login_msedge_skips_chromium_install(self, runner, mock_login_browser):
        """Test --browser msedge skips _ensure_chromium_installed."""
        mock_ensure, _ = mock_login_browser
        runner.invoke(cli, ["login", "--browser", "msedge"])
        mock_ensure.assert_not_called()

    def test_login_msedge_passes_channel_param(self, runner, mock_login_browser):
        """Test --browser msedge passes channel='msedge' to launch_persistent_context."""
        _, mock_launch = mock_login_browser
        runner.invoke(cli, ["login", "--browser", "msedge"])
        assert mock_launch.call_args[1].get("channel") == "msedge"

    def test_login_chromium_default_no_channel(self, runner, mock_login_browser):
        """Test default chromium calls _ensure_chromium_installed and has no channel."""
        mock_ensure, mock_launch = mock_login_browser
        runner.invoke(cli, ["login", "--browser", "chromium"])
        mock_ensure.assert_called_once()
        assert "channel" not in mock_launch.call_args[1]

    def test_login_msedge_not_installed_shows_helpful_error(self, runner, tmp_path):
        """Test --browser msedge shows helpful error when Edge is not installed."""
        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch(
                "notebooklm.cli.session.get_storage_path", return_value=tmp_path / "storage.json"
            ),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=tmp_path / "profile",
            ),
        ):
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.side_effect = Exception(
                "Executable doesn't exist at /ms-edge\nFailed to launch"
            )

            result = runner.invoke(cli, ["login", "--browser", "msedge"])

        assert result.exit_code == 1
        assert "Microsoft Edge not found" in result.output
        assert "microsoft.com/edge" in result.output

    @pytest.fixture
    def mock_login_browser_with_storage(self, tmp_path):
        """Mock Playwright browser for login tests that assert exit_code == 0.

        Like mock_login_browser but also makes storage_state() create the file
        so that storage_path.chmod() succeeds.
        """
        storage_file = tmp_path / "storage.json"
        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=tmp_path / "profile",
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            # Make storage_state create the file so chmod succeeds
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            yield mock_page

    @pytest.mark.parametrize(
        "error_message",
        [
            "Page.goto: Navigation interrupted by another one",
            (
                'Page.goto: Navigation to "https://accounts.google.com/" is interrupted by '
                'another navigation to "https://notebooklm.google.com/"'
            ),
        ],
    )
    def test_login_handles_navigation_interrupted_error(
        self, runner, mock_login_browser_with_storage, error_message
    ):
        """Test login succeeds when page.goto raises navigation interruption errors."""
        mock_page = mock_login_browser_with_storage
        from playwright.sync_api import Error as PlaywrightError

        call_count = 0
        original_url = mock_page.url

        def goto_side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            # First goto (NOTEBOOKLM_URL before login) succeeds
            # Second and third (cookie-forcing) raise navigation interrupted
            if call_count >= 2:
                raise PlaywrightError(error_message)

        mock_page.goto.side_effect = goto_side_effect
        mock_page.url = original_url

        result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output

    def test_login_reraises_non_navigation_playwright_errors(
        self, runner, mock_login_browser_with_storage
    ):
        """Test login re-raises PlaywrightError that is not a navigation interruption."""
        mock_page = mock_login_browser_with_storage
        from playwright.sync_api import Error as PlaywrightError

        call_count = 0

        def goto_side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise PlaywrightError("Page.goto: net::ERR_CONNECTION_REFUSED")

        mock_page.goto.side_effect = goto_side_effect

        result = runner.invoke(cli, ["login"])

        assert result.exit_code != 0

    def test_login_uses_commit_wait_strategy(self, runner, mock_login_browser_with_storage):
        """Test login uses wait_until='commit' for cookie-forcing navigation."""
        mock_page = mock_login_browser_with_storage

        result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        goto_calls = mock_page.goto.call_args_list
        # 3 calls: initial NOTEBOOKLM_URL, then accounts.google.com, then NOTEBOOKLM_URL
        assert len(goto_calls) == 3
        assert goto_calls[1].kwargs.get("wait_until") == "commit"
        assert goto_calls[2].kwargs.get("wait_until") == "commit"

    def test_login_retries_on_connection_closed_error(
        self, runner, mock_login_browser_with_storage
    ):
        """Test login retries when initial navigation fails with ERR_CONNECTION_CLOSED (#243)."""
        mock_page = mock_login_browser_with_storage
        from playwright.sync_api import Error as PlaywrightError

        call_count = 0

        def goto_side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call fails with connection closed, second succeeds
            if call_count == 1:
                raise PlaywrightError(
                    "Page.goto: net::ERR_CONNECTION_CLOSED at https://notebooklm.google.com/"
                )
            # All other calls succeed

        mock_page.goto.side_effect = goto_side_effect

        with patch("notebooklm.cli.session.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output
        # Verify that goto was called more than once (retried)
        assert mock_page.goto.call_count >= 2

    def test_login_retries_on_connection_reset_error(self, runner, mock_login_browser_with_storage):
        """Test login retries when initial navigation fails with ERR_CONNECTION_RESET (#243)."""
        mock_page = mock_login_browser_with_storage
        from playwright.sync_api import Error as PlaywrightError

        call_count = 0

        def goto_side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call fails with connection reset, second succeeds
            if call_count == 1:
                raise PlaywrightError(
                    "Page.goto: net::ERR_CONNECTION_RESET at https://notebooklm.google.com/"
                )
            # All other calls succeed

        mock_page.goto.side_effect = goto_side_effect

        with patch("notebooklm.cli.session.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output

    def test_login_exits_after_max_retries(self, runner, mock_login_browser_with_storage):
        """Test login exits with error message after 3 failed connection attempts (#243)."""
        mock_page = mock_login_browser_with_storage
        from playwright.sync_api import Error as PlaywrightError

        def goto_side_effect(url, **kwargs):
            raise PlaywrightError(
                "Page.goto: net::ERR_CONNECTION_CLOSED at https://notebooklm.google.com/"
            )

        mock_page.goto.side_effect = goto_side_effect

        with patch("notebooklm.cli.session.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        assert "Failed to connect to NotebookLM" in result.output
        assert "Network connectivity" in result.output or "Firewall" in result.output
        # Verify retry attempts were made
        assert mock_page.goto.call_count == 3

    def test_login_fails_fast_on_non_retryable_errors(
        self, runner, mock_login_browser_with_storage
    ):
        """Test login fails immediately on non-connection errors during initial navigation."""
        mock_page = mock_login_browser_with_storage
        from playwright.sync_api import Error as PlaywrightError

        def goto_side_effect(url, **kwargs):
            # Fail on first call with a non-retryable error
            raise PlaywrightError(
                "Page.goto: net::ERR_INVALID_URL at https://notebooklm.google.com/"
            )

        mock_page.goto.side_effect = goto_side_effect

        with patch("notebooklm.cli.session.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code != 0
        # Should fail immediately without retrying (only 1 call)
        assert mock_page.goto.call_count == 1

    def test_login_displays_help_text_after_exhausting_retries(
        self, runner, mock_login_browser_with_storage
    ):
        """Test login displays CONNECTION_ERROR_HELP after exhausting retries (#243)."""
        mock_page = mock_login_browser_with_storage
        from playwright.sync_api import Error as PlaywrightError

        def goto_side_effect(url, **kwargs):
            # Always fail with retryable error to exhaust retries
            raise PlaywrightError(
                "Page.goto: net::ERR_CONNECTION_CLOSED at https://notebooklm.google.com/"
            )

        mock_page.goto.side_effect = goto_side_effect

        with patch("notebooklm.cli.session.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        # Verify that CONNECTION_ERROR_HELP is actually displayed
        assert "Failed to connect to NotebookLM after multiple retries" in result.output
        assert "Network connectivity issues" in result.output
        assert "Firewall or VPN" in result.output
        assert "Check your internet connection" in result.output
        # Verify exactly 3 retry attempts
        assert mock_page.goto.call_count == 3

    def test_login_fresh_deletes_browser_profile(self, runner, tmp_path):
        """Test --fresh deletes existing browser_profile directory before login."""
        browser_dir = tmp_path / "profile"
        browser_dir.mkdir()
        (browser_dir / "Default" / "Cookies").parent.mkdir(parents=True)
        (browser_dir / "Default" / "Cookies").write_text("fake cookies")

        storage_file = tmp_path / "storage.json"

        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login", "--fresh"])

        assert result.exit_code == 0
        # The old cached cookies file was removed by shutil.rmtree;
        # mkdir recreates an empty directory, then Playwright populates it
        assert not (browser_dir / "Default" / "Cookies").exists()
        assert "Cleared cached browser session" in result.output

    def test_login_fresh_works_when_no_profile_exists(self, runner, tmp_path):
        """Test --fresh works when browser_profile doesn't exist yet (first login)."""
        browser_dir = tmp_path / "profile"
        # Do NOT create browser_dir - simulates first login
        storage_file = tmp_path / "storage.json"

        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login", "--fresh"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output

    def test_playwright_login_clears_stale_account_metadata(self, runner, tmp_path):
        """Interactive login targets the visible account, so stale browser-cookie
        account routing metadata must not survive the new storage state."""
        browser_dir = tmp_path / "profile"
        storage_file = tmp_path / "storage.json"
        context_file = tmp_path / "context.json"
        context_file.write_text(
            json.dumps(
                {
                    "notebook_id": "nb_existing",
                    "account": {"authuser": 1, "email": "old@example.com"},
                }
            )
        )

        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0, result.output
        assert storage_file.exists()
        assert json.loads(context_file.read_text()) == {"notebook_id": "nb_existing"}

    def test_login_fresh_ignored_with_browser_cookies(self, runner, tmp_path):
        """Test --fresh warns and is ignored when combined with --browser-cookies."""
        # Pass explicit "auto" value for cross-platform Click compatibility.
        with (
            patch("notebooklm.cli.session._login_with_browser_cookies"),
            patch("notebooklm.cli.session.get_storage_path", return_value=tmp_path / "s.json"),
        ):
            result = runner.invoke(cli, ["login", "--fresh", "--browser-cookies", "auto"])
        assert "--fresh has no effect" in result.output

    def test_login_help_shows_fresh_option(self, runner):
        """Test login --help shows --fresh flag."""
        result = runner.invoke(cli, ["login", "--help"])
        assert "--fresh" in result.output

    def test_login_fresh_oserror_on_rmtree(self, runner, tmp_path):
        """Test --fresh handles OSError on rmtree gracefully."""
        browser_dir = tmp_path / "profile"
        browser_dir.mkdir()

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=tmp_path / "s.json"),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session.shutil.rmtree", side_effect=OSError("locked")),
        ):
            result = runner.invoke(cli, ["login", "--fresh"])

        assert result.exit_code == 1
        assert "Cannot clear browser profile" in result.output

    def test_login_recovers_from_target_closed_on_initial_navigation(self, runner, tmp_path):
        """Test login retries with fresh page when initial goto gets TargetClosedError (#246)."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            from playwright.sync_api import Error as PlaywrightError

            mock_context = MagicMock()
            mock_page_stale = MagicMock()
            mock_page_fresh = MagicMock()
            mock_page_fresh.url = "https://notebooklm.google.com/"
            mock_page_fresh.goto.side_effect = None

            # Stale page raises TargetClosedError on every call
            mock_page_stale.goto.side_effect = PlaywrightError(
                "Page.goto: Target page, context or browser has been closed"
            )
            mock_context.pages = [mock_page_stale]
            # new_page() returns a working fresh page
            mock_context.new_page.return_value = mock_page_fresh
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            with patch("notebooklm.cli.session.time.sleep"):
                result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output
        # Verify new_page was called to recover from the stale page
        mock_context.new_page.assert_called()

    def test_login_recovers_from_target_closed_in_cookie_forcing(self, runner, tmp_path):
        """Test login recovers when cookie-forcing goto hits TargetClosedError (#246).

        This is the PRIMARY crash site: after user switches accounts in the browser,
        the old page reference is dead. The cookie-forcing section must get a fresh
        page and continue.
        """
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            from playwright.sync_api import Error as PlaywrightError

            mock_context = MagicMock()
            mock_page_stale = MagicMock()
            mock_page_fresh = MagicMock()
            mock_page_fresh.url = "https://notebooklm.google.com/"
            mock_page_fresh.goto.side_effect = None

            # Initial navigation succeeds (auto-login via cached session)
            goto_call_count = 0

            def stale_goto_side_effect(url, **kwargs):
                nonlocal goto_call_count
                goto_call_count += 1
                # Call 1: initial goto to NOTEBOOKLM_URL -- succeeds
                if goto_call_count == 1:
                    return
                # Call 2+: cookie-forcing -- page is stale, user switched accounts
                raise PlaywrightError("Page.goto: Target page, context or browser has been closed")

            mock_page_stale.goto.side_effect = stale_goto_side_effect
            mock_page_stale.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page_stale]
            mock_context.new_page.return_value = mock_page_fresh
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output
        # Verify new_page was called to get a fresh page after the stale one died
        mock_context.new_page.assert_called()

    def test_login_ignores_navigation_interrupted_after_recovering_page(self, runner, tmp_path):
        """Test recovered pages can also hit the Playwright navigation race (#317)."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            from playwright.sync_api import Error as PlaywrightError

            mock_context = MagicMock()
            mock_page_stale = MagicMock()
            mock_page_recovered = MagicMock()
            mock_page_recovered.url = "https://notebooklm.google.com/"

            goto_call_count = 0

            def stale_goto_side_effect(url, **kwargs):
                nonlocal goto_call_count
                goto_call_count += 1
                if goto_call_count == 1:
                    return
                raise PlaywrightError("Page.goto: Target page, context or browser has been closed")

            mock_page_stale.goto.side_effect = stale_goto_side_effect
            mock_page_stale.url = "https://notebooklm.google.com/"
            mock_page_recovered.goto.side_effect = PlaywrightError(
                'Page.goto: Navigation to "https://accounts.google.com/" is interrupted by '
                'another navigation to "https://notebooklm.google.com/"'
            )
            mock_context.pages = [mock_page_stale]
            mock_context.new_page.return_value = mock_page_recovered
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output
        mock_context.new_page.assert_called()

    def test_login_shows_browser_closed_message_after_exhausting_retries(self, runner, tmp_path):
        """Test login shows browser-specific error (not network error) when TargetClosedError exhausts retries."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            from playwright.sync_api import Error as PlaywrightError

            mock_context = MagicMock()
            mock_page = MagicMock()
            # Every page (original + recovered) raises TargetClosedError
            mock_page.goto.side_effect = PlaywrightError(
                "Page.goto: Target page, context or browser has been closed"
            )
            mock_context.pages = [mock_page]
            mock_context.new_page.return_value = mock_page  # new pages also fail
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            with patch("notebooklm.cli.session.time.sleep"):
                result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        # Should show browser-closed message, NOT network error message
        assert "browser" in result.output.lower() and "closed" in result.output.lower()
        assert "Network connectivity" not in result.output

    def test_login_cookie_forcing_double_failure_shows_browser_closed(self, runner, tmp_path):
        """Test cookie-forcing shows BROWSER_CLOSED_HELP when recovered page also raises TargetClosedError (#246).

        This is the final safety net: if the recovered page is also dead during
        cookie-forcing, the user should see BROWSER_CLOSED_HELP, not a traceback.
        """
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            from playwright.sync_api import Error as PlaywrightError

            mock_context = MagicMock()
            mock_page_stale = MagicMock()
            mock_page_recovered = MagicMock()

            # Initial navigation succeeds
            goto_call_count = 0

            def stale_goto_side_effect(url, **kwargs):
                nonlocal goto_call_count
                goto_call_count += 1
                if goto_call_count == 1:
                    return  # initial navigation OK
                raise PlaywrightError("Page.goto: Target page, context or browser has been closed")

            mock_page_stale.goto.side_effect = stale_goto_side_effect
            mock_page_stale.url = "https://notebooklm.google.com/"
            # Recovered page also raises TargetClosedError on goto
            mock_page_recovered.goto.side_effect = PlaywrightError(
                "Page.goto: Target page, context or browser has been closed"
            )
            mock_context.pages = [mock_page_stale]
            mock_context.new_page.return_value = mock_page_recovered
            mock_context.storage_state.side_effect = lambda path: Path(path).write_text("{}")

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        assert "browser" in result.output.lower() and "closed" in result.output.lower()


# =============================================================================
# USE COMMAND TESTS
# =============================================================================


class TestUseCommand:
    def test_use_sets_notebook_context(self, runner, mock_auth, mock_context_file):
        """Test 'use' command sets the current notebook context."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get = AsyncMock(
                return_value=Notebook(
                    id="nb_123",
                    title="Test Notebook",
                    created_at=datetime(2024, 1, 15),
                    is_owner=True,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")

                # Patch in session module where it's imported
                with patch(
                    "notebooklm.cli.session.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.return_value = "nb_123"

                    result = runner.invoke(cli, ["use", "nb_123"])

        assert result.exit_code == 0
        assert "nb_123" in result.output or "Test Notebook" in result.output

    def test_use_with_partial_id(self, runner, mock_auth, mock_context_file):
        """Test 'use' command resolves partial notebook ID."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get = AsyncMock(
                return_value=Notebook(
                    id="nb_full_id_123",
                    title="Resolved Notebook",
                    created_at=datetime(2024, 1, 15),
                    is_owner=True,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")

                # Patch in session module where it's imported
                with patch(
                    "notebooklm.cli.session.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.return_value = "nb_full_id_123"

                    result = runner.invoke(cli, ["use", "nb_full"])

        assert result.exit_code == 0
        # Should show resolved full ID
        assert "nb_full_id_123" in result.output or "Resolved Notebook" in result.output

    def test_use_without_auth_sets_id_anyway(self, runner, mock_context_file):
        """Test 'use' command sets ID even without auth file."""
        with patch(
            "notebooklm.cli.helpers.load_auth_from_storage",
            side_effect=FileNotFoundError("No auth"),
        ):
            result = runner.invoke(cli, ["use", "nb_noauth"])

        # Should still set the context (with warning)
        assert result.exit_code == 0
        assert "nb_noauth" in result.output

    def test_use_shows_owner_status(self, runner, mock_auth, mock_context_file):
        """Test 'use' command displays ownership status correctly."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get = AsyncMock(
                return_value=Notebook(
                    id="nb_shared",
                    title="Shared Notebook",
                    created_at=datetime(2024, 1, 15),
                    is_owner=False,  # Shared notebook
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")

                # Patch in session module where it's imported
                with patch(
                    "notebooklm.cli.session.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.return_value = "nb_shared"

                    result = runner.invoke(cli, ["use", "nb_shared"])

        assert result.exit_code == 0
        assert "Shared" in result.output or "nb_shared" in result.output


# =============================================================================
# STATUS COMMAND TESTS
# =============================================================================


class TestStatusCommand:
    def test_status_no_context(self, runner, mock_context_file):
        """Test status command when no notebook is selected."""
        # Ensure context file doesn't exist
        if mock_context_file.exists():
            mock_context_file.unlink()

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        assert "No notebook selected" in result.output or "use" in result.output.lower()

    def test_status_with_context(self, runner, mock_context_file):
        """Test status command shows current notebook context."""
        # Create context file with notebook info
        context_data = {
            "notebook_id": "nb_test_123",
            "title": "My Test Notebook",
            "is_owner": True,
            "created_at": "2024-01-15",
        }
        mock_context_file.write_text(json.dumps(context_data))

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        assert "nb_test_123" in result.output or "My Test Notebook" in result.output

    def test_status_with_conversation(self, runner, mock_context_file):
        """Test status command shows conversation ID when set."""
        context_data = {
            "notebook_id": "nb_conv_test",
            "title": "Notebook with Conversation",
            "is_owner": True,
            "created_at": "2024-01-15",
            "conversation_id": "conv_abc123",
        }
        mock_context_file.write_text(json.dumps(context_data))

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        assert "conv_abc123" in result.output or "Conversation" in result.output

    def test_status_json_output_with_context(self, runner, mock_context_file):
        """Test status --json outputs valid JSON."""
        context_data = {
            "notebook_id": "nb_json_test",
            "title": "JSON Test Notebook",
            "is_owner": True,
            "created_at": "2024-01-15",
        }
        mock_context_file.write_text(json.dumps(context_data))

        result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0
        # Should be valid JSON
        output_data = json.loads(result.output)
        assert output_data["has_context"] is True
        assert output_data["notebook"]["id"] == "nb_json_test"

    def test_status_json_output_no_context(self, runner, mock_context_file):
        """Test status --json outputs valid JSON when no context."""
        if mock_context_file.exists():
            mock_context_file.unlink()

        result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0
        output_data = json.loads(result.output)
        assert output_data["has_context"] is False
        assert output_data["notebook"] is None

    def test_status_handles_corrupted_context_file(self, runner, mock_context_file):
        """Test status handles corrupted context file gracefully."""
        # Write invalid JSON
        mock_context_file.write_text("{ invalid json }")

        result = runner.invoke(cli, ["status"])

        # Should not crash, should show minimal info or no context
        assert result.exit_code == 0


# =============================================================================
# CLEAR COMMAND TESTS
# =============================================================================


class TestClearCommand:
    def test_clear_removes_context(self, runner, mock_context_file):
        """Test clear command removes context file."""
        # Create context file
        context_data = {"notebook_id": "nb_to_clear", "title": "Clear Me"}
        mock_context_file.write_text(json.dumps(context_data))

        result = runner.invoke(cli, ["clear"])

        assert result.exit_code == 0
        assert "cleared" in result.output.lower() or "Context" in result.output

    def test_clear_when_no_context(self, runner, mock_context_file):
        """Test clear command when no context exists."""
        if mock_context_file.exists():
            mock_context_file.unlink()

        result = runner.invoke(cli, ["clear"])

        # Should succeed even if no context exists
        assert result.exit_code == 0


# =============================================================================
# EDGE CASES
# =============================================================================


class TestStatusPaths:
    """Tests for status --paths flag."""

    def test_status_paths_flag_shows_table(self, runner, mock_context_file):
        """Test status --paths shows configuration paths table."""
        with patch("notebooklm.cli.session.get_path_info") as mock_path_info:
            mock_path_info.return_value = {
                "home_dir": "/home/test/.notebooklm",
                "home_source": "default",
                "storage_path": "/home/test/.notebooklm/storage_state.json",
                "context_path": "/home/test/.notebooklm/context.json",
                "browser_profile_dir": "/home/test/.notebooklm/browser_profile",
            }

            result = runner.invoke(cli, ["status", "--paths"])

        assert result.exit_code == 0
        assert "Configuration Paths" in result.output
        assert "/home/test/.notebooklm" in result.output
        assert "storage_state.json" in result.output

    def test_status_paths_json_output(self, runner, mock_context_file):
        """Test status --paths --json outputs path info as JSON."""
        with patch("notebooklm.cli.session.get_path_info") as mock_path_info:
            mock_path_info.return_value = {
                "home_dir": "/custom/path/.notebooklm",
                "home_source": "NOTEBOOKLM_HOME",
                "storage_path": "/custom/path/.notebooklm/storage_state.json",
                "context_path": "/custom/path/.notebooklm/context.json",
                "browser_profile_dir": "/custom/path/.notebooklm/browser_profile",
            }

            result = runner.invoke(cli, ["status", "--paths", "--json"])

        assert result.exit_code == 0
        output_data = json.loads(result.output)
        assert "paths" in output_data
        assert output_data["paths"]["home_dir"] == "/custom/path/.notebooklm"
        assert output_data["paths"]["home_source"] == "NOTEBOOKLM_HOME"

    def test_status_paths_shows_auth_json_note(self, runner, mock_context_file, monkeypatch):
        """Test status --paths shows note when NOTEBOOKLM_AUTH_JSON is set."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[]}')

        with patch("notebooklm.cli.session.get_path_info") as mock_path_info:
            mock_path_info.return_value = {
                "home_dir": "/home/test/.notebooklm",
                "home_source": "default",
                "storage_path": "/home/test/.notebooklm/storage_state.json",
                "context_path": "/home/test/.notebooklm/context.json",
                "browser_profile_dir": "/home/test/.notebooklm/browser_profile",
            }

            result = runner.invoke(cli, ["status", "--paths"])

        assert result.exit_code == 0
        assert "NOTEBOOKLM_AUTH_JSON is set" in result.output


# =============================================================================
# AUTH CHECK COMMAND TESTS
# =============================================================================


class TestAuthCheckCommand:
    """Tests for the 'auth check' command."""

    @pytest.fixture
    def mock_storage_path(self, tmp_path):
        """Provide a temporary storage path for testing."""
        storage_file = tmp_path / "storage_state.json"
        with patch("notebooklm.cli.session.get_storage_path", return_value=storage_file):
            yield storage_file

    def test_auth_check_storage_not_found(self, runner, mock_storage_path):
        """Test auth check when storage file doesn't exist."""
        # Ensure file doesn't exist
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "Storage exists" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output

    def test_auth_check_storage_not_found_json(self, runner, mock_storage_path):
        """Test auth check --json when storage file doesn't exist."""
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["storage_exists"] is False
        assert "not found" in output["details"]["error"]

    def test_auth_check_invalid_json(self, runner, mock_storage_path):
        """Test auth check when storage file contains invalid JSON."""
        mock_storage_path.write_text("{ invalid json }")

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "JSON valid" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output

    def test_auth_check_invalid_json_output(self, runner, mock_storage_path):
        """Test auth check --json when storage contains invalid JSON."""
        mock_storage_path.write_text("not valid json at all")

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["storage_exists"] is True
        assert output["checks"]["json_valid"] is False
        assert "Invalid JSON" in output["details"]["error"]

    def test_auth_check_missing_sid_cookie(self, runner, mock_storage_path):
        """Test auth check when SID cookie is missing."""
        # Valid JSON but no SID cookie
        storage_data = {
            "cookies": [
                {"name": "OTHER", "value": "test", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "SID" in result.output or "cookie" in result.output.lower()

    def test_auth_check_valid_storage(self, runner, mock_storage_path):
        """Test auth check with valid storage containing SID."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "pass" in result.output.lower() or "✓" in result.output
        assert "Authentication is valid" in result.output

    def test_auth_check_valid_storage_json(self, runner, mock_storage_path):
        """Test auth check --json with valid storage."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["checks"]["storage_exists"] is True
        assert output["checks"]["json_valid"] is True
        assert output["checks"]["cookies_present"] is True
        assert output["checks"]["sid_cookie"] is True
        assert "SID" in output["details"]["cookies_found"]

    def test_auth_check_missing_1psidts_surfaces_tier1_error(self, runner, mock_storage_path):
        """SID present but ``__Secure-1PSIDTS`` absent must surface the Tier 1 error.

        Pinned by the #371 two-tier pre-flight: ``MINIMUM_REQUIRED_COOKIES``
        now contains both ``SID`` and ``__Secure-1PSIDTS``; the load helpers
        in ``auth.py`` raise on absence, and ``auth check`` reports the raised
        ``ValueError`` so users see the new diagnostic.

        Note: ``auth check`` itself returns exit code 0 regardless — that's a
        pre-existing UX gap orthogonal to #371. We assert on the surfaced
        error text instead, which is what users would actually see.
        """
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                # Note: __Secure-1PSIDTS deliberately omitted.
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["cookies_present"] is False
        assert "__Secure-1PSIDTS" in output["details"].get("error", "")

    def test_auth_check_with_test_flag_success(self, runner, mock_storage_path):
        """Test auth check --test with successful token fetch."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_token_abc", "session_id_xyz")

            result = runner.invoke(cli, ["auth", "check", "--test"])

        assert result.exit_code == 0
        assert "Token fetch" in result.output
        assert "pass" in result.output.lower() or "✓" in result.output

    def test_auth_check_with_test_flag_failure(self, runner, mock_storage_path):
        """Test auth check --test when token fetch fails."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = ValueError("Authentication expired")

            result = runner.invoke(cli, ["auth", "check", "--test"])

        assert result.exit_code == 0
        assert "Token fetch" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output
        assert "expired" in result.output.lower() or "refresh" in result.output.lower()

    def test_auth_check_with_test_flag_json(self, runner, mock_storage_path):
        """Test auth check --test --json with successful token fetch."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_12345", "sess_67890")

            result = runner.invoke(cli, ["auth", "check", "--test", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["checks"]["token_fetch"] is True
        assert output["details"]["csrf_length"] == 10
        assert output["details"]["session_id_length"] == 10

    def test_auth_check_env_var_takes_precedence(self, runner, mock_storage_path, monkeypatch):
        """Test auth check uses NOTEBOOKLM_AUTH_JSON when set."""
        # Even if storage file doesn't exist, env var should work
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        env_storage = {
            "cookies": [
                {"name": "SID", "value": "env_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["details"]["auth_source"] == "NOTEBOOKLM_AUTH_JSON"

    def test_auth_check_shows_cookie_domains(self, runner, mock_storage_path):
        """Test auth check displays cookie domains."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "NID", "value": "test_nid", "domain": ".google.com.sg"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert ".google.com" in output["details"]["cookie_domains"]

    def test_auth_check_shows_cookies_by_domain(self, runner, mock_storage_path):
        """Test auth check --json includes detailed cookies_by_domain."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
                {"name": "SID", "value": "regional_sid", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSID", "value": "secure1", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        cookies_by_domain = output["details"]["cookies_by_domain"]

        # Verify .google.com has expected cookies
        assert ".google.com" in cookies_by_domain
        assert "SID" in cookies_by_domain[".google.com"]
        assert "HSID" in cookies_by_domain[".google.com"]
        assert "__Secure-1PSID" in cookies_by_domain[".google.com"]

        # Verify regional domain has its cookies
        assert ".google.com.sg" in cookies_by_domain
        assert "SID" in cookies_by_domain[".google.com.sg"]

    def test_auth_check_skipped_token_fetch_shown(self, runner, mock_storage_path):
        """Test auth check shows token fetch as skipped when --test not used."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["checks"]["token_fetch"] is None  # Not tested

    def test_auth_check_help(self, runner):
        """Test auth check --help shows usage information."""
        result = runner.invoke(cli, ["auth", "check", "--help"])

        assert result.exit_code == 0
        assert "Check authentication status" in result.output
        assert "--test" in result.output
        assert "--json" in result.output


# =============================================================================
# LOGIN LANGUAGE SYNC TESTS
# =============================================================================


class TestLoginLanguageSync:
    """Tests for syncing server language setting to local config after login."""

    @pytest.fixture(autouse=True)
    def _language_module(self):
        """Get the actual language module, bypassing Click group shadowing on Python 3.10."""
        import importlib

        self.language_mod = importlib.import_module("notebooklm.cli.language")

    def test_sync_persists_server_language(self, tmp_path):
        """After login, server language setting is fetched and saved to local config."""
        from notebooklm.cli.session import _sync_server_language_to_config

        config_path = tmp_path / "config.json"

        with (
            patch("notebooklm.cli.session.NotebookLMClient") as mock_client_cls,
            patch.object(self.language_mod, "get_config_path", return_value=config_path),
            patch.object(self.language_mod, "get_home_dir"),
        ):
            mock_client = create_mock_client()
            mock_client.settings = MagicMock()
            mock_client.settings.get_output_language = AsyncMock(return_value="zh_Hans")
            mock_client_cls.from_storage = AsyncMock(return_value=mock_client)

            _sync_server_language_to_config()

        # Verify language was persisted to config
        config = json.loads(config_path.read_text())
        assert config["language"] == "zh_Hans"

    def test_sync_skips_when_server_returns_none(self, tmp_path):
        """No config change when server returns no language."""
        from notebooklm.cli.session import _sync_server_language_to_config

        config_path = tmp_path / "config.json"

        with (
            patch("notebooklm.cli.session.NotebookLMClient") as mock_client_cls,
            patch.object(self.language_mod, "get_config_path", return_value=config_path),
        ):
            mock_client = create_mock_client()
            mock_client.settings = MagicMock()
            mock_client.settings.get_output_language = AsyncMock(return_value=None)
            mock_client_cls.from_storage = AsyncMock(return_value=mock_client)

            _sync_server_language_to_config()

        # Config file should not exist
        assert not config_path.exists()

    def test_sync_does_not_raise_on_error(self):
        """Language sync failure should not raise and should warn the user."""
        from notebooklm.cli.session import _sync_server_language_to_config

        with (
            patch("notebooklm.cli.session.NotebookLMClient") as mock_client_cls,
            patch("notebooklm.cli.session.console") as mock_console,
        ):
            mock_client_cls.from_storage = AsyncMock(side_effect=Exception("Network error"))

            # Should not raise
            _sync_server_language_to_config()

        # Should print a warning so the user knows to sync manually
        mock_console.print.assert_called_once()
        warning_text = mock_console.print.call_args[0][0]
        assert "language" in warning_text.lower()


# =============================================================================
# EDGE CASES
# =============================================================================


class TestSessionEdgeCases:
    def test_use_handles_api_error_gracefully(self, runner, mock_auth, mock_context_file):
        """Test 'use' command handles API errors gracefully."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get = AsyncMock(side_effect=Exception("API Error: Rate limited"))
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")

                # Patch in session module where it's imported
                with patch(
                    "notebooklm.cli.session.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.return_value = "nb_error"

                    result = runner.invoke(cli, ["use", "nb_error"])

        # Should still set context with warning, not crash
        assert result.exit_code == 0
        # Error message should be shown
        assert "Warning" in result.output or "Error" in result.output or "nb_error" in result.output

    def test_status_shows_shared_notebook_correctly(self, runner, mock_context_file):
        """Test status correctly shows shared (non-owner) notebooks."""
        context_data = {
            "notebook_id": "nb_shared",
            "title": "Shared With Me",
            "is_owner": False,
            "created_at": "2024-01-15",
        }
        mock_context_file.write_text(json.dumps(context_data))

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        assert "Shared" in result.output or "nb_shared" in result.output

    def test_use_click_exception_propagates(self, runner, mock_auth, mock_context_file):
        """Test 'use' command re-raises ClickException from resolve_notebook_id."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")

                # Patch resolve_notebook_id to raise ClickException (e.g., ambiguous ID)
                with patch(
                    "notebooklm.cli.session.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.side_effect = click.ClickException("Multiple notebooks match 'nb'")

                    result = runner.invoke(cli, ["use", "nb"])

        # ClickException should propagate (exit code 1)
        assert result.exit_code == 1
        assert "Multiple notebooks match" in result.output

    def test_status_corrupted_json_with_json_flag(self, runner, mock_context_file):
        """Test status --json handles corrupted context file gracefully."""
        # Write invalid JSON but with notebook_id in helpers
        mock_context_file.write_text("{ invalid json }")

        # Mock get_current_notebook to return an ID (simulating partial read)
        with patch("notebooklm.cli.session.get_current_notebook") as mock_get_nb:
            mock_get_nb.return_value = "nb_corrupted"

            result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0
        output_data = json.loads(result.output)
        assert output_data["has_context"] is True
        assert output_data["notebook"]["id"] == "nb_corrupted"
        # Title and is_owner should be None due to JSONDecodeError
        assert output_data["notebook"]["title"] is None
        assert output_data["notebook"]["is_owner"] is None


# =============================================================================
# WINDOWS PERMISSION REGRESSION TESTS (fixes #212)
# =============================================================================


class TestLoginWindowsPermissions:
    """Regression tests for Windows permission handling in login command.

    On Windows, mkdir(mode=0o700) and chmod() can cause PermissionError
    because Python 3.13+ applies restrictive ACLs. The login command must
    skip both on Windows while preserving Unix hardening.

    See: https://github.com/teng-lin/notebooklm-py/issues/212
    """

    @pytest.fixture
    def _patch_login_deps(self, tmp_path, monkeypatch):
        """Patch all login dependencies to isolate mkdir/chmod behavior."""
        storage_path = tmp_path / "home" / "storage_state.json"
        browser_profile = tmp_path / "profile"

        monkeypatch.setattr("notebooklm.cli.session.get_storage_path", lambda: storage_path)
        monkeypatch.setattr(
            "notebooklm.cli.session.get_browser_profile_dir", lambda: browser_profile
        )
        self.storage_parent = storage_path.parent
        self.browser_profile = browser_profile

    def test_windows_login_skips_mode_and_chmod(self, monkeypatch, _patch_login_deps, runner):
        """On Windows, login mkdir calls omit mode= and chmod is never called."""
        import notebooklm.cli.session as session_mod

        monkeypatch.setattr(session_mod.sys, "platform", "win32")

        mkdir_calls = []
        chmod_calls = []
        _orig_mkdir = Path.mkdir

        def _track_mkdir(self, *args, **kwargs):
            mkdir_calls.append({"path": self, "kwargs": kwargs})
            return _orig_mkdir(self, *args, **kwargs)

        def _track_chmod(self, *args, **kwargs):
            chmod_calls.append({"path": self, "args": args})

        monkeypatch.setattr(Path, "mkdir", _track_mkdir)
        monkeypatch.setattr(Path, "chmod", _track_chmod)

        # Trigger the login command but abort early at playwright import
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            runner.invoke(cli, ["login"])

        # mkdir should NOT receive mode= on Windows
        for call in mkdir_calls:
            assert "mode" not in call["kwargs"], (
                f"mkdir received mode= on Windows for {call['path']}"
            )

        # chmod should NOT be called on Windows
        assert len(chmod_calls) == 0, (
            f"chmod called {len(chmod_calls)} time(s) on Windows: {chmod_calls}"
        )

    def test_unix_login_sets_mode_and_chmod(self, monkeypatch, _patch_login_deps, runner):
        """On Unix, login mkdir calls include mode=0o700 and chmod is called."""
        import notebooklm.cli.session as session_mod

        monkeypatch.setattr(session_mod.sys, "platform", "linux")

        mkdir_calls = []
        chmod_calls = []
        _orig_mkdir = Path.mkdir

        def _track_mkdir(self, *args, **kwargs):
            mkdir_calls.append({"path": self, "kwargs": kwargs})
            return _orig_mkdir(self, *args, **kwargs)

        def _track_chmod(self, *args, **kwargs):
            chmod_calls.append({"path": self, "args": args})

        monkeypatch.setattr(Path, "mkdir", _track_mkdir)
        monkeypatch.setattr(Path, "chmod", _track_chmod)

        # Trigger the login command but abort early at playwright import
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            runner.invoke(cli, ["login"])

        # mkdir should receive mode=0o700 on Unix (2 calls: storage_parent + browser_profile)
        mode_calls = [c for c in mkdir_calls if c["kwargs"].get("mode") == 0o700]
        assert len(mode_calls) >= 2, (
            f"Expected ≥2 mkdir calls with mode=0o700 on Unix, got {len(mode_calls)}"
        )

        # chmod(0o700) should be called on Unix (2 calls: storage_parent + browser_profile)
        chmod_700 = [c for c in chmod_calls if c["args"] == (0o700,)]
        assert len(chmod_700) >= 2, f"Expected ≥2 chmod(0o700) calls on Unix, got {len(chmod_700)}"

    def test_windows_storage_chmod_skipped(self, monkeypatch, _patch_login_deps):
        """On Windows, storage_state.json chmod(0o600) is also skipped."""
        import notebooklm.cli.session as session_mod

        monkeypatch.setattr(session_mod.sys, "platform", "win32")

        # The code at line 280-282 checks sys.platform before chmod(0o600)
        # Verify the guard exists by checking the source
        import inspect

        source = inspect.getsource(session_mod)
        # The pattern: if sys.platform != "win32": ... storage_path.chmod(0o600)
        assert 'sys.platform != "win32"' in source or "sys.platform != 'win32'" in source, (
            "Missing Windows guard for storage_state.json chmod(0o600)"
        )


class TestLoginBrowserCookies:
    """Tests for notebooklm login --browser-cookies."""

    def test_browser_cookies_in_help(self, runner):
        """--browser-cookies appears in login --help."""
        result = runner.invoke(cli, ["login", "--help"])
        assert "--browser-cookies" in result.output

    def test_rookiepy_not_installed_shows_error(self, runner):
        """Shows helpful error when rookiepy is not installed."""
        with patch.dict(sys.modules, {"rookiepy": None}):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        assert "rookiepy" in result.output
        assert "pip install" in result.output

    def test_auto_detect_calls_rookiepy_load(self, runner, tmp_path):
        """Auto-detect calls rookiepy.load()."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expires": 1234567890,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": 1234567890,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code == 0, result.output
        mock_rookiepy.load.assert_called_once()

    def test_named_browser_calls_rookiepy_function(self, runner, tmp_path):
        """Named browser calls the matching rookiepy function."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.chrome = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"])
        assert result.exit_code == 0, result.output
        mock_rookiepy.chrome.assert_called_once()

    def test_no_google_cookies_shows_error(self, runner, tmp_path):
        """Shows error when no Google cookies found."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=[])

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm.cli.session.get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        assert "SID" in result.output or "Google" in result.output

    def test_locked_db_shows_close_browser_hint(self, runner, tmp_path):
        """Shows close-browser hint when DB is locked."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(side_effect=OSError("database is locked"))

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm.cli.session.get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        output_lower = result.output.lower()
        assert "close" in output_lower or "browser" in output_lower

    def test_cookies_saved_to_storage_file(self, runner, tmp_path):
        """Cookies are written to storage_state.json."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "mysid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "APISID",
                "value": "apisid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "SAPISID",
                "value": "sapisid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        data = json.loads(storage_file.read_text())
        assert any(c["name"] == "SID" and c["value"] == "mysid" for c in data["cookies"])

    def test_unknown_browser_shows_error(self, runner, tmp_path):
        """Unknown browser name shows a clear error."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(
            side_effect=AttributeError("module has no attribute 'netscape'")
        )

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm.cli.session.get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "netscape"])
        assert result.exit_code != 0

    # ------------------------------------------------------------------
    # firefox::<container> syntax (issue #367)
    # ------------------------------------------------------------------

    def test_firefox_container_syntax_invokes_extractor(self, runner, tmp_path):
        """``--browser-cookies firefox::<name>`` calls the container extractor.

        rookiepy must NOT be touched on this path — that's the whole point
        of the bypass.
        """
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "work_sid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
        ]
        mock_rookiepy = MagicMock()
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm._firefox_containers.resolve_container_id",
                return_value=2,
            ),
            patch(
                "notebooklm._firefox_containers.extract_firefox_container_cookies",
                return_value=mock_cookies,
            ) as mock_extract,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Work"])
        assert result.exit_code == 0, result.output
        mock_extract.assert_called_once()
        # rookiepy must NOT have been called for the firefox:: path.
        mock_rookiepy.firefox.assert_not_called()
        mock_rookiepy.load.assert_not_called()
        # The container's SID should land in the saved storage state.
        data = json.loads(storage_file.read_text())
        assert any(c["name"] == "SID" and c["value"] == "work_sid" for c in data["cookies"])

    def test_firefox_container_none_passes_literal_none(self, runner, tmp_path):
        """``firefox::none`` resolves to ``"none"`` and skips rookiepy."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "default_sid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
        ]
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch(
                "notebooklm._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm._firefox_containers.extract_firefox_container_cookies",
                return_value=mock_cookies,
            ) as mock_extract,
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::none"])
        assert result.exit_code == 0, result.output
        # Confirm the extractor was called with the ``"none"`` sentinel.
        _, kwargs = mock_extract.call_args
        positional = mock_extract.call_args.args
        # signature: extract_firefox_container_cookies(profile, container_id, domains=…)
        assert positional[1] == "none" or kwargs.get("container_id") == "none"

    def test_firefox_container_unknown_name_shows_listing(self, runner, tmp_path):
        """Unknown container name shows a helpful error and exits non-zero."""
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch(
                "notebooklm._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm._firefox_containers.resolve_container_id",
                side_effect=ValueError(
                    "Firefox container 'Nope' not found. Available containers: 'Work', 'Personal'."
                ),
            ),
            patch(
                "notebooklm.cli.session.get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Nope"])
        assert result.exit_code != 0
        assert "Nope" in result.output
        assert "Work" in result.output

    def test_firefox_container_no_firefox_profile_shows_error(self, runner, tmp_path):
        """Missing Firefox install shows a friendly error, not a stack trace."""
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch(
                "notebooklm._firefox_containers.find_firefox_profile_path",
                return_value=None,
            ),
            patch(
                "notebooklm.cli.session.get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Work"])
        assert result.exit_code != 0
        # The message should mention firefox / profile so the user knows what's up.
        out_lower = result.output.lower()
        assert "firefox" in out_lower
        assert "profile" in out_lower

    def test_firefox_empty_container_spec_rejected(self, runner, tmp_path):
        """`--browser-cookies firefox::` (empty spec) must error, not silently
        fall through to the unfiltered merge this feature exists to prevent.
        Regression guard for the polish review (3-way HIGH consensus).
        """
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch(
                "notebooklm.cli.session.get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::"])
        assert result.exit_code != 0
        assert "Empty Firefox container specifier" in result.output
        # The error should point at the correct syntax so the user can recover.
        assert "firefox::none" in result.output
        assert "container-name" in result.output

    def test_unscoped_firefox_warns_when_containers_in_use(self, runner, tmp_path):
        """Unscoped ``firefox`` emits a yellow warning if containers are in use."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.firefox = MagicMock(return_value=mock_cookies)
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm._firefox_containers.has_container_cookies_in_use",
                return_value=True,
            ),
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox"])
        assert result.exit_code == 0, result.output
        # Rich may wrap the message; assert on substrings that survive wrap.
        assert "Multi-Account" in result.output
        assert "firefox::" in result.output

    def test_unscoped_firefox_no_warning_when_no_containers(self, runner, tmp_path):
        """No warning when the profile is not actually using containers."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.firefox = MagicMock(return_value=mock_cookies)
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm._firefox_containers.has_container_cookies_in_use",
                return_value=False,
            ),
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox"])
        assert result.exit_code == 0, result.output
        assert "Multi-Account" not in result.output


# =============================================================================
# AUTH LOGOUT COMMAND TESTS
# =============================================================================


class TestAuthLogoutCommand:
    def test_auth_logout_deletes_storage_and_browser_profile(
        self, runner, tmp_path, mock_context_file
    ):
        """Test auth logout deletes both storage_state.json and browser_profile/."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        mock_context_file.write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@example.com"}})
        )
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()
        (browser_dir / "Default").mkdir()
        (browser_dir / "Default" / "Cookies").write_text("data")

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not storage_file.exists()
        assert not mock_context_file.exists()
        assert not browser_dir.exists()

    def test_auth_logout_when_already_logged_out(self, runner, tmp_path, mock_context_file):
        """Test auth logout is a no-op with friendly message when not logged in."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "browser_profile"
        # Neither exists

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "already" in result.output.lower() or "No active session" in result.output

    def test_auth_logout_partial_state_only_storage(self, runner, tmp_path, mock_context_file):
        """Test auth logout handles case where only storage_state.json exists."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # browser_dir does not exist

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not storage_file.exists()

    def test_auth_logout_handles_permission_error_on_rmtree(
        self, runner, tmp_path, mock_context_file
    ):
        """Test auth logout handles locked browser profile gracefully."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch(
                "notebooklm.cli.session.shutil.rmtree",
                side_effect=OSError("sharing violation"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 1
        assert "in use" in result.output.lower() or "Cannot" in result.output

    def test_auth_logout_handles_permission_error_on_unlink(
        self, runner, tmp_path, mock_context_file
    ):
        """Test auth logout handles locked storage_state.json gracefully on Windows."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No browser dir

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch.object(
                type(storage_file),
                "unlink",
                side_effect=OSError("file in use"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 1
        assert "Cannot" in result.output or "in use" in result.output.lower()

    def test_auth_logout_clears_cached_notebook_context(self, runner, tmp_path, mock_context_file):
        """Logout must remove context.json so the next command does not reuse
        notebook_id / conversation_id from the previous account.

        Issues #114 / #294 surfaced as "not found" / permission errors after an
        account switch. The PR's account-mismatch hint steers users to
        logout→login as the fix; the flow only works if context is actually
        cleared on logout.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()

        # Simulate cached notebook / conversation from a previous session.
        mock_context_file.write_text(
            json.dumps(
                {
                    "notebook_id": "old-account-notebook",
                    "conversation_id": "old-account-conversation",
                }
            )
        )
        assert mock_context_file.exists()

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not mock_context_file.exists()

    def test_auth_logout_no_context_file_does_not_error(self, runner, tmp_path, mock_context_file):
        """Logout must tolerate a missing context.json without erroring.

        clear_context() is a no-op when the file does not exist; assert that
        the main logout path still succeeds.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No context file, no browser dir.

        assert not mock_context_file.exists()

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output

    def test_auth_logout_handles_os_error_on_context_unlink(
        self, runner, tmp_path, mock_context_file
    ):
        """Logout must surface an OSError on context.json removal as SystemExit(1).

        Parity with the existing handlers for storage_state.json and the browser
        profile: a locked/unwritable context file should produce a clean
        diagnostic message, not an unhandled traceback.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No browser dir — nothing to remove in that step.
        mock_context_file.write_text('{"notebook_id": "stale"}')

        with (
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch(
                "notebooklm.cli.session.clear_context",
                side_effect=OSError("file in use"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 1
        assert "context file" in result.output.lower()


# =============================================================================
# AUTH REFRESH COMMAND TESTS
# =============================================================================


class TestAuthRefreshCommand:
    """Tests for the 'auth refresh' one-shot keepalive command."""

    @pytest.fixture
    def mock_storage_path(self, tmp_path):
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )
        with patch("notebooklm.cli.session.get_storage_path", return_value=storage_file):
            yield storage_file

    def test_auth_refresh_success(self, runner, mock_storage_path):
        """auth refresh exits 0 and prints `ok` on a successful token fetch."""
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 0
        assert "ok" in result.output.lower()
        mock_fetch.assert_awaited_once()

    def test_auth_refresh_quiet_suppresses_success_output(self, runner, mock_storage_path):
        """--quiet keeps stdout clean when refresh succeeds (cron-friendly)."""
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh", "--quiet"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_auth_refresh_failure_exits_nonzero(self, runner, mock_storage_path):
        """Token fetch failure exits 1 with stderr message — picked up by cron logs."""
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = ValueError("Authentication expired or invalid.")
            result = runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 1
        assert "authentication expired" in result.output.lower()

    def test_auth_refresh_failure_includes_exception_class(self, runner, mock_storage_path):
        """Sparse exception messages (e.g. httpx.ConnectTimeout) still get a
        diagnostic class name in the cron log."""
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = httpx.ConnectTimeout("")  # empty message
            result = runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 1
        assert "ConnectTimeout" in result.output

    def test_auth_refresh_rejects_env_var_auth(self, runner, monkeypatch, mock_storage_path):
        """NOTEBOOKLM_AUTH_JSON has no writable backing store; refreshing it
        would silently rotate SIDTS but persist nothing. Refuse loudly."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[]}')
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            result = runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 1
        assert "NOTEBOOKLM_AUTH_JSON" in result.output
        assert "incompatible" in result.output.lower()
        # Critical: no token fetch should run when the env var is set —
        # otherwise we'd be doing a server-side rotation that gets lost.
        mock_fetch.assert_not_awaited()

    def test_auth_refresh_propagates_global_profile_flag(self, runner, tmp_path):
        """`notebooklm --profile work auth refresh` resolves the work profile.

        Guards against the launchd/cron case where the global -p flag must
        flow through ctx.obj into fetch_tokens_with_domains.
        """
        work_storage = tmp_path / "work_storage_state.json"
        work_storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "y", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )

        def fake_storage_path(profile=None):
            assert profile == "work", f"expected profile='work', got {profile!r}"
            return work_storage

        with (
            patch("notebooklm.cli.session.get_storage_path", side_effect=fake_storage_path),
            patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["--profile", "work", "auth", "refresh"])

        assert result.exit_code == 0, result.output
        # fetch_tokens_with_domains(path, profile) — verify the work profile
        # was threaded through to the auth layer.
        called_args = mock_fetch.call_args
        assert called_args.args[0] == work_storage
        assert called_args.args[1] == "work"

    def test_auth_refresh_browser_cookies_repairs_account_after_order_change(
        self, runner, tmp_path
    ):
        """If a browser account logs out and indices shift, match by email and
        rewrite context.json with the new internal account index."""
        storage = tmp_path / "profiles" / "bob" / "storage_state.json"
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
        (storage.parent / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@gmail.com"}}),
            encoding="utf-8",
        )
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="bob@gmail.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.cli.session.get_storage_path", return_value=storage),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf_ok", "session_ok"),
            ) as mock_fetch,
        ):
            result = runner.invoke(cli, ["auth", "refresh", "--browser-cookie", "chrome"])

        assert result.exit_code == 0, result.output
        assert "bob@gmail.com" in result.output
        assert "authuser" not in result.output
        assert json.loads((storage.parent / "context.json").read_text())["account"] == {
            "authuser": 0,
            "email": "bob@gmail.com",
        }
        mock_fetch.assert_awaited_once()

    def test_auth_refresh_browser_cookies_fails_when_profile_email_signed_out(
        self, runner, tmp_path
    ):
        """A stored email is identity; if that account is absent from the browser,
        do not refresh the profile with a different signed-in account."""
        storage = tmp_path / "profiles" / "bob" / "storage_state.json"
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
        (storage.parent / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@gmail.com"}}),
            encoding="utf-8",
        )
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.cli.session.get_storage_path", return_value=storage),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
            ) as mock_fetch,
        ):
            result = runner.invoke(cli, ["auth", "refresh", "--browser-cookies", "chrome"])

        assert result.exit_code == 1
        assert "bob@gmail.com" in result.output
        assert "not signed in" in result.output.lower()
        assert "alice@example.com" in result.output
        assert json.loads((storage.parent / "context.json").read_text())["account"] == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }
        mock_fetch.assert_not_awaited()


# =============================================================================
# AUTH INSPECT + MULTI-ACCOUNT LOGIN TESTS (issue #359)
# =============================================================================


def _multiaccount_rookiepy_mock():
    """Build a rookiepy mock that returns the same SID-bearing cookies for any
    domain query. Account enumeration is controlled by the patched
    enumerate_accounts coroutine in each test.
    """
    cookies = [
        {
            "domain": ".google.com",
            "name": name,
            "value": f"{name}-value",
            "path": "/",
            "secure": True,
            "expires": 9999,
            "http_only": False,
        }
        for name in ("SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSIDTS")
    ]
    mock = MagicMock()
    mock.chrome = MagicMock(return_value=cookies)
    mock.load = MagicMock(return_value=cookies)
    return mock


class TestAuthInspect:
    def test_inspect_lists_accounts(self, runner):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@gmail.com", is_default=False),
                Account(authuser=2, email="carol@ws.com", is_default=False),
            ]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch(
                "notebooklm.cli.session.run_async",
                side_effect=lambda c: c.send(None) if False else __import__("asyncio").run(c),
            ),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome"])
        assert result.exit_code == 0, result.output
        assert "alice@example.com" in result.output
        assert "bob@gmail.com" in result.output
        assert "carol@ws.com" in result.output
        assert "authuser" not in result.output

    def test_inspect_json_output(self, runner):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["accounts"][0]["email"] == "alice@example.com"
        assert "authuser" not in data["accounts"][0]
        assert data["accounts"][0]["is_default"] is True


class TestLoginMultiAccount:
    """--account / --profile-name / --all-accounts on `notebooklm login --browser-cookies`."""

    def test_account_writes_account_metadata(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@gmail.com", is_default=False),
            ]

        # Layout: tmp_path/profiles/bob/storage_state.json and context.json.
        target_dir = tmp_path / "profiles" / "bob"

        def fake_get_storage_path(profile=None):
            return target_dir / "storage_state.json"

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.cli.session.get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--account", "bob@gmail.com"],
            )

        assert result.exit_code == 0, result.output
        context_json = target_dir / "context.json"
        assert context_json.exists()
        assert json.loads(context_json.read_text())["account"] == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }

    def test_storage_without_account_keeps_default_import_path(self, runner, tmp_path):
        target = tmp_path / "storage_state.json"

        with (
            patch("notebooklm.cli.session._login_with_browser_cookies") as login_mock,
            patch(
                "notebooklm.auth.enumerate_accounts",
                side_effect=AssertionError("should not enumerate accounts"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--storage", str(target)],
            )

        assert result.exit_code == 0, result.output
        login_mock.assert_called_once()
        assert login_mock.call_args.args[0] == target
        assert login_mock.call_args.args[1] == "chrome"

    def test_account_not_found_aborts(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch(
                "notebooklm.cli.session.get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--account", "bob@gmail.com"],
            )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_all_accounts_writes_one_profile_per_account(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@gmail.com", is_default=False),
            ]

        target_root = tmp_path / "profiles"

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            if not target_root.exists():
                return []
            return sorted(path.name for path in target_root.iterdir() if path.is_dir())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.cli.session.get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert result.exit_code == 0, result.output
        alice_meta = json.loads((target_root / "alice" / "context.json").read_text())["account"]
        bob_meta = json.loads((target_root / "bob" / "context.json").read_text())["account"]
        assert alice_meta == {"authuser": 0, "email": "alice@example.com"}
        assert bob_meta == {"authuser": 1, "email": "bob@gmail.com"}

    def test_all_accounts_rerun_reuses_profiles_by_email(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@gmail.com", is_default=False),
            ]

        target_root = tmp_path / "profiles"

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            if not target_root.exists():
                return []
            return sorted(path.name for path in target_root.iterdir() if path.is_dir())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.cli.session.get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            first = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])
            second = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert sorted(path.name for path in target_root.iterdir()) == ["alice", "bob"]

    def test_all_accounts_does_not_overwrite_same_name_without_matching_email(
        self, runner, tmp_path
    ):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        target_root = tmp_path / "profiles"
        existing = target_root / "alice"
        existing.mkdir(parents=True)
        (existing / "storage_state.json").write_text("{}")

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            return sorted(path.name for path in target_root.iterdir() if path.is_dir())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.cli.session.get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert result.exit_code == 0, result.output
        assert (target_root / "alice-2" / "context.json").exists()
        assert json.loads((target_root / "alice-2" / "context.json").read_text())["account"] == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    def test_all_accounts_updates_existing_profile_when_authuser_index_changes(
        self, runner, tmp_path
    ):
        mock_rk = _multiaccount_rookiepy_mock()

        first_accounts = None

        async def _enum(*args, **kwargs):
            nonlocal first_accounts
            from notebooklm.auth import Account

            if first_accounts is None:
                first_accounts = True
                return [
                    Account(authuser=0, email="alice@example.com", is_default=True),
                    Account(authuser=1, email="bob@gmail.com", is_default=False),
                ]
            return [Account(authuser=0, email="bob@gmail.com", is_default=True)]

        target_root = tmp_path / "profiles"

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            if not target_root.exists():
                return []
            return sorted(path.name for path in target_root.iterdir() if path.is_dir())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.cli.session.get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            first = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])
            second = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert json.loads((target_root / "bob" / "context.json").read_text())["account"] == {
            "authuser": 0,
            "email": "bob@gmail.com",
        }
        assert sorted(path.name for path in target_root.iterdir()) == ["alice", "bob"]

    def test_account_without_browser_cookies_rejected(self, runner):
        # --account only makes sense with --browser-cookies; the CLI should
        # tell the user instead of silently ignoring it.
        result = runner.invoke(cli, ["login", "--account", "bob@gmail.com"])
        assert result.exit_code != 0
        assert "browser-cookies" in result.output

    def test_authuser_option_is_not_exposed(self, runner):
        result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--authuser", "1"])
        assert result.exit_code != 0
        assert "No such option: --authuser" in result.output

    def test_all_accounts_combined_with_account_rejected(self, runner):
        result = runner.invoke(
            cli,
            [
                "login",
                "--browser-cookies",
                "chrome",
                "--all-accounts",
                "--account",
                "bob@gmail.com",
            ],
        )
        assert result.exit_code != 0
        assert "all-accounts" in result.output.lower()


class TestStaleAccountMetadataCleanup:
    """Default-account login must clear stale account metadata from previous targeted runs."""

    def test_default_login_removes_stale_account_metadata(self, runner, tmp_path):
        storage_file = tmp_path / "storage.json"
        # Simulate a previous targeted extraction.
        (tmp_path / "context.json").write_text(
            json.dumps(
                {
                    "notebook_id": "nb_existing",
                    "account": {"authuser": 1, "email": "bob@gmail.com"},
                }
            ),
            encoding="utf-8",
        )

        mock_cookies = [
            {
                "domain": ".google.com",
                "name": name,
                "value": f"{name}-value",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            }
            for name in ("SID", "APISID", "SAPISID", "__Secure-1PSIDTS")
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch("notebooklm.cli.session.get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch(
                "notebooklm.cli.session.fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])

        assert result.exit_code == 0, result.output
        # Account metadata must be gone so subsequent token fetches don't keep
        # routing to the old account, while unrelated notebook context survives.
        assert json.loads((tmp_path / "context.json").read_text()) == {"notebook_id": "nb_existing"}
