"""Unit tests for the core cookie persistence collaborator."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

import notebooklm._runtime.lifecycle as lifecycle_module
import notebooklm._web.transport.cookie_persistence as persistence_module
from notebooklm._auth.paths import canonical_storage_key
from notebooklm._auth.storage import (
    CookieSaveResult,
    CookieSnapshot,
    CookieSnapshotKey,
    save_cookies_to_storage,
    snapshot_cookie_jar,
)
from notebooklm._web.transport.cookie_persistence import CookiePersistence
from notebooklm.auth import (
    AuthTokens,
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


def _stored_cookie(
    name: str,
    value: str,
    *,
    domain: str = ".google.com",
    path: str = "/",
) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": -1,
        "httpOnly": False,
        "secure": False,
        "sameSite": "None",
    }


def _write_storage(path: Path, cookies: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")


async def _inline_to_thread(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
    return func(*args, **kwargs)


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
async def test_direct_client_preserves_auth_baseline_across_pre_open_sibling_write(
    tmp_path: Path,
) -> None:
    """The supported loaded-AuthTokens -> client path keeps load provenance."""
    path = tmp_path / "storage_state.json"
    _write_storage(
        path,
        [_stored_cookie("SID", "old"), _stored_cookie("__Secure-1PSIDTS", "psidts")],
    )
    with pytest.warns(DeprecationWarning):
        auth = _auth_tokens(path)
    auth.cookie_snapshot = snapshot_cookie_jar(_jar(sid="old"))
    core = build_client_shell_for_tests(auth)
    persistence = core._collaborators.cookie_persistence

    # A sibling advances disk after auth loaded but before this client opens.
    _write_storage(
        path,
        [_stored_cookie("SID", "sibling"), _stored_cookie("__Secure-1PSIDTS", "psidts")],
    )
    await persistence._prepare_open_baseline(path, to_thread=_inline_to_thread)
    await persistence._save_canonical(_jar(sid="old"), path, to_thread=_inline_to_thread)

    stored = json.loads(path.read_text(encoding="utf-8"))
    values = {row["name"]: row["value"] for row in stored["cookies"]}
    assert values["SID"] == "sibling"


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
      ``cookie_persistence._save_v0_callback(to_thread=asyncio.to_thread)``).
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

    await core._collaborators.web_transport.save_cookies(_jar())

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

    await persistence._save_v0_callback(
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

    await persistence._save_v0_callback(
        _jar(sid="sid-silent", psidts="psidts-silent"),
        save_cookies_to_storage=fake_save,
        to_thread=inline_to_thread,
    )

    assert persistence.loaded_cookie_snapshot is baseline
    assert auth.cookie_snapshot is baseline

    await persistence._save_v0_callback(
        _jar(sid="sid-accepted", psidts="psidts-rejected"),
        save_cookies_to_storage=fake_save,
        to_thread=inline_to_thread,
    )

    assert persistence.loaded_cookie_snapshot is not None
    assert persistence.loaded_cookie_snapshot is auth.cookie_snapshot
    assert persistence.loaded_cookie_snapshot[sid_key].value == "sid-accepted"
    assert persistence.loaded_cookie_snapshot[psidts_key].value == "psidts-old"

    await persistence._save_v0_callback(
        _jar(sid="sid-final", psidts="psidts-final"),
        save_cookies_to_storage=fake_save,
        to_thread=inline_to_thread,
    )

    assert persistence.loaded_cookie_snapshot is not None
    assert persistence.loaded_cookie_snapshot is auth.cookie_snapshot
    assert persistence.loaded_cookie_snapshot[sid_key].value == "sid-final"
    assert persistence.loaded_cookie_snapshot[psidts_key].value == "psidts-final"


def test_default_path_compatibility_view_getter_setter_and_capture(tmp_path: Path) -> None:
    """An unprepared file baseline never overrides the live compatibility view.

    Phase 10 keeps the legacy AuthTokens mirror, but only a prepared/registered
    typed baseline may seed persistence. Direct capture from Uninitialized uses
    the live jar while leaving typed state uninitialized.
    """
    path = tmp_path / "storage_state.json"
    auth = _auth_tokens(path)
    persistence = CookiePersistence(auth, path)
    default_key = canonical_storage_key(path)
    inherited = snapshot_cookie_jar(_jar(sid="inherited"))
    auth.cookie_snapshot = inherited

    captured = persistence.capture_open_snapshot(_jar(sid="live"))

    sid_key = CookieSnapshotKey("SID", ".google.com", "/")
    assert captured[sid_key].value == "live"
    assert inherited[sid_key].value == "inherited"
    assert captured is not inherited
    assert captured is persistence.loaded_cookie_snapshot
    assert captured is auth.cookie_snapshot
    assert persistence._loaded_cookie_snapshots == {default_key: captured}

    replacement = snapshot_cookie_jar(_jar(sid="replacement"))
    persistence.loaded_cookie_snapshot = replacement
    assert persistence.loaded_cookie_snapshot is replacement
    assert auth.cookie_snapshot is replacement
    assert persistence._loaded_cookie_snapshots == {default_key: replacement}

    persistence.loaded_cookie_snapshot = None
    assert persistence.loaded_cookie_snapshot is None
    assert auth.cookie_snapshot is None
    assert default_key not in persistence._loaded_cookie_snapshots


@pytest.mark.asyncio
async def test_alternating_default_and_override_saves_keep_independent_baselines(
    tmp_path: Path,
) -> None:
    path_a = tmp_path / "a" / "storage_state.json"
    path_b = tmp_path / "b" / "storage_state.json"
    _write_storage(
        path_b,
        [
            _stored_cookie("SID", "b0"),
            _stored_cookie("__Secure-1PSIDTS", "b-psidts"),
        ],
    )
    auth = _auth_tokens(path_a)
    persistence = CookiePersistence(auth, path_a)
    baseline_a0 = persistence.capture_open_snapshot(_jar(sid="a0", psidts="a-psidts"))
    calls: list[tuple[Path, CookieSnapshot | None]] = []

    def fake_save(
        cookie_jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: CookieSnapshot | None = None,
        return_result: bool = False,
    ) -> bool | CookieSaveResult:
        assert path is not None
        assert return_result is True
        calls.append((path, original_snapshot))
        return True

    await persistence._save_v0_callback(
        _jar(sid="a1", psidts="a-psidts"),
        save_cookies_to_storage=fake_save,
        to_thread=_inline_to_thread,
    )
    baseline_a1 = persistence.loaded_cookie_snapshot
    assert baseline_a1 is not None and baseline_a1 is auth.cookie_snapshot

    await persistence._save_v0_callback(
        _jar(sid="b1", psidts="b-psidts"),
        path_b,
        save_cookies_to_storage=fake_save,
        to_thread=_inline_to_thread,
    )
    key_b = canonical_storage_key(path_b)
    baseline_b1 = persistence._loaded_cookie_snapshots[key_b]
    assert persistence.loaded_cookie_snapshot is baseline_a1
    assert auth.cookie_snapshot is baseline_a1

    await persistence._save_v0_callback(
        _jar(sid="a2", psidts="a-psidts"),
        save_cookies_to_storage=fake_save,
        to_thread=_inline_to_thread,
    )

    sid_key = CookieSnapshotKey("SID", ".google.com", "/")
    assert calls[0] == (path_a, baseline_a0)
    assert calls[1][0] == path_b
    assert calls[1][1] is not None
    assert calls[1][1][sid_key].value == "b0"
    assert calls[2] == (path_a, baseline_a1)
    assert persistence.loaded_cookie_snapshot is auth.cookie_snapshot
    assert persistence.loaded_cookie_snapshot is not None
    assert persistence.loaded_cookie_snapshot[sid_key].value == "a2"
    assert persistence._loaded_cookie_snapshots[key_b] is baseline_b1
    assert baseline_b1[sid_key].value == "b1"


@pytest.mark.asyncio
async def test_alias_spellings_share_baseline_but_writer_receives_each_raw_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    default_path = tmp_path / "default" / "storage_state.json"
    resolved_path = tmp_path / "override" / "storage_state.json"
    _write_storage(
        resolved_path,
        [
            _stored_cookie("SID", "disk"),
            _stored_cookie("__Secure-1PSIDTS", "psidts"),
        ],
    )
    relative_path = Path("override") / "storage_state.json"
    symlink_path = tmp_path / "override-alias.json"
    symlink_path.symlink_to(resolved_path)
    persistence = CookiePersistence(_auth_tokens(default_path), default_path)
    calls: list[tuple[Path, CookieSnapshot | None]] = []

    def fake_save(
        cookie_jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: CookieSnapshot | None = None,
        return_result: bool = False,
    ) -> bool:
        assert path is not None
        assert return_result is True
        calls.append((path, original_snapshot))
        return True

    for raw_path, sid in (
        (relative_path, "relative"),
        (resolved_path, "resolved"),
        (symlink_path, "symlink"),
    ):
        await persistence._save_v0_callback(
            _jar(sid=sid),
            raw_path,
            save_cookies_to_storage=fake_save,
            to_thread=_inline_to_thread,
        )

    sid_key = CookieSnapshotKey("SID", ".google.com", "/")
    canonical_key = canonical_storage_key(resolved_path)
    assert [path for path, _ in calls] == [relative_path, resolved_path, symlink_path]
    assert [snapshot[sid_key].value for _, snapshot in calls if snapshot is not None] == [
        "disk",
        "relative",
        "resolved",
    ]
    assert set(persistence._loaded_cookie_snapshots) == {canonical_key}
    assert persistence._loaded_cookie_snapshots[canonical_key][sid_key].value == "symlink"
    assert persistence._last_applied_seq == {canonical_key: 2}
    assert persistence.loaded_cookie_snapshot is None
    assert persistence.auth.cookie_snapshot is None


@pytest.mark.asyncio
async def test_override_initialization_occurs_in_worker_under_save_lock(tmp_path: Path) -> None:
    default_path = tmp_path / "default.json"
    override_path = tmp_path / "override.json"
    persistence = CookiePersistence(_auth_tokens(default_path), default_path)
    workers: list[Any] = []

    async def collect_worker(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        workers.append(lambda: func(*args, **kwargs))

    observed: list[CookieSnapshot] = []

    def fake_save(
        cookie_jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: CookieSnapshot | None = None,
        return_result: bool = False,
    ) -> CookieSaveResult:
        assert path == override_path
        assert persistence.save_lock.locked()
        assert original_snapshot is not None
        observed.append(original_snapshot)
        return CookieSaveResult(True)

    await persistence._save_v0_callback(
        _jar(sid="memory"),
        override_path,
        save_cookies_to_storage=fake_save,
        to_thread=collect_worker,
    )
    assert workers and not override_path.exists(), "dispatch must not initialize on the loop"

    _write_storage(
        override_path,
        [
            _stored_cookie("SID", "worker-disk"),
            _stored_cookie(
                "__Secure-1PSIDTS",
                "app-host-psidts",
                domain=".notebook.google.com",
            ),
        ],
    )
    workers[0]()

    sid_key = CookieSnapshotKey("SID", ".google.com", "/")
    psidts_key = CookieSnapshotKey("__Secure-1PSIDTS", ".notebook.google.com", "/")
    assert observed[0][sid_key].value == "worker-disk"
    assert observed[0][psidts_key].value == "app-host-psidts"


def _write_invalid_override(path: Path, category: str, secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if category == "missing":
        return
    if category == "filesystem":
        path.mkdir()
        return
    if category == "utf8":
        path.write_bytes(b"\xff\xfe")
        return
    if category == "json":
        path.write_text('{"cookies": [', encoding="utf-8")
        return
    if category == "non_object":
        path.write_text("[]", encoding="utf-8")
        return
    if category == "missing_cookies":
        path.write_text(json.dumps({"origins": []}), encoding="utf-8")
        return
    if category == "non_list_cookies":
        path.write_text(json.dumps({"cookies": {}}), encoding="utf-8")
        return
    if category == "invalid_required":
        _write_storage(path, [_stored_cookie("SID", secret)])
        return
    raise AssertionError(f"unknown test category: {category}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    [
        "missing",
        "filesystem",
        "utf8",
        "json",
        "non_object",
        "missing_cookies",
        "non_list_cookies",
        "invalid_required",
    ],
)
async def test_override_initialization_failures_are_non_apply_and_retryable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    category: str,
) -> None:
    secret = "sentinel-cookie-value"
    default_path = tmp_path / "default.json"
    override_path = tmp_path / "override.json"
    _write_invalid_override(override_path, category, secret)
    persistence = CookiePersistence(_auth_tokens(default_path), default_path)
    calls: list[CookieSnapshot | None] = []

    def fake_save(
        cookie_jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: CookieSnapshot | None = None,
        return_result: bool = False,
    ) -> CookieSaveResult:
        calls.append(original_snapshot)
        return CookieSaveResult(True)

    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        await persistence._save_v0_callback(
            _jar(sid="memory"),
            override_path,
            save_cookies_to_storage=fake_save,
            to_thread=_inline_to_thread,
        )

    key = canonical_storage_key(override_path)
    assert calls == []
    assert key not in persistence._loaded_cookie_snapshots
    assert key not in persistence._last_applied_seq
    assert "override baseline initialization failed" in caplog.text
    assert secret not in caplog.text

    if override_path.is_dir():
        override_path.rmdir()
    _write_storage(
        override_path,
        [
            _stored_cookie("SID", "repaired"),
            _stored_cookie("__Secure-1PSIDTS", "psidts"),
        ],
    )
    await persistence._save_v0_callback(
        _jar(sid="after-repair"),
        override_path,
        save_cookies_to_storage=fake_save,
        to_thread=_inline_to_thread,
    )

    sid_key = CookieSnapshotKey("SID", ".google.com", "/")
    assert calls[0] is not None
    assert calls[0][sid_key].value == "repaired"
    assert persistence._loaded_cookie_snapshots[key][sid_key].value == "after-repair"
    assert persistence._last_applied_seq[key] == 1


@pytest.mark.asyncio
async def test_override_loader_skips_opaque_rows_and_real_writer_preserves_them(
    tmp_path: Path,
) -> None:
    default_path = tmp_path / "default.json"
    override_path = tmp_path / "override.json"
    opaque_rows: list[Any] = [
        {"opaque": {"value": "opaque-secret", "future": [1, 2, 3]}},
        ["future-row", {"value": "nested-secret"}],
        {"name": "BROKEN", "value": "malformed-secret", "domain": 17},
    ]
    _write_storage(
        override_path,
        [
            _stored_cookie("SID", "disk"),
            *opaque_rows,
            _stored_cookie(
                "__Secure-1PSIDTS",
                "app-host-psidts",
                domain=".notebook.google.com",
            ),
        ],
    )
    auth = _auth_tokens(default_path)
    persistence = CookiePersistence(auth, default_path)
    observed: list[CookieSnapshot | None] = []

    def recording_real_writer(
        cookie_jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: CookieSnapshot | None = None,
        return_result: bool = False,
    ) -> bool | CookieSaveResult:
        observed.append(original_snapshot)
        return save_cookies_to_storage(
            cookie_jar,
            path,
            original_snapshot=original_snapshot,
            return_result=return_result,
        )

    jar = httpx.Cookies()
    jar.set("SID", "rotated", domain=".google.com", path="/")
    jar.set(
        "__Secure-1PSIDTS",
        "app-host-psidts",
        domain=".notebook.google.com",
        path="/",
    )
    await persistence._save_v0_callback(
        jar,
        override_path,
        save_cookies_to_storage=recording_real_writer,
        to_thread=_inline_to_thread,
    )

    sid_key = CookieSnapshotKey("SID", ".google.com", "/")
    app_psidts_key = CookieSnapshotKey("__Secure-1PSIDTS", ".notebook.google.com", "/")
    assert observed[0] is not None
    assert observed[0][sid_key].value == "disk"
    assert observed[0][app_psidts_key].value == "app-host-psidts"
    stored_rows = json.loads(override_path.read_text(encoding="utf-8"))["cookies"]
    assert stored_rows[1:4] == opaque_rows
    assert stored_rows[0]["value"] == "rotated"
    assert persistence.loaded_cookie_snapshot is None
    assert auth.cookie_snapshot is None
