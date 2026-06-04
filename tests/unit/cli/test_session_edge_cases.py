"""Edge-case tests for session CLI (legacy "TestSessionEdgeCases" + Windows-permissions regression).

This file was extracted from the legacy ``test_session.py`` as part of
D1 PR-3 (test-monkeypatch-policy migration). The shared fixtures and
helpers live in ``_session_helpers.py``; the proxy-block-aware
``patch_session_login_dual`` lives in ``tests/_fixtures``.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import click
import pytest

import notebooklm.cli.services.playwright_login as _pl
from notebooklm.notebooklm_cli import cli

from .conftest import create_mock_client, patch_main_cli_client


class TestSessionEdgeCases:
    def test_use_handles_api_error_fails_closed(self, runner, mock_auth, mock_context_file):
        """'use' fails closed when the API errors.

        Previously: an exception during ``client.notebooks.get`` was swallowed
        and the unverified ID was persisted with a "Warning" tag, poisoning
        downstream commands. New contract: exit 1, leave context.json untouched.
        """
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
                    "notebooklm.cli.session_cmd.resolve_notebook_id", new_callable=AsyncMock
                ) as mock_resolve:
                    mock_resolve.return_value = "nb_error"

                    result = runner.invoke(cli, ["use", "nb_error"])

        assert result.exit_code == 1
        assert not mock_context_file.exists()
        assert "API Error" in result.output or "Could not verify" in result.output

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
                    "notebooklm.cli.session_cmd.resolve_notebook_id", new_callable=AsyncMock
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

        # Mock get_current_notebook to return an ID (simulating partial read).
        # ``read_status`` in the P3.T3 service layer imports
        # ``get_current_notebook`` from ``cli.context`` directly, so the
        # patch target follows the new call site.
        with patch("notebooklm.cli.services.session_context.get_current_notebook") as mock_get_nb:
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
    def _patch_login_deps(self, tmp_path):
        """Patch all login dependencies to isolate mkdir/chmod behavior.

        D1 PR-3 migration: previously used the string-target ``setattr`` form
        on a ``"notebooklm....X.Y"`` literal path. ADR-0007 forbids that
        form because it silently no-ops when the target relocates. Now uses
        ``patch(...)`` context managers which raise ``AttributeError`` if
        the target is missing, surfacing relocations immediately.

        #1367: ``get_storage_path`` / ``get_browser_profile_dir`` are the
        service-path (login) bindings, so the patch target is the consumer
        module ``services.playwright_login`` whose ``prepare_login_paths``
        resolves both names directly (``session_cmd.login`` ->
        ``_prepare_login_paths`` -> ``playwright_login.prepare_login_paths``).
        The ``_resolve_paths_helper`` precedence shim was removed in #1367; the
        consumer-module bindings are now the only lookup site.
        """
        storage_path = tmp_path / "home" / "storage_state.json"
        browser_profile = tmp_path / "profile"

        with (
            patch.object(_pl, "get_storage_path", return_value=storage_path),
            patch.object(_pl, "get_browser_profile_dir", return_value=browser_profile),
        ):
            self.storage_parent = storage_path.parent
            self.browser_profile = browser_profile
            yield

    def test_windows_login_skips_mode_and_chmod(self, monkeypatch, _patch_login_deps, runner):
        """On Windows, login mkdir calls omit mode= and chmod is never called."""
        # ``prepare_login_paths`` (in ``services.playwright_login``) reads
        # ``sys.platform`` to pick the mkdir/chmod hardening path; patch the
        # consumer module's ``sys`` binding (#1367 removed the ``session_cmd``
        # stdlib re-export — ``sys`` is the same singleton either way).
        monkeypatch.setattr(_pl.sys, "platform", "win32")

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

        # Guard against the assertion-block running vacuously: if no mkdir
        # fired at all, the "no mode=" / "no chmod" checks below trivially
        # pass even though we never exercised the Windows-skip code.
        assert mkdir_calls, "Expected at least one mkdir call on the login path"

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
        # See the Windows variant above: patch the consumer module's ``sys``.
        monkeypatch.setattr(_pl.sys, "platform", "linux")

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

    def test_windows_storage_chmod_skipped(self, _patch_login_deps):
        """On Windows, storage_state.json chmod(0o600) is also skipped.

        The ``storage_state.json`` save path runs through
        ``services.login.cookie_writes._write_extracted_cookies`` and
        ``services.login.refresh._login_with_browser_cookies`` (D1 PR-3
        cutover moved the body out of session.py; P3.T4 split the single
        ``services/login.py`` module into a package). We verify the
        Windows guard exists by grepping the source of those submodules —
        fragile compared to a behaviour assertion, but the writers are
        wrapped in ``atomic_write_json`` which intentionally hides the
        platform-dependent ``chmod`` from observers.
        """
        import inspect

        from notebooklm.cli.services.login import cookie_writes, refresh

        # The pattern: ``if sys.platform != "win32": storage_path.parent.chmod(0o700)``.
        # Either quote style is acceptable so the assertion survives style changes.
        for module in (cookie_writes, refresh):
            source = inspect.getsource(module)
            assert 'sys.platform != "win32"' in source or "sys.platform != 'win32'" in source, (
                f"Missing Windows guard for storage_state.json chmod in "
                f"{module.__name__} (moved from session.py in D1 PR-3, "
                "split into login/ package in P3.T4)"
            )
