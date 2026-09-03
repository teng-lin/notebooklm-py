"""Direct behavior tests for the canonical stored-auth composition values."""

from __future__ import annotations

import dataclasses
import json
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import httpx
import pytest

import notebooklm._auth.tokens as _auth_tokens
from notebooklm._auth import cookie_semantics as _cookie_semantics
from notebooklm._auth import cookies as _auth_cookies
from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.cookies import (
    _build_cookie_pair_from_storage_state,
    _LoadedCookiePair,
)
from notebooklm._auth.paths import resolve_auth_json_env
from notebooklm._auth.profile_account import ProfileAccount
from notebooklm._auth.profile_document import ProfileDocument
from notebooklm._auth.profile_migration import (
    LegacyAccountContext,
    LegacyAccountMigrator,
    LegacyPromotionScheduler,
)
from notebooklm._auth.profile_store import (
    CookieMergeDisposition,
    CookieMergeResult,
    ProfileStore,
)
from notebooklm._auth.tokens import (
    AccountRouteResolver,
    AuthTokens,
    FileAuthSource,
    FileLoadedAuth,
    InlineAuthSource,
    InlineLoadedAuth,
    LoadPolicy,
    SessionSeed,
    SessionSeedLoader,
    StoredAuthLoader,
    TokenAcquisition,
)


def _row(
    name: str,
    value: str,
    *,
    expires: object = -1,
    secure: object = False,
    http_only: object = False,
    same_site: object = None,
) -> dict[str, object]:
    return {
        "name": name,
        "value": value,
        "domain": ".google.com",
        "path": "",
        "expires": expires,
        "secure": secure,
        "httpOnly": http_only,
        "sameSite": same_site,
    }


def test_paired_cookie_projection_uses_one_winner_and_exact_converted_fields() -> None:
    state = {
        "cookies": [
            _row("SID", "first", expires="41.9", secure=1, http_only="yes", same_site="Future"),
            _row("SID", "second", expires=99, same_site="Strict"),
            _row("__Secure-1PSIDTS", "ts", expires=-1, same_site="Lax"),
        ]
    }

    pair = _build_cookie_pair_from_storage_state(state, require_routable=True)

    assert [(cookie.name, cookie.value) for cookie in pair.live.jar] == [
        ("SID", "first"),
        ("__Secure-1PSIDTS", "ts"),
    ]
    sid, psidts = tuple(pair.baseline)
    assert (
        sid.name,
        sid.value,
        sid.domain,
        sid.path,
        sid.expires,
        type(sid.expires),
        sid.secure,
        sid.http_only,
        sid.same_site,
    ) == ("SID", "first", ".google.com", "/", 41, int, True, True, "Future")
    assert (psidts.expires, psidts.same_site) == (None, "Lax")


def test_paired_cookie_projection_skips_malformed_first_without_reserving_identity() -> None:
    state = {
        "cookies": [
            _row("SID", "malformed", expires="never", same_site="Strict"),
            _row("SID", "survivor", expires="-1", same_site="ForwardCompatible"),
            _row("__Secure-1PSIDTS", "ts"),
        ]
    }

    pair = _build_cookie_pair_from_storage_state(state, require_routable=False)

    sid = tuple(pair.baseline)[0]
    assert sid.value == "survivor"
    assert (sid.expires, type(sid.expires), sid.same_site) == (
        -1,
        int,
        "ForwardCompatible",
    )
    assert pair.live.get("SID", domain=".google.com") == "survivor"


