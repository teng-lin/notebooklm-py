"""Tests for cross-process locking in :func:`migrate_to_profiles`.

The migration moves legacy flat-layout files into ``profiles/default/`` and
must run as a single writer: two ``notebooklm`` CLI invocations starting
under a fresh home (e.g., container start-up race) would otherwise both
copy + delete, leaving partial state visible to either.

Critical invariants exercised here:

* Two concurrent threads → exactly one copy is performed; the second
  observes the marker inside the lock and no-ops.
* Pre-existing partial state (a legacy source dir removed mid-migration)
  does not crash the loser of the race.
* The lock file is intentionally left on disk after migration completes
  (``filelock`` reuses lock files across runs — see ``_atomic_io.py``).
* A held lock surfaces :class:`MigrationLockTimeoutError` rather than
  blocking forever.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from filelock import FileLock

from notebooklm.migration import (
    _MIGRATION_LOCK,
    _MIGRATION_MARKER,
    MigrationLockTimeoutError,
    migrate_to_profiles,
)
from notebooklm.paths import _reset_config_cache, set_active_profile


@pytest.fixture(autouse=True)
def _reset_profile_state():
    """Reset module-level profile state between tests."""
    set_active_profile(None)
    _reset_config_cache()
    yield
    set_active_profile(None)
    _reset_config_cache()


def _seed_legacy_layout(home: Path) -> None:
    """Create a minimal legacy flat layout under ``home``."""
    (home / "storage_state.json").write_text('{"cookies":[]}', encoding="utf-8")
    (home / "context.json").write_text('{"notebook_id":"nb1"}', encoding="utf-8")
    (home / "browser_profile").mkdir()
    (home / "browser_profile" / "data").write_text("chrome data", encoding="utf-8")


def test_concurrent_threads_only_one_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads race on a fresh home — only one performs the copy.

    The loser must acquire the lock AFTER the winner has written the
    marker, re-check the marker, and return ``False`` as a no-op. Without
    the lock, both threads would copy + delete, racing on the same legacy
    files (and the second ``unlink`` would have crashed pre-fix with
    ``FileNotFoundError``).

    Note: thread-based contention exercises ``filelock``'s internal
    ``threading.Lock`` rather than the OS-level ``fcntl``/``LockFileEx``
    layer that production cross-process races would hit. The invariant
    under test — single-writer migration body — is identical in both
    cases; we accept the unit-test tradeoff to avoid subprocess overhead.
    """
    _seed_legacy_layout(tmp_path)
    # Set NOTEBOOKLM_HOME once outside the workers. Using
    # ``patch.dict(os.environ, ..., clear=True)`` inside concurrent
    # threads races: each thread snapshots the *current* (already
    # cleared) env as its "original", and the last thread to exit
    # restores a near-empty env — wiping USERPROFILE on Windows and
    # breaking later tests that call ``Path.home()``.
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))

    results: list[bool] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(migrate_to_profiles())
        except BaseException as exc:  # noqa: BLE001 - capture all failures
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"unexpected exception(s) in worker(s): {errors}"
    assert sorted(results) == [False, True], (
        f"exactly one thread should migrate; got results={results}"
    )

    default_dir = tmp_path / "profiles" / "default"
    assert (default_dir / _MIGRATION_MARKER).exists()
    assert (default_dir / "storage_state.json").exists()
    assert (default_dir / "context.json").exists()
    assert (default_dir / "browser_profile" / "data").exists()
    # Originals removed exactly once — no leftover legacy files.
    assert not (tmp_path / "storage_state.json").exists()
    assert not (tmp_path / "context.json").exists()
    assert not (tmp_path / "browser_profile").exists()


