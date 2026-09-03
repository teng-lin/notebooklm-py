"""Edge-case tests for session CLI (legacy "TestSessionEdgeCases" + Windows-permissions regression).

This file was extracted from the legacy ``test_session.py`` as part of
D1 PR-3 (test-monkeypatch-policy migration). The shared fixtures and
helpers live in ``_session_helpers.py``; the proxy-block-aware
``patch_session_login_dual`` lives in ``tests/_fixtures``.
"""

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import click
import pytest

import notebooklm._app.login_browser as login_browser
import notebooklm.auth as auth_module
import notebooklm.cli.services.session_context as session_context_module
import notebooklm.cli.session_cmd as session_cmd_module
from notebooklm.notebooklm_cli import cli

from .conftest import create_mock_client, inject_client

_LOGIN_PARAMETERS = {
    "ctx",
    "storage",
    "browser",
    "browser_timeout",
    "browser_cookies",
    "account_email",
    "all_accounts",
    "update",
    "profile_name",
    "fresh",
    "include_domains_raw",
    "master_token",
    "master_token_refresh",
    "oauth_token",
    "android_id",
    "cdp_url",
    "force",
}
_LOGIN_DERIVED = {
    "run_master_token_login",
    "include_domains",
    "active_profile",
    "confirm_overwrite",
    "profile",
    "storage_path",
    "browser_profile",
}
_LOGIN_SCRUB = _LOGIN_PARAMETERS | _LOGIN_DERIVED


class _Abort(BaseException):
    pass


