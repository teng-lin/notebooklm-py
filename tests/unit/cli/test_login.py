"""Tests for login command and Playwright browser automation.

Tests for:
- _ensure_chromium_installed() pre-flight check
- login command with Playwright browser automation
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.cli.session import _ensure_chromium_installed
from notebooklm.notebooklm_cli import cli


@pytest.fixture
def runner():
    return CliRunner()


# =============================================================================
# _ensure_chromium_installed() TESTS
# =============================================================================


class TestEnsureChromiumInstalled:
    """Tests for the _ensure_chromium_installed() pre-flight check."""

    def test_chromium_already_installed(self):
        """Test that function returns silently when Chromium is already installed."""
        mock_result = MagicMock()
        mock_result.stdout = "chromium: installed at /path/to/browser"
        mock_result.returncode = 0

        with patch("notebooklm.cli.session.subprocess.run", return_value=mock_result) as mock_run:
            # Should return silently without installing
            _ensure_chromium_installed()

            # Should only call dry-run, not actual install
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert "playwright" in call_args
            assert "--dry-run" in call_args

    def test_chromium_install_success(self, capsys):
        """Test successful Chromium installation when not present."""
        # First call (dry-run) indicates download needed
        dry_run_result = MagicMock()
        dry_run_result.stdout = "chromium will download and install"
        dry_run_result.returncode = 0

        # Second call (actual install) succeeds
        install_result = MagicMock()
        install_result.stdout = "Downloaded chromium"
        install_result.returncode = 0

        with (
            patch(
                "notebooklm.cli.session.subprocess.run",
                side_effect=[dry_run_result, install_result],
            ) as mock_run,
            patch("notebooklm.cli.session.console.print") as mock_print,
        ):
            _ensure_chromium_installed()

            # Should call both dry-run and install
            assert mock_run.call_count == 2
            dry_run_call = mock_run.call_args_list[0][0][0]
            install_call = mock_run.call_args_list[1][0][0]

            assert "--dry-run" in dry_run_call
            assert "--dry-run" not in install_call
            assert "chromium" in install_call

            # Should print success message
            calls = [str(c) for c in mock_print.call_args_list]
            success_printed = any("success" in c.lower() or "green" in c.lower() for c in calls)
            assert success_printed

    def test_chromium_install_failure(self):
        """Test that installation failure raises SystemExit(1)."""
        # First call (dry-run) indicates download needed
        dry_run_result = MagicMock()
        dry_run_result.stdout = "chromium will download and install"
        dry_run_result.returncode = 0

        # Second call (actual install) fails
        install_result = MagicMock()
        install_result.stdout = "Installation failed"
        install_result.returncode = 1

        with (
            patch(
                "notebooklm.cli.session.subprocess.run",
                side_effect=[dry_run_result, install_result],
            ),
            patch("notebooklm.cli.session.console.print"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _ensure_chromium_installed()

            assert exc_info.value.code == 1

    def test_playwright_cli_not_found(self):
        """Test that FileNotFoundError is handled gracefully with warning."""
        with (
            patch(
                "notebooklm.cli.session.subprocess.run",
                side_effect=FileNotFoundError("playwright not found"),
            ),
            patch("notebooklm.cli.session.console.print") as mock_print,
        ):
            # Should not raise, just print warning and proceed
            _ensure_chromium_installed()

            # Should print warning about pre-flight check failure
            calls = [str(c) for c in mock_print.call_args_list]
            warning_printed = any("warning" in c.lower() or "failed" in c.lower() for c in calls)
            assert warning_printed

    def test_unexpected_subprocess_error(self):
        """Test that unexpected RuntimeError is handled gracefully with warning."""
        with (
            patch(
                "notebooklm.cli.session.subprocess.run",
                side_effect=RuntimeError("Unexpected error during subprocess"),
            ),
            patch("notebooklm.cli.session.console.print") as mock_print,
        ):
            # Should not raise, just print warning and proceed
            _ensure_chromium_installed()

            # Should print warning about pre-flight check failure
            calls = [str(c) for c in mock_print.call_args_list]
            warning_printed = any("warning" in c.lower() or "failed" in c.lower() for c in calls)
            assert warning_printed


# =============================================================================
# login() COMMAND TESTS
# =============================================================================


class TestLoginCommand:
    """Tests for the login command with Playwright browser automation."""

    @pytest.fixture
    def mock_playwright_setup(self):
        """Common setup for mocking Playwright."""
        mock_context = MagicMock()
        mock_page = MagicMock()
        mock_page.url = "https://notebooklm.google.com/"
        mock_context.pages = [mock_page]

        # storage_state must create the file for chmod to work
        def mock_storage_state(path):
            with open(path, "w") as f:
                f.write('{"cookies": []}')

        mock_context.storage_state = MagicMock(side_effect=mock_storage_state)
        mock_context.close = MagicMock()

        mock_pw = MagicMock()
        mock_pw.chromium.launch_persistent_context.return_value = mock_context

        return mock_pw, mock_context, mock_page

    def test_login_happy_path(self, runner, tmp_path, mock_playwright_setup):
        """Test successful login flow saves storage and prints success."""
        mock_pw, mock_context, mock_page = mock_playwright_setup
        storage_file = tmp_path / "storage.json"

        with (
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir", return_value=tmp_path / "profile"
            ),
            patch("builtins.input", return_value=""),
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw

            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        mock_context.storage_state.assert_called_once_with(path=str(storage_file))
        assert "saved" in result.output.lower() or "Authentication" in result.output

    def test_login_wrong_url_user_confirms(self, runner, tmp_path, mock_playwright_setup):
        """Test login at wrong URL but user confirms to save anyway."""
        mock_pw, mock_context, mock_page = mock_playwright_setup
        mock_page.url = "https://accounts.google.com/signin"  # Wrong URL
        storage_file = tmp_path / "storage.json"

        with (
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir", return_value=tmp_path / "profile"
            ),
            patch("builtins.input", return_value=""),
            patch("click.confirm", return_value=True),  # User confirms
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw

            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        # Should show warning about wrong URL
        assert "Warning" in result.output
        assert "accounts.google.com" in result.output
        # But still save storage
        mock_context.storage_state.assert_called_once()

    def test_login_wrong_url_user_cancels(self, runner, tmp_path, mock_playwright_setup):
        """Test login at wrong URL and user cancels."""
        mock_pw, mock_context, mock_page = mock_playwright_setup
        mock_page.url = "https://accounts.google.com/signin"  # Wrong URL
        storage_file = tmp_path / "storage.json"

        with (
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir", return_value=tmp_path / "profile"
            ),
            patch("builtins.input", return_value=""),
            patch("click.confirm", return_value=False),  # User cancels
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw

            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 1
        # Should NOT save storage
        mock_context.storage_state.assert_not_called()

    def test_login_custom_storage_path(self, runner, tmp_path, mock_playwright_setup):
        """Test login with custom storage path."""
        mock_pw, mock_context, mock_page = mock_playwright_setup
        custom_storage = tmp_path / "custom" / "my_storage.json"

        with (
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir", return_value=tmp_path / "profile"
            ),
            patch("builtins.input", return_value=""),
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw

            result = runner.invoke(cli, ["login", "--storage", str(custom_storage)])

        assert result.exit_code == 0
        mock_context.storage_state.assert_called_once_with(path=str(custom_storage))
        # Check filename is in output (path may be line-wrapped)
        assert "my_storage.json" in result.output

    def test_login_creates_parent_directories(self, runner, tmp_path, mock_playwright_setup):
        """Test login creates parent directories with mode 0o700."""
        mock_pw, mock_context, mock_page = mock_playwright_setup
        nested_storage = tmp_path / "deep" / "nested" / "path" / "storage.json"

        with (
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir", return_value=tmp_path / "profile"
            ),
            patch("builtins.input", return_value=""),
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw

            result = runner.invoke(cli, ["login", "--storage", str(nested_storage)])

        assert result.exit_code == 0
        # Parent directory should exist
        assert nested_storage.parent.exists()
        # Check parent directory permissions (0o700)
        parent_mode = nested_storage.parent.stat().st_mode & 0o777
        assert parent_mode == 0o700

    def test_login_storage_file_permissions(self, runner, tmp_path, mock_playwright_setup):
        """Test that storage file is created with mode 0o600."""
        mock_pw, mock_context, mock_page = mock_playwright_setup
        storage_file = tmp_path / "storage.json"

        with (
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir", return_value=tmp_path / "profile"
            ),
            patch("builtins.input", return_value=""),
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw

            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        assert storage_file.exists()
        # Check file permissions (0o600)
        file_mode = storage_file.stat().st_mode & 0o777
        assert file_mode == 0o600

    def test_login_uses_persistent_browser_profile(self, runner, tmp_path, mock_playwright_setup):
        """Test that login uses persistent browser profile directory."""
        mock_pw, mock_context, mock_page = mock_playwright_setup
        storage_file = tmp_path / "storage.json"
        profile_dir = tmp_path / "my_browser_profile"

        with (
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch("notebooklm.cli.session.get_browser_profile_dir", return_value=profile_dir),
            patch("builtins.input", return_value=""),
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw

            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        # Verify launch_persistent_context was called with correct user_data_dir
        call_kwargs = mock_pw.chromium.launch_persistent_context.call_args[1]
        assert call_kwargs["user_data_dir"] == str(profile_dir)

    def test_login_browser_args(self, runner, tmp_path, mock_playwright_setup):
        """Test that login uses correct browser arguments."""
        mock_pw, mock_context, mock_page = mock_playwright_setup
        storage_file = tmp_path / "storage.json"

        with (
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
            patch("notebooklm.cli.session._ensure_chromium_installed"),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir", return_value=tmp_path / "profile"
            ),
            patch("builtins.input", return_value=""),
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw

            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        call_kwargs = mock_pw.chromium.launch_persistent_context.call_args[1]

        # Verify browser args
        args = call_kwargs.get("args", [])
        assert "--disable-blink-features=AutomationControlled" in args
        assert "--password-store=basic" in args

        # Verify automation is disabled
        ignore_default = call_kwargs.get("ignore_default_args", [])
        assert "--enable-automation" in ignore_default

        # Verify headless is False
        assert call_kwargs.get("headless") is False
