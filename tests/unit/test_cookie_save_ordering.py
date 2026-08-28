"""Save-ordering guard for ``CookiePersistence`` [storage-F3 / refresh-F3].

Each ``save()`` dispatch is stamped from a monotonic counter on the loop thread;
the worker, under the save lock, drops itself if a NEWER dispatch has already
applied a merge to the same effective path. This is the "close() must win"
invariant (per client instance) that was previously documented but not enforced
— a queued stale save could run last and overwrite fresh cookies.

These tests are deterministic: the fake ``to_thread`` collects worker closures
instead of running them, so the test drives worker execution order explicitly
(no reliance on real thread-scheduling luck). Three behaviours are pinned:

* newer-first wins on disk (the older worker, running second, is dropped);
* a newer worker that only *hard-failed* does NOT suppress the older worker;
* per-path independence (a newer save to path B never drops an older save to A).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from notebooklm._auth.paths import canonical_storage_key
from notebooklm._auth.storage import (
    CookieSaveResult,
    CookieSnapshotKey,
    snapshot_cookie_jar,
)
from notebooklm._web.transport.cookie_persistence import CookiePersistence
from notebooklm.auth import AuthTokens


def _auth(storage_path: Path) -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "sid"},
        csrf_token="csrf",
        session_id="session",
        storage_path=storage_path,
    )


def _tagged_jar(tag: str) -> httpx.Cookies:
    """A jar carrying a ``TAG`` cookie so the fake writer can identify it."""
    jar = httpx.Cookies()
    jar.set("SID", "sid", domain=".google.com", path="/")
    jar.set("TAG", tag, domain=".google.com", path="/")
    return jar


def _write_storage(path: Path, *, sid: str = "sid", tag: str | None = None) -> None:
    cookies = [
        {
            "name": "SID",
            "value": sid,
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": False,
            "sameSite": "None",
        },
        {
            "name": "__Secure-1PSIDTS",
            "value": "psidts",
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": False,
            "sameSite": "None",
        },
    ]
    if tag is not None:
        cookies.append(
            {
                "name": "TAG",
                "value": tag,
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "None",
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")


class _Recorder:
    """Fake ``save_cookies_to_storage`` recording (tag, path) and returning a
    per-tag result."""

    def __init__(
        self,
        results: dict[str, bool | CookieSaveResult] | None = None,
    ) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.snapshots: list[object] = []
        self._results = results or {}

    def __call__(
        self,
        jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: object = None,
        return_result: bool = False,
    ) -> bool | CookieSaveResult:
        tag = next(c.value for c in jar.jar if c.name == "TAG")
        assert path is not None
        self.calls.append((tag, path))
        self.snapshots.append(original_snapshot)
        return self._results.get(tag, CookieSaveResult(True))

    @property
    def tags(self) -> list[str]:
        return [tag for tag, _ in self.calls]


class _Collector:
    """Fake ``to_thread`` that stores worker closures for explicit execution."""

    def __init__(self) -> None:
        self.workers: list[Callable[[], None]] = []

    async def __call__(self, fn: Callable[[], None]) -> None:
        self.workers.append(fn)


@pytest.mark.asyncio
async def test_newer_first_run_drops_older_worker(tmp_path: Path) -> None:
    """When the newer dispatch's worker runs first, the older worker (running
    second) drops itself — the newer cookies stay on disk."""
    path = tmp_path / "storage_state.json"
    persistence = CookiePersistence(_auth(path), default_path=path)
    recorder = _Recorder()
    collector = _Collector()

    # Dispatch older (seq 0) then newer (seq 1) — dispatch order stamps the seq.
    await persistence._save_v0_callback(
        _tagged_jar("old"), path, save_cookies_to_storage=recorder, to_thread=collector
    )
    await persistence._save_v0_callback(
        _tagged_jar("new"), path, save_cookies_to_storage=recorder, to_thread=collector
    )

    # Run the NEWER worker first, then the OLDER one.
    collector.workers[1]()  # seq 1 applies -> last_applied[path] = 1
    collector.workers[0]()  # seq 0 < 1 -> dropped before any write

    assert recorder.tags == ["new"], "older worker must be dropped once newer applied"
    tag_key = CookieSnapshotKey("TAG", ".google.com", "/")
    assert persistence.loaded_cookie_snapshot is not None
    assert persistence.loaded_cookie_snapshot[tag_key].value == "new"
    assert persistence._last_applied_seq == {canonical_storage_key(path): 1}


@pytest.mark.asyncio
async def test_older_first_then_newer_both_apply(tmp_path: Path) -> None:
    """Baseline: when workers run in dispatch order, both apply (no spurious
    drop)."""
    path = tmp_path / "storage_state.json"
    persistence = CookiePersistence(_auth(path), default_path=path)
    recorder = _Recorder()
    collector = _Collector()

    await persistence._save_v0_callback(
        _tagged_jar("old"), path, save_cookies_to_storage=recorder, to_thread=collector
    )
    await persistence._save_v0_callback(
        _tagged_jar("new"), path, save_cookies_to_storage=recorder, to_thread=collector
    )

    collector.workers[0]()  # seq 0 applies -> last = 0
    collector.workers[1]()  # seq 1 > 0 -> applies -> last = 1

    assert recorder.tags == ["old", "new"]


@pytest.mark.asyncio
async def test_newer_hard_fail_lets_older_proceed(tmp_path: Path) -> None:
    """A newer worker that HARD-fails (ok=False, no CAS-rejected keys) must NOT
    advance the marker, so the older worker still proceeds — its CAS-guarded
    write is strictly newer than disk."""
    path = tmp_path / "storage_state.json"
    persistence = CookiePersistence(_auth(path), default_path=path)
    # Newer save hard-fails (e.g. disk write error): ok=False, no rejected keys.
    recorder = _Recorder(results={"new": CookieSaveResult(False)})
    collector = _Collector()

    await persistence._save_v0_callback(
        _tagged_jar("old"), path, save_cookies_to_storage=recorder, to_thread=collector
    )
    await persistence._save_v0_callback(
        _tagged_jar("new"), path, save_cookies_to_storage=recorder, to_thread=collector
    )

    collector.workers[1]()  # seq 1 hard-fail -> marker NOT advanced
    collector.workers[0]()  # seq 0, marker still -1 -> proceeds

    assert recorder.tags == ["new", "old"], "older worker must proceed after newer hard-fail"


@pytest.mark.asyncio
async def test_newer_cas_partial_drops_older_worker(tmp_path: Path) -> None:
    """A newer worker whose merge was a CAS-partial apply (ok=False WITH rejected
    keys) DID run the merge, so it advances the marker and the older worker is
    dropped (distinguished from a hard-fail)."""
    path = tmp_path / "storage_state.json"
    persistence = CookiePersistence(_auth(path), default_path=path)
    rejected = frozenset({CookieSnapshotKey("SID", ".google.com", "/")})
    recorder = _Recorder(results={"new": CookieSaveResult(False, rejected)})
    collector = _Collector()

    await persistence._save_v0_callback(
        _tagged_jar("old"), path, save_cookies_to_storage=recorder, to_thread=collector
    )
    await persistence._save_v0_callback(
        _tagged_jar("new"), path, save_cookies_to_storage=recorder, to_thread=collector
    )

    collector.workers[1]()  # seq 1 CAS-partial (merge ran) -> marker advanced to 1
    collector.workers[0]()  # seq 0 < 1 -> dropped

    assert recorder.tags == ["new"]


@pytest.mark.asyncio
async def test_ordering_is_keyed_per_effective_path(tmp_path: Path) -> None:
    """A newer save to path B must not drop an older save to path A — the marker
    is keyed per effective path."""
    path_a = tmp_path / "a" / "storage_state.json"
    path_b = tmp_path / "b" / "storage_state.json"
    _write_storage(path_b)
    persistence = CookiePersistence(_auth(path_a), default_path=path_a)
    recorder = _Recorder()
    collector = _Collector()

    # Older dispatch targets path A (seq 0); newer targets path B (seq 1).
    await persistence._save_v0_callback(
        _tagged_jar("A"), path_a, save_cookies_to_storage=recorder, to_thread=collector
    )
    await persistence._save_v0_callback(
        _tagged_jar("B"), path_b, save_cookies_to_storage=recorder, to_thread=collector
    )

    collector.workers[1]()  # seq 1 on path B -> last_applied[B] = 1
    collector.workers[0]()  # seq 0 on path A -> last_applied[A] = -1 -> proceeds

    assert sorted(recorder.tags) == ["A", "B"], "cross-path save must not be dropped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "applied"),
    [
        pytest.param(True, True, id="bool-success"),
        pytest.param(CookieSaveResult(True), True, id="detailed-success"),
        pytest.param(False, False, id="bool-hard-failure"),
        pytest.param(CookieSaveResult(False), False, id="detailed-hard-failure"),
    ],
)
@pytest.mark.parametrize("target", ["default", "override"])
async def test_bool_and_detailed_results_update_only_the_target_path(
    tmp_path: Path,
    result: bool | CookieSaveResult,
    applied: bool,
    target: str,
) -> None:
    path_a = tmp_path / "a" / "storage_state.json"
    path_b = tmp_path / "b" / "storage_state.json"
    _write_storage(path_b, sid="b0", tag="b0")
    auth = _auth(path_a)
    persistence = CookiePersistence(auth, default_path=path_a)
    baseline_a = snapshot_cookie_jar(_tagged_jar("a0"))
    persistence.loaded_cookie_snapshot = baseline_a
    target_path = path_a if target == "default" else path_b
    recorder = _Recorder(results={"next": result})
    collector = _Collector()

    await persistence._save_v0_callback(
        _tagged_jar("next"),
        target_path,
        save_cookies_to_storage=recorder,
        to_thread=collector,
    )
    collector.workers[0]()

    target_key = canonical_storage_key(target_path)
    tag_key = CookieSnapshotKey("TAG", ".google.com", "/")
    original = recorder.snapshots[0]
    assert isinstance(original, dict)
    if applied:
        assert persistence._loaded_cookie_snapshots[target_key][tag_key].value == "next"
        assert persistence._last_applied_seq[target_key] == 0
    else:
        assert persistence._loaded_cookie_snapshots[target_key] is original
        assert target_key not in persistence._last_applied_seq

    if target == "override":
        assert original[tag_key].value == "b0"
        assert persistence.loaded_cookie_snapshot is baseline_a
        assert auth.cookie_snapshot is baseline_a
    elif applied:
        assert persistence.loaded_cookie_snapshot is auth.cookie_snapshot
        assert persistence.loaded_cookie_snapshot[tag_key].value == "next"
    else:
        assert persistence.loaded_cookie_snapshot is baseline_a
        assert auth.cookie_snapshot is baseline_a


@pytest.mark.asyncio
async def test_override_cas_partial_advances_accepted_keys_without_touching_default(
    tmp_path: Path,
) -> None:
    path_a = tmp_path / "a" / "storage_state.json"
    path_b = tmp_path / "b" / "storage_state.json"
    _write_storage(path_b, sid="b0", tag="b0")
    auth = _auth(path_a)
    persistence = CookiePersistence(auth, default_path=path_a)
    baseline_a = snapshot_cookie_jar(_tagged_jar("a0"))
    persistence.loaded_cookie_snapshot = baseline_a
    sid_key = CookieSnapshotKey("SID", ".google.com", "/")
    tag_key = CookieSnapshotKey("TAG", ".google.com", "/")
    result = CookieSaveResult(False, frozenset({sid_key}))
    recorder = _Recorder(results={"b1": result})
    collector = _Collector()
    jar = _tagged_jar("b1")
    for cookie in jar.jar:
        if cookie.name == "SID":
            cookie.value = "b1"

    await persistence._save_v0_callback(
        jar,
        path_b,
        save_cookies_to_storage=recorder,
        to_thread=collector,
    )
    collector.workers[0]()

    key_b = canonical_storage_key(path_b)
    assert persistence._loaded_cookie_snapshots[key_b][sid_key].value == "b0"
    assert persistence._loaded_cookie_snapshots[key_b][tag_key].value == "b1"
    assert persistence._last_applied_seq[key_b] == 0
    assert persistence.loaded_cookie_snapshot is baseline_a
    assert auth.cookie_snapshot is baseline_a


@pytest.mark.asyncio
async def test_alias_order_key_drops_stale_worker_and_preserves_raw_writer_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    default_path = tmp_path / "default.json"
    resolved_path = tmp_path / "override" / "storage_state.json"
    _write_storage(resolved_path, tag="disk")
    relative_path = Path("override") / "storage_state.json"
    symlink_path = tmp_path / "override-alias.json"
    symlink_path.symlink_to(resolved_path)
    persistence = CookiePersistence(_auth(default_path), default_path=default_path)
    recorder = _Recorder()
    collector = _Collector()

    await persistence._save_v0_callback(
        _tagged_jar("old"),
        relative_path,
        save_cookies_to_storage=recorder,
        to_thread=collector,
    )
    await persistence._save_v0_callback(
        _tagged_jar("new"),
        symlink_path,
        save_cookies_to_storage=recorder,
        to_thread=collector,
    )

    collector.workers[1]()
    collector.workers[0]()

    key = canonical_storage_key(resolved_path)
    tag_key = CookieSnapshotKey("TAG", ".google.com", "/")
    assert recorder.calls == [("new", symlink_path)]
    assert persistence._last_applied_seq == {key: 1}
    assert persistence._loaded_cookie_snapshots[key][tag_key].value == "new"
    assert persistence.loaded_cookie_snapshot is None