def _assert_login_scrub_contract(source: str) -> None:
    tree = ast.parse(source)
    login = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "login"
    )
    outer_tries = [node for node in login.body if isinstance(node, ast.Try) and node.finalbody]
    assert len(outer_tries) == 1
    outer = outer_tries[0]
    assert len(outer.body) == 1 and isinstance(outer.body[0], ast.With)

    preinitialized: set[str] = set()
    for statement in login.body[: login.body.index(outer)]:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is None
        ):
            preinitialized.add(statement.target.id)
            continue
        if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Constant):
            continue
        if statement.value.value is None:
            preinitialized.update(
                target.id for target in statement.targets if isinstance(target, ast.Name)
            )
    assert preinitialized == _LOGIN_DERIVED

    deleted = {
        target.id
        for statement in outer.finalbody
        if isinstance(statement, ast.Delete)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    assert deleted == _LOGIN_SCRUB


def _assert_login_frame_scrubbed(error: BaseException) -> None:
    frames = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if (
            frame.f_code.co_name == "login"
            and frame.f_code.co_filename == session_cmd_module.__file__
        ):
            frames.append(frame)
        traceback = traceback.tb_next
    assert len(frames) == 1
    assert _LOGIN_SCRUB.isdisjoint(frames[0].f_locals)


def test_login_scrub_ast_contract_is_exact_and_fail_closed() -> None:
    source = Path(session_cmd_module.__file__).read_text(encoding="utf-8")
    _assert_login_scrub_contract(source)
    weakened = source.replace(
        "include_domains = active_profile = None",
        "include_domains = None",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_login_scrub_contract(weakened)


def test_login_scrubs_earliest_failure_frame(runner, monkeypatch: pytest.MonkeyPatch) -> None:
    error = _Abort("env lookup failed")

    def fail() -> bool:
        raise error

    monkeypatch.setattr(session_cmd_module, "has_env_auth_json", fail)
    with pytest.raises(_Abort) as excinfo:
        runner.invoke(cli, ["login"])
    assert excinfo.value is error
    _assert_login_frame_scrubbed(error)


def test_login_scrubs_master_token_and_driver_frames(
    runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebooklm.cli import master_token_login as driver

    error = _Abort("master-token adapter failed")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(session_cmd_module, "has_env_auth_json", lambda: False)
    monkeypatch.setattr(driver, "bootstrap_login", fail)
    with pytest.raises(_Abort) as excinfo:
        runner.invoke(
            cli,
            [
                "login",
                "--master-token",
                "--account",
                "owner@example.com",
                "--oauth-token",
                "oauth-secret",
            ],
        )
    assert excinfo.value is error
    _assert_login_frame_scrubbed(error)
    driver_frames = []
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "run_master_token_login":
            driver_frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert len(driver_frames) == 1
    assert {
        "ctx",
        "storage",
        "browser",
        "account_email",
        "oauth_token",
        "android_id",
        "cdp_url",
        "refresh",
        "force",
        "profile",
        "storage_path",
        "plan",
        "capture_oauth_token",
        "run_async",
        "login_result",
        "remint_result",
    }.isdisjoint(driver_frames[0].f_locals)


def test_login_scrubs_browser_cookie_failure_frame(runner, monkeypatch: pytest.MonkeyPatch) -> None:
    error = _Abort("browser cookie adapter failed")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(session_cmd_module, "has_env_auth_json", lambda: False)
    monkeypatch.setattr(session_cmd_module, "_login_browser_cookies_single", fail)
    with pytest.raises(_Abort) as excinfo:
        runner.invoke(cli, ["login", "--browser-cookies", "chrome"])
    assert excinfo.value is error
    _assert_login_frame_scrubbed(error)


def test_login_scrubs_playwright_failure_frame(
    runner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = _Abort("playwright adapter failed")

    def fail(**_kwargs):
        raise error

    monkeypatch.setattr(session_cmd_module, "has_env_auth_json", lambda: False)
    monkeypatch.setattr(
        session_cmd_module,
        "prepare_paths_or_exit",
        lambda *_args: (tmp_path / "storage.json", tmp_path / "browser"),
    )
    monkeypatch.setattr(session_cmd_module, "_run_playwright_login", fail)
    with pytest.raises(_Abort) as excinfo:
        runner.invoke(cli, ["login"])
    assert excinfo.value is error
    _assert_login_frame_scrubbed(error)


class TestSessionEdgeCases:
    def test_use_handles_api_error_fails_closed(self, runner, mock_auth, mock_context_file):
        """'use' fails closed when the API errors.

        Previously: an exception during ``client.notebooks.get`` was swallowed
        and the unverified ID was persisted with a "Warning" tag, poisoning
        downstream commands. New contract: exit 1, leave context.json untouched.
        """
        mock_client = create_mock_client()
        mock_client.notebooks.get = AsyncMock(side_effect=Exception("API Error: Rate limited"))

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")

            # Patch in session module where it's imported
            with patch.object(
                session_cmd_module, "resolve_notebook_id", new_callable=AsyncMock
            ) as mock_resolve:
                mock_resolve.return_value = "nb_error"

                result = runner.invoke(cli, ["use", "nb_error"], obj=inject_client(mock_client))

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
        mock_client = create_mock_client()

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")

            # Patch resolve_notebook_id to raise ClickException (e.g., ambiguous ID)
            with patch.object(
                session_cmd_module, "resolve_notebook_id", new_callable=AsyncMock
            ) as mock_resolve:
                mock_resolve.side_effect = click.ClickException("Multiple notebooks match 'nb'")

                result = runner.invoke(cli, ["use", "nb"], obj=inject_client(mock_client))

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
        with patch.object(session_context_module, "get_current_notebook") as mock_get_nb:
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

        ``get_storage_path`` / ``get_browser_profile_dir`` are app-path
        bindings, so patch the consumer module ``_app.login_browser``, whose
        ``prepare_login_paths`` resolves both names directly
        (``session_cmd.login`` -> ``_prepare_login_paths`` ->
        ``_app.login_browser.prepare_login_paths``).
        The ``_resolve_paths_helper`` precedence shim was removed in #1367; the
        consumer-module bindings are now the only lookup site.
        """
        storage_path = tmp_path / "home" / "storage_state.json"
        browser_profile = tmp_path / "profile"

        with (
            patch.object(login_browser, "get_storage_path", return_value=storage_path),
            patch.object(
                login_browser,
                "get_browser_profile_dir",
                return_value=browser_profile,
            ),
        ):
            self.storage_parent = storage_path.parent
            self.browser_profile = browser_profile
            yield

    def test_windows_login_skips_mode_and_chmod(self, monkeypatch, _patch_login_deps, runner):
        """On Windows, login mkdir calls omit mode= and chmod is never called."""
        # ``prepare_login_paths`` (in ``_app.login_browser``) reads
        # ``sys.platform`` to pick the mkdir/chmod hardening path; patch the
        # consumer module's ``sys`` binding (#1367 removed the ``session_cmd``
        # stdlib re-export — ``sys`` is the same singleton either way).
        monkeypatch.setattr(login_browser.sys, "platform", "win32")

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
        monkeypatch.setattr(login_browser.sys, "platform", "linux")

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

    def test_windows_storage_chmod_skipped(self, tmp_path, monkeypatch):
        """On Windows the profile-store owner skips all POSIX permission mutation.

        Behavior test (not a source grep): since b-PR3 the ``storage_state.json``
        save path funnels through ``ProfileStore``, whose parent-dir
        ``0700`` and backup ``0600`` chmods (and the file-mode ``fchmod``) are
        POSIX-only and guarded on ``sys.platform``. With the platform forced to
        ``win32`` a real ``storage_state.json`` write through the canonical
        ``replace_from_login`` performs NO ``os.chmod``/``os.fchmod`` — and still
        writes the file correctly. The ``backup=True`` ``.bak`` chmod is NOT
        covered here (see the note at the ``replace_from_login`` call below).
        """
        import os

        from notebooklm._atomic_io import _atomic_write_json_unchecked
        from notebooklm._auth import storage as storage_mod
        from notebooklm._auth.profile_account import DomainSelection, KeepAccount
        from notebooklm._auth.profile_document import ProfileDocument
        from notebooklm._auth.profile_store import LoginWriteRequest, ProfileStore, ReplaceStatus
        from notebooklm._auth.storage_lock import LockState, StorageLockManager

        chmod_calls: list[tuple] = []
        real_chmod = os.chmod

        def _spy_chmod(*args, **kwargs):
            chmod_calls.append(args)
            return real_chmod(*args, **kwargs)

        fchmod_calls: list[tuple] = []
        real_fchmod = os.fchmod if hasattr(os, "fchmod") else None

        def _spy_fchmod(*args, **kwargs):
            fchmod_calls.append(args)
            return real_fchmod(*args, **kwargs)

        # ``sys`` is process-global, so use an always-held manager stub while
        # the permission branches are forced through their win32 paths.
        class HeldLocks(StorageLockManager):
            def acquire(self, request):
                import contextlib

                return contextlib.nullcontext(LockState.HELD)

        monkeypatch.setattr(os, "chmod", _spy_chmod)
        if real_fchmod is not None:
            monkeypatch.setattr(os, "fchmod", _spy_fchmod)
        monkeypatch.setattr(storage_mod.sys, "platform", "win32")

        path = tmp_path / "storage_state.json"
        # Pre-existing target so the writer takes its overwrite path (the
        # ``os.replace`` onto an existing inode), not a first-write path.
        _atomic_write_json_unchecked(path, {"cookies": [], "origins": []})
        chmod_calls.clear()
        fchmod_calls.clear()

        state = {
            "cookies": [
                {"name": "SID", "value": "s", "domain": ".google.com", "path": "/"},
                {"name": "__Secure-1PSIDTS", "value": "p", "domain": ".google.com", "path": "/"},
            ],
            "origins": [],
        }
        # ``backup`` deliberately omitted: shutil.copy2 replicates the source mode
        # via its OWN os.chmod (unrelated to the writer's win32-guarded chmod),
        # which would be noise here. The parent-dir 0700 chmod and the file-mode
        # fchmod are the profile-store/_atomic_io permission bits under test.
        outcome = ProfileStore(path, locks=HeldLocks()).replace_from_login(
            LoginWriteRequest(
                source=ProfileDocument.decode(state),
                domain_selection=DomainSelection(),
                account=KeepAccount(),
            )
        )

        assert outcome.status is ReplaceStatus.APPLIED, outcome
        assert path.exists()  # the write still succeeded under the win32 guard
        assert chmod_calls == [], (
            f"os.chmod (parent-dir 0700) must be skipped on win32, got {chmod_calls}"
        )
        assert fchmod_calls == [], (
            f"os.fchmod (file 0600) must be skipped on win32, got {fchmod_calls}"
        )
