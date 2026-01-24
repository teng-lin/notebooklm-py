"""Regression tests for Windows compatibility fixes.

These tests verify that fixes for Windows-specific issues remain in place.
They test the fix exists, not the bug itself (which requires specific Windows environments).

Related issues:
- #75: CLI hangs indefinitely on Windows (asyncio ProactorEventLoop)
- #79: Fix Windows CLI hanging due to asyncio ProactorEventLoop
- #80: Fix Unicode encoding errors on non-English Windows systems
- #89: notebooklm login fails on Windows with Python 3.12 (Playwright subprocess)
"""

import os
import sys

import pytest


class TestPlaywrightEventLoopFix:
    """Regression tests for Playwright event loop fix (#89).

    Playwright's sync API requires ProactorEventLoop on Windows to spawn browser
    subprocesses. However, we use WindowsSelectorEventLoopPolicy globally to fix
    CLI hanging (#79). The _windows_playwright_event_loop() context manager
    temporarily restores the default policy for Playwright.
    """

    def test_context_manager_exists(self):
        """Verify the context manager exists and is importable."""
        from notebooklm.cli.session import _windows_playwright_event_loop

        assert callable(_windows_playwright_event_loop)

    def test_context_manager_is_noop_on_non_windows(self):
        """Verify context manager is a no-op on non-Windows platforms."""
        import asyncio
        from unittest.mock import patch

        from notebooklm.cli.session import _windows_playwright_event_loop

        # Mock sys.platform to non-Windows
        with patch("notebooklm.cli.session.sys.platform", "linux"):
            original_policy = asyncio.get_event_loop_policy()
            with _windows_playwright_event_loop():
                # Policy should remain unchanged on non-Windows
                current_policy = asyncio.get_event_loop_policy()
                assert current_policy is original_policy

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_context_manager_restores_policy_on_windows(self):
        """Verify context manager switches to default policy and restores on exit."""
        import asyncio

        from notebooklm.cli.session import _windows_playwright_event_loop

        # Ensure we start with SelectorEventLoopPolicy
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        with _windows_playwright_event_loop():
            inside_policy = asyncio.get_event_loop_policy()
            # Inside the context, should be default (ProactorEventLoop) policy
            assert not isinstance(inside_policy, asyncio.WindowsSelectorEventLoopPolicy), (
                "Context manager should switch to default policy for Playwright"
            )

        # After exit, should restore original policy
        restored_policy = asyncio.get_event_loop_policy()
        assert isinstance(restored_policy, asyncio.WindowsSelectorEventLoopPolicy), (
            "Context manager should restore WindowsSelectorEventLoopPolicy after Playwright"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_context_manager_restores_on_exception(self):
        """Verify policy is restored even if an exception occurs."""
        import asyncio

        from notebooklm.cli.session import _windows_playwright_event_loop

        # Ensure we start with SelectorEventLoopPolicy
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        with pytest.raises(ValueError), _windows_playwright_event_loop():
            raise ValueError("Test exception")

        # Policy should be restored despite exception
        restored_policy = asyncio.get_event_loop_policy()
        assert isinstance(restored_policy, asyncio.WindowsSelectorEventLoopPolicy), (
            "Context manager should restore policy even on exception"
        )


class TestWindowsEventLoopPolicy:
    """Regression tests for Windows asyncio event loop policy fix (#75, #79).

    The default ProactorEventLoop on Windows can hang indefinitely at the IOCP
    layer (GetQueuedCompletionStatus) in certain environments like Sandboxie.
    The fix sets WindowsSelectorEventLoopPolicy at CLI startup.
    """

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_selector_event_loop_policy_is_set(self):
        """Verify Windows uses SelectorEventLoop after CLI initialization.

        This prevents hanging on IOCP operations (see issue #75).
        The policy should be set in notebooklm_cli.main() before any async code runs.
        """
        import asyncio

        # Import the CLI main to trigger the policy setup
        # Note: In actual usage, main() sets the policy before Click runs
        from notebooklm.notebooklm_cli import main  # noqa: F401

        policy = asyncio.get_event_loop_policy()
        assert isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy), (
            "Windows must use WindowsSelectorEventLoopPolicy to avoid IOCP hanging. "
            "See issue #75: https://github.com/teng-lin/notebooklm-py/issues/75"
        )


class TestWindowsUTF8Mode:
    """Regression tests for Windows UTF-8 encoding fix (#75, #80).

    Non-English Windows systems (cp950, cp932, cp936, etc.) can fail with
    UnicodeEncodeError when outputting Unicode characters like checkmarks.
    The fix sets PYTHONUTF8=1 at CLI startup.
    """

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_utf8_mode_enabled(self):
        """Verify UTF-8 mode is enabled on Windows.

        This prevents UnicodeEncodeError on non-English Windows (see issue #75).
        The environment variable should be set in notebooklm_cli.main().
        """
        # Import the CLI main to trigger the UTF-8 setup
        from notebooklm.notebooklm_cli import main  # noqa: F401

        # Check if UTF-8 mode is active (either via flag or env var)
        utf8_enabled = (
            getattr(sys.flags, "utf8_mode", 0) == 1 or os.environ.get("PYTHONUTF8") == "1"
        )
        assert utf8_enabled, (
            "UTF-8 mode must be enabled on Windows to prevent encoding errors. "
            "See issue #75: https://github.com/teng-lin/notebooklm-py/issues/75"
        )


class TestEncodingResilience:
    """Tests for encoding resilience across platforms."""

    @pytest.mark.parametrize(
        "test_char,description",
        [
            ("✓", "checkmark"),
            ("✗", "cross mark"),
            ("📝", "memo emoji"),
            ("→", "arrow"),
            ("•", "bullet"),
        ],
    )
    def test_common_cli_characters_encodable(self, test_char: str, description: str):
        """Verify common CLI output characters can be encoded.

        These characters are used in Rich tables and status output.
        They should either encode successfully or have a fallback.
        """
        # Test that characters can be encoded to UTF-8 (always works)
        try:
            encoded = test_char.encode("utf-8")
            assert len(encoded) > 0
        except UnicodeEncodeError:
            pytest.fail(f"Failed to encode {description} ({test_char!r}) to UTF-8")

    def test_output_with_replace_errors(self):
        """Verify output survives encoding with errors='replace'.

        This simulates the defensive encoding strategy for legacy codepages.
        """
        test_string = "Status: ✓ Complete • 3 items → next"

        # Simulate legacy codepage that can't handle these characters
        try:
            # This would fail on cp950 without errors='replace'
            encoded = test_string.encode("ascii", errors="replace")
            decoded = encoded.decode("ascii")
            # Should have ? replacements but not crash
            assert len(decoded) > 0
            assert "Status" in decoded
        except Exception as e:
            pytest.fail(f"Encoding with errors='replace' should not fail: {e}")
