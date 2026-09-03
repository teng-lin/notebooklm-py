"""Type guards, default-store ownership, and no-store short circuits.

Cookie persistence decides *whether* a credential file is written at all. The
guards below are the ones that keep a wrongly typed baseline, a foreign profile
path, or a missing store from turning into a write, so each is pinned
explicitly. All cookie values here are obvious fakes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._auth.storage import CookieSnapshot, snapshot_cookie_jar
from notebooklm._web.transport.cookie_persistence import CookiePersistence, ReadyBaseline
from notebooklm.auth import AuthTokens


def _auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "sid-fake", "__Secure-1PSIDTS": "psidts-fake"},
        csrf_token="csrf-fake",
        session_id="session-fake",
    )


def _jar(sid: str = "sid-fake") -> httpx.Cookies:
    jar = httpx.Cookies()
    jar.set("SID", sid, domain=".google.com", path="/")
    jar.set("__Secure-1PSIDTS", "psidts-fake", domain=".google.com", path="/")
    return jar


def _write_storage(path: Path, sid: str = "sid-fake") -> None:
    rows = [
        {
            "name": name,
            "value": value,
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": False,
            "sameSite": "None",
        }
        for name, value in (("SID", sid), ("__Secure-1PSIDTS", "psidts-fake"))
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": rows, "origins": []}), encoding="utf-8")


async def _inline_to_thread(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
    return func(*args, **kwargs)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param([], id="list"),
        pytest.param((), id="tuple"),
        pytest.param(None, id="none"),
    ],
)
def test_ready_baseline_rejects_a_value_that_is_not_a_cookie_jar(value: Any) -> None:
    """The baseline is compared against disk to authorise a write; a look-alike
    sequence would compare unequal forever and silently disable persistence."""
    with pytest.raises(TypeError, match="value must be a CookieJar"):
        ReadyBaseline(value)


def test_ready_baseline_copies_its_jar_so_later_mutation_cannot_rewrite_history() -> None:
    """Provenance must be frozen at construction, not aliased to the caller's jar."""
    cookie = Cookie(name="SID", domain=".google.com", path="/", value="sid-fake")
    source = [cookie]

    ready = ReadyBaseline(CookieJar(source))
    source.append(Cookie(name="OSID", domain=".google.com", path="/", value="osid-fake"))

    assert list(ready.value) == [cookie]


def test_cookie_persistence_rejects_an_auth_argument_that_is_not_auth_tokens() -> None:
    """The compat mirror writes straight back into ``auth.cookie_snapshot``."""
    with pytest.raises(TypeError, match="auth must be an AuthTokens"):
        CookiePersistence("not-auth-tokens", None)  # type: ignore[arg-type]


def test_from_store_rejects_a_store_argument_that_is_not_a_profile_store() -> None:
    """Only ``ProfileStore`` owns the locked write transaction for a path."""
    with pytest.raises(TypeError, match="store must be a ProfileStore or None"):
        CookiePersistence._from_store("not-a-store")  # type: ignore[arg-type]


def test_default_path_reports_the_configured_store_path_and_none_without_one(
    tmp_path: Path,
) -> None:
    """Callers read this to decide where a save would land."""
    path = tmp_path / "storage_state.json"

    assert CookiePersistence(_auth(), path).default_path == path
    assert CookiePersistence(_auth(), None).default_path is None


@pytest.mark.parametrize(
    ("store", "baseline", "match"),
    [
        pytest.param("not-a-store", CookieJar(()), "store must be a ProfileStore", id="store"),
        pytest.param(None, [], "baseline must be a CookieJar", id="baseline"),
    ],
)
def test_register_open_baseline_rejects_wrongly_typed_arguments(
    tmp_path: Path, store: Any, baseline: Any, match: str
) -> None:
    """A bad registration would authorise writes against unverified provenance."""
    persistence = CookiePersistence(_auth(), None)
    resolved = ProfileStore(tmp_path / "storage_state.json") if store is None else store

    with pytest.raises(TypeError, match=match):
        persistence.register_open_baseline(resolved, baseline)


