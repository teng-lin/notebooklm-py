"""Cookie persistence collaborator for the NotebookLM client runtime."""

from __future__ import annotations

__all__ = ["CookiePersistence", "SaveCookiesToStorage"]

import itertools
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeAlias, TypeVar

import httpx

from ..._auth.cookie_policy import RequiredCookieValidationError
from ..._auth.cookie_types import Cookie, CookieJar
from ..._auth.cookies import StorageStateValidationError, _load_cookie_pair_pure
from ..._auth.profile_store import ProfileStore
from ..._auth.storage import (
    CookieSaveResult,
    CookieSnapshot,
    CookieSnapshotKey,
    CookieSnapshotValue,
    advance_cookie_snapshot_after_save,
    snapshot_cookie_jar,
)
from ...auth import AuthTokens

logger = logging.getLogger("notebooklm.auth")


class SaveCookiesToStorage(Protocol):
    """Callable shape for the exact v0.x callback invocation."""

    def __call__(
        self,
        cookie_jar: httpx.Cookies,
        path: Path,
        /,
        *,
        original_snapshot: CookieSnapshot | None,
        return_result: bool,
    ) -> bool | CookieSaveResult: ...


T = TypeVar("T")


class ToThread(Protocol):
    def __call__(self, func: Callable[[], T], /) -> Awaitable[T]: ...


@dataclass(frozen=True, slots=True)
class UninitializedBaseline:
    pass


@dataclass(frozen=True, slots=True, repr=False)
class ReadyBaseline:
    value: CookieJar = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, CookieJar):
            raise TypeError("value must be a CookieJar")
        object.__setattr__(self, "value", CookieJar(tuple(self.value)))


@dataclass(frozen=True, slots=True)
class FailedBaseline:
    pass


BaselineState: TypeAlias = UninitializedBaseline | ReadyBaseline | FailedBaseline


@dataclass
class _PathSaveState:
    baseline: BaselineState = field(default_factory=UninitializedBaseline)
    last_applied_sequence: int = -1


def _snapshot_from_typed(jar: CookieJar) -> CookieSnapshot:
    """Project a typed baseline to the legacy, SameSite-free snapshot shape."""
    return {
        CookieSnapshotKey(cookie.name, cookie.domain, cookie.path): CookieSnapshotValue(
            cookie.value,
            int(cookie.expires) if cookie.expires is not None else None,
            cookie.secure,
            cookie.http_only,
        )
        for cookie in jar
    }


def _typed_from_snapshot(snapshot: CookieSnapshot) -> CookieJar:
    """Restore the typed load-time baseline carried by ``AuthTokens``."""
    return CookieJar(
        Cookie(
            name=key.name,
            domain=key.domain,
            path=key.path,
            value=value.value,
            expires=value.expires,
            secure=value.secure,
            http_only=value.http_only,
        )
        for key, value in snapshot.items()
    )


class _LegacySnapshotAdapter:
    """Own v0.x snapshots and the compatibility-only AuthTokens mirror."""

    def __init__(
        self,
        default_key: Path | None,
        *,
        auth: AuthTokens | None,
        initial: CookieSnapshot | None = None,
    ) -> None:
        self._default_key = default_key
        self._auth = auth
        self._snapshots: dict[Path | None, CookieSnapshot] = {}
        if initial is not None:
            self.set(default_key, initial)

    @property
    def auth(self) -> AuthTokens | None:
        return self._auth

    @property
    def snapshots(self) -> dict[Path | None, CookieSnapshot]:
        return self._snapshots

    def set_default_key(self, key: Path | None) -> None:
        self._default_key = key

    def get(self, key: Path | None) -> CookieSnapshot | None:
        return self._snapshots.get(key)

    def get_default(self) -> CookieSnapshot | None:
        if self._auth is not None and self._auth.cookie_snapshot is not self.get(self._default_key):
            external = self._auth.cookie_snapshot
            self.set(
                self._default_key,
                None if external is None else dict(external),
            )
        return self.get(self._default_key)

    def set(self, key: Path | None, snapshot: CookieSnapshot | None) -> None:
        if snapshot is None:
            self._snapshots.pop(key, None)
        else:
            self._snapshots[key] = snapshot
        if key == self._default_key and self._auth is not None:
            self._auth.cookie_snapshot = snapshot

    def project(self, key: Path | None, baseline: CookieJar) -> CookieSnapshot:
        snapshot = _snapshot_from_typed(baseline)
        self.set(key, snapshot)
        return snapshot

    def advance(
        self,
        key: Path,
        snapshot: CookieSnapshot,
    ) -> None:
        self.set(key, snapshot)


_BASELINE_ERRORS = (
    OSError,
    UnicodeDecodeError,
    json.JSONDecodeError,
    StorageStateValidationError,
    RequiredCookieValidationError,
    TypeError,
    ValueError,
    OverflowError,
)


