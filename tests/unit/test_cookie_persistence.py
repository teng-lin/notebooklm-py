"""Unit tests for the core cookie persistence collaborator."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

import notebooklm._cookie_persistence as persistence_module
import notebooklm._runtime.lifecycle as lifecycle_module
from notebooklm._cookie_persistence import CookiePersistence
from notebooklm.auth import (
    AuthTokens,
    CookieSaveResult,
    CookieSnapshot,
    CookieSnapshotKey,
    snapshot_cookie_jar,
)
from tests._helpers.client_factory import build_client_shell_for_tests


def _auth_tokens(storage_path: Path | None = None) -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "sid", "__Secure-1PSIDTS": "psidts"},
        csrf_token="csrf",
        session_id="session",
        storage_path=storage_path,
    )


def _jar(sid: str = "sid", psidts: str = "psidts") -> httpx.Cookies:
    jar = httpx.Cookies()
    jar.set("SID", sid, domain=".google.com", path="/")
    jar.set("__Secure-1PSIDTS", psidts, domain=".google.com", path="/")
    return jar


def test_client_core_exposes_cookie_persistence(tmp_path: Path) -> None:
    core = build_client_shell_for_tests(_auth_tokens(tmp_path / "storage_state.json"))
    baseline = snapshot_cookie_jar(_jar())

    # The ``Session._save_lock`` + ``Session._loaded_cookie_snapshot`` compat
    # bridges were retired in the session-shrink arc; tests read on the
    # collaborator directly.
    core._collaborators.cookie_persistence.loaded_cookie_snapshot = baseline

    assert isinstance(core._collaborators.cookie_persistence, CookiePersistence)
    assert core._collaborators.cookie_persistence.loaded_cookie_snapshot is baseline
    # The save lock is a stock ``threading.Lock`` owned by the collaborator.
    assert isinstance(core._collaborators.cookie_persistence.save_lock, type(threading.Lock()))


@pytest.mark.asyncio
async def test_client_core_save_cookies_routes_through_injected_seam_and_to_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Session.save_cookies`` routes the write through
    ``asyncio.to_thread`` (off the loop) and then invokes the
    constructor-injected ``cookie_saver``.

    Both halves of this test avoid the legacy ``_core`` indirection:

    - ``save_cookies_to_storage`` is injected at construction via the
      ``cookie_saver`` seam (Wave 1's :class:`ClientLifecycle` change).
    - ``asyncio.to_thread`` is patched on its canonical importing
      module :mod:`notebooklm._runtime.lifecycle` (where
      ``ClientLifecycle.save_cookies`` sources it via
      ``cookie_persistence.save(to_thread=asyncio.to_thread)``).
    """
    calls: list[str] = []

    def fake_save(cookie_jar: httpx.Cookies, path: Path | None = None, **kwargs: Any) -> bool:
        calls.append("save")
        assert path == tmp_path / "storage_state.json"
        assert "original_snapshot" in kwargs
        return True

    async def fake_to_thread(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append("to_thread")
        return func(*args, **kwargs)

    monkeypatch.setattr(lifecycle_module.asyncio, "to_thread", fake_to_thread)
    core = build_client_shell_for_tests(
        _auth_tokens(tmp_path / "storage_state.json"), cookie_saver=fake_save
    )

    await core._collaborators.lifecycle.save_cookies(core._collaborators.cookie_persistence, _jar())

    assert calls == ["to_thread", "save"]


@pytest.mark.asyncio
async def test_cookie_persistence_snapshots_on_loop_and_writes_off_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = _auth_tokens(tmp_path / "storage_state.json")
    persistence = CookiePersistence(auth, tmp_path / "storage_state.json")
    baseline = snapshot_cookie_jar(_jar())
    persistence.loaded_cookie_snapshot = baseline
    auth.cookie_snapshot = baseline

    loop_thread = threading.current_thread()
    snapshot_threads: list[threading.Thread] = []
    writer_thread: threading.Thread | None = None
    real_snapshot = persistence_module.snapshot_cookie_jar

    def snapshot_spy(cookie_jar: httpx.Cookies) -> CookieSnapshot:
        snapshot_threads.append(threading.current_thread())
        return real_snapshot(cookie_jar)

    async def worker_to_thread(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        assert snapshot_threads == [loop_thread]
        raised: list[BaseException] = []

        def run() -> None:
            try:
                func(*args, **kwargs)
            except BaseException as exc:  # pragma: no cover - re-raised on loop thread
                raised.append(exc)

        worker = threading.Thread(target=run, name="cookie-save-test-worker")
        worker.start()
        worker.join(timeout=5.0)
        assert not worker.is_alive()
        if raised:
            raise raised[0]

    def fake_save(
        cookie_jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: CookieSnapshot | None = None,
        return_result: bool = False,
    ) -> bool | CookieSaveResult:
        nonlocal writer_thread
        writer_thread = threading.current_thread()
        assert path == tmp_path / "storage_state.json"
        assert original_snapshot is baseline
        assert return_result is True
        assert persistence.save_lock.locked()
        return CookieSaveResult(True)

    monkeypatch.setattr(persistence_module, "snapshot_cookie_jar", snapshot_spy)

    await persistence.save(
        _jar(psidts="rotated"),
        save_cookies_to_storage=fake_save,
        to_thread=worker_to_thread,
    )

    psidts_key = CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
    assert snapshot_threads == [loop_thread]
    assert writer_thread is not None and writer_thread is not loop_thread
    assert persistence.loaded_cookie_snapshot is not baseline
    assert persistence.loaded_cookie_snapshot is auth.cookie_snapshot
    assert persistence.loaded_cookie_snapshot[psidts_key].value == "rotated"


@pytest.mark.asyncio
async def test_cookie_persistence_advances_baseline_only_on_accepted_saves(
    tmp_path: Path,
) -> None:
    auth = _auth_tokens(tmp_path / "storage_state.json")
    persistence = CookiePersistence(auth, tmp_path / "storage_state.json")
    baseline = snapshot_cookie_jar(_jar(sid="sid-old", psidts="psidts-old"))
    persistence.loaded_cookie_snapshot = baseline
    auth.cookie_snapshot = baseline

    sid_key = CookieSnapshotKey("SID", ".google.com", "/")
    psidts_key = CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
    results: list[CookieSaveResult] = [
        CookieSaveResult(False),
        CookieSaveResult(False, frozenset({psidts_key})),
        CookieSaveResult(True),
    ]

    async def inline_to_thread(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    def fake_save(
        cookie_jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: CookieSnapshot | None = None,
        return_result: bool = False,
    ) -> bool | CookieSaveResult:
        assert original_snapshot is persistence.loaded_cookie_snapshot
        assert return_result is True
        return results.pop(0)

    await persistence.save(
        _jar(sid="sid-silent", psidts="psidts-silent"),
        save_cookies_to_storage=fake_save,
        to_thread=inline_to_thread,
    )

    assert persistence.loaded_cookie_snapshot is baseline
    assert auth.cookie_snapshot is baseline

    await persistence.save(
        _jar(sid="sid-accepted", psidts="psidts-rejected"),
        save_cookies_to_storage=fake_save,
        to_thread=inline_to_thread,
    )

    assert persistence.loaded_cookie_snapshot is not None
    assert persistence.loaded_cookie_snapshot is auth.cookie_snapshot
    assert persistence.loaded_cookie_snapshot[sid_key].value == "sid-accepted"
    assert persistence.loaded_cookie_snapshot[psidts_key].value == "psidts-old"

    await persistence.save(
        _jar(sid="sid-final", psidts="psidts-final"),
        save_cookies_to_storage=fake_save,
        to_thread=inline_to_thread,
    )

    assert persistence.loaded_cookie_snapshot is not None
    assert persistence.loaded_cookie_snapshot is auth.cookie_snapshot
    assert persistence.loaded_cookie_snapshot[sid_key].value == "sid-final"
    assert persistence.loaded_cookie_snapshot[psidts_key].value == "psidts-final"