def test_register_open_baseline_for_a_foreign_path_does_not_steal_the_default_store(
    tmp_path: Path,
) -> None:
    """Registering a second profile must not silently redirect default saves.

    ``default_path`` is where a ``path=None`` save lands; letting an override
    path claim it would write one account's cookies into another's profile.
    """
    default_path = tmp_path / "default" / "storage_state.json"
    other_path = tmp_path / "other" / "storage_state.json"
    persistence = CookiePersistence(_auth(), default_path)
    other_store = ProfileStore(other_path)
    baseline = CookieJar([Cookie(name="SID", domain=".google.com", path="/", value="other-fake")])

    persistence.register_open_baseline(other_store, baseline)

    assert persistence.default_path == default_path
    snapshots = persistence._loaded_cookie_snapshots
    assert other_store.ordering_key in snapshots
    # The default profile was never given a projection of the foreign baseline.
    assert persistence.loaded_cookie_snapshot is None


@pytest.mark.asyncio
async def test_prepare_open_baseline_without_a_path_keeps_the_configured_default(
    tmp_path: Path,
) -> None:
    """``path=None`` means "use the default"; it must not clear or rebind it."""
    default_path = tmp_path / "storage_state.json"
    _write_storage(default_path, sid="on-disk-fake")
    persistence = CookiePersistence(_auth(), default_path)

    await persistence._prepare_open_baseline(None, to_thread=_inline_to_thread)

    assert persistence.default_path == default_path
    snapshot = persistence.loaded_cookie_snapshot
    assert snapshot is not None
    assert {key.name for key in snapshot} == {"SID", "__Secure-1PSIDTS"}


@pytest.mark.asyncio
async def test_adopt_reloaded_baseline_rejects_an_expectation_that_is_not_a_cookie_jar(
    tmp_path: Path,
) -> None:
    """The expectation gates adoption; a non-jar would compare unequal by type."""
    persistence = CookiePersistence(_auth(), tmp_path / "storage_state.json")

    with pytest.raises(TypeError, match="expected must be a CookieJar"):
        await persistence._adopt_reloaded_baseline(
            tmp_path / "storage_state.json",
            [],  # type: ignore[arg-type]
            to_thread=_inline_to_thread,
        )


@pytest.mark.asyncio
async def test_v0_callback_save_is_never_invoked_when_no_store_is_configured() -> None:
    """With no profile path there is nowhere legitimate to write credentials.

    The writer callback must not be reached at all — invoking it with a
    placeholder path is how a jar ends up in the wrong file.
    """
    calls: list[Any] = []

    def _saver(cookie_jar: httpx.Cookies, path: Path, /, **kwargs: Any) -> bool:
        calls.append((cookie_jar, path, kwargs))
        return True

    persistence = CookiePersistence(_auth(), None)

    await persistence._save_v0_callback(
        _jar(),
        None,
        save_cookies_to_storage=_saver,
        to_thread=_inline_to_thread,
    )

    assert calls == []


def test_loaded_cookie_snapshot_resyncs_from_an_externally_replaced_auth_snapshot(
    tmp_path: Path,
) -> None:
    """``AuthTokens`` stays writable for compatibility, so the adapter re-reads it.

    The re-read must copy: sharing the caller's dict would let later mutations
    of ``auth.cookie_snapshot`` rewrite the provenance a save is checked against.
    """
    auth = _auth()
    persistence = CookiePersistence(auth, tmp_path / "storage_state.json")
    assert persistence.loaded_cookie_snapshot is None

    external: CookieSnapshot = snapshot_cookie_jar(_jar())
    auth.cookie_snapshot = external

    adopted = persistence.loaded_cookie_snapshot

    assert adopted == external
    assert adopted is not external


def test_loaded_cookie_snapshot_follows_an_external_clear_back_to_none(
    tmp_path: Path,
) -> None:
    """Clearing the compat mirror must drop the projection, not serve a stale one."""
    auth = _auth()
    persistence = CookiePersistence(auth, tmp_path / "storage_state.json")
    auth.cookie_snapshot = snapshot_cookie_jar(_jar())
    assert persistence.loaded_cookie_snapshot is not None

    auth.cookie_snapshot = None

    assert persistence.loaded_cookie_snapshot is None
