"""Behavioral contract for the dependency-bottom auth storage lock manager."""

from __future__ import annotations

import dataclasses
import inspect
import logging
import os
import threading
from pathlib import Path

import pytest

from notebooklm._auth import keepalive, profile_store, storage
from notebooklm._auth import storage_lock as lock_mod
from notebooklm._auth.storage_lock import LockRequest, LockState, StorageLockManager


class _FakeGateway(lock_mod._PlatformLockGateway):
    def __init__(
        self,
        states: list[LockState] | None = None,
        *,
        blocking_uses_retry: bool = False,
        trace: list[str] | None = None,
        release_error: OSError | None = None,
    ) -> None:
        self.states = list(states or [LockState.HELD])
        self.retry = blocking_uses_retry
        self.trace = trace if trace is not None else []
        self.release_error = release_error
        self.acquire_calls = 0

    @property
    def blocking_uses_retry(self) -> bool:
        return self.retry

    def acquire(self, fd: int, *, blocking: bool, operation: str) -> LockState:
        self.acquire_calls += 1
        self.trace.append(f"os-acquire:{fd}:{blocking}:{operation}")
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def release(self, fd: int) -> None:
        self.trace.append(f"os-release:{fd}")
        if self.release_error is not None:
            raise self.release_error


def _request(
    path: Path,
    *,
    blocking: bool = False,
    deadline_seconds: float | None = None,
    operation: str = "test operation",
) -> LockRequest:
    return LockRequest(path, blocking, operation, deadline_seconds)


def test_value_shapes_signatures_and_string_projections(tmp_path: Path) -> None:
    assert [(state.name, state.value) for state in LockState] == [
        ("HELD", "held"),
        ("CONTENDED", "contended"),
        ("UNAVAILABLE", "unavailable"),
    ]
    assert dataclasses.is_dataclass(LockRequest)
    assert LockRequest.__dataclass_params__.frozen is True
    assert LockRequest.__slots__ == ("path", "blocking", "operation", "deadline_seconds")
    assert str(inspect.signature(StorageLockManager.acquire)) == (
        "(self, request: 'LockRequest') -> 'Iterator[LockState]'"
    )

    manager = StorageLockManager(_gateway=_FakeGateway([LockState.CONTENDED]))
    with manager.acquire(_request(tmp_path / ".lock")) as state:
        assert state.value == "contended"


def test_request_union_rejects_blocking_deadline_without_echoing_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        _request(tmp_path / ".lock", blocking=True, deadline_seconds=123.456)
    assert "123.456" not in str(caught.value)


@pytest.mark.parametrize("deadline", [0.0, -1.0])
def test_nonblocking_nonpositive_deadline_still_probes_once(
    tmp_path: Path, deadline: float
) -> None:
    gateway = _FakeGateway([LockState.CONTENDED])
    manager = StorageLockManager(_gateway=gateway, _monotonic=lambda: 0.0)
    with manager.acquire(_request(tmp_path / ".lock", deadline_seconds=deadline)) as state:
        assert state is LockState.UNAVAILABLE
    assert gateway.acquire_calls == 1


def test_process_default_and_module_ownership_are_identity_stable() -> None:
    default = StorageLockManager.process_default()
    assert StorageLockManager.process_default() is default
    assert profile_store._STORAGE_LOCKS is default
    assert storage._STORAGE_LOCKS is default
    assert keepalive._STORAGE_LOCKS is default
    assert StorageLockManager() is not default
    assert storage._file_lock is not keepalive._file_lock


def test_process_default_is_thread_safe_and_identity_stable() -> None:
    results: list[StorageLockManager] = []
    threads = [
        threading.Thread(target=lambda: results.append(StorageLockManager.process_default()))
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(result) for result in results}) == 1


def test_registry_uses_exact_raw_path_spelling(tmp_path: Path) -> None:
    manager = StorageLockManager()
    relative = Path("profile/.lock")
    absolute = tmp_path / "profile" / ".lock"
    literal_home = Path("~/profile/.lock")
    real = tmp_path / "real" / ".lock"
    symlink = tmp_path / "alias" / ".lock"

    assert manager._inprocess_lock_for(relative) is manager._inprocess_lock_for(relative)
    assert manager._inprocess_lock_for(relative) is not manager._inprocess_lock_for(absolute)
    assert manager._inprocess_lock_for(literal_home) is not manager._inprocess_lock_for(absolute)
    assert manager._inprocess_lock_for(real) is not manager._inprocess_lock_for(symlink)
    assert manager._inprocess_lock_for(tmp_path / ".lock") is not manager._inprocess_lock_for(
        tmp_path / ".rotate.lock"
    )


def test_nested_nonblocking_contention_happens_before_second_os_probe(tmp_path: Path) -> None:
    gateway = _FakeGateway()
    manager = StorageLockManager(_gateway=gateway)
    request = _request(tmp_path / ".lock")

    with manager.acquire(request) as outer:
        assert outer is LockState.HELD
        with manager.acquire(request) as inner:
            assert inner is LockState.CONTENDED
        assert gateway.acquire_calls == 1