def test_paired_cookie_projection_sanitizes_and_converts_each_row_once(monkeypatch) -> None:
    """One source pass, policy revalidation, and conversion occur per row.

    These pure, idempotent operations leave no distinct result-state signal for
    duplicate calls, so lexical spies retain the invocation-count contract
    without moving the patches into a shared helper or fixture.
    """
    state = {
        "cookies": [
            _row("SID", "sid"),
            _row("__Secure-1PSIDTS", "ts"),
        ]
    }
    source_sanitize_calls = 0
    policy_revalidation_calls = 0
    convert_calls = 0
    original_sanitize = _cookie_semantics.sanitize_cookie_entry
    original_convert = _auth_cookies._cookie_from_normalized_entry

    def count_sanitize(entry, *, check_value=True):
        nonlocal policy_revalidation_calls, source_sanitize_calls
        if isinstance(entry, _auth_cookies._SanitizedCookieEntry):
            policy_revalidation_calls += 1
        else:
            source_sanitize_calls += 1
        return original_sanitize(entry, check_value=check_value)

    def count_convert(normalized, *, http_only_key):
        nonlocal convert_calls
        convert_calls += 1
        return original_convert(normalized, http_only_key=http_only_key)

    monkeypatch.setattr(_cookie_semantics, "sanitize_cookie_entry", count_sanitize)
    monkeypatch.setattr(_auth_cookies, "_cookie_from_normalized_entry", count_convert)

    _build_cookie_pair_from_storage_state(state, require_routable=True)

    assert source_sanitize_calls == 2
    assert policy_revalidation_calls == 2
    assert convert_calls == 2


def test_loaded_cookie_pair_is_frozen_redacted_and_copies_typed_baseline() -> None:
    source = [Cookie("SID", ".google.com", "/", "secret", same_site="Lax")]
    baseline = CookieJar(source)
    pair = _LoadedCookiePair(httpx.Cookies(), baseline)
    source.append(Cookie("APISID", ".google.com", "/", "later"))

    assert dataclasses.is_dataclass(pair)
    assert pair.__slots__ == ("live", "baseline")
    assert tuple(pair.baseline) == tuple(baseline)
    assert "secret" not in repr(pair)
    with pytest.raises(FrozenInstanceError):
        pair.baseline = CookieJar()  # type: ignore[misc]
    with pytest.raises(TypeError, match="loaded cookie pair fields are invalid"):
        _LoadedCookiePair(object(), baseline)  # type: ignore[arg-type]


def _document(*, account: object = None) -> ProfileDocument:
    payload: dict[str, object] = {
        "cookies": [_row("SID", "sid"), _row("__Secure-1PSIDTS", "ts")],
        "origins": [],
    }
    if account is not None:
        payload["notebooklm"] = {"account": account}
    return ProfileDocument.decode(payload)


def _live(value: str = "sid") -> httpx.Cookies:
    jar = httpx.Cookies()
    jar.set("SID", value, domain=".google.com", path="/")
    jar.set("__Secure-1PSIDTS", "ts", domain=".google.com", path="/")
    return jar


def _baseline(value: str = "sid", same_site: str | None = "Lax") -> CookieJar:
    return CookieJar((Cookie("SID", ".google.com", "/", value, same_site=same_site),))


def _auth(*, path: Path | None = None) -> AuthTokens:
    live = _live()
    return AuthTokens(
        cookies={},
        csrf_token="csrf-secret",
        session_id="session-secret",
        storage_path=path,
        cookie_jar=live,
    )


def test_closed_values_validate_carriers_copy_baselines_and_redact_secrets(tmp_path: Path) -> None:
    document = _document()
    store = ProfileStore(tmp_path / "state.json")
    baseline = _baseline("baseline-secret")
    seed = SessionSeed(_live("live-secret"), baseline)
    acquisition = TokenAcquisition(
        "csrf-secret",
        "session-secret",
        seed.live,
        baseline,
        ProfileAccount(2, "agent@example.com"),
    )
    file_loaded = FileLoadedAuth(_auth(path=store.path), store, baseline)
    inline_loaded = InlineLoadedAuth(_auth())

    assert InlineAuthSource(document).document is not document
    assert FileAuthSource(store, "raw-profile").profile == "raw-profile"
    assert tuple(seed.baseline) == tuple(baseline)
    assert tuple(acquisition.baseline_before_fetch_mutations) == tuple(baseline)
    assert tuple(file_loaded.persistence_baseline) == tuple(baseline)
    rendered = repr(seed) + repr(acquisition) + repr(file_loaded) + repr(inline_loaded)
    assert "secret" not in rendered
    with pytest.raises(TypeError, match="document must be"):
        InlineAuthSource(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="store must be"):
        FileAuthSource(object(), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="session seed fields"):
        SessionSeed(object(), baseline)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="allow_headless"):
        LoadPolicy(allow_headless=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="heal_psidts"):
        LoadPolicy(heal_psidts=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="token acquisition fields"):
        TokenAcquisition("csrf", "session", _live(), baseline, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="file loaded auth fields"):
        FileLoadedAuth(_auth(), object(), baseline)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_inherited_from_storage_constructs_runtime_subclass_exactly_once(monkeypatch) -> None:
    calls = 0

    class DerivedAuth(AuthTokens):
        def __post_init__(self) -> None:
            nonlocal calls
            calls += 1
            super().__post_init__()

    async def fake_load(*, path, profile, policy, auth_type):
        assert auth_type is DerivedAuth
        return InlineLoadedAuth(
            auth_type(
                cookies={},
                csrf_token="csrf",
                session_id="session",
                cookie_jar=_live(),
            )
        )

    monkeypatch.setattr(_auth_tokens, "_load_stored_auth", fake_load)

    result = await DerivedAuth.from_storage()

    assert type(result) is DerivedAuth
    assert calls == 1


