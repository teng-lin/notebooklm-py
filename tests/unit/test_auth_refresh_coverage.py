"""Coverage-focused tests for ``notebooklm._auth.refresh`` branches.

Targets the refresh-cmd driver branches that the concern-aligned
``test_auth_refresh.py`` / ``test_refresh_cmd_shlex.py`` /
``test_refresh_cmd_redaction.py`` suites do not exercise: the
``_should_try_refresh`` attempted-flag guard, ``_run_refresh_cmd``
missing-command and subprocess-failure (timeout / OSError) raises, the
shell-mode and empty-target basename extraction in the non-zero-exit raise,
the ``_settle`` future-cancel callback, and the post-refresh retry
``route_kwargs`` (``account_email`` / ``force_authuser_query``) plumbing.

New file per ADR-0007: patches the owning ``_auth.refresh`` module at the
bare-name call site rather than editing the existing concern-aligned files.
"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._auth import refresh as _auth_refresh


@pytest.fixture(autouse=True)
def _clear_refresh_env(monkeypatch):
    """Each test starts with no inherited refresh-cmd env vars / flags."""
    monkeypatch.delenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, raising=False)
    monkeypatch.delenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV, raising=False)
    monkeypatch.delenv(_auth_refresh._REFRESH_ATTEMPTED_ENV, raising=False)


class TestShouldTryRefresh:
    """``_should_try_refresh`` guard branches (line 247)."""

    def test_false_when_context_flag_set(self, monkeypatch):
        # ContextVar attempted flag set → no second refresh attempt (line 247).
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        token = _auth_refresh._REFRESH_ATTEMPTED_CONTEXT.set(True)
        try:
            assert _auth_refresh._should_try_refresh(ValueError("authentication expired")) is False
        finally:
            _auth_refresh._REFRESH_ATTEMPTED_CONTEXT.reset(token)

    def test_false_when_env_attempted_flag_set(self, monkeypatch):
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        monkeypatch.setenv(_auth_refresh._REFRESH_ATTEMPTED_ENV, "1")
        assert _auth_refresh._should_try_refresh(ValueError("authentication expired")) is False

    def test_false_when_cmd_env_unset(self):
        assert _auth_refresh._should_try_refresh(ValueError("authentication expired")) is False

    def test_true_for_auth_signal_with_cmd_set(self, monkeypatch):
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        assert _auth_refresh._should_try_refresh(ValueError("Redirected to login")) is True

    def test_false_for_non_auth_error(self, monkeypatch):
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        assert _auth_refresh._should_try_refresh(ValueError("network down")) is False


class TestRunRefreshCmdMissing:
    """``_run_refresh_cmd`` raises when the env var is unset (line 315)."""

    @pytest.mark.asyncio
    async def test_missing_cmd_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="is not set; cannot refresh cookies"):
            await _auth_refresh._run_refresh_cmd()


class TestRunRefreshCmdSubprocessFailure:
    """``subprocess.run`` raising → RuntimeError (lines 370-371)."""

    def _stub_storage(self, monkeypatch, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(_auth_refresh, "get_storage_path", lambda profile=None: storage)
        return storage

    @pytest.mark.asyncio
    async def test_timeout_expired_becomes_runtime_error(self, monkeypatch, tmp_path):
        self._stub_storage(monkeypatch, tmp_path)
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")

        def _raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="echo", timeout=60)

        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        with pytest.raises(RuntimeError, match="failed to execute"):
            await _auth_refresh._run_refresh_cmd()

    @pytest.mark.asyncio
    async def test_oserror_becomes_runtime_error(self, monkeypatch, tmp_path):
        self._stub_storage(monkeypatch, tmp_path)
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")

        def _raise_oserror(*args, **kwargs):
            raise OSError("exec format error")

        monkeypatch.setattr(subprocess, "run", _raise_oserror)
        with pytest.raises(RuntimeError, match="failed to execute"):
            await _auth_refresh._run_refresh_cmd()


class TestRunRefreshCmdNonZeroExitBasename:
    """Non-zero exit basename extraction for shell / empty targets (390-393)."""

    def _stub_storage(self, monkeypatch, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(_auth_refresh, "get_storage_path", lambda profile=None: storage)

    def _stub_nonzero_run(self, monkeypatch):
        class _Result:
            returncode = 7
            stdout = "secret-out"
            stderr = "secret-err"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())

    @pytest.mark.asyncio
    async def test_shell_mode_string_target_basename(self, monkeypatch, tmp_path):
        # Shell-mode keeps ``run_target`` as a raw string; basename comes from
        # its first whitespace-split token (lines 390-391).
        self._stub_storage(monkeypatch, tmp_path)
        monkeypatch.setenv(
            _auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV,
            "/opt/secrets/do-refresh.sh --token=hunter2 | tee log",
        )
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV, "1")
        self._stub_nonzero_run(monkeypatch)

        with pytest.raises(RuntimeError) as exc_info:
            await _auth_refresh._run_refresh_cmd()

        message = exc_info.value.args[0]
        assert "exited 7" in message
        assert "do-refresh.sh" in message
        # Neither the directory nor the token leak into the user-facing message.
        assert "/opt/secrets" not in message
        assert "hunter2" not in message

    @pytest.mark.asyncio
    async def test_shell_mode_whitespace_only_target_uses_shell_literal(
        self, monkeypatch, tmp_path
    ):
        # Whitespace-only command string in shell-mode → ``run_target.strip()``
        # is empty so the basename falls back to the literal ``"shell"`` (393).
        self._stub_storage(monkeypatch, tmp_path)
        # The env-not-set guard treats "" as missing; spaces bypass it while
        # still stripping to empty in the basename branch.
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "   ")
        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV, "1")
        self._stub_nonzero_run(monkeypatch)

        with pytest.raises(RuntimeError) as exc_info:
            await _auth_refresh._run_refresh_cmd()

        message = exc_info.value.args[0]
        assert "exited 7" in message
        assert "executable: shell" in message


class TestSplitRefreshCmdWindowsBranch:
    """Cover the Windows ``CommandLineToArgvW`` branch on non-Windows hosts.

    Lines 270-295 are gated on ``os.name == "nt"`` and call into
    ``ctypes.windll`` (which does not exist off-Windows). We fake ``os.name``
    and inject a stand-in ``ctypes.windll`` so the parsing logic — including
    the NULL-pointer empty-input guard and the empty-entry filter — is
    exercised on Linux CI.
    """

    @staticmethod
    def _install_fake_windll(monkeypatch, *, parsed: list[str] | None, null: bool = False):
        freed = {"count": 0}

        def _argv_for(_cmd, argc_ref):
            if null:
                argc_ref._obj.value = 0
                return None  # NULL pointer → empty-input guard (282->286).
            argc_ref._obj.value = len(parsed or [])
            # Return a simple index-addressable container; the source reads
            # ``argv_ptr[i]`` for ``i in range(argc.value)``.
            return list(parsed or [])

        class _Shell32:
            CommandLineToArgvW = staticmethod(_argv_for)

        class _Kernel32:
            @staticmethod
            def LocalFree(_ptr):
                freed["count"] += 1
                return None

        class _WinDLL:
            shell32 = _Shell32()
            kernel32 = _Kernel32()

        monkeypatch.setattr(_auth_refresh.os, "name", "nt")
        monkeypatch.setattr(ctypes, "windll", _WinDLL(), raising=False)

        # ``ctypes.byref`` wraps the ``c_int`` so the fake can mutate ``.value``.
        real_byref = ctypes.byref

        class _Ref:
            def __init__(self, obj):
                self._obj = obj

        monkeypatch.setattr(ctypes, "byref", lambda obj: _Ref(obj))
        # ``cast`` is only used to free the buffer; make it a passthrough so it
        # does not choke on our list stand-in.
        monkeypatch.setattr(ctypes, "cast", lambda ptr, typ: ptr)
        return freed, real_byref

    def test_windows_parses_quoted_tokens_and_frees(self, monkeypatch):
        freed, _ = self._install_fake_windll(
            monkeypatch, parsed=[r"C:\Program Files\python.exe", "script.py"]
        )
        argv = _auth_refresh._split_refresh_cmd('"C:\\Program Files\\python.exe" script.py')
        assert argv == [r"C:\Program Files\python.exe", "script.py"]
        # The ``finally`` LocalFree cleanup ran (line 295).
        assert freed["count"] == 1

    def test_windows_filters_empty_entries(self, monkeypatch):
        # Whitespace-only input → CommandLineToArgvW returns a single empty
        # entry; the comprehension filters it so the caller's empty-argv guard
        # trips (line 293 filter).
        self._install_fake_windll(monkeypatch, parsed=[""])
        assert _auth_refresh._split_refresh_cmd("   ") == []

    def test_windows_null_pointer_returns_empty(self, monkeypatch):
        # NULL pointer for some empty-input edge cases → early empty list
        # return (lines 282->286).
        self._install_fake_windll(monkeypatch, parsed=None, null=True)
        assert _auth_refresh._split_refresh_cmd("") == []


class TestSettleCallbackCancel:
    """``_settle`` propagates task cancellation to the future (line 195)."""

    @pytest.mark.asyncio
    async def test_cancelled_task_cancels_future(self, monkeypatch):
        # Drive ``_coalesced_run_refresh_cmd`` where the underlying
        # ``_run_refresh_cmd`` task is cancelled mid-flight. ``_settle`` must
        # call ``future.cancel()`` (line 195), surfacing CancelledError to the
        # awaiter.
        started = asyncio.Event()

        async def _slow_refresh(storage_path=None, profile=None):
            started.set()
            await asyncio.sleep(10)

        monkeypatch.setattr(_auth_refresh, "_run_refresh_cmd", _slow_refresh)

        async def _drive():
            await _auth_refresh._coalesced_run_refresh_cmd(
                "cancel-key", Path("/tmp/storage_state.json"), None
            )

        task = asyncio.create_task(_drive())
        await started.wait()
        # Cancel the leader subprocess task directly so ``_settle`` runs the
        # ``t.cancelled()`` → ``future.cancel()`` branch.
        inflight = list(_auth_refresh._REFRESH_INFLIGHT_TASKS)
        assert inflight, "expected an in-flight refresh task"
        for t in inflight:
            t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestFetchTokensCancelSettleRace:
    """Caller-cancel + already-settled in-flight future race (lines 575, 578, 580, 581).

    When ``_coalesced_run_refresh_cmd`` raises ``CancelledError`` (caller-side
    cancellation) but the underlying subprocess future is already ``done()``
    and left in the registry by ``_settle``, ``_fetch_tokens_with_refresh``
    inspects the future's terminal state: a cancelled future yields a synthetic
    ``CancelledError`` (575->578); a failed future yields its exception
    (580); either way the loop breaks (581) and the caller propagates
    ``CancelledError``.
    """

    def _common_patches(self, monkeypatch, storage):
        async def fake_fetch_tokens_with_jar(cookie_jar, storage_path=None, **route_kwargs):
            raise ValueError("Authentication expired. Redirected to login.")

        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        monkeypatch.setattr(_auth_refresh, "get_storage_path", lambda profile=None: storage)
        monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)

    @pytest.mark.asyncio
    async def test_done_future_with_exception_propagates_cancel(self, monkeypatch, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("{}", encoding="utf-8")
        self._common_patches(monkeypatch, storage)
        refresh_key = str(storage.expanduser().resolve())

        async def fake_coalesced(key, resolved_storage_path, profile):
            # Insert a DONE future carrying an exception into the per-loop
            # registry, then raise CancelledError to simulate caller-side
            # cancellation arriving after the subprocess settled with failure.
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[None] = loop.create_future()
            fut.set_exception(RuntimeError("subprocess boom"))
            registry = _auth_refresh._get_inflight_registry()
            with _auth_refresh._REFRESH_STATE_LOCK:
                registry[key] = fut
            raise asyncio.CancelledError()

        monkeypatch.setattr(_auth_refresh, "_coalesced_run_refresh_cmd", fake_coalesced)

        with pytest.raises(asyncio.CancelledError):
            await _auth_refresh._fetch_tokens_with_refresh(httpx.Cookies(), storage, None)

        # Clean up the leftover registry entry so other tests start clean.
        registry = _auth_refresh._get_inflight_registry()
        with _auth_refresh._REFRESH_STATE_LOCK:
            registry.pop(refresh_key, None)

    @pytest.mark.asyncio
    async def test_done_future_cancelled_propagates_cancel(self, monkeypatch, tmp_path):
        storage = tmp_path / "storage_state2.json"
        storage.write_text("{}", encoding="utf-8")
        self._common_patches(monkeypatch, storage)
        refresh_key = str(storage.expanduser().resolve())

        async def fake_coalesced(key, resolved_storage_path, profile):
            # Insert a DONE *cancelled* future, then raise CancelledError. This
            # drives the ``inflight.cancelled()`` branch (575 -> 578).
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[None] = loop.create_future()
            fut.cancel()
            # Allow the cancellation to settle the future as cancelled.
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            registry = _auth_refresh._get_inflight_registry()
            with _auth_refresh._REFRESH_STATE_LOCK:
                registry[key] = fut
            raise asyncio.CancelledError()

        monkeypatch.setattr(_auth_refresh, "_coalesced_run_refresh_cmd", fake_coalesced)

        with pytest.raises(asyncio.CancelledError):
            await _auth_refresh._fetch_tokens_with_refresh(httpx.Cookies(), storage, None)

        registry = _auth_refresh._get_inflight_registry()
        with _auth_refresh._REFRESH_STATE_LOCK:
            registry.pop(refresh_key, None)


class TestPostRefreshRetryRouteKwargs:
    """Post-refresh retry forwards account_email / force_authuser_query (637, 639)."""

    @pytest.mark.asyncio
    async def test_retry_forwards_account_email_and_force_authuser(self, monkeypatch, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("{}", encoding="utf-8")

        # First fetch raises an auth-expiry ValueError to trigger refresh; the
        # retry fetch records the route kwargs it received.
        calls: list[dict[str, Any]] = []
        state = {"first": True}

        async def fake_fetch_tokens_with_jar(cookie_jar, storage_path=None, **route_kwargs):
            if state["first"]:
                state["first"] = False
                raise ValueError("Authentication expired. Redirected to login.")
            calls.append(route_kwargs)
            return "csrf_after", "sess_after"

        async def fake_coalesced(refresh_key, resolved_storage_path, profile):
            return None

        def fake_build_jar(path):
            return httpx.Cookies()

        def fake_replace(target, source):
            return None

        def fake_snapshot(jar):
            return None

        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        monkeypatch.setattr(_auth_refresh, "get_storage_path", lambda profile=None: storage)
        monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
        monkeypatch.setattr(_auth_refresh, "_coalesced_run_refresh_cmd", fake_coalesced)
        monkeypatch.setattr(_auth_refresh, "build_httpx_cookies_from_storage", fake_build_jar)
        monkeypatch.setattr(_auth_refresh, "_replace_cookie_jar", fake_replace)
        monkeypatch.setattr(_auth_refresh, "snapshot_cookie_jar", fake_snapshot)

        csrf, session_id, refreshed, _snap = await _auth_refresh._fetch_tokens_with_refresh(
            httpx.Cookies(),
            storage,
            None,
            authuser=0,
            account_email="bob@example.com",
            force_authuser_query=True,
        )

        assert refreshed is True
        assert csrf == "csrf_after"
        assert session_id == "sess_after"
        assert calls, "retry fetch was not invoked"
        retry_kwargs = calls[0]
        assert retry_kwargs["account_email"] == "bob@example.com"
        assert retry_kwargs["force_authuser_query"] is True
        assert retry_kwargs["authuser"] == 0
