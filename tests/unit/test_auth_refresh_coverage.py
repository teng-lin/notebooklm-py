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
from typing import Any

import httpx
import pytest

from notebooklm._auth import refresh as _auth_refresh
from notebooklm._auth import single_flight as _single_flight


@pytest.fixture(autouse=True)
def _reset_single_flight():
    """Keep the process-global single-flight core hermetic per test."""
    _single_flight._reset_for_tests()
    yield
    _single_flight._reset_for_tests()


@pytest.fixture(autouse=True)
def _clear_refresh_env(monkeypatch):
    """Each test starts with no inherited refresh-cmd env vars / flags."""
    monkeypatch.delenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, raising=False)
    monkeypatch.delenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV, raising=False)
    monkeypatch.delenv(_auth_refresh._REFRESH_ATTEMPTED_ENV, raising=False)


class TestShouldTryRefresh:
    """``_should_try_refresh`` guard branches ."""

    def test_false_when_context_flag_set(self, monkeypatch):
        # ContextVar attempted flag set → no second refresh attempt .
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
    """``_run_refresh_cmd`` raises when the env var is unset ."""

    @pytest.mark.asyncio
    async def test_missing_cmd_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="is not set; cannot refresh cookies"):
            await _auth_refresh._run_refresh_cmd()


class TestRunRefreshCmdSubprocessFailure:
    """``subprocess.run`` raising → RuntimeError ."""

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
        # its first whitespace-split token .
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

    The branch is gated on ``os.name == "nt"`` and calls into
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
        # The ``finally`` LocalFree cleanup ran .
        assert freed["count"] == 1

    def test_windows_filters_empty_entries(self, monkeypatch):
        # Whitespace-only input → CommandLineToArgvW returns a single empty
        # entry; the comprehension filters it so the caller's empty-argv guard
        # trips.
        self._install_fake_windll(monkeypatch, parsed=[""])
        assert _auth_refresh._split_refresh_cmd("   ") == []

    def test_windows_null_pointer_returns_empty(self, monkeypatch):
        # NULL pointer for some empty-input edge cases -> early empty list return.
        self._install_fake_windll(monkeypatch, parsed=None, null=True)
        assert _auth_refresh._split_refresh_cmd("") == []


class TestSettleCallbackCancel:
    """A cancelled leader task surfaces ``CancelledError`` to the flight awaiter.

    c-PR2: the leader task is driven + strong-ref'd by
    ``single_flight`` (``_LEADER_TASKS``), and its terminal state is mirrored
    into the flight bridge by ``single_flight._mirror``. Cancelling the leader
    task must surface ``CancelledError`` to any ``await_flight`` waiter — here
    the driving ``_coalesced_run_refresh_cmd`` call.
    """

    @pytest.mark.asyncio
    async def test_cancelled_leader_task_surfaces_cancel(self, monkeypatch, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("{}", encoding="utf-8")
        started = asyncio.Event()

        async def _slow_refresh(storage_path=None, profile=None):
            started.set()
            await asyncio.sleep(10)

        # Injected, not monkeypatched (plan §7 deps record).
        deps = _auth_refresh.RefreshCmdDeps(run_refresh_cmd=_slow_refresh)

        async def _drive():
            await _auth_refresh._coalesced_run_refresh_cmd(
                str(storage.expanduser().resolve()),
                storage.expanduser().resolve(),
                None,
                deps=deps,
            )

        task = asyncio.create_task(_drive())
        await started.wait()
        # Cancel the leader task directly so ``_mirror`` runs the
        # ``t.cancelled()`` branch (bridge.set_exception(CancelledError)).
        inflight = list(_single_flight.SingleFlight.process_default()._leader_tasks)
        assert inflight, "expected an in-flight leader task"
        for t in inflight:
            t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestFetchTokensCancelPropagation:
    """``_fetch_tokens_with_refresh`` propagates caller-side cancellation.

    c-PR2 removed the hand-rolled cancel/settle inspection dance from
    ``_fetch_tokens_with_refresh`` (the observed_inflight / prior_inflight
    machinery). Cancellation handling now lives entirely in
    ``single_flight.await_flight`` (settle-before-propagate); the fetch path
    simply lets ``CancelledError`` raised by ``_coalesced_run_refresh_cmd``
    propagate, and no success epoch is bumped for a cancelled attempt.
    """

    def _common_patches(self, monkeypatch, storage):
        async def fake_fetch_tokens_with_jar(cookie_jar, storage_path=None, **route_kwargs):
            raise ValueError("Authentication expired. Redirected to login.")

        monkeypatch.setenv(_auth_refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        monkeypatch.setattr(_auth_refresh, "get_storage_path", lambda profile=None: storage)
        monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)

    @pytest.mark.asyncio
    async def test_cancel_propagates_without_epoch_bump(self, monkeypatch, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("{}", encoding="utf-8")
        self._common_patches(monkeypatch, storage)
        path_key = str(storage.expanduser().resolve())

        async def fake_coalesced(key, resolved_storage_path, profile, *, deps=None):
            raise asyncio.CancelledError()

        monkeypatch.setattr(_auth_refresh, "_coalesced_run_refresh_cmd", fake_coalesced)
        with pytest.raises(asyncio.CancelledError):
            await _auth_refresh._fetch_tokens_with_refresh(httpx.Cookies(), storage, None)
        assert _single_flight.read_success_epoch(path_key) == 0


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

        async def fake_coalesced(refresh_key, resolved_storage_path, profile, *, deps=None):
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
