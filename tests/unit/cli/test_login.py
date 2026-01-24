"""Tests for login command and Playwright browser automation."""

import sys
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from notebooklm.cli.session import _ensure_chromium_installed
from notebooklm.notebooklm_cli import cli

SESSION_MODULE = "notebooklm.cli.session"

# Permission tests only work on Unix-like systems
unix_only = pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions")


def make_subprocess_result(stdout: str, returncode: int = 0) -> MagicMock:
    """Create a mock subprocess result."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


def print_output_contains(mock_print: MagicMock, keyword: str) -> bool:
    """Check if any print call contains the keyword (case-insensitive)."""
    keyword_lower = keyword.lower()
    return any(keyword_lower in str(call).lower() for call in mock_print.call_args_list)


class TestEnsureChromiumInstalled:
    """Tests for the _ensure_chromium_installed() pre-flight check."""

    def test_chromium_already_installed(self):
        """When Chromium is already installed, only dry-run is called."""
        mock_result = make_subprocess_result("chromium: installed at /path/to/browser")

        with patch(f"{SESSION_MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            _ensure_chromium_installed()

            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert "playwright" in call_args
            assert "--dry-run" in call_args

    def test_chromium_install_success(self):
        """When Chromium is missing, it is installed successfully."""
        dry_run_result = make_subprocess_result("chromium will download and install")
        install_result = make_subprocess_result("Downloaded chromium")

        with (
            patch(
                f"{SESSION_MODULE}.subprocess.run",
                side_effect=[dry_run_result, install_result],
            ) as mock_run,
            patch(f"{SESSION_MODULE}.console.print") as mock_print,
        ):
            _ensure_chromium_installed()

            assert mock_run.call_count == 2
            dry_run_call = mock_run.call_args_list[0][0][0]
            install_call = mock_run.call_args_list[1][0][0]

            assert "--dry-run" in dry_run_call
            assert "--dry-run" not in install_call
            assert "chromium" in install_call

            # Check for success message (may contain Rich markup like [green])
            assert print_output_contains(mock_print, "success") or print_output_contains(
                mock_print, "installed"
            ), f"Expected success message, got: {mock_print.call_args_list}"

    def test_chromium_install_failure_exits(self):
        """Installation failure raises SystemExit(1)."""
        dry_run_result = make_subprocess_result("chromium will download and install")
        install_result = make_subprocess_result("Installation failed", returncode=1)

        with (
            patch(
                f"{SESSION_MODULE}.subprocess.run",
                side_effect=[dry_run_result, install_result],
            ),
            patch(f"{SESSION_MODULE}.console.print"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _ensure_chromium_installed()

            assert exc_info.value.code == 1

    def test_playwright_cli_not_found_warns(self):
        """FileNotFoundError is handled gracefully with a warning."""
        with (
            patch(
                f"{SESSION_MODULE}.subprocess.run",
                side_effect=FileNotFoundError("playwright not found"),
            ),
            patch(f"{SESSION_MODULE}.console.print") as mock_print,
        ):
            _ensure_chromium_installed()

            # Check for warning/failure message
            assert print_output_contains(mock_print, "warning") or print_output_contains(
                mock_print, "failed"
            ), f"Expected warning message, got: {mock_print.call_args_list}"

    def test_unexpected_subprocess_error_warns(self):
        """Unexpected RuntimeError is handled gracefully with a warning."""
        with (
            patch(
                f"{SESSION_MODULE}.subprocess.run",
                side_effect=RuntimeError("Unexpected error during subprocess"),
            ),
            patch(f"{SESSION_MODULE}.console.print") as mock_print,
        ):
            _ensure_chromium_installed()

            # Check for warning/failure message
            assert print_output_contains(mock_print, "warning") or print_output_contains(
                mock_print, "failed"
            ), f"Expected warning message, got: {mock_print.call_args_list}"


def create_playwright_mocks(page_url: str = "https://notebooklm.google.com/") -> tuple:
    """Create Playwright mock objects for login tests.

    Returns:
        Tuple of (mock_pw, mock_context, mock_page).
    """
    mock_page = MagicMock()
    mock_page.url = page_url

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_context.close = MagicMock()

    def write_storage_file(path):
        with open(path, "w") as f:
            f.write('{"cookies": []}')

    mock_context.storage_state = MagicMock(side_effect=write_storage_file)

    mock_pw = MagicMock()
    mock_pw.chromium.launch_persistent_context.return_value = mock_context

    return mock_pw, mock_context, mock_page


@contextmanager
def patch_playwright_login(mock_pw, profile_dir, confirm_value=None):
    """Context manager that patches all dependencies for login command tests.

    Args:
        mock_pw: The mock Playwright instance to use.
        profile_dir: Path to use as the browser profile directory.
        confirm_value: If set, patches click.confirm to return this value.
    """
    with ExitStack() as stack:
        mock_sync_pw = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
        mock_sync_pw.return_value.__enter__.return_value = mock_pw

        stack.enter_context(patch(f"{SESSION_MODULE}._ensure_chromium_installed"))
        stack.enter_context(
            patch(f"{SESSION_MODULE}.get_browser_profile_dir", return_value=profile_dir)
        )
        stack.enter_context(patch("builtins.input", return_value=""))

        if confirm_value is not None:
            stack.enter_context(patch("click.confirm", return_value=confirm_value))

        yield


class TestLoginCommand:
    """Tests for the login command with Playwright browser automation."""

    def test_login_happy_path(self, runner, tmp_path):
        """Test successful login flow saves storage and prints success."""
        mock_pw, mock_context, _ = create_playwright_mocks()
        storage_file = tmp_path / "storage.json"

        with patch_playwright_login(mock_pw, tmp_path / "profile"):
            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        mock_context.storage_state.assert_called_once_with(path=str(storage_file))
        assert "saved" in result.output.lower() or "Authentication" in result.output

    def test_login_wrong_url_user_confirms(self, runner, tmp_path):
        """Test login at wrong URL but user confirms to save anyway."""
        mock_pw, mock_context, _ = create_playwright_mocks(
            page_url="https://accounts.google.com/signin"
        )
        storage_file = tmp_path / "storage.json"

        with patch_playwright_login(mock_pw, tmp_path / "profile", confirm_value=True):
            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "accounts.google.com" in result.output
        mock_context.storage_state.assert_called_once()

    def test_login_wrong_url_user_cancels(self, runner, tmp_path):
        """Test login at wrong URL and user cancels."""
        mock_pw, mock_context, _ = create_playwright_mocks(
            page_url="https://accounts.google.com/signin"
        )
        storage_file = tmp_path / "storage.json"

        with patch_playwright_login(mock_pw, tmp_path / "profile", confirm_value=False):
            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 1
        mock_context.storage_state.assert_not_called()

    def test_login_custom_storage_path(self, runner, tmp_path):
        """Test login with custom storage path."""
        mock_pw, mock_context, _ = create_playwright_mocks()
        custom_storage = tmp_path / "custom" / "my_storage.json"

        with patch_playwright_login(mock_pw, tmp_path / "profile"):
            result = runner.invoke(cli, ["login", "--storage", str(custom_storage)])

        assert result.exit_code == 0
        mock_context.storage_state.assert_called_once_with(path=str(custom_storage))
        assert "my_storage.json" in result.output

    @unix_only
    def test_login_creates_parent_directories(self, runner, tmp_path):
        """Test login creates parent directories with mode 0o700."""
        mock_pw, _, _ = create_playwright_mocks()
        nested_storage = tmp_path / "deep" / "nested" / "path" / "storage.json"

        with patch_playwright_login(mock_pw, tmp_path / "profile"):
            result = runner.invoke(cli, ["login", "--storage", str(nested_storage)])

        assert result.exit_code == 0
        assert nested_storage.parent.exists()
        parent_mode = nested_storage.parent.stat().st_mode & 0o777
        assert parent_mode == 0o700

    @unix_only
    def test_login_storage_file_permissions(self, runner, tmp_path):
        """Test that storage file is created with mode 0o600."""
        mock_pw, _, _ = create_playwright_mocks()
        storage_file = tmp_path / "storage.json"

        with patch_playwright_login(mock_pw, tmp_path / "profile"):
            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        assert storage_file.exists()
        file_mode = storage_file.stat().st_mode & 0o777
        assert file_mode == 0o600

    def test_login_uses_persistent_browser_profile(self, runner, tmp_path):
        """Test that login uses persistent browser profile directory."""
        mock_pw, _, _ = create_playwright_mocks()
        storage_file = tmp_path / "storage.json"
        profile_dir = tmp_path / "my_browser_profile"

        with patch_playwright_login(mock_pw, profile_dir):
            result = runner.invoke(cli, ["login", "--storage", str(storage_file)])

        assert result.exit_code == 0
        call_kwargs = mock_pw.chromium.launch_persistent_context.call_args[1]
        assert call_kwargs["user_data_dir"] == str(profile_dir)

    def test_login_browser_args(self, runner, tmp_path):
        """Test that login uses correct browser arguments."""
        mock_pw, _, _ = create_playwright_mocks()
        storage_file = tmp_path / "storage.json"

        with patch_playwright_login(mock_pw, tmp_path / "profile"):
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