@pytest.mark.asyncio
async def test_source_resolution_preserves_explicit_env_empty_and_profile_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    loader = StoredAuthLoader(
        seeds=SessionSeedLoader(),
        token_acquirer=_FakeAcquirer(),
        migrator=LegacyAccountMigrator(),
        promotions=LegacyPromotionScheduler(),
    )
    explicit = tmp_path / "explicit.json"
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(_document().to_json()))
    source = await loader._resolve_source(explicit, "raw-profile")
    assert isinstance(source, FileAuthSource)
    assert source.store.path == explicit
    assert source.profile == "raw-profile"

    source = await loader._resolve_source(None, "ignored-profile")
    assert isinstance(source, InlineAuthSource)

    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "")
    with pytest.raises(ValueError, match="set but empty"):
        await loader._resolve_source(None, "ignored-profile")

    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON")
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    source = await loader._resolve_source(None, "work")
    assert isinstance(source, FileAuthSource)
    assert source.profile == "work"
    assert source.store.path == tmp_path / "profiles" / "work" / "storage_state.json"


@pytest.mark.asyncio
async def test_inline_source_resolution_uses_real_environment_value(
    monkeypatch,
) -> None:
    loader = StoredAuthLoader(
        seeds=SessionSeedLoader(),
        token_acquirer=_FakeAcquirer(),
        migrator=LegacyAccountMigrator(),
        promotions=LegacyPromotionScheduler(),
    )
    captured = json.dumps(_document(account={"email": "captured@example.com"}).to_json())
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", captured)

    source = await loader._resolve_source(None, "must-not-be-read")

    assert isinstance(source, InlineAuthSource)
    assert source.document.to_json()["notebooklm"]["account"]["email"] == ("captured@example.com")


@pytest.mark.parametrize("raw", [None, "", "  raw-json  "])
def test_auth_json_env_owner_preserves_presence_and_raw_value(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
) -> None:
    """The canonical owner performs the environment read without test rebinding."""
    if raw is None:
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    else:
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", raw)

    assert resolve_auth_json_env() == raw


@pytest.mark.asyncio
async def test_inline_route_uses_captured_raw_boolean_and_stripped_email() -> None:
    resolver = AccountRouteResolver(
        InlineAuthSource(_document(account={"authuser": True, "email": "  a@example.com  "})),
        migrator=LegacyAccountMigrator(),
        promotions=LegacyPromotionScheduler(),
    )

    account = await resolver.resolve()

    assert account is not None
    assert account.authuser is True
    assert account.email == "a@example.com"


@pytest.mark.asyncio
async def test_file_route_detects_stale_legacy_cleanup_off_the_event_loop(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(_document(account={"authuser": 7, "email": "fresh@example.com"}).to_json()),
        encoding="utf-8",
    )
    (tmp_path / "context.json").write_text(
        json.dumps({"account": {"authuser": 1, "email": "stale@example.com"}}),
        encoding="utf-8",
    )
    context_threads: list[int] = []

    class _ThreadRecordingContext(LegacyAccountContext):
        def read(self, storage_path: Path):
            context_threads.append(threading.get_ident())
            return super().read(storage_path)

    class _RecordingScheduler(LegacyPromotionScheduler):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[object, object]] = []

        def schedule(self, store, migrator):
            self.calls.append((store, migrator))
            return True

    migrator = LegacyAccountMigrator(_ThreadRecordingContext())
    scheduler = _RecordingScheduler()
    resolver = AccountRouteResolver(
        FileAuthSource(ProfileStore(path), "work"),
        migrator=migrator,
        promotions=scheduler,
    )
    event_loop_thread = threading.get_ident()

    account = await resolver.resolve()

    assert account == ProfileAccount(7, "fresh@example.com")
    assert scheduler.calls and scheduler.calls[0][1] is migrator
    assert context_threads and all(thread_id != event_loop_thread for thread_id in context_threads)