def test_loser_tolerates_partial_state_mid_race(tmp_path: Path) -> None:
    """Pre-existing partial state (legacy dir removed) does not crash.

    Simulates the window where the winner copied + unlinked the legacy
    files but the loser entered before the marker was written. The loser
    must not crash on the missing source paths.
    """
    _seed_legacy_layout(tmp_path)

    # Simulate winner having copied + removed legacy files BUT not yet
    # written the marker (which is normally the last step).
    default_dir = tmp_path / "profiles" / "default"
    default_dir.mkdir(parents=True)
    (default_dir / "storage_state.json").write_text('{"cookies":[]}', encoding="utf-8")
    (default_dir / "context.json").write_text('{"notebook_id":"nb1"}', encoding="utf-8")
    (default_dir / "browser_profile").mkdir()
    (default_dir / "browser_profile" / "data").write_text("chrome data", encoding="utf-8")

    # Remove the legacy SOURCES (winner already unlinked them) but skip
    # writing the marker (winner crashed before marker write).
    (tmp_path / "storage_state.json").unlink()
    (tmp_path / "context.json").unlink()
    import shutil as _shutil

    _shutil.rmtree(tmp_path / "browser_profile")

    # Loser enters with NO legacy files left and NO marker yet — exercises
    # the "already migrated" no-legacy branch.
    with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
        # Must not raise on already-removed sources.
        result = migrate_to_profiles()

    assert result is False
    # Profile copies are intact regardless of which branch the loser took.
    assert (default_dir / "storage_state.json").exists()
    assert (default_dir / "context.json").exists()


def test_lock_does_not_block_subsequent_runs(tmp_path: Path) -> None:
    """The migration lock does not interfere with subsequent invocations.

    ``filelock`` on POSIX unlinks the lock file on release (3.25+ behavior),
    while on Windows the file may persist — either outcome is acceptable
    because the lock is recreated lazily on next acquisition. What matters
    is that subsequent ``migrate_to_profiles()`` calls are clean no-ops
    and do not leave the home dir in a broken state.
    """
    _seed_legacy_layout(tmp_path)

    with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
        assert migrate_to_profiles() is True

    # Subsequent invocations are clean no-ops — proves the lock state from
    # the first run did not poison the second acquisition.
    with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
        assert migrate_to_profiles() is False
        assert migrate_to_profiles() is False

    # Marker still present — no accidental wipe.
    assert (tmp_path / "profiles" / "default" / _MIGRATION_MARKER).exists()


def test_held_lock_surfaces_timeout_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A held lock causes :class:`MigrationLockTimeoutError` to surface.

    A separate thread holds the lock for the duration of the call under
    test; we shrink the timeout to 1s so the test runs quickly. The
    expected outcome is the new wrapper exception, NOT a raw
    :class:`filelock.Timeout` (callers should see a domain-specific
    error).
    """
    # Pre-create the home so the lock file's parent directory exists.
    tmp_path.mkdir(exist_ok=True)
    _seed_legacy_layout(tmp_path)

    lock_path = tmp_path / _MIGRATION_LOCK
    holder = FileLock(str(lock_path))
    holder.acquire()

    # Shrink the migration timeout so the assertion runs in ~1 second.
    monkeypatch.setattr("notebooklm.migration._MIGRATION_LOCK_TIMEOUT", 1.0, raising=True)

    try:
        with (
            patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True),
            pytest.raises(MigrationLockTimeoutError) as exc_info,
        ):
            migrate_to_profiles()
    finally:
        holder.release()

    # Wrapper preserves the underlying filelock Timeout as the cause.
    assert exc_info.value.__cause__ is not None
    assert "migration lock" in str(exc_info.value).lower()

    # Once released, the call succeeds — proves the lock was the cause.
    with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
        assert migrate_to_profiles() is True


def test_migration_lock_timeout_error_is_runtime_error() -> None:
    """The exception class is a ``RuntimeError`` subclass.

    Lets callers catch ``RuntimeError`` and still hit our migration-
    specific failure (matches the existing ``RuntimeError`` pattern in
    ``auth.py``).
    """
    assert issubclass(MigrationLockTimeoutError, RuntimeError)
