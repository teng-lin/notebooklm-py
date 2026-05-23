"""Unit tests for :mod:`notebooklm._atomic_io` / :mod:`notebooklm.io`.

Covers the contract required by both write sites (auth.py cookie sync and
cli/session.py initial login save):

- Round-trip: data written can be read back unchanged.
- Permissions: file mode is ``0o600`` on POSIX (sensitive cookies).
- Concurrency: simultaneous writers never produce a partial/corrupt file —
  every observable state is valid JSON matching exactly one writer's payload.
- Crash safety: if the write fails mid-flight, the original file is untouched
  and no temp files leak into the parent dir.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from notebooklm._atomic_io import atomic_write_json as atomic_write_json_private
from notebooklm.io import atomic_write_json


def test_public_shim_is_same_callable() -> None:
    """`notebooklm.io.atomic_write_json` must re-export the private symbol."""
    assert atomic_write_json is atomic_write_json_private


def test_roundtrip_dict(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    payload = {"cookies": [{"name": "SID", "value": "abc"}], "origins": []}
    atomic_write_json(target, payload)
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_roundtrip_list(tmp_path: Path) -> None:
    target = tmp_path / "list.json"
    payload = [1, 2, {"x": "y"}]
    atomic_write_json(target, payload)
    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_roundtrip_unicode_preserved(tmp_path: Path) -> None:
    target = tmp_path / "u.json"
    payload = {"city": "Zürich", "emoji": "naïve"}
    atomic_write_json(target, payload)
    # ensure_ascii=False is part of the contract (matches auth.py legacy)
    assert json.loads(target.read_text(encoding="utf-8")) == payload


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_chmod_0o600(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    atomic_write_json(target, {"k": "v"})
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_chmod_override(tmp_path: Path) -> None:
    target = tmp_path / "rw.json"
    atomic_write_json(target, {"k": "v"}, mode=0o644)
    assert target.stat().st_mode & 0o777 == 0o644


def test_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"old": True}), encoding="utf-8")
    atomic_write_json(target, {"new": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


@pytest.mark.parametrize(
    "winerror",
    [
        pytest.param(5, id="ERROR_ACCESS_DENIED"),
        pytest.param(32, id="ERROR_SHARING_VIOLATION"),
    ],
)
def test_windows_replace_transient_error_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    """Windows can transiently deny a replace racing on the same target."""
    target = tmp_path / "state.json"

    import notebooklm._atomic_io as mod

    real_replace = mod.os.replace
    calls = 0

    def flaky_replace(src: str | Path, dst: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            err = PermissionError(13, "Access is denied", str(src), str(dst))
            err.winerror = winerror
            raise err
        real_replace(src, dst)

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod.os, "replace", flaky_replace)

    atomic_write_json(target, {"retried": True})

    assert calls == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"retried": True}


def test_windows_replace_retries_exhausted_raises_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"

    import notebooklm._atomic_io as mod

    calls = 0
    sleeps: list[float] = []

    def blocked_replace(src: str | Path, dst: str | Path) -> None:
        nonlocal calls
        calls += 1
        err = PermissionError(13, "Access is denied", str(src), str(dst))
        err.winerror = 5
        raise err

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod.os, "replace", blocked_replace)
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError):
        atomic_write_json(target, {"never": "committed"})

    assert calls == mod._WINDOWS_REPLACE_MAX_ATTEMPTS
    assert len(sleeps) == mod._WINDOWS_REPLACE_MAX_ATTEMPTS - 1
    leaked = list(tmp_path.glob(f".{target.name}.*.tmp"))
    assert not leaked, f"leaked temp files: {leaked}"


def test_concurrent_writers_never_corrupt(tmp_path: Path) -> None:
    """Two threads racing on the same path must always leave a valid JSON
    file matching one of the writers' payloads (no partial / interleaved bytes).
    """
    target = tmp_path / "race.json"
    payload_a = {"who": "A", "filler": "a" * 256}
    payload_b = {"who": "B", "filler": "b" * 256}

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write(payload: dict) -> None:
        try:
            barrier.wait()
            for _ in range(50):
                atomic_write_json(target, payload)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(payload_a,)),
        threading.Thread(target=write, args=(payload_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"writer thread errors: {errors!r}"

    # Final state: file exists, parses as JSON, matches one of the payloads.
    raw = target.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed in (payload_a, payload_b)

    # No leftover temp files in the parent dir (NamedTemporaryFile uses
    # ".<name>.*.tmp" prefix). os.replace is atomic, so a successful run
    # cleans up after itself; a leak here would mean a temp file survived.
    leaked = list(tmp_path.glob(f".{target.name}.*.tmp"))
    assert not leaked, f"leaked temp files: {leaked}"


def test_crash_midwrite_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If json.dump raises mid-write, the pre-existing file is untouched
    and no temp file is left behind in the parent dir.
    """
    target = tmp_path / "state.json"
    original = {"original": True, "value": 42}
    target.write_text(json.dumps(original), encoding="utf-8")
    original_bytes = target.read_bytes()

    class Boom(RuntimeError):
        pass

    import notebooklm._atomic_io as mod

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise Boom("disk full")

    monkeypatch.setattr(mod.json, "dump", explode)

    with pytest.raises(Boom):
        atomic_write_json(target, {"new": "value"})

    # Original file untouched
    assert target.read_bytes() == original_bytes
    # No temp file leaked
    leaked = list(tmp_path.glob(f".{target.name}.*.tmp"))
    assert not leaked, f"leaked temp files: {leaked}"


def test_crash_during_replace_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace itself fails, the original file stays intact and the
    temp file is cleaned up (otherwise repeated failures leak files).
    """
    target = tmp_path / "state.json"
    original = {"original": True}
    target.write_text(json.dumps(original), encoding="utf-8")
    original_bytes = target.read_bytes()

    import notebooklm._atomic_io as mod

    def boom(src, dst):  # noqa: ANN001
        raise OSError("EXDEV-like cross-fs replace failure")

    monkeypatch.setattr(mod.os, "replace", boom)

    with pytest.raises(OSError, match="EXDEV-like"):
        atomic_write_json(target, {"new": "value"})

    assert target.read_bytes() == original_bytes
    leaked = list(tmp_path.glob(f".{target.name}.*.tmp"))
    assert not leaked, f"leaked temp files: {leaked}"


def test_temp_file_uses_target_directory(tmp_path: Path) -> None:
    """Temp file must live next to target so os.replace is same-filesystem
    (atomic). Verified indirectly: a write into a sub-dir succeeds.
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "state.json"
    atomic_write_json(target, {"k": "v"})
    assert target.exists()
    # Parent dir is the one we asked for
    assert target.parent == sub