class _FakeAcquirer:
    def __init__(
        self, *, mutate_to: str | None = None, account: ProfileAccount | None = None
    ) -> None:
        self.mutate_to = mutate_to
        self.account = account

    async def acquire(self, seed, *, source, route, policy):
        if self.mutate_to is not None:
            seed.live.set("SID", self.mutate_to, domain=".google.com", path="/")
        return TokenAcquisition(
            "csrf",
            "session",
            seed.live,
            seed.baseline,
            self.account,
        )


class _FixedSeeds(SessionSeedLoader):
    def __init__(self, seed: SessionSeed) -> None:
        self.seed = seed
        self.sources: list[object] = []

    async def load(self, source, policy):
        self.sources.append(source)
        return self.seed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    [
        CookieMergeDisposition.APPLIED,
        CookieMergeDisposition.NO_CHANGE,
        CookieMergeDisposition.CONFLICT,
        CookieMergeDisposition.HARD_FAILURE,
    ],
)
async def test_file_loader_selects_merge_next_baseline_or_hard_failure_fallback(
    tmp_path: Path,
    monkeypatch,
    disposition: CookieMergeDisposition,
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps(_document().to_json()), encoding="utf-8")
    original = _baseline("old", "Strict")
    selected = _baseline("advanced", "ForwardCompatible")
    seeds = _FixedSeeds(SessionSeed(_live("old"), original))
    seen: dict[str, Any] = {}

    def merge(self, observation, *, baseline, recovery_observation=None):
        seen["observation"] = observation
        seen["baseline"] = baseline
        if disposition is CookieMergeDisposition.HARD_FAILURE:
            return CookieMergeResult(disposition, False, None)
        rejected = (
            frozenset({next(iter(original)).key})
            if disposition is CookieMergeDisposition.CONFLICT
            else frozenset()
        )
        return CookieMergeResult(
            disposition,
            True,
            disposition is CookieMergeDisposition.APPLIED,
            selected,
            rejected,
        )

    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", merge)
    loader = StoredAuthLoader(
        seeds=seeds,
        token_acquirer=_FakeAcquirer(mutate_to="observed", account=ProfileAccount(3, "a@b")),
        migrator=LegacyAccountMigrator(),
        promotions=LegacyPromotionScheduler(),
    )

    loaded = await loader.load(
        path=path,
        profile="raw",
        policy=LoadPolicy(),
        auth_type=AuthTokens,
    )

    assert isinstance(loaded, FileLoadedAuth)
    expected = original if disposition is CookieMergeDisposition.HARD_FAILURE else selected
    assert loaded.persistence_baseline == expected
    assert seen["baseline"] == original
    assert next(iter(seen["observation"])).value == "observed"
    assert loaded.auth.authuser == 3
    assert loaded.auth.account_email == "a@b"
    assert loaded.auth.cookie_snapshot is not None
    key = _auth_tokens._auth_storage.CookieSnapshotKey("SID", ".google.com", "/")
    assert loaded.auth.cookie_snapshot[key].value == next(iter(expected)).value


@pytest.mark.asyncio
async def test_inline_loader_never_constructs_or_calls_a_store(monkeypatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(_document().to_json()))
    seeds = _FixedSeeds(SessionSeed(_live(), _baseline()))

    def forbidden(*args, **kwargs):
        raise AssertionError("inline load touched ProfileStore")

    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", forbidden)
    loader = StoredAuthLoader(
        seeds=seeds,
        token_acquirer=_FakeAcquirer(),
        migrator=LegacyAccountMigrator(),
        promotions=LegacyPromotionScheduler(),
    )

    loaded = await loader.load(
        path=None,
        profile="ignored",
        policy=LoadPolicy(),
        auth_type=AuthTokens,
    )

    assert isinstance(loaded, InlineLoadedAuth)
    assert loaded.auth.storage_path is None
