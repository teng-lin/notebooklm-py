"""Platform-simulated tests for the Windows bounded storage-lock path."""

from __future__ import annotations

import errno
import sys
import types
from pathlib import Path

import pytest

from notebooklm._auth import storage_lock as lock_mod
from notebooklm._auth.storage_lock import LockRequest, LockState, StorageLockManager


def _make_fake_msvcrt(side_effect):
    module = types.ModuleType("msvcrt")
    module.LK_LOCK = 1
    module.LK_NBLCK = 2
    module.LK_UNLCK = 0
    calls = {"nblck": 0, "unlck": 0, "lock": 0}

    def locking(fd, mode, nbytes):  # noqa: ARG001
        if mode == module.LK_UNLCK:
            calls["unlck"] += 1
            return None
        if mode == module.LK_LOCK:
            calls["lock"] += 1
            raise AssertionError("Windows storage locks must never use LK_LOCK")
        calls["nblck"] += 1
        return side_effect(calls["nblck"])

    module.locking = locking
    module.calls = calls
    return module


def _simulate_win32(monkeypatch: pytest.MonkeyPatch, fake: types.ModuleType) -> None:
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(sys, "platform", "win32")


def _request(path: Path, *, blocking: bool = True) -> LockRequest:
    return LockRequest(path=path, blocking=blocking, operation="test")


def test_win32_blocking_retries_contention_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def side(call: int) -> None:
        if call <= 4:
            raise OSError(errno.EACCES, "contended")

    fake = _make_fake_msvcrt(side)
    _simulate_win32(monkeypatch, fake)
    manager = StorageLockManager(_monotonic=lambda: 0.0, _sleep=lambda _: None)

    with manager.acquire(_request(tmp_path / ".storage_state.json.lock")) as state:
        assert state is LockState.HELD

    assert fake.calls == {"nblck": 5, "unlck": 1, "lock": 0}


def test_win32_blocking_contention_expires_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def contended(call: int) -> None:  # noqa: ARG001
        raise OSError(errno.EACCES, "contended")

    fake = _make_fake_msvcrt(contended)
    _simulate_win32(monkeypatch, fake)
    monkeypatch.setattr(lock_mod, "_LOCK_ACQUIRE_DEADLINE_SECONDS", 0.02)
    times = iter((0.0, 0.0, 0.02))
    manager = StorageLockManager(
        _monotonic=lambda: next(times),
        _sleep=lambda _: None,
        _jitter=lambda _low, _high: 0.0,
    )

    with manager.acquire(_request(tmp_path / ".storage_state.json.lock")) as state:
        assert state is LockState.UNAVAILABLE

    assert fake.calls["nblck"] == 2
    assert fake.calls["unlck"] == 0
    assert fake.calls["lock"] == 0


def test_win32_blocking_non_contention_is_one_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(call: int) -> None:  # noqa: ARG001
        raise OSError(errno.EBADF, "bad fd")

    fake = _make_fake_msvcrt(broken)
    _simulate_win32(monkeypatch, fake)

    with StorageLockManager().acquire(_request(tmp_path / ".lock")) as state:
        assert state is LockState.UNAVAILABLE

    assert fake.calls == {"nblck": 1, "unlck": 0, "lock": 0}


def test_win32_nonblocking_contention_is_one_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def contended(call: int) -> None:  # noqa: ARG001
        raise OSError(errno.EACCES, "contended")

    fake = _make_fake_msvcrt(contended)
    _simulate_win32(monkeypatch, fake)

    with StorageLockManager().acquire(_request(tmp_path / ".lock", blocking=False)) as state:
        assert state is LockState.CONTENDED

    assert fake.calls == {"nblck": 1, "unlck": 0, "lock": 0}


def test_posix_blocking_path_uses_native_flock(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-specific")

    with StorageLockManager().acquire(_request(tmp_path / ".lock")) as state:
        assert state is LockState.HELD


def test_posix_path_does_not_import_msvcrt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-specific")

    def poison(call: int) -> None:  # noqa: ARG001
        raise AssertionError("POSIX must not call msvcrt")

    monkeypatch.setitem(sys.modules, "msvcrt", _make_fake_msvcrt(poison))
    with StorageLockManager().acquire(_request(tmp_path / ".lock")) as state:
        assert state is LockState.HELD
