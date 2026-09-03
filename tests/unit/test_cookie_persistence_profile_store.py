"""Direct behavior tests for typed ProfileStore-backed cookie persistence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any, TypeVar, cast
from unittest.mock import AsyncMock

import httpx
import pytest

import notebooklm._runtime.lifecycle as lifecycle_module
import notebooklm._web.transport.cookie_persistence as persistence_module
from notebooklm import client as client_module
from notebooklm._auth.cookie_merge import RecoveryObservation
from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.profile_store import (
    CookieMergeDisposition,
    CookieMergeResult,
    ProfileStore,
)
from notebooklm._auth.storage import CookieSaveResult, snapshot_cookie_jar
from notebooklm._auth.tokens import FileLoadedAuth, InlineLoadedAuth
from notebooklm._web.transport.cookie_persistence import CookiePersistence
from notebooklm.auth import AuthTokens
from tests._helpers.client_factory import build_client_shell_for_tests

T = TypeVar("T")


async def _inline_to_thread(func: Callable[[], T], /) -> T:
    return func()


def _auth(path: Path | None) -> AuthTokens:
    return AuthTokens(
        cookies={
            ("SID", ".google.com", "/"): "sid",
            ("__Secure-1PSIDTS", ".google.com", "/"): "psidts",
        },
        csrf_token="csrf",
        session_id="session",
        storage_path=path,
    )


def _live(sid: str = "sid", psidts: str = "psidts") -> httpx.Cookies:
    jar = httpx.Cookies()
    jar.set("SID", sid, domain=".google.com", path="/")
    jar.set("__Secure-1PSIDTS", psidts, domain=".google.com", path="/")
    return jar


def _typed(sid: str = "sid", *, same_site: str | None = "Lax") -> CookieJar:
    return CookieJar(
        (
            Cookie("SID", ".google.com", "/", sid, secure=True, same_site=same_site),
            Cookie(
                "__Secure-1PSIDTS",
                ".google.com",
                "/",
                "psidts",
                secure=True,
                http_only=True,
                same_site="Strict",
            ),
        )
    )


def _write(path: Path, sid: str = "sid", *, same_site: Any = "Lax") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": sid,
                        "domain": ".google.com",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": same_site,
                    },
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "psidts",
                        "domain": ".google.com",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Strict",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )


def test_frozen_baseline_values_copy_and_redact() -> None:
    source = _typed("secret-value")
    ready = persistence_module.ReadyBaseline(source)

    assert tuple(type(ready.value)(tuple(ready.value))) == tuple(source)
    assert ready.value is not source
    assert "secret-value" not in repr(ready)
    assert [field.name for field in fields(persistence_module._PathSaveState)] == [
        "baseline",
        "last_applied_sequence",
    ]
    with pytest.raises(FrozenInstanceError):
        ready.value = CookieJar()  # type: ignore[misc]
    assert persistence_module.BaselineState == (
        persistence_module.UninitializedBaseline
        | persistence_module.ReadyBaseline
        | persistence_module.FailedBaseline
    )


def test_compatibility_constructor_mirrors_but_store_factory_retains_no_auth(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.json"
    auth = _auth(path)
    initial = snapshot_cookie_jar(_live("initial"))
    auth.cookie_snapshot = initial
    compatible = CookiePersistence(auth, path)

    assert compatible.loaded_cookie_snapshot is initial
    replacement = snapshot_cookie_jar(_live("replacement"))
    compatible.loaded_cookie_snapshot = replacement
    assert auth.cookie_snapshot is replacement

    production = CookiePersistence._from_store(ProfileStore(path))
    assert production._legacy.auth is None
    with pytest.raises(AttributeError):
        _ = production.auth


def test_register_open_baseline_uses_exact_store_and_typed_projection(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profile.json")
    persistence = CookiePersistence._from_store(None)
    baseline = _typed("registered", same_site="FuturePolicy")

    persistence.register_open_baseline(store, baseline)

    assert persistence._default_store is store
    state = persistence._states[store.ordering_key]
    assert isinstance(state.baseline, persistence_module.ReadyBaseline)
    assert state.baseline.value is not baseline
    assert persistence.loaded_cookie_snapshot is not None
    assert next(iter(persistence.loaded_cookie_snapshot.values())).value == "registered"


@pytest.mark.asyncio
async def test_prepare_samples_once_off_loop_and_keeps_exact_samesite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profile.json"
    _write(path, same_site="FuturePolicy")
    persistence = CookiePersistence._from_store(ProfileStore(path))
    calls = 0
    real_load = persistence_module._load_cookie_pair_pure

    def load_once(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        assert kwargs == {"require_routable": False}
        return real_load(*args, **kwargs)

    monkeypatch.setattr(persistence_module, "_load_cookie_pair_pure", load_once)
    await persistence._prepare_open_baseline(path, to_thread=_inline_to_thread)
    await persistence._prepare_open_baseline(path, to_thread=_inline_to_thread)

    state = persistence._states[ProfileStore(path).ordering_key]
    assert calls == 1
    assert isinstance(state.baseline, persistence_module.ReadyBaseline)
    assert tuple(state.baseline.value)[0].same_site == "FuturePolicy"


@pytest.mark.asyncio
async def test_failed_prepare_is_sticky_until_exact_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "missing.json"
    store = ProfileStore(path)
    persistence = CookiePersistence._from_store(store)
    calls = 0

    def missing(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        raise FileNotFoundError(path)

    monkeypatch.setattr(persistence_module, "_load_cookie_pair_pure", missing)
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        await persistence._prepare_open_baseline(path, to_thread=_inline_to_thread)
        await persistence._prepare_open_baseline(path, to_thread=_inline_to_thread)
        await persistence._save_canonical(_live("ignored"), path, to_thread=_inline_to_thread)

    state = persistence._states[store.ordering_key]
    assert calls == 1
    assert isinstance(state.baseline, persistence_module.FailedBaseline)
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        (
            "WARNING",
            f"Cookie persistence disabled for {path}: baseline load failed (FileNotFoundError)",
        )
    ]
    persistence.register_open_baseline(store, _typed("repaired"))
    assert isinstance(
        persistence._states[store.ordering_key].baseline,
        persistence_module.ReadyBaseline,
    )


@pytest.mark.asyncio
async def test_cancelled_prepare_does_not_install_detached_result(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    _write(path)
    store = ProfileStore(path)
    persistence = CookiePersistence._from_store(store)
    task_started = asyncio.Event()
    release = asyncio.Event()

    async def cancelled_to_thread(func: Callable[[], T]) -> T:
        task_started.set()
        await release.wait()
        return func()

    task = asyncio.create_task(
        persistence._prepare_open_baseline(path, to_thread=cancelled_to_thread)
    )
    await task_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0)
    assert isinstance(
        persistence._states[store.ordering_key].baseline,
        persistence_module.UninitializedBaseline,
    )


def test_no_store_capture_is_legacy_only() -> None:
    persistence = CookiePersistence._from_store(None)
    snapshot = persistence.capture_open_snapshot(_live("captured"))
    assert persistence.loaded_cookie_snapshot is snapshot
    assert persistence._states == {}


@pytest.mark.asyncio
async def test_canonical_merge_preserves_samesite_and_refreshes_projection(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    _write(path, "old", same_site="Lax")
    store = ProfileStore(path)
    persistence = CookiePersistence._from_store(store)
    await persistence._prepare_open_baseline(path, to_thread=_inline_to_thread)

    await persistence._save_canonical(_live("new"), path, to_thread=_inline_to_thread)

    rows = json.loads(path.read_text(encoding="utf-8"))["cookies"]
    assert rows[0]["value"] == "new"
    assert rows[0]["sameSite"] == "Lax"
    state = persistence._states[store.ordering_key]
    assert state.last_applied_sequence == 0
    assert isinstance(state.baseline, persistence_module.ReadyBaseline)
    assert persistence.loaded_cookie_snapshot is not None
    assert next(iter(persistence.loaded_cookie_snapshot.values())).value == "new"


class _RecordingStore(ProfileStore):
    def __init__(self, path: Path, results: list[CookieMergeResult]) -> None:
        super().__init__(path)
        self.results = results
        self.calls: list[tuple[CookieJar, CookieJar]] = []

    def merge_cookie_observation(
        self,
        observation: CookieJar,
        *,
        baseline: CookieJar,
        recovery_observation: RecoveryObservation | None = None,
    ) -> CookieMergeResult:
        self.calls.append((observation, baseline))
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_canonical_hard_failure_does_not_advance_or_mutate_baseline(tmp_path: Path) -> None:
    hard = CookieMergeResult(
        CookieMergeDisposition.HARD_FAILURE,
        advances_ordering=False,
        committed=None,
    )
    store = _RecordingStore(tmp_path / "profile.json", [hard])
    persistence = CookiePersistence._from_store(store)
    persistence.register_open_baseline(store, _typed("old"))
    before = persistence._states[store.ordering_key].baseline

    await persistence._save_canonical(_live("new"), store.path, to_thread=_inline_to_thread)

    state = persistence._states[store.ordering_key]
    assert len(store.calls) == 1
    assert state.baseline is before
    assert state.last_applied_sequence == -1


@pytest.mark.asyncio
async def test_lazy_canonical_override_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profile.json"
    store = _RecordingStore(
        path,
        [
            CookieMergeResult(
                CookieMergeDisposition.NO_CHANGE,
                advances_ordering=True,
                committed=False,
                next_baseline=_typed("new"),
            )
        ],
    )
    persistence = CookiePersistence._from_store(None)
    real_load = persistence_module._load_cookie_pair_pure
    calls = 0

    def first_fails(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError(path)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(persistence_module, "_load_cookie_pair_pure", first_fails)
    await persistence._save_canonical(_live("new"), path, to_thread=_inline_to_thread)
    assert store.calls == []
    assert isinstance(
        persistence._states[store.ordering_key].baseline,
        persistence_module.UninitializedBaseline,
    )

    _write(path, "old")
    monkeypatch.setattr(persistence_module, "ProfileStore", lambda unused: store)
    await persistence._save_canonical(_live("new"), path, to_thread=_inline_to_thread)
    assert calls == 2
    assert len(store.calls) == 1


@pytest.mark.asyncio
async def test_legacy_success_invalidates_ready_but_failed_remains_sticky(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    store = ProfileStore(path)
    persistence = CookiePersistence._from_store(store)
    persistence.register_open_baseline(store, _typed("old"))
    calls = 0

    def writer(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        return True

    await persistence._save_v0_callback(
        _live("new"),
        path,
        save_cookies_to_storage=writer,
        to_thread=_inline_to_thread,
    )
    assert calls == 1
    assert isinstance(
        persistence._states[store.ordering_key].baseline,
        persistence_module.UninitializedBaseline,
    )

    persistence._states[store.ordering_key].baseline = persistence_module.FailedBaseline()
    await persistence._save_v0_callback(
        _live("newer"),
        path,
        save_cookies_to_storage=writer,
        to_thread=_inline_to_thread,
    )
    assert calls == 2
    assert isinstance(
        persistence._states[store.ordering_key].baseline,
        persistence_module.FailedBaseline,
    )


@pytest.mark.asyncio
async def test_nondefault_legacy_override_uses_own_retryable_snapshot(tmp_path: Path) -> None:
    default = tmp_path / "default.json"
    override = tmp_path / "override.json"
    persistence = CookiePersistence(_auth(default), default)
    calls: list[Any] = []

    def writer(*args: Any, **kwargs: Any) -> CookieSaveResult:
        calls.append(kwargs["original_snapshot"])
        return CookieSaveResult(True)

    await persistence._save_v0_callback(
        _live("skipped"),
        override,
        save_cookies_to_storage=writer,
        to_thread=_inline_to_thread,
    )
    assert calls == []
    _write(override, "disk")
    await persistence._save_v0_callback(
        _live("saved"),
        override,
        save_cookies_to_storage=writer,
        to_thread=_inline_to_thread,
    )
    assert len(calls) == 1
    assert next(iter(calls[0].values())).value == "disk"


def test_runtime_factory_keeps_raw_auth_store_and_resolved_lifecycle_target(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw.json"
    explicit = tmp_path / "explicit.json"
    auth = _auth(raw)
    client = build_client_shell_for_tests(auth, keepalive_storage_path=explicit)
    persistence = client._web_runtime.cookie_persistence
    web_transport = client._web_runtime.web_transport

    assert persistence._default_store is not None
    assert persistence._default_store.path == raw
    assert persistence._legacy.auth is None
    assert web_transport._cookie_persistence_path == explicit
    assert web_transport._auth is auth


@pytest.mark.asyncio
async def test_lifecycle_default_canonical_and_explicit_saver_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def legacy_jar(sid: str) -> httpx.Cookies:
        jar = _live(sid)
        jar.set("HOST_EXTENSION", "opaque", domain=".example.com", path="/")
        jar.set("HOST_ONLY", "local", path="/")
        return jar

    def rows(jar: httpx.Cookies) -> set[tuple[str, str, str, str | None]]:
        return {(cookie.name, cookie.domain, cookie.path, cookie.value) for cookie in jar.jar}

    def mutate_writer_copy(jar: httpx.Cookies) -> None:
        jar.set("SID", "writer-mutated", domain=".google.com", path="/")
        jar.set("WRITER_ONLY", "private", domain=".writer.invalid", path="/")

    path = tmp_path / "profile.json"
    _write(path)
    auth = _auth(path)
    default_client = build_client_shell_for_tests(auth)
    persistence = default_client._web_runtime.cookie_persistence
    web_transport = default_client._web_runtime.web_transport
    canonical = AsyncMock()
    monkeypatch.setattr(persistence, "_save_canonical", canonical)
    await web_transport.save_cookies(_live())
    canonical.assert_awaited_once()

    custom_calls: list[tuple[httpx.Cookies, set[tuple[str, str, str, str | None]]]] = []

    def custom(*args: Any, **kwargs: Any) -> bool:
        received = args[0]
        custom_calls.append((received, rows(received)))
        mutate_writer_copy(received)
        return True

    custom_client = build_client_shell_for_tests(_auth(path), cookie_saver=custom)
    custom_input = legacy_jar("custom")
    custom_expected = rows(custom_input)
    await custom_client._web_runtime.web_transport.save_cookies(custom_input)
    assert len(custom_calls) == 1
    assert custom_calls[0][0] is not custom_input
    assert custom_calls[0][1] == custom_expected
    assert rows(custom_input) == custom_expected


@pytest.mark.asyncio
async def test_file_loaded_client_registers_pair_inline_does_not_and_subclass_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profile.json"
    store = ProfileStore(path)
    baseline = _typed("loaded")
    file_auth = _auth(path)

    async def load_file(**kwargs: Any) -> FileLoadedAuth:
        return FileLoadedAuth(file_auth, store, baseline)

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_file)
    context = client_module.NotebookLMClient.from_storage(str(path))
    client = await context._build()
    persistence = client._web_runtime.cookie_persistence
    assert persistence._default_store is store
    assert isinstance(
        persistence._states[store.ordering_key].baseline,
        persistence_module.ReadyBaseline,
    )

    inline_auth = _auth(None)

    async def load_inline(**kwargs: Any) -> InlineLoadedAuth:
        return InlineLoadedAuth(inline_auth)

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_inline)
    inline = await client_module.NotebookLMClient.from_storage(str(path))._build()
    assert inline._web_runtime.cookie_persistence._states == {}

    class BareClient(client_module.NotebookLMClient):
        def __init__(self, auth: AuthTokens, **kwargs: Any) -> None:
            self.seen_auth = auth

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_file)
    bare = cast(BareClient, await BareClient.from_storage(str(path))._build())
    assert bare.seen_auth is file_auth
    assert not hasattr(bare, "_collaborators")


def test_deleted_default_saver_is_not_reexported() -> None:
    assert not hasattr(lifecycle_module, "_default_cookie_saver")
    assert not hasattr(persistence_module, "_default_cookie_saver")
