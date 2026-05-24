"""Tests for the core ``notebooklm login`` command (Playwright + URL validation flows).

This file was extracted from the legacy ``test_session.py`` as part of
D1 PR-3 (test-monkeypatch-policy migration). The shared fixtures and
helpers live in ``_session_helpers.py``; the proxy-block-aware
``patch_session_login_dual`` lives in ``tests/_fixtures``.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from _fixtures import patch_session_login_dual
from notebooklm.notebooklm_cli import cli

from .conftest import create_mock_client


def _make_from_storage_cm(client):
    """Wrap ``client`` in an async context manager.

    ``NotebookLMClient.from_storage`` returns ``_FromStorageContext``
    (an awaitable async-context-manager). Tests that mock
    ``from_storage`` need a stand-in that supports ``async with``;
    this helper builds one from a plain mock client.
    """

    @asynccontextmanager
    async def _cm():
        yield client

    return _cm()


def _required_cookie_state() -> dict:
    return {
        "cookies": [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "psidts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
        "origins": [{"origin": "https://notebooklm.google.com", "localStorage": []}],
    }


def _storage_account(storage_file):
    data = json.loads(storage_file.read_text())
    return data.get("notebooklm", {}).get("account")


class TestLoginUrlValidation:
    def test_url_matches_default_base_host(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

        from notebooklm.cli.session_cmd import _url_matches_base_host

        assert _url_matches_base_host("https://notebooklm.google.com/notebook/abc")
        assert not _url_matches_base_host(
            "https://example.com/path?next=https://notebooklm.google.com/"
        )

    def test_url_matches_enterprise_base_host(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")

        from notebooklm.cli.session_cmd import _url_matches_base_host

        assert _url_matches_base_host("https://notebooklm.cloud.google.com/notebook/abc")
        assert not _url_matches_base_host("https://notebooklm.google.com/notebook/abc")

    def test_connection_error_help_uses_enterprise_base_host(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")

        from notebooklm.cli.session_cmd import _connection_error_help

        blocked_host = (
            _connection_error_help().split("Firewall or VPN blocking ", 1)[1].split("\n", 1)[0]
        )
        assert blocked_host == "notebooklm.cloud.google.com"


class TestLoginCommand:
    def test_login_playwright_import_error_handling(self, runner, tmp_path, monkeypatch):
        """Test that ImportError for playwright is handled gracefully.

        Hermetic: NOTEBOOKLM_HOME=tmp_path so the test doesn't write to real
        ~/.notebooklm/ (PermissionError in sandboxes).
        """
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        # Patch the import inside the login function to raise ImportError
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = runner.invoke(cli, ["login"])

            # Should exit with code 1 and show helpful message
            assert result.exit_code == 1
            assert "Playwright not installed" in result.output or "pip install" in result.output

    def test_login_install_hint_includes_browser_extra(self, runner, tmp_path, monkeypatch):
        """Regression: the install hint must include the literal `[browser]` extra.

        Before the fix, the hint was passed through `console.print()` with
        markup enabled, so rich interpreted `[browser]` as a (nonexistent)
        style tag and stripped it — leaving users with `pip install
        "notebooklm-py"` (no extras), which doesn't pull Playwright.

        Hermetic: `NOTEBOOKLM_HOME=tmp_path` so the test doesn't write to the
        real `~/.notebooklm/` (would fail with PermissionError in sandboxes).
        """
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = runner.invoke(cli, ["login"])
            assert result.exit_code == 1
            assert '"notebooklm-py[browser]"' in result.output, (
                f"Install hint must show the literal [browser] extra; got: {result.output!r}"
            )

        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result_edge = runner.invoke(cli, ["login", "--browser", "msedge"])
            assert result_edge.exit_code == 1
            assert '"notebooklm-py[browser]"' in result_edge.output, (
                "Install hint must show the literal [browser] extra for msedge too; "
                f"got: {result_edge.output!r}"
            )

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
        """Test login --help shows --browser option with chromium/chrome/msedge choices."""
        result = runner.invoke(cli, ["login", "--help"])

        assert result.exit_code == 0
        assert "--browser" in result.output
        assert "chromium" in result.output
        assert "chrome" in result.output
        assert "msedge" in result.output

    def test_login_rejects_invalid_browser(self, runner):
        """Test login rejects invalid --browser values."""
        result = runner.invoke(cli, ["login", "--browser", "firefox"])

        assert result.exit_code != 0

    @pytest.fixture
    def mock_login_browser(self, tmp_path):
        """Mock Playwright browser launch for login --browser tests.

        The mocked page reports it is already on the NotebookLM host, so the
        auto-detect ``wait_for_url`` fast-path is taken and the test does not
        block. Yields (mock_ensure, mock_launch) for assertions on chromium
        install check and launch_persistent_context kwargs.
        """
        # patch() below resolves the target module at setup time and raises
        # ModuleNotFoundError if playwright is not installed. Skip cleanly
        # so local runs without ``uv sync --extra browser`` don't crash.
        pytest.importorskip(
            "playwright.sync_api",
            reason="playwright not installed; install with: uv sync --extra browser",
        )
        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed") as mock_ensure,
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=tmp_path / "storage.json"),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=tmp_path / "profile",
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
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

    @pytest.mark.parametrize("browser", ["msedge", "chrome"])
    def test_login_channel_browser_skips_chromium_install(
        self, runner, mock_login_browser, browser
    ):
        """--browser msedge|chrome skips _ensure_chromium_installed."""
        mock_ensure, _ = mock_login_browser
        runner.invoke(cli, ["login", "--browser", browser])
        mock_ensure.assert_not_called()

    @pytest.mark.parametrize("browser", ["msedge", "chrome"])
    def test_login_channel_browser_passes_channel_param(self, runner, mock_login_browser, browser):
        """--browser msedge|chrome passes channel=<browser> to launch_persistent_context."""
        _, mock_launch = mock_login_browser
        runner.invoke(cli, ["login", "--browser", browser])
        assert mock_launch.call_args[1].get("channel") == browser

    def test_login_chromium_default_no_channel(self, runner, mock_login_browser):
        """Test default chromium calls _ensure_chromium_installed and has no channel."""
        mock_ensure, mock_launch = mock_login_browser
        runner.invoke(cli, ["login", "--browser", "chromium"])
        mock_ensure.assert_called_once()
        assert "channel" not in mock_launch.call_args[1]

    @pytest.mark.parametrize(
        ("browser", "expected_label", "expected_install_url_fragment"),
        [
            ("msedge", "Microsoft Edge", "microsoft.com/edge"),
            ("chrome", "Google Chrome", "google.com/chrome"),
        ],
    )
    @pytest.mark.requires_playwright
    def test_login_channel_browser_not_installed_shows_helpful_error(
        self, runner, tmp_path, browser, expected_label, expected_install_url_fragment
    ):
        """--browser msedge|chrome shows helpful error when the browser is not installed."""
        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=tmp_path / "storage.json"),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=tmp_path / "profile",
            ),
        ):
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.side_effect = Exception(
                f"Executable doesn't exist at /{browser}\nFailed to launch"
            )

            result = runner.invoke(cli, ["login", "--browser", browser])

        assert result.exit_code == 1
        assert f"{expected_label} not found" in result.output
        assert expected_install_url_fragment in result.output

    @pytest.fixture
    def mock_login_browser_with_storage(self, tmp_path):
        """Mock Playwright browser for login tests that assert exit_code == 0.

        Like mock_login_browser but also makes storage_state() return a dict
        that the login flow can write via atomic_write_json. The mocked page
        reports it is already on the NotebookLM host, so the auto-detect
        fast-path is taken.
        """
        # See ``mock_login_browser`` above — same playwright-missing skip path.
        pytest.importorskip(
            "playwright.sync_api",
            reason="playwright not installed; install with: uv sync --extra browser",
        )
        storage_file = tmp_path / "storage.json"
        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=tmp_path / "profile",
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            # storage_state() now returns a dict; atomic_write_json writes it.
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}
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
    @pytest.mark.requires_playwright
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

    @pytest.mark.requires_playwright
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

    def test_login_auto_detect_skipped_when_already_logged_in(
        self, runner, mock_login_browser_with_storage
    ):
        """When the initial page is already on NotebookLM, wait_for_url is not called."""
        mock_page = mock_login_browser_with_storage

        result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Already logged in" in result.output
        mock_page.wait_for_url.assert_not_called()

    def test_login_auto_detect_waits_for_url_when_not_logged_in(
        self, runner, mock_login_browser_with_storage
    ):
        """When the initial page is on accounts.google.com, wait_for_url is called."""
        mock_page = mock_login_browser_with_storage
        # Initial URL is on Google login, then wait_for_url "succeeds" and the
        # next reads of mock_page.url return the NotebookLM host for the
        # subsequent cookie-forcing navigation.
        mock_page.url = "https://accounts.google.com/signin"

        def succeed(url, **kwargs):
            mock_page.url = "https://notebooklm.google.com/"

        mock_page.wait_for_url.side_effect = succeed

        result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        mock_page.wait_for_url.assert_called_once()
        # Verify timeout=300_000 (5 minutes) is passed
        assert mock_page.wait_for_url.call_args.kwargs.get("timeout") == 300_000
        assert "Login detected" in result.output

    @pytest.mark.requires_playwright
    def test_login_auto_detect_timeout_exits_with_helpful_message(
        self, runner, mock_login_browser_with_storage
    ):
        """When wait_for_url times out, login exits 1 with a helpful message."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        mock_page = mock_login_browser_with_storage
        mock_page.url = "https://accounts.google.com/signin"
        mock_page.wait_for_url.side_effect = PlaywrightTimeout("Timeout 300000ms exceeded")

        result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        assert "Login not detected within 5 minutes" in result.output

    @pytest.mark.requires_playwright
    def test_login_auto_detect_browser_closed_during_wait_shows_help(
        self, runner, mock_login_browser_with_storage
    ):
        """When the browser is closed during wait_for_url, login surfaces BROWSER_CLOSED_HELP."""
        from playwright.sync_api import Error as PlaywrightError

        mock_page = mock_login_browser_with_storage
        mock_page.url = "https://accounts.google.com/signin"
        mock_page.wait_for_url.side_effect = PlaywrightError(
            "Target page, context or browser has been closed"
        )

        result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        assert "browser window was closed" in result.output.lower()

    def test_login_auto_detect_final_url_drift_fails_safely(
        self, runner, mock_login_browser_with_storage
    ):
        """If the cookie-forcing round-trip leaves us off-host, fail without saving auth."""
        mock_page = mock_login_browser_with_storage
        # Start unauthenticated; wait_for_url succeeds; final cookie-forcing
        # goto bounces back to accounts.google.com (session invalidated mid-flow).
        mock_page.url = "https://accounts.google.com/signin"

        def wait_succeeds(url, **kwargs):
            mock_page.url = "https://notebooklm.google.com/"

        def goto_drifts(url, **kwargs):
            if "notebooklm" in url:
                mock_page.url = "https://accounts.google.com/AccountChooser"

        mock_page.wait_for_url.side_effect = wait_succeeds
        mock_page.goto.side_effect = goto_drifts

        result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        assert "Unexpected URL after login" in result.output
        assert "Authentication saved" not in result.output

    @pytest.mark.requires_playwright
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

        with patch("notebooklm.cli.session_cmd.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output
        # Verify that goto was called more than once (retried)
        assert mock_page.goto.call_count >= 2

    @pytest.mark.requires_playwright
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

        with patch("notebooklm.cli.session_cmd.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output

    @pytest.mark.requires_playwright
    def test_login_exits_after_max_retries(self, runner, mock_login_browser_with_storage):
        """Test login exits with error message after 3 failed connection attempts (#243)."""
        mock_page = mock_login_browser_with_storage
        from playwright.sync_api import Error as PlaywrightError

        def goto_side_effect(url, **kwargs):
            raise PlaywrightError(
                "Page.goto: net::ERR_CONNECTION_CLOSED at https://notebooklm.google.com/"
            )

        mock_page.goto.side_effect = goto_side_effect

        with patch("notebooklm.cli.session_cmd.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        assert "Failed to connect to NotebookLM" in result.output
        assert "Network connectivity" in result.output or "Firewall" in result.output
        # Verify retry attempts were made
        assert mock_page.goto.call_count == 3

    @pytest.mark.requires_playwright
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

        with patch("notebooklm.cli.session_cmd.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code != 0
        # Should fail immediately without retrying (only 1 call)
        assert mock_page.goto.call_count == 1

    @pytest.mark.requires_playwright
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

        with patch("notebooklm.cli.session_cmd.time.sleep"):
            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        # Verify that CONNECTION_ERROR_HELP is actually displayed
        assert "Failed to connect to NotebookLM after multiple retries" in result.output
        assert "Network connectivity issues" in result.output
        assert "Firewall or VPN" in result.output
        assert "Check your internet connection" in result.output
        # Verify exactly 3 retry attempts
        assert mock_page.goto.call_count == 3

    @pytest.mark.requires_playwright
    def test_login_fresh_deletes_browser_profile(self, runner, tmp_path):
        """Test --fresh deletes existing browser_profile directory before login."""
        browser_dir = tmp_path / "profile"
        browser_dir.mkdir()
        (browser_dir / "Default" / "Cookies").parent.mkdir(parents=True)
        (browser_dir / "Default" / "Cookies").write_text("fake cookies")

        storage_file = tmp_path / "storage.json"

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}
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

    @pytest.mark.requires_playwright
    def test_login_fresh_works_when_no_profile_exists(self, runner, tmp_path):
        """Test --fresh works when browser_profile doesn't exist yet (first login)."""
        browser_dir = tmp_path / "profile"
        # Do NOT create browser_dir - simulates first login
        storage_file = tmp_path / "storage.json"

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login", "--fresh"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output

    @pytest.mark.requires_playwright
    def test_playwright_login_writes_single_account_metadata(self, runner, tmp_path):
        """Playwright login records account metadata when discovery has one account."""
        from notebooklm.auth import Account

        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        async def _enum(*args, **kwargs):
            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_page.content.return_value = "<html></html>"
            mock_context.pages = [mock_page]
            mock_context.storage_state.return_value = _required_cookie_state()
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0, result.output
        assert _storage_account(storage_file) == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    @pytest.mark.requires_playwright
    def test_playwright_login_repairs_metadata_after_sync_context_exits(self, tmp_path):
        """Metadata repair must not run while Playwright's sync loop is active."""
        from notebooklm.cli.services import playwright_login

        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"
        context_active = False
        repair_calls = []

        # Issue #1000: account repair calls run_async(), so it must happen
        # after Playwright's sync context has torn down its event loop.
        mock_context = MagicMock()
        mock_page = MagicMock()
        mock_page.url = "https://notebooklm.google.com/"
        mock_page.content.return_value = "<html></html>"
        mock_context.pages = [mock_page]
        mock_context.storage_state.return_value = _required_cookie_state()
        mock_playwright = MagicMock()
        mock_playwright.chromium.launch_persistent_context.return_value = mock_context

        class FakeSyncPlaywright:
            def __enter__(self):
                nonlocal context_active
                context_active = True
                return mock_playwright

            def __exit__(self, exc_type, exc, tb):
                nonlocal context_active
                context_active = False
                return False

        def fake_sync_playwright():
            return FakeSyncPlaywright()

        def fake_repair(storage_path, *, page_html=None, quiet=False):
            repair_calls.append(
                {
                    "storage_path": storage_path,
                    "page_html": page_html,
                    "quiet": quiet,
                    "context_active": context_active,
                }
            )

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright", side_effect=fake_sync_playwright),
            patch(
                "notebooklm.cli.services.playwright_login.repair_playwright_account_metadata",
                side_effect=fake_repair,
            ),
        ):
            playwright_login.run_playwright_login(
                playwright_login.PlaywrightLoginPlan(
                    browser="chromium",
                    browser_profile=browser_dir,
                    storage_path=storage_file,
                )
            )

        assert repair_calls == [
            {
                "storage_path": storage_file,
                "page_html": "<html></html>",
                "quiet": False,
                "context_active": False,
            }
        ]

    @pytest.mark.requires_playwright
    def test_playwright_login_writes_account_matched_by_page_email(self, runner, tmp_path):
        """When multiple accounts are visible, the current page email selects the route."""
        from notebooklm.auth import Account

        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        async def _enum(*args, **kwargs):
            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@example.com", is_default=False),
            ]

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_page.content.return_value = '<script>"bob@example.com"</script>'
            mock_context.pages = [mock_page]
            mock_context.storage_state.return_value = _required_cookie_state()
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0, result.output
        assert _storage_account(storage_file) == {
            "authuser": 1,
            "email": "bob@example.com",
        }

    @pytest.mark.requires_playwright
    def test_playwright_login_uses_recovered_page_email_for_metadata(self, runner, tmp_path):
        """If cookie-forcing recovers a page, stale pre-recovery HTML is ignored."""
        from playwright.sync_api import Error as PlaywrightError

        from notebooklm.auth import Account

        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        async def _enum(*args, **kwargs):
            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@example.com", is_default=False),
            ]

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            mock_context = MagicMock()
            mock_page_stale = MagicMock()
            mock_page_stale.url = "https://notebooklm.google.com/"
            mock_page_stale.content.return_value = '<script>"alice@example.com"</script>'
            goto_count = 0

            def stale_goto(url, **kwargs):
                nonlocal goto_count
                goto_count += 1
                if goto_count == 1:
                    return None
                raise PlaywrightError("Page.goto: Target page, context or browser has been closed")

            mock_page_stale.goto.side_effect = stale_goto
            mock_page_recovered = MagicMock()
            mock_page_recovered.url = "https://notebooklm.google.com/"
            mock_page_recovered.content.return_value = '<script>"bob@example.com"</script>'
            mock_context.pages = [mock_page_stale]
            mock_context.new_page.return_value = mock_page_recovered
            mock_context.storage_state.return_value = _required_cookie_state()
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0, result.output
        assert _storage_account(storage_file) == {
            "authuser": 1,
            "email": "bob@example.com",
        }

    @pytest.mark.requires_playwright
    def test_playwright_login_clears_metadata_when_account_ambiguous(self, runner, tmp_path):
        """Multiple discovered accounts without a page email must not pick silently."""
        from notebooklm.auth import Account

        storage_file = tmp_path / "storage.json"
        context_file = tmp_path / "context.json"
        context_file.write_text(
            json.dumps(
                {
                    "notebook_id": "nb_existing",
                    "account": {"authuser": 1, "email": "old@example.com"},
                }
            ),
            encoding="utf-8",
        )
        browser_dir = tmp_path / "profile"

        async def _enum(*args, **kwargs):
            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@example.com", is_default=False),
            ]

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_page.content.return_value = "<html></html>"
            mock_context.pages = [mock_page]
            mock_context.storage_state.return_value = _required_cookie_state()
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0, result.output
        assert _storage_account(storage_file) is None
        assert json.loads(context_file.read_text()) == {"notebook_id": "nb_existing"}
        assert "account metadata was not written" in result.output

    def test_auth_refresh_repairs_missing_playwright_account_metadata(self, runner, tmp_path):
        """File-backed auth refresh can migrate a Playwright state missing metadata."""
        from notebooklm.auth import Account

        storage_file = tmp_path / "storage.json"
        original_state = _required_cookie_state()
        storage_file.write_text(json.dumps(original_state), encoding="utf-8")

        async def _enum(*args, **kwargs):
            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session_cmd.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh"])

        assert result.exit_code == 0, result.output
        repaired_state = json.loads(storage_file.read_text())
        assert repaired_state["cookies"] == original_state["cookies"]
        assert repaired_state["origins"] == original_state["origins"]
        assert repaired_state["notebooklm"]["account"] == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    @pytest.mark.parametrize("authuser", ["1", True])
    def test_auth_refresh_repairs_malformed_playwright_account_metadata(
        self, runner, tmp_path, authuser
    ):
        """Non-empty but malformed metadata must not block Playwright repair."""
        from notebooklm.auth import Account

        storage_file = tmp_path / "storage.json"
        original_state = _required_cookie_state()
        original_state["notebooklm"] = {
            "version": 1,
            "account": {"authuser": authuser, "email": "wrong@example.com"},
        }
        storage_file.write_text(json.dumps(original_state), encoding="utf-8")

        async def _enum(*args, **kwargs):
            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch(
                "notebooklm.cli.session_cmd.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh"])

        assert result.exit_code == 0, result.output
        repaired_state = json.loads(storage_file.read_text())
        assert repaired_state["cookies"] == original_state["cookies"]
        assert repaired_state["origins"] == original_state["origins"]
        assert repaired_state["notebooklm"]["account"] == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    def test_auth_refresh_skips_repair_when_account_metadata_exists(self, runner, tmp_path):
        """Existing account metadata is an explicit binding; keepalive must not replace it."""
        storage_file = tmp_path / "storage.json"
        state = _required_cookie_state()
        state["notebooklm"] = {
            "version": 1,
            "account": {"authuser": 1, "email": "bob@example.com"},
        }
        storage_file.write_text(json.dumps(state), encoding="utf-8")

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch("notebooklm.cli.session_cmd._repair_playwright_account_metadata") as mock_repair,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh"])

        assert result.exit_code == 0, result.output
        mock_repair.assert_not_called()
        assert _storage_account(storage_file) == {
            "authuser": 1,
            "email": "bob@example.com",
        }

    @pytest.mark.requires_playwright
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
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
            patch("builtins.input", return_value=""),
        ):
            mock_context = MagicMock()
            mock_page = MagicMock()
            mock_page.url = "https://notebooklm.google.com/"
            mock_context.pages = [mock_page]
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}
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
            patch_session_login_dual("_login_with_browser_cookies"),
            patch_session_login_dual("get_storage_path", return_value=tmp_path / "s.json"),
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
            patch_session_login_dual("get_storage_path", return_value=tmp_path / "s.json"),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd.shutil.rmtree", side_effect=OSError("locked")),
        ):
            result = runner.invoke(cli, ["login", "--fresh"])

        assert result.exit_code == 1
        assert "Cannot clear browser profile" in result.output

    @pytest.mark.requires_playwright
    def test_login_recovers_from_target_closed_on_initial_navigation(self, runner, tmp_path):
        """Test login retries with fresh page when initial goto gets TargetClosedError (#246)."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
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
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            with patch("notebooklm.cli.session_cmd.time.sleep"):
                result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output
        # Verify new_page was called to recover from the stale page
        mock_context.new_page.assert_called()

    @pytest.mark.requires_playwright
    def test_login_recovers_from_target_closed_in_cookie_forcing(self, runner, tmp_path):
        """Test login recovers when cookie-forcing goto hits TargetClosedError (#246).

        This is the PRIMARY crash site: after user switches accounts in the browser,
        the old page reference is dead. The cookie-forcing section must get a fresh
        page and continue.
        """
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
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
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output
        # Verify new_page was called to get a fresh page after the stale one died
        mock_context.new_page.assert_called()

    @pytest.mark.requires_playwright
    def test_login_ignores_navigation_interrupted_after_recovering_page(self, runner, tmp_path):
        """Test recovered pages can also hit the Playwright navigation race (#317)."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
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
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 0
        assert "Authentication saved" in result.output
        mock_context.new_page.assert_called()

    @pytest.mark.requires_playwright
    def test_login_shows_browser_closed_message_after_exhausting_retries(self, runner, tmp_path):
        """Test login shows browser-specific error (not network error) when TargetClosedError exhausts retries."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
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
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            with patch("notebooklm.cli.session_cmd.time.sleep"):
                result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        # Should show browser-closed message, NOT network error message
        assert "browser" in result.output.lower() and "closed" in result.output.lower()
        assert "Network connectivity" not in result.output

    @pytest.mark.requires_playwright
    def test_login_cookie_forcing_double_failure_shows_browser_closed(self, runner, tmp_path):
        """Test cookie-forcing shows BROWSER_CLOSED_HELP when recovered page also raises TargetClosedError (#246).

        This is the final safety net: if the recovered page is also dead during
        cookie-forcing, the user should see BROWSER_CLOSED_HELP, not a traceback.
        """
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "profile"

        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
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
            mock_context.storage_state.return_value = {"cookies": [], "origins": []}

            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            mock_launch.return_value = mock_context

            result = runner.invoke(cli, ["login"])

        assert result.exit_code == 1
        assert "browser" in result.output.lower() and "closed" in result.output.lower()


class TestLoginNoTraceback:
    """Regression: ``login`` must wrap unexpected failures in handle_errors so
    users see a friendly one-liner instead of a Python traceback.

    Without the wrap, the bare ``raise`` at the end of the Playwright
    ``except Exception`` block re-raises out of the command body, escapes
    Click's ``standalone_mode``, and the interpreter prints
    ``Traceback (most recent call last):`` to stderr at process exit. The
    CliRunner shim surfaces that as ``result.exception`` being the raw
    exception instead of a ``SystemExit``.
    """

    @pytest.fixture
    def mock_login_crash(self, tmp_path, monkeypatch):
        """Set up a Playwright environment where ``launch_persistent_context``
        raises an arbitrary exception, exercising the catch-all path at the
        end of login's ``except Exception`` block. Yields the
        ``launch_persistent_context`` mock so each test can install its own
        ``side_effect``.

        Hermetic: ``NOTEBOOKLM_HOME=tmp_path`` so the test never touches the
        real ``~/.notebooklm/`` (would fail with PermissionError in sandboxes).
        """
        # See ``mock_login_browser`` above — same playwright-missing skip path.
        pytest.importorskip(
            "playwright.sync_api",
            reason="playwright not installed; install with: uv sync --extra browser",
        )
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        with (
            patch("notebooklm.cli.session_cmd._ensure_chromium_installed"),
            patch("playwright.sync_api.sync_playwright") as mock_pw,
            patch_session_login_dual("get_storage_path", return_value=tmp_path / "storage.json"),
            patch(
                "notebooklm.cli.session_cmd.get_browser_profile_dir",
                return_value=tmp_path / "profile",
            ),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
        ):
            mock_launch = (
                mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
            )
            yield mock_launch

    def test_login_unexpected_exception_no_traceback(self, runner, mock_login_crash):
        """An unexpected error inside the Playwright block exits cleanly with
        a SystemExit (not a raw exception that would print a traceback)."""
        # An arbitrary RuntimeError surfaces from a Playwright internal —
        # this is the catch-all path at the end of the ``except Exception``
        # block (after the channel-browser-not-found short-circuit).
        mock_login_crash.side_effect = RuntimeError("internal playwright crash xyz")

        result = runner.invoke(cli, ["login"])

        # (a) No raw exception should escape — handle_errors converts it to SystemExit.
        # If this fires with ``RuntimeError`` (or any non-SystemExit), it means
        # the unexpected exception escaped the command body, and in production
        # Python would print ``Traceback (most recent call last):`` to stderr.
        assert isinstance(result.exception, SystemExit) or result.exception is None, (
            f"Expected handle_errors to convert RuntimeError to SystemExit, "
            f"got {type(result.exception).__name__}: {result.exception!r}"
        )
        # (b) Exit code per error_handler.py policy: 2 for unexpected errors.
        assert result.exit_code == 2, (
            f"Unexpected exception should exit 2 per error_handler policy, got {result.exit_code}"
        )
        # (c) A friendly error line — not a traceback — should appear.
        assert "Unexpected error" in result.output, (
            f"Expected friendly 'Unexpected error: ...' message, got: {result.output!r}"
        )
        assert "internal playwright crash xyz" in result.output
        # And the literal traceback marker must not appear in output.
        assert "Traceback (most recent call last)" not in result.output

    def test_login_unexpected_exception_includes_bug_report_hint(self, runner, mock_login_crash):
        """handle_errors' UNEXPECTED_ERROR branch should include the bug-report URL."""
        mock_login_crash.side_effect = RuntimeError("xyz")
        result = runner.invoke(cli, ["login"])
        assert "github.com/teng-lin/notebooklm-py/issues" in result.output


# =============================================================================
# USE COMMAND TESTS
# =============================================================================


class TestLoginLanguageSync:
    """Tests for syncing server language setting to local config after login."""

    @pytest.fixture(autouse=True)
    def _language_module(self):
        """Get the actual language module, bypassing Click group shadowing on Python 3.10."""
        import importlib

        self.language_mod = importlib.import_module("notebooklm.cli.language_cmd")

    def test_sync_persists_server_language(self, tmp_path):
        """After login, server language setting is fetched and saved to local config."""
        from notebooklm.cli.session_cmd import _sync_server_language_to_config

        config_path = tmp_path / "config.json"

        with (
            patch_session_login_dual("NotebookLMClient") as mock_client_cls,
            patch.object(self.language_mod, "get_config_path", return_value=config_path),
            patch.object(self.language_mod, "get_home_dir"),
        ):
            mock_client = create_mock_client()
            mock_client.settings = MagicMock()
            mock_client.settings.get_output_language = AsyncMock(return_value="zh_Hans")
            # `from_storage` is now sync and returns a `_FromStorageContext`
            # (an async context manager). We mock it as a sync MagicMock
            # whose return value is an async-context-manager wrapping the
            # mock client.
            mock_client_cls.from_storage = MagicMock(
                return_value=_make_from_storage_cm(mock_client)
            )

            _sync_server_language_to_config()

        # Verify language was persisted to config
        config = json.loads(config_path.read_text())
        assert config["language"] == "zh_Hans"

    def test_sync_skips_when_server_returns_none(self, tmp_path):
        """No config change when server returns no language."""
        from notebooklm.cli.session_cmd import _sync_server_language_to_config

        config_path = tmp_path / "config.json"

        with (
            patch_session_login_dual("NotebookLMClient") as mock_client_cls,
            patch.object(self.language_mod, "get_config_path", return_value=config_path),
        ):
            mock_client = create_mock_client()
            mock_client.settings = MagicMock()
            mock_client.settings.get_output_language = AsyncMock(return_value=None)
            mock_client_cls.from_storage = MagicMock(
                return_value=_make_from_storage_cm(mock_client)
            )

            _sync_server_language_to_config()

        # Config file should not exist
        assert not config_path.exists()

    def test_sync_uses_explicit_storage_and_profile(self, tmp_path):
        """Language sync should use the freshly written login target."""
        from notebooklm.cli.session_cmd import _sync_server_language_to_config

        config_path = tmp_path / "config.json"
        storage_path = tmp_path / "profiles" / "work" / "storage_state.json"

        with (
            patch_session_login_dual("NotebookLMClient") as mock_client_cls,
            patch.object(self.language_mod, "get_config_path", return_value=config_path),
        ):
            mock_client = create_mock_client()
            mock_client.settings = MagicMock()
            mock_client.settings.get_output_language = AsyncMock(return_value="fr")
            mock_client_cls.from_storage = MagicMock(
                return_value=_make_from_storage_cm(mock_client)
            )

            _sync_server_language_to_config(storage_path=storage_path, profile="work")

        # `from_storage` is now sync; assert the call shape directly.
        mock_client_cls.from_storage.assert_called_once_with(
            path=str(storage_path),
            profile="work",
        )
        config = json.loads(config_path.read_text())
        assert config["language"] == "fr"

    def test_sync_does_not_raise_on_error(self):
        """Language sync failure should not raise and should warn the user."""
        from notebooklm.cli.session_cmd import _sync_server_language_to_config

        with (
            patch_session_login_dual("NotebookLMClient") as mock_client_cls,
            patch_session_login_dual("console") as mock_console,
        ):
            # Raise from the sync `from_storage` call itself.
            mock_client_cls.from_storage = MagicMock(side_effect=Exception("Network error"))

            # Should not raise
            _sync_server_language_to_config()

        # Should print a warning so the user knows to sync manually
        mock_console.print.assert_called_once()
        warning_text = mock_console.print.call_args[0][0]
        assert "language" in warning_text.lower()


# =============================================================================
# EDGE CASES
# =============================================================================