def test_open_flags_mode_persistent_empty_sentinel_and_no_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chmod_calls: list[tuple[object, object]] = []
    monkeypatch.setattr(os, "chmod", lambda *args: chmod_calls.append(args))
    path = tmp_path / "new" / ".storage_state.json.lock"

    with StorageLockManager().acquire(_request(path, blocking=True)) as state:
        assert state is LockState.HELD

    assert path.exists()
    assert path.read_bytes() == b""
    assert chmod_calls == []
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_acquire_and_reverse_release_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trace: list[str] = []
    gateway = _FakeGateway(trace=trace)
    real_open = os.open
    real_close = os.close

    def traced_open(path: Path, flags: int, mode: int) -> int:
        trace.append(f"open:{flags}:{mode:o}")
        return real_open(path, flags, mode)

    def traced_close(fd: int) -> None:
        trace.append(f"close:{fd}")
        real_close(fd)

    monkeypatch.setattr(os, "open", traced_open)
    monkeypatch.setattr(os, "close", traced_close)
    manager = StorageLockManager(_gateway=gateway)

    with manager.acquire(_request(tmp_path / ".lock", blocking=True)) as state:
        assert state is LockState.HELD
        trace.append("body")

    assert trace[0].startswith("open:")
    assert trace[1].startswith("os-acquire:")
    assert trace[2] == "body"
    assert trace[3].startswith("os-release:")
    assert trace[4].startswith("close:")


def test_body_exception_releases_for_next_acquire(tmp_path: Path) -> None:
    manager = StorageLockManager()
    request = _request(tmp_path / ".lock", blocking=True)
    with pytest.raises(RuntimeError, match="body failed"), manager.acquire(request) as state:
        assert state is LockState.HELD
        raise RuntimeError("body failed")
    with manager.acquire(_request(tmp_path / ".lock")) as state:
        assert state is LockState.HELD


def test_unlock_error_is_debug_swallowed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    gateway = _FakeGateway(release_error=OSError("unlock failed"))
    manager = StorageLockManager(_gateway=gateway)
    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.auth"),
        manager.acquire(_request(tmp_path / ".lock", blocking=True)) as state,
    ):
        assert state is LockState.HELD
    assert [record.getMessage() for record in caplog.records] == [
        "test operation: failed to release file lock (OSError)"
    ]


def test_close_error_propagates_and_releases_thread_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = StorageLockManager(_gateway=_FakeGateway())
    request = _request(tmp_path / ".lock", blocking=True)
    real_close = os.close
    fail_once = True

    def broken_close(fd: int) -> None:
        nonlocal fail_once
        real_close(fd)
        if fail_once:
            fail_once = False
            raise OSError("close failed")

    monkeypatch.setattr(os, "close", broken_close)
    with pytest.raises(OSError, match="close failed"), manager.acquire(request):
        pass
    with manager.acquire(_request(tmp_path / ".lock")) as state:
        assert state is LockState.HELD


def test_bounded_retry_releases_failed_attempt_and_pins_jitter(tmp_path: Path) -> None:
    gateway = _FakeGateway([LockState.CONTENDED, LockState.HELD])
    times = iter((0.0, 0.0))
    sleeps: list[float] = []
    manager = StorageLockManager(
        _gateway=gateway,
        _monotonic=lambda: next(times),
        _sleep=sleeps.append,
        _jitter=lambda low, high: high,
    )
    request = _request(tmp_path / ".lock", deadline_seconds=1.0)

    with manager.acquire(request) as state:
        assert state is LockState.HELD
        assert manager._inprocess_lock_for(request.path).locked()

    assert gateway.acquire_calls == 2
    assert sleeps == [0.02]
    assert gateway.trace.count("os-release:3") <= 1


def test_infrastructure_failure_does_not_retry(tmp_path: Path) -> None:
    gateway = _FakeGateway([LockState.UNAVAILABLE])
    sleeps: list[float] = []
    manager = StorageLockManager(_gateway=gateway, _sleep=sleeps.append)

    with manager.acquire(_request(tmp_path / ".lock", deadline_seconds=90.0)) as state:
        assert state is LockState.UNAVAILABLE

    assert gateway.acquire_calls == 1
    assert sleeps == []


def test_bounded_expiry_log_is_value_free(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    manager = StorageLockManager(
        _gateway=_FakeGateway([LockState.CONTENDED]),
        _monotonic=lambda: 10.0,
    )
    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.auth"),
        manager.acquire(_request(tmp_path / ".lock", deadline_seconds=0.0)) as state,
    ):
        assert state is LockState.UNAVAILABLE
    assert [record.getMessage() for record in caplog.records] == [
        "test operation: bounded storage-lock acquire exceeded 0s deadline; giving up"
    ]


def test_warning_claim_is_thread_safe_and_isolated_per_manager() -> None:
    manager = StorageLockManager()
    claims: list[bool] = []
    threads = [
        threading.Thread(target=lambda: claims.append(manager._claim_cookie_warning()))
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert claims.count(True) == 1
    assert StorageLockManager()._claim_cookie_warning() is True
    assert not hasattr(manager, "reset_warning")
