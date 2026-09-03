"""Focused regression tests for remaining auth failure and coordination branches."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

import notebooklm.client as client_module
from notebooklm._auth import (
    account_email,
    cookie_merge,
    cookies,
    keepalive,
    master_token,
    profile_store,
    psidts_recovery,
    recovery,
    recovery_rungs,
    refresh,
    single_flight,
    storage,
    tokens,
)
from notebooklm._auth.cookie_types import Cookie, CookieIdentity, CookieJar
from notebooklm._auth.extraction import _LoginRedirectError
from notebooklm._auth.master_token_types import MasterTokenError
from notebooklm._auth.profile_account import DomainSelection, ProfileAccount, SetAccount
from notebooklm._auth.profile_document import ProfileDocument, _ProfileDocumentStructureError
from notebooklm._auth.profile_migration import (
    LegacyAccountMigrator,
    LegacyPromotionScheduler,
    _LoadedProfilePair,
)
from notebooklm._auth.profile_store import (
    CookieMergeDisposition,
    CookieMergeResult,
    LoginWriteRequest,
    MintedSessionWriteRequest,
    ProfileStore,
    RemintWriteRequest,
    ReplaceResult,
    ReplaceStatus,
)
from notebooklm._auth.storage_lock import LockState


@pytest.fixture(autouse=True)
def _reset_auth_process_state():
    """Keep the process-global recovery coordinators hermetic per test."""
    keepalive._reset_poke_state_for_tests()
    single_flight._reset_for_tests()
    recovery.ColdRecoveryState.process_default()._reset_for_tests()
    yield
    keepalive._reset_poke_state_for_tests()
    single_flight._reset_for_tests()
    recovery.ColdRecoveryState.process_default()._reset_for_tests()


def _row(
    name: str,
    value: str,
    *,
    domain: str = ".google.com",
    path: str = "/",
) -> dict[str, object]:
    return {"name": name, "value": value, "domain": domain, "path": path}


def _jar(sid: str = "sid", *, psidts_domain: str = ".google.com") -> httpx.Cookies:
    jar = httpx.Cookies()
    jar.set("SID", sid, domain=".google.com", path="/")
    jar.set("__Secure-1PSIDTS", "ts", domain=psidts_domain, path="/")
    return jar


def _auth(*, storage_path: Path | None = None, authuser: int = 0) -> tokens.AuthTokens:
    jar = _jar()
    return tokens.AuthTokens(
        cookies={},
        csrf_token="csrf",
        session_id="session",
        storage_path=storage_path,
        cookie_jar=jar,
        authuser=authuser,
    )


def _document(*, authuser: int | None = None) -> ProfileDocument:
    payload: dict[str, object] = {
        "cookies": [_row("SID", "sid"), _row("__Secure-1PSIDTS", "ts")],
        "origins": [],
    }
    if authuser is not None:
        payload["notebooklm"] = {"account": {"authuser": authuser}}
    return ProfileDocument.decode(payload)


def test_cookie_value_objects_reject_foreign_comparands_and_rows() -> None:
    cookie = Cookie("SID", ".google.com", "/", "secret")
    assert cookie.__eq__(object()) is NotImplemented
    assert CookieJar().__eq__(object()) is NotImplemented

    rows: list[dict[str, Any]] = [
        {"not": "a cookie"},
        _row("TRACKER", "value", domain="tracker.example"),
    ]
    assert CookieJar.from_rookiepy(rows) == CookieJar()
    assert CookieJar.from_storage_state({"cookies": "not-a-list"}) == CookieJar()


def test_cookie_helpers_cover_malformed_duplicate_and_identity_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = cookies._SanitizedCookieEntry(_row("SID", "sid"))
    assert cookies._sanitize_cookie_entry(marker) is marker
    assert cookies._sanitized_auth_entries({"cookies": "not-a-list"}) == []

    duplicate_state = {
        "cookies": [
            _row("SID", "first"),
            _row("SID", "second"),
            _row("__Secure-1PSIDTS", "ts"),
        ]
    }
    extracted = cookies.extract_cookies_with_domains(duplicate_state)
    assert extracted[("SID", ".google.com", "/")] == "first"

    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    invalid = cookies.get_storage_path()
    invalid.parent.mkdir(parents=True)
    invalid.write_text("[]", encoding="utf-8")
    with pytest.raises(cookies.StorageStateValidationError):
        cookies._load_storage_state()

    assert cookies._safe_to_cookie({"not": "a cookie"}) is None

    def unusable(_entry: dict[str, Any]):
        raise OverflowError("unusable expiry")

    assert cookies._safe_to_cookie(_row("SID", "sid"), unusable) is None
    assert ("SID", "google.com", "/") in cookies._cookie_key_variants(("SID", ".google.com", "/"))
    assert cookies._find_cookie_for_storage({}, ("SID", ".google.com", "/"), "old") is None

    candidate_jar = _jar()
    candidate = next(cookie for cookie in candidate_jar.jar if cookie.name == "SID")
    by_key = {(candidate.name, candidate.domain, candidate.path): candidate}
    assert (
        cookies._find_cookie_for_storage(
            by_key,
            (candidate.name, candidate.domain, candidate.path),
            candidate.value,
        )
        is candidate
    )
    cookies._replace_cookie_jar(candidate_jar, candidate_jar)
    assert CookieJar.from_httpx(candidate_jar) == CookieJar.from_httpx(_jar())

    target = {("OLD", ".google.com", "/"): "old"}
    fresh = {("SID", ".google.com", "/"): "new"}
    cookies._update_cookie_input(target, fresh)
    assert target == fresh


def test_cookie_pair_skips_a_converter_failure_and_reports_unroutable_psidts() -> None:
    class UnusableFlag:
        def __bool__(self) -> bool:
            raise OverflowError("unusable secure flag")

    broken_psidts = _row("__Secure-1PSIDTS", "ts")
    broken_psidts["secure"] = UnusableFlag()
    state = {"cookies": [_row("SID", "sid"), broken_psidts]}

    with pytest.raises(cookies.RequiredCookieValidationError, match="not routable"):
        cookies._build_cookie_pair_from_storage_state(state, require_routable=True)


async def test_account_email_handles_missing_live_jar_and_profile_read_failure(
    tmp_path: Path,
) -> None:
    auth = _auth(storage_path=tmp_path)

    def no_live_jar() -> httpx.Cookies:
        raise RuntimeError("client is not open")

    async def unused_probe(_client: httpx.AsyncClient, _authuser: int) -> str | None:
        raise AssertionError("probe must not run")

    resolved = await account_email.resolve_account_email(
        auth=auth,
        cached_email=None,
        cached_key=None,
        live_fallback=False,
        get_cookies=no_live_jar,
        get_http_client=httpx.AsyncClient,
        probe=unused_probe,
    )
    assert resolved[0] is None

    resolved = await account_email.resolve_account_email(
        auth=auth,
        cached_email=None,
        cached_key=None,
        live_fallback=False,
        get_cookies=lambda: _jar(),
        get_http_client=httpx.AsyncClient,
        probe=unused_probe,
    )
    assert resolved[0] is None


def test_account_email_rejects_a_matching_cookie_set_for_the_wrong_route(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps(_document(authuser=2).to_json()), encoding="utf-8")

    assert (
        account_email._read_matching_account_heal_document(
            path,
            expected_cookies=CookieJar.from_httpx(_jar()),
            expected_authuser=1,
        )
        is None
    )


async def test_account_email_bounds_transport_error_churn() -> None:
    auth = _auth()

    async def churn_then_fail(_client: httpx.AsyncClient, _authuser: int) -> str | None:
        auth._profile_session_generation += 1
        raise httpx.ConnectError("offline")

    result = await account_email.resolve_account_email(
        auth=auth,
        cached_email=None,
        cached_key=None,
        live_fallback=True,
        get_cookies=lambda: _jar(),
        get_http_client=httpx.AsyncClient,
        probe=churn_then_fail,
    )

    assert result == (None, None, (2, 0, None))


async def test_account_email_discards_a_probe_result_when_session_changes_during_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps(_document().to_json()), encoding="utf-8")
    auth = _auth(storage_path=path)

    async def probe(_client: httpx.AsyncClient, _authuser: int) -> str | None:
        return "old@example.com"

    async def churn_during_write(*args: object, **kwargs: object) -> object:
        auth._profile_session_generation += 1
        return True

    result = await account_email.resolve_account_email(
        auth=auth,
        cached_email=None,
        cached_key=None,
        live_fallback=True,
        get_cookies=lambda: _jar(),
        get_http_client=httpx.AsyncClient,
        probe=probe,
        to_thread=churn_during_write,
    )

    assert result == (None, None, (2, 0, None))


def test_account_email_rechecks_cookie_and_route_state_after_legacy_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _document()
    changed_cookies = ProfileDocument.decode(
        {"cookies": [_row("SID", "other"), _row("__Secure-1PSIDTS", "ts")]}
    )
    promoted = ProfileDocument.decode(
        {
            "cookies": [_row("SID", "sid"), _row("__Secure-1PSIDTS", "ts")],
            "notebooklm": {"account": {"authuser": 3, "email": "promoted@example.com"}},
        }
    )
    documents: Iterator[ProfileDocument] = iter([first, changed_cookies])

    class SequencedStore:
        def __init__(self, _path: Path) -> None:
            pass

        def read_document(self) -> ProfileDocument:
            return next(documents)

    monkeypatch.setattr(ProfileStore, "read_document", SequencedStore.read_document)
    monkeypatch.setattr(
        account_email.LegacyAccountContext,
        "read",
        lambda _self, _path: None,
    )
    path = tmp_path / "storage_state.json"
    expected = CookieJar.from_httpx(_jar())

    assert (
        account_email._read_matching_account_heal_document(
            path,
            expected_cookies=expected,
            expected_authuser=0,
        )
        is None
    )

    documents = iter([first, promoted])
    matched = account_email._read_matching_account_heal_document(
        path,
        expected_cookies=expected,
        expected_authuser=3,
    )
    assert matched is not None
    assert matched[1] == "promoted@example.com"


def test_master_token_persistence_facade_forwards_all_policy_flags(
    tmp_path: Path,
) -> None:
    jar = _jar()
    path = tmp_path / "ownerless.json"
    path.write_text(json.dumps({"cookies": [_row("OLD", "old")]}), encoding="utf-8")
    master_token.persist_minted_jar(
        path,
        jar,
        email="agent@example.com",
        force=False,
        refuse_unknown_owner=False,
    )

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["notebooklm"]["account"] == {
        "authuser": 0,
        "email": "agent@example.com",
    }
    assert {row["name"] for row in persisted["cookies"]} == {
        "SID",
        "__Secure-1PSIDTS",
    }

    path = tmp_path / "different-owner.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [_row("OLD", "old")],
                "notebooklm": {
                    "version": 1,
                    "account": {"authuser": 0, "email": "previous@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    master_token.persist_minted_jar(
        path,
        jar,
        email="forced@example.com",
        force=True,
        refuse_unknown_owner=True,
    )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["notebooklm"]["account"]["email"] == "forced@example.com"


async def test_master_token_default_verifier_uses_the_client_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    class FakeClient:
        async def __aenter__(self):
            seen.append("enter")
            return self

        async def __aexit__(self, *args: object) -> None:
            seen.append("exit")

    async def list_notebooks() -> list[int]:
        return [1, 2, 3]

    fake_client = FakeClient()
    fake_client.notebooks = SimpleNamespace(list=list_notebooks)

    class FakeNotebookLMClient:
        @classmethod
        def from_storage(cls, *, path: str) -> FakeClient:
            seen.append(path)
            return fake_client

    monkeypatch.setattr(client_module, "NotebookLMClient", FakeNotebookLMClient)
    storage_path = tmp_path / "storage_state.json"

    assert await master_token._verify_by_listing_notebooks(storage_path) == 3
    assert seen == [str(storage_path), "enter", "exit"]


async def test_master_token_bootstrap_translates_kernel_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_bootstrap_storage(self: object, *, strict_loader: object):
        del self, strict_loader
        raise master_token._BootstrapError("stored token is invalid")

    monkeypatch.setattr(
        master_token.MasterTokenBootstrapper,
        "bootstrap_storage",
        fail_bootstrap_storage,
    )

    with pytest.raises(MasterTokenError, match="stored token is invalid"):
        await master_token.bootstrap_storage_from_master_token(tmp_path / "storage_state.json")


async def test_master_token_adapters_replace_active_context_with_explicit_cause(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def raise_translated(*args: object, **kwargs: object) -> None:
        raise master_token._BootstrapError("translated") from ValueError("explicit cause")

    async def raise_translated_async(*args: object, **kwargs: object):
        raise_translated()

    monkeypatch.setattr(
        master_token.MasterTokenBootstrapper,
        "assert_account_writable",
        raise_translated,
    )
    monkeypatch.setattr(
        master_token.MasterTokenBootstrapper,
        "bootstrap_from_oauth_token",
        raise_translated_async,
    )
    monkeypatch.setattr(
        master_token.MasterTokenBootstrapper,
        "remint_from_stored_token",
        raise_translated_async,
    )
    monkeypatch.setattr(
        master_token.MasterTokenBootstrapper,
        "bootstrap_storage",
        raise_translated_async,
    )
    path = tmp_path / "storage_state.json"
    errors: list[MasterTokenError] = []

    try:
        raise RuntimeError("active caller error")
    except RuntimeError:
        with pytest.raises(MasterTokenError) as exc_info:
            master_token.assert_account_writable(email="agent@example.com", storage_path=path)
        errors.append(exc_info.value)
        with pytest.raises(MasterTokenError) as exc_info:
            await master_token.bootstrap_from_oauth_token(
                email="agent@example.com",
                oauth_token="single-use",
                storage_path=path,
            )
        errors.append(exc_info.value)
        with pytest.raises(MasterTokenError) as exc_info:
            await master_token.remint_from_stored_token(path)
        errors.append(exc_info.value)
        with pytest.raises(MasterTokenError) as exc_info:
            await master_token.bootstrap_storage_from_master_token(path)
        errors.append(exc_info.value)

    assert all(isinstance(error.__cause__, ValueError) for error in errors)
    assert "notebooklm" not in master_token.storage_state_from_jar(_jar())


def test_profile_store_value_guards_reject_untrusted_shapes() -> None:
    with pytest.raises(TypeError, match="disposition"):
        CookieMergeResult("invalid", False, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="fields are invalid"):
        CookieMergeResult(
            CookieMergeDisposition.CONFLICT,
            advances_ordering=True,
            committed=False,
            next_baseline=CookieJar(),
            rejected=frozenset({object()}),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="inconsistent"):
        CookieMergeResult(
            CookieMergeDisposition.CONFLICT,
            advances_ordering=True,
            committed=False,
            next_baseline=CookieJar(),
        )
    with pytest.raises(TypeError, match="present_names"):
        ReplaceResult(ReplaceStatus.APPLIED, present_names=["SID"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="backup_path"):
        ReplaceResult(ReplaceStatus.APPLIED, backup_path="backup")  # type: ignore[arg-type]

    malformed_set = SetAccount.__new__(SetAccount)
    object.__setattr__(malformed_set, "record", object())
    with pytest.raises(TypeError, match="set account record"):
        LoginWriteRequest(_document(), DomainSelection(), malformed_set)


@pytest.mark.parametrize("operation", ["remint", "mint", "read_account", "clear_account"])
def test_profile_store_propagates_non_root_structure_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text("{}", encoding="utf-8")
    store = ProfileStore(path)
    failure = _ProfileDocumentStructureError(field="cookies", reason="not_list")

    def fail_read() -> ProfileDocument:
        raise failure

    monkeypatch.setattr(store, "read_document", fail_read)
    with pytest.raises(_ProfileDocumentStructureError) as exc_info:
        if operation == "remint":
            store.replace_from_remint(RemintWriteRequest(_document(), carry_account=True))
        elif operation == "mint":
            store.replace_minted_session(MintedSessionWriteRequest(CookieJar(), email=None))
        elif operation == "read_account":
            store.read_account()
        else:
            store.clear_account()
    assert exc_info.value is failure


def test_profile_store_account_cas_handles_missing_and_corrupt_documents(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    store = ProfileStore(path)
    expected = _document()
    record = ProfileAccount(0, "agent@example.com")

    assert store._update_account_if_document_unchanged(record, expected=expected) is False
    path.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="is corrupted"):
        store._update_account_if_document_unchanged(record, expected=expected)

    path.write_text(json.dumps(expected.to_json()), encoding="utf-8")
    assert store._update_account_if_document_unchanged(
        ProfileAccount(4, None),
        expected=expected,
    )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["notebooklm"]["account"] == {"authuser": 4}

    namespaced = ProfileDocument.decode(
        {
            **expected.to_json(),
            "notebooklm": {"version": 1, "future": "preserved"},
        }
    )
    path.write_text(json.dumps(namespaced.to_json()), encoding="utf-8")
    assert store._update_account_if_document_unchanged(
        ProfileAccount(5, "next@example.com"),
        expected=namespaced,
    )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["notebooklm"]["account"] == {
        "authuser": 5,
        "email": "next@example.com",
    }
    assert persisted["notebooklm"]["future"] == "preserved"


def test_profile_store_legacy_commit_failure_and_compat_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    path.mkdir()
    store = ProfileStore(path)
    monkeypatch.setattr(store, "read_document", _document)

    result = store.merge_legacy_cookie_observation(
        CookieJar((Cookie("SID", ".google.com", "/", "fresh"),))
    )
    assert result.disposition is CookieMergeDisposition.HARD_FAILURE
    assert result.committed is None

    assert (
        profile_store.in_storage_transaction(
            path,
            lambda: "done",
            log_prefix="test",
            on_unavailable=lambda _path: "unavailable",
        )
        == "done"
    )


def test_psidts_row_filters_reject_malformed_and_unusable_rows() -> None:
    assert psidts_recovery._bounded_row_field(42, "name") == "int"
    assert psidts_recovery._try_cookie({"not": "a cookie"}, lambda entry: entry) is None

    bad_psidts = _row("__Secure-1PSIDTS", "ts")

    def unusable(_entry: dict[str, Any]):
        raise ValueError("bad expiry")

    assert (
        list(
            psidts_recovery._iter_routable_psidts_cookies(
                [bad_psidts],
                to_cookie=unusable,
            )
        )
        == []
    )
    assert len(psidts_recovery._build_recovery_jar([bad_psidts], unusable)) == 0
    assert psidts_recovery._recovery_observation([None]) == cookie_merge.RecoveryObservation({})


def test_psidts_inline_fresh_state_declines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    initial = ProfileDocument.decode({"cookies": [_row("SID", "sid"), _row("OSID", "osid")]})
    path.write_text(json.dumps(initial.to_json()), encoding="utf-8")
    reads = 0

    def disappear_after_first_read(_self: ProfileStore) -> ProfileDocument:
        nonlocal reads
        reads += 1
        if reads == 1:
            return initial
        raise OSError("profile disappeared")

    monkeypatch.setattr(ProfileStore, "read_document", disappear_after_first_read)
    assert psidts_recovery._recover_psidts_inline(path) is False
    assert psidts_recovery._is_psidts_routed_on_disk(tmp_path / "missing.json") is False


def test_psidts_inline_declines_incomplete_binding_after_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    initial = ProfileDocument.decode({"cookies": [_row("SID", "sid"), _row("OSID", "osid")]})
    incomplete = ProfileDocument.decode({"cookies": [_row("SID", "sid")]})
    samples = iter([initial, incomplete])
    path.write_text(json.dumps(initial.to_json()), encoding="utf-8")
    monkeypatch.setattr(ProfileStore, "read_document", lambda _self: next(samples))

    assert psidts_recovery._recover_psidts_inline(path) is False


@pytest.mark.no_default_keepalive_mock
def test_psidts_persistence_exception_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    path = tmp_path / "storage_state.json"
    entries = [_row("SID", "sid"), _row("OSID", "osid")]
    httpx_mock.add_response(
        url="https://accounts.google.com/RotateCookies",
        headers={
            "Set-Cookie": ("__Secure-1PSIDTS=fresh; Domain=.google.com; Path=/; Secure; HttpOnly")
        },
    )

    def fail_merge(self: ProfileStore, *args: object, **kwargs: object):
        raise OSError("disk full")

    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", fail_merge)

    assert psidts_recovery._attempt_rotation(path, entries) is False


@pytest.mark.no_default_keepalive_mock
def test_psidts_in_memory_collapses_duplicate_rotated_identity(
    httpx_mock: HTTPXMock,
) -> None:
    rows = [
        {"malformed": True},
        _row("SID", "sid"),
        _row("OSID", "osid"),
        _row("__Secure-3PSIDTS", "old-a"),
        _row("__Secure-3PSIDTS", "old-b"),
    ]
    httpx_mock.add_response(
        url="https://accounts.google.com/RotateCookies",
        headers=[
            (
                "Set-Cookie",
                "__Secure-1PSIDTS=fresh; Domain=.google.com; Path=/; Secure; HttpOnly",
            ),
            (
                "Set-Cookie",
                "__Secure-3PSIDTS=fresh; Domain=.google.com; Path=/; Secure; HttpOnly",
            ),
            ("Set-Cookie", "LSID=; Domain=.google.com; Path=/; Secure; HttpOnly"),
        ],
    )

    assert psidts_recovery.recover_psidts_in_memory(rows) is True
    surviving = [row for row in rows if row.get("name") == "__Secure-3PSIDTS"]
    assert len(surviving) == 1
    assert surviving[0]["value"] == "fresh"


async def test_recovery_result_guard_and_storage_install_race_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="fields are invalid"):
        recovery.ColdRecoveryResult(object(), {}, CookieJar())  # type: ignore[arg-type]

    async def install_none(*args: object) -> None:
        return None

    async def load_same(_path: Path) -> _LoadedProfilePair:
        return _LoadedProfilePair(cookies._LoadedCookiePair(_jar(), CookieJar()), None)

    assert await recovery.try_storage_cookie_reload(
        storage_path=tmp_path / "storage_state.json",
        cookie_jar=_jar(),
        load_profile_pair=load_same,
        install_profile=install_none,
    )

    async def load_different(_path: Path) -> _LoadedProfilePair:
        return _LoadedProfilePair(cookies._LoadedCookiePair(_jar("fresh"), CookieJar()), None)

    assert await recovery.try_storage_cookie_reload(
        storage_path=tmp_path / "storage_state.json",
        cookie_jar=_jar(),
        load_profile_pair=load_different,
        install_profile=install_none,
    )
    assert recovery._auth_material_changed(rejected=None, live=CookieJar()) is False


async def test_recovery_baseline_and_post_remint_load_failures_are_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_adoption(_path: Path, _baseline: CookieJar) -> None:
        raise OSError("read only")

    await recovery._try_adopt_storage_baseline(
        storage_path=tmp_path / "storage_state.json",
        baseline=CookieJar(),
        adopt_baseline=fail_adoption,
    )

    rung_calls = 0

    def rung(**kwargs: object) -> recovery_rungs.HeadlessRungOutcome:
        nonlocal rung_calls
        rung_calls += 1
        return recovery_rungs.HeadlessRungOutcome(
            recovery_rungs.HeadlessRungStatus.SUCCEEDED,
            "captured",
        )

    previous = recovery_rungs.install_headless_rung(rung)
    try:
        assert (
            await recovery._try_headless_reauth_result(
                storage_path=tmp_path / "storage_state.json",
                allow_headless=True,
            )
            is None
        )
        assert rung_calls == 1
    finally:
        recovery_rungs.install_headless_rung(previous)

    remint_calls = 0

    async def remint(
        _self: object,
        *,
        strict_loader: object,
    ) -> httpx.Cookies:
        nonlocal remint_calls
        del strict_loader
        remint_calls += 1
        return httpx.Cookies()

    monkeypatch.setattr(
        master_token.MasterTokenBootstrapper,
        "remint_from_stored_token",
        remint,
    )
    assert (
        await recovery._run_master_token_reauth(storage_path=tmp_path / "storage_state.json")
        is None
    )
    assert remint_calls == 1


async def test_recovery_cold_driver_tracks_redirects_and_uses_master_adapter(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    state = recovery.ColdRecoveryState()
    state.note_success(path)
    initial_pair = cookies._LoadedCookiePair(_jar("stale"), CookieJar())
    master_pair = cookies._LoadedCookiePair(_jar("master"), CookieJar())
    redirects = iter(
        [
            _LoginRedirectError("cached generation rejected"),
            _LoginRedirectError("master generation rejected"),
        ]
    )

    async def reject(_jar: httpx.Cookies) -> None:
        raise next(redirects)

    async def decline_headless(
        _path: Path | None = None,
        _allowed: bool | None = None,
        **kwargs: object,
    ):
        return None

    async def use_master(_path: Path | None = None, **kwargs: object):
        return master_pair

    with pytest.raises(_LoginRedirectError, match="master generation rejected"):
        await recovery.ColdRecoveryCoordinator._drive_cold(
            state=state,
            storage_path=path,
            allow_headless=False,
            load_cookie_pair=lambda _path: initial_pair,
            run_headless_attempt=decline_headless,
            run_master_token_attempt=use_master,
            validate_recovered=reject,
            snapshot_cookie_jar=lambda _jar: {},
            initial_error=_LoginRedirectError("initial"),
        )

    adapter_path = tmp_path / "adapter-storage.json"
    adapter_path.write_text(json.dumps(_document().to_json()), encoding="utf-8")
    previous = recovery_rungs.install_headless_rung(None)
    try:
        with pytest.raises(_LoginRedirectError, match="adapter initial"):
            await recovery._run_cold_recovery(
                storage_path=adapter_path,
                allow_headless=False,
                validate=lambda _jar: asyncio.sleep(0),
                initial_error=_LoginRedirectError("adapter initial"),
            )
    finally:
        recovery_rungs.install_headless_rung(previous)


@contextlib.contextmanager
def _unused_flock(_path: Path):
    raise AssertionError("flock should not be used")
    yield True


async def test_refresh_leader_runs_without_a_derived_lock_path(tmp_path: Path) -> None:
    refresh._single_flight._reset_for_tests()
    calls: list[tuple[Path, str | None]] = []

    async def run(path: Path, profile: str | None) -> None:
        calls.append((path, profile))

    path = tmp_path / "storage_state.json"
    deps = refresh.RefreshCmdDeps(
        run_refresh_cmd=run,
        acquire_refresh_flock=_unused_flock,
        derive_refresh_lock_path=lambda _path: None,
    )
    await refresh._refresh_cmd_leader_body(str(path), path, "work", deps=deps)

    assert calls == [(path, "work")]
    assert refresh._single_flight.read_success_epoch(str(path)) == 1
    refresh._single_flight._reset_for_tests()


async def test_refresh_coalescer_covers_epoch_skip_and_follower_retry_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    monkeypatch.setattr(
        refresh._single_flight.SingleFlight,
        "read_success_epoch",
        lambda _self, _key: 0,
    )
    monkeypatch.setattr(
        refresh._single_flight.SingleFlight,
        "claim_if_epoch_current",
        lambda *args, **kwargs: None,
    )
    await refresh._coalesced_run_refresh_cmd(str(path), path, None)

    claims = 0

    def follower_claim(*args: object, **kwargs: object):
        nonlocal claims
        claims += 1
        return False, object()

    async def failed_flight(_flight: object) -> None:
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(
        refresh._single_flight.SingleFlight,
        "claim_if_epoch_current",
        follower_claim,
    )
    monkeypatch.setattr(
        refresh._single_flight.SingleFlight,
        "await_flight",
        lambda _self, flight: failed_flight(flight),
    )
    with pytest.raises(RuntimeError, match="refresh failed"):
        await refresh._coalesced_run_refresh_cmd(str(path), path, None)
    assert claims == refresh._MAX_REFRESH_FOLLOW_RETRIES


async def test_refresh_coalescer_accepts_a_concurrent_success_epoch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    epochs = iter([0, 1])
    monkeypatch.setattr(
        refresh._single_flight.SingleFlight,
        "read_success_epoch",
        lambda _self, _key: next(epochs),
    )
    monkeypatch.setattr(
        refresh._single_flight.SingleFlight,
        "claim_if_epoch_current",
        lambda *args, **kwargs: (False, object()),
    )

    async def failed_flight(_flight: object) -> None:
        raise RuntimeError("superseded")

    monkeypatch.setattr(
        refresh._single_flight.SingleFlight,
        "await_flight",
        lambda _self, flight: failed_flight(flight),
    )
    await refresh._coalesced_run_refresh_cmd(str(path), path, None)


async def test_refresh_mid_session_propagates_cancel_and_declines_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    monkeypatch.setenv(refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh")
    monkeypatch.setenv(refresh.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV, "1")

    async def cancel(_path: Path, _profile: str | None) -> None:
        raise asyncio.CancelledError()

    cancel_deps = refresh.RefreshCmdDeps(
        run_refresh_cmd=cancel,
        derive_refresh_lock_path=lambda _path: None,
    )
    with pytest.raises(asyncio.CancelledError):
        await refresh.try_refresh_cmd_reauth(
            storage_path=path,
            cookie_jar=_jar(),
            deps=cancel_deps,
        )
    refresh._single_flight._reset_for_tests()

    async def fail(_path: Path, _profile: str | None) -> None:
        raise RuntimeError("failed")

    fail_deps = refresh.RefreshCmdDeps(
        run_refresh_cmd=fail,
        derive_refresh_lock_path=lambda _path: None,
    )
    assert not await refresh.try_refresh_cmd_reauth(
        storage_path=path,
        cookie_jar=_jar(),
        deps=fail_deps,
    )
    refresh._single_flight._reset_for_tests()


def test_refresh_inline_route_metadata_failure_uses_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{")

    assert refresh._resolve_token_route_kwargs(None, authuser=None, account_email=None) == {
        "authuser": 0
    }


def test_storage_lock_snapshot_and_merge_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    @contextlib.contextmanager
    def unavailable_lock(_self: object, _request: object):
        yield LockState.UNAVAILABLE

    warning_claims = iter([True, False])
    monkeypatch.setattr(
        storage.StorageLockManager,
        "acquire",
        unavailable_lock,
    )
    monkeypatch.setattr(
        storage.StorageLockManager,
        "_claim_cookie_warning",
        lambda _self: next(warning_claims),
    )
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        with storage._file_lock_exclusive(tmp_path / ".storage.lock"):
            pass
        with storage._file_lock_exclusive(tmp_path / ".storage.lock"):
            pass
    fallback_warnings = [
        record
        for record in caplog.records
        if "Cross-process file lock unavailable" in record.getMessage()
    ]
    assert len(fallback_warnings) == 1

    assert storage._stored_cookie_snapshot_key({"bad": "row"}) is None
    key = storage._stored_cookie_snapshot_key(_row("SID", "sid"))
    assert key == storage.CookieSnapshotKey("SID", ".google.com", "/")

    payload = {"cookies": []}
    storage._install_decided_document(payload, None)
    assert payload == {"cookies": []}

    identities = (
        CookieIdentity("SID", ".google.com", "/"),
        CookieIdentity("APISID", ".google.com", "/"),
    )
    payload = {
        "cookies": [
            _row("SID", "sibling"),
            _row("APISID", "sibling-api"),
        ]
    }
    live = httpx.Cookies()
    live.set("SID", "local", domain=".google.com", path="/")
    live.set("APISID", "local-api", domain=".google.com", path="/")
    baseline = {
        storage.CookieSnapshotKey("SID", ".google.com", "/"): (
            storage.CookieSnapshotValue("old", None, False, False)
        )
    }
    updated, rejected = storage._merge_cookies_with_snapshot(
        live,
        payload,
        original_snapshot=baseline,
    )
    assert updated == 0
    assert len(rejected) == 2

    snapshot = {
        storage.CookieSnapshotKey(identity.name, identity.domain, identity.path): (
            storage.CookieSnapshotValue("old", None, False, False)
        )
        for identity in identities
    }
    missing_key = storage.CookieSnapshotKey("SSID", ".google.com", "/")
    assert (
        storage.advance_cookie_snapshot_after_save(
            snapshot,
            {},
            frozenset((*snapshot, missing_key)),
        )
        == snapshot
    )


def test_storage_no_path_cookie_merges_are_successful_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    inline = storage.merge_cookie_delta(httpx.Cookies(), return_result=True)
    assert inline == storage.CookieSaveResult(True)

    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON")
    pathless = storage.merge_cookie_delta(httpx.Cookies(), return_result=True)
    assert pathless == storage.CookieSaveResult(True)


async def test_stored_auth_value_guards_and_inline_recovery_decline(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "storage_state.json")
    document = ProfileDocument.decode(
        {
            "cookies": [
                _row("SID", "sid"),
                _row(
                    "__Secure-1PSIDTS",
                    "ts",
                    domain=".notebooklm.google.com",
                ),
            ]
        }
    )
    source = tokens.InlineAuthSource(document)
    migrator = LegacyAccountMigrator()
    promotions = LegacyPromotionScheduler()

    with pytest.raises(TypeError, match="profile must be"):
        tokens.FileAuthSource(store, 3)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="auth must be"):
        tokens.InlineLoadedAuth(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="resolved auth source"):
        await tokens.SessionSeedLoader().load(object(), tokens.LoadPolicy())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="LoadPolicy"):
        await tokens.SessionSeedLoader().load(source, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="resolved auth source"):
        tokens.AccountRouteResolver(
            object(),  # type: ignore[arg-type]
            migrator=migrator,
            promotions=promotions,
        )
    with pytest.raises(TypeError, match="collaborators"):
        tokens.AccountRouteResolver(
            source,
            migrator=object(),  # type: ignore[arg-type]
            promotions=promotions,
        )
    with pytest.raises(TypeError, match="seeds"):
        tokens.StoredAuthLoader(
            seeds=object(),  # type: ignore[arg-type]
            token_acquirer=object(),  # type: ignore[arg-type]
            migrator=migrator,
            promotions=promotions,
        )
    with pytest.raises(TypeError, match="collaborators"):
        tokens.StoredAuthLoader(
            seeds=tokens.SessionSeedLoader(),
            token_acquirer=object(),  # type: ignore[arg-type]
            migrator=object(),  # type: ignore[arg-type]
            promotions=promotions,
        )

    loader = tokens.StoredAuthLoader(
        seeds=tokens.SessionSeedLoader(),
        token_acquirer=object(),  # type: ignore[arg-type]
        migrator=migrator,
        promotions=promotions,
    )
    with pytest.raises(TypeError, match="load arguments"):
        await loader.load(
            path=None,
            profile=None,
            policy=object(),  # type: ignore[arg-type]
            auth_type=tokens.AuthTokens,
        )

    seed = await tokens.SessionSeedLoader().load(source, tokens.LoadPolicy())
    assert seed.live.get("SID", domain=".google.com") == "sid"