class CookiePersistence:
    """Own per-profile typed baselines, ordering, and v0.x projections."""

    def __init__(
        self,
        auth: AuthTokens,
        default_path: Path | None,
        *,
        save_lock: threading.Lock | None = None,
    ) -> None:
        if not isinstance(auth, AuthTokens):
            raise TypeError("auth must be an AuthTokens")
        store = ProfileStore(default_path) if default_path is not None else None
        self._initialize(
            store,
            save_lock=save_lock,
            adapter=_LegacySnapshotAdapter(
                store.ordering_key if store is not None else None,
                auth=auth,
                initial=auth.cookie_snapshot,
            ),
        )

    @classmethod
    def _from_store(
        cls,
        store: ProfileStore | None,
        *,
        save_lock: threading.Lock | None = None,
        initial_snapshot: CookieSnapshot | None = None,
    ) -> CookiePersistence:
        if store is not None and not isinstance(store, ProfileStore):
            raise TypeError("store must be a ProfileStore or None")
        instance = cls.__new__(cls)
        instance._initialize(
            store,
            save_lock=save_lock,
            adapter=_LegacySnapshotAdapter(
                store.ordering_key if store is not None else None,
                auth=None,
                initial=initial_snapshot,
            ),
        )
        if store is not None and initial_snapshot is not None:
            instance._states[store.ordering_key] = _PathSaveState(
                baseline=ReadyBaseline(_typed_from_snapshot(initial_snapshot))
            )
        return instance

    def _initialize(
        self,
        store: ProfileStore | None,
        *,
        save_lock: threading.Lock | None,
        adapter: _LegacySnapshotAdapter,
    ) -> None:
        self._default_store = store
        # This blocking lock is acquired only inside the nested ``_save`` and
        # ``_adopt`` closures, each dispatched through ``to_thread``. Never
        # acquire it directly on the event-loop thread.
        self.save_lock = save_lock if save_lock is not None else threading.Lock()
        self._save_seq = itertools.count()
        self._states: dict[Path, _PathSaveState] = {}
        self._legacy = adapter

    @property
    def auth(self) -> AuthTokens:
        """Compatibility view; production factory instances have no AuthTokens."""
        auth = self._legacy.auth
        if auth is None:
            raise AttributeError("production CookiePersistence has no AuthTokens")
        return auth

    @property
    def default_path(self) -> Path | None:
        return self._default_store.path if self._default_store is not None else None

    @property
    def _default_key(self) -> Path | None:
        return self._default_store.ordering_key if self._default_store is not None else None

    @property
    def _loaded_cookie_snapshots(self) -> dict[Path | None, CookieSnapshot]:
        """Historical inspection view, owned by the concrete legacy adapter."""
        return self._legacy.snapshots

    @property
    def _last_applied_seq(self) -> dict[Path, int]:
        """Historical inspection view over the typed per-path state."""
        return {
            key: state.last_applied_sequence
            for key, state in self._states.items()
            if state.last_applied_sequence >= 0
        }

    @property
    def loaded_cookie_snapshot(self) -> CookieSnapshot | None:
        return self._legacy.get_default()

    @loaded_cookie_snapshot.setter
    def loaded_cookie_snapshot(self, snapshot: CookieSnapshot | None) -> None:
        self._legacy.set(self._default_key, snapshot)

    def register_open_baseline(self, store: ProfileStore, baseline: CookieJar) -> None:
        if not isinstance(store, ProfileStore):
            raise TypeError("store must be a ProfileStore")
        if not isinstance(baseline, CookieJar):
            raise TypeError("baseline must be a CookieJar")
        ready = ReadyBaseline(baseline)
        key = store.ordering_key
        self._states[key] = _PathSaveState(baseline=ready)
        if self._default_store is None or self._default_store.ordering_key == key:
            self._default_store = store
            self._legacy.set_default_key(key)
        self._legacy.project(key, ready.value)

    def _resolve_store(self, path: Path | None) -> ProfileStore | None:
        if path is None:
            return self._default_store
        if self._default_store is not None and path == self._default_store.path:
            return self._default_store
        return ProfileStore(path)

    async def _prepare_open_baseline(
        self,
        path: Path | None,
        *,
        to_thread: ToThread,
    ) -> None:
        store = self._resolve_store(path)
        if store is None:
            return
        if path is not None:
            self._default_store = store
            self._legacy.set_default_key(store.ordering_key)
        key = store.ordering_key
        state = self._states.setdefault(key, _PathSaveState())
        if isinstance(state.baseline, ReadyBaseline | FailedBaseline):
            return

        try:
            pair = await to_thread(
                lambda: _load_cookie_pair_pure(store.path, require_routable=False)
            )
        except _BASELINE_ERRORS as exc:
            logger.warning(
                "Cookie persistence disabled for %s: baseline load failed (%s)",
                store.path,
                type(exc).__name__,
            )
            state.baseline = FailedBaseline()
            return
        ready = ReadyBaseline(pair.baseline)
        state.baseline = ready
        self._legacy.project(key, ready.value)

    async def _adopt_reloaded_baseline(
        self,
        path: Path,
        expected: CookieJar,
        *,
        to_thread: ToThread,
    ) -> None:
        """Adopt the current disk baseline after a recovery jar replacement.

        The recovery path replaces the live jar before calling this method, so
        a save sequenced before this operation observed the rejected jar and a
        save sequenced after it observes the replacement. Re-read under
        ``save_lock`` to cover a later save that reached the lock first. If disk
        advanced again, keep the old baseline: adopting unmatched provenance
        would authorize the live replacement to overwrite that sibling update.
        """
        if not isinstance(expected, CookieJar):
            raise TypeError("expected must be a CookieJar")
        store = self._resolve_store(path)
        if store is None:  # pragma: no cover - ``path`` is concrete by contract
            raise ValueError("baseline adoption requires a storage path")
        key = store.ordering_key
        seq = next(self._save_seq)

        def _adopt() -> None:
            with self.save_lock:
                pair = _load_cookie_pair_pure(store.path, require_routable=False)
                if pair.baseline != expected:
                    logger.debug(
                        "Cookie profile advanced again; recovery baseline not adopted: %s",
                        store.path,
                    )
                    return
                state = self._states.setdefault(key, _PathSaveState())
                ready = ReadyBaseline(pair.baseline)
                state.baseline = ready
                state.last_applied_sequence = max(state.last_applied_sequence, seq)
                self._legacy.project(key, ready.value)

        await to_thread(_adopt)

    def capture_open_snapshot(self, jar: httpx.Cookies) -> CookieSnapshot:
        store = self._default_store
        if store is None:
            snapshot = snapshot_cookie_jar(jar)
            self._legacy.set(None, snapshot)
            return snapshot
        key = store.ordering_key
        state = self._states.setdefault(key, _PathSaveState())
        if isinstance(state.baseline, ReadyBaseline):
            return self._legacy.project(key, state.baseline.value)
        snapshot = snapshot_cookie_jar(jar)
        self._legacy.set(key, snapshot)
        return snapshot

    async def _save_canonical(
        self,
        jar: httpx.Cookies,
        path: Path | None,
        *,
        to_thread: ToThread,
    ) -> None:
        store = self._resolve_store(path)
        if store is None:
            return
        key = store.ordering_key
        seq = next(self._save_seq)
        observation = CookieJar.from_httpx(jar)

        def _save() -> None:
            with self.save_lock:
                state = self._states.setdefault(key, _PathSaveState())
                if seq < state.last_applied_sequence:
                    return
                if isinstance(state.baseline, FailedBaseline):
                    return
                if isinstance(state.baseline, UninitializedBaseline):
                    try:
                        pair = _load_cookie_pair_pure(store.path, require_routable=False)
                    except _BASELINE_ERRORS:
                        return
                    state.baseline = ReadyBaseline(pair.baseline)
                    self._legacy.project(key, pair.baseline)
                baseline = state.baseline
                if not isinstance(baseline, ReadyBaseline):  # pragma: no cover
                    raise AssertionError("canonical baseline must be ready")
                result = store.merge_cookie_observation(
                    observation,
                    baseline=baseline.value,
                )
                if not result.advances_ordering:
                    return
                if result.next_baseline is None:  # pragma: no cover - result invariant
                    raise AssertionError("accepted merge must provide a next baseline")
                state.last_applied_sequence = seq
                state.baseline = ReadyBaseline(result.next_baseline)
                self._legacy.project(key, result.next_baseline)

        await to_thread(_save)

    async def _save_v0_callback(
        self,
        jar: httpx.Cookies,
        path: Path | None = None,
        *,
        save_cookies_to_storage: SaveCookiesToStorage,
        to_thread: ToThread,
    ) -> None:
        """Persist through an explicitly injected v0.x writer callback."""
        store = self._resolve_store(path)
        if store is None:
            return
        key = store.ordering_key
        is_default = key == self._default_key
        seq = next(self._save_seq)
        jar_copy = httpx.Cookies(jar)
        post = snapshot_cookie_jar(jar_copy)

        def _save() -> None:
            with self.save_lock:
                state = self._states.setdefault(key, _PathSaveState())
                if seq < state.last_applied_sequence:
                    return
                original = self._legacy.get(key)
                if original is None and not is_default:
                    try:
                        pair = _load_cookie_pair_pure(store.path, require_routable=False)
                    except _BASELINE_ERRORS as exc:
                        logger.warning(
                            "Skipping cookie save: override baseline initialization failed (%s)",
                            type(exc).__name__,
                        )
                        return
                    original = self._legacy.project(key, pair.baseline)
                result = save_cookies_to_storage(
                    jar_copy,
                    store.path,
                    original_snapshot=original,
                    return_result=True,
                )
                advanced: CookieSnapshot | None = None
                advances_ordering = False
                if isinstance(result, CookieSaveResult):
                    if result.ok:
                        advanced = post
                        advances_ordering = True
                    elif result.cas_rejected_keys:
                        advanced = advance_cookie_snapshot_after_save(
                            original,
                            post,
                            result.cas_rejected_keys,
                        )
                        advances_ordering = True
                elif result:
                    advanced = post
                    advances_ordering = True
                if not advances_ordering:
                    return
                if advanced is not None:
                    self._legacy.advance(key, advanced)
                state.last_applied_sequence = seq
                if isinstance(state.baseline, ReadyBaseline):
                    state.baseline = UninitializedBaseline()

        await to_thread(_save)
