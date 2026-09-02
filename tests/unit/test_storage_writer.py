"""Unit tests for the canonical storage writers (refactor (b), b-PR1).

The writers were absorbed into ``_auth/storage.py`` by ADR-0033's persistence
merge; this suite drives them there (``storage_mod``).

Covers the relocated intent-shaped API's per-intent lock-failure policy and the
value-free outcome contract. The CAS ``merge_cookie_delta`` adapter and its
``ProfileStore`` transaction are exercised by the cookie save-race suite via
the ``save_cookies_to_storage`` delegate and are not re-tested here.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import pytest

from notebooklm._auth import master_token as mt_mod
from notebooklm._auth import master_token_file, profile_store
from notebooklm._auth import storage as storage_mod
from notebooklm._auth.profile_account import DomainSelection
from notebooklm._auth.profile_document import ProfileDocument
from notebooklm._auth.profile_store import ReplaceResult, ReplaceStatus
from notebooklm._auth.storage_lock import LockState


class _UnavailableLocks:
    @contextlib.contextmanager
    def acquire(self, request):
        yield LockState.UNAVAILABLE


def _patch_lock_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force bounded writer transactions to see infrastructure failure."""
    monkeypatch.setattr(profile_store, "_STORAGE_LOCKS", _UnavailableLocks())


def _patch_master_token_lock_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the path-owned token writer to use an unavailable manager."""
    monkeypatch.setattr(
        master_token_file.StorageLockManager,
        "process_default",
        lambda: _UnavailableLocks(),
    )


# --- value-free outcome contract -------------------------------------------


def test_write_outcome_is_value_free() -> None:
    """``WriteOutcome`` carries only an enum status — never any payload."""
    ok = storage_mod.WriteOutcome(storage_mod.WriteStatus.OK)
    bad = storage_mod.WriteOutcome(storage_mod.WriteStatus.LOCK_UNAVAILABLE)
    assert ok.ok and not ok.lock_unavailable
    assert bad.lock_unavailable and not bad.ok
    # A sentinel secret must never be able to reach repr/str (there is no field
    # to carry it — this pins that contract).
    assert "SENTINEL" not in repr(ok)
    for outcome in (ok, bad):
        assert set(vars(outcome)) == {"status"}


# --- update_account_metadata: full-file RMW, fails CLOSED -------------------


def test_update_account_metadata_writes_in_band(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    storage_mod.update_account_metadata(path, authuser=2, email="a@example.com")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"] == {
        "version": 1,
        "account": {"authuser": 2, "email": "a@example.com"},
    }


def test_update_account_metadata_only_if_absent_skips_when_already_present(
    tmp_path: Path,
) -> None:
    """#2103 PR-0 review: ``only_if_absent`` re-checks under the SAME lock as
    the write, closing the check-then-act race where an unlocked caller
    (``account.promote_legacy_account``) decided to write stale legacy values
    before a concurrent fresh login/account-switch committed a different
    record in the gap. Simulates the race deterministically: the "winner"
    write completes fully, THEN the "loser" (stale, ``only_if_absent=True``)
    write is attempted — it must be a no-op, never overwriting the winner."""
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    # The concurrent fresh write that "wins" the race.
    storage_mod.update_account_metadata(path, authuser=0, email="new@example.com")

    # The belated stale write a slow promoter attempts — must be rejected.
    wrote = storage_mod.update_account_metadata(
        path, authuser=2, email="old@example.com", only_if_absent=True
    )
    assert wrote is False

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"] == {"authuser": 0, "email": "new@example.com"}


def test_update_account_metadata_only_if_absent_writes_when_empty(tmp_path: Path) -> None:
    """The other half: ``only_if_absent`` still writes when nothing raced in —
    the normal (non-contended) promotion path."""
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    wrote = storage_mod.update_account_metadata(
        path, authuser=3, email="x@example.com", only_if_absent=True
    )

    assert wrote is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"] == {"authuser": 3, "email": "x@example.com"}


def test_update_account_metadata_fails_closed_on_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    _patch_lock_unavailable(monkeypatch)
    with pytest.raises(storage_mod.LockUnavailableError):
        storage_mod.update_account_metadata(path, authuser=1, email="a@example.com")
    # Fail-closed: the file must be untouched (no partial account write).
    assert "notebooklm" not in json.loads(path.read_text(encoding="utf-8"))


# --- clear_in_band_account: best-effort, swallows lock unavailability -------


def test_clear_in_band_account_swallows_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "storage_state.json"
    storage_mod.update_account_metadata(path, authuser=1, email="a@example.com")  # seed a record
    _patch_lock_unavailable(monkeypatch)
    # Best-effort: no raise, and the record is left intact.
    storage_mod.clear_in_band_account(path)
    assert "notebooklm" in json.loads(path.read_text(encoding="utf-8"))


# --- replace_from_remint: browser-capture re-mint (b-PR2) ------------------


def _captured_state() -> dict:
    """A minimal captured storage-state dict (auth cookies on ``.google.com``)."""
    return {
        "cookies": [
            {"name": "SID", "value": "v", "domain": ".google.com", "path": "/"},
            {"name": "SAPISID", "value": "s", "domain": ".google.com", "path": "/"},
        ],
        "origins": [],
    }


def test_replace_from_remint_carry_account_preserves_namespace(tmp_path: Path) -> None:
    """[capture-1] regression: an unattended (carry_account=True) re-mint keeps
    the pre-existing ``notebooklm`` account namespace. Pre-b-PR2 the bare
    ``atomic_write_json`` re-mint dropped it, misrouting the account."""
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [{"name": "OLD", "value": "x", "domain": ".google.com"}],
                "origins": [],
                "notebooklm": {"version": 1, "account": {"authuser": 3, "email": "keep@x.com"}},
            }
        ),
        encoding="utf-8",
    )
    outcome = storage_mod.replace_from_remint(path, _captured_state(), carry_account=True)
    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    # Cookies replaced (not merged) …
    assert {c["name"] for c in data["cookies"]} == {"SID", "SAPISID"}
    # … and the account binding survived the re-mint.
    assert data["notebooklm"] == {"version": 1, "account": {"authuser": 3, "email": "keep@x.com"}}


def test_replace_from_remint_no_carry_drops_stale_binding(tmp_path: Path) -> None:
    """The interactive arm (carry_account=False) drops the stale binding — the
    user may have signed into a different account; the CLI adapter's repair
    re-establishes it."""
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [{"name": "OLD", "value": "x", "domain": ".google.com"}],
                "origins": [],
                "notebooklm": {"version": 1, "account": {"authuser": 3, "email": "stale@x.com"}},
            }
        ),
        encoding="utf-8",
    )
    outcome = storage_mod.replace_from_remint(path, _captured_state(), carry_account=False)
    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {c["name"] for c in data["cookies"]} == {"SID", "SAPISID"}
    assert data["notebooklm"] == {
        "version": 1,
        "account_route_cleared": True,
    }  # stale binding dropped and cannot be resurrected from legacy context


def test_replace_from_remint_takes_storage_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[capture-2] lock-contract: the re-mint write serializes on the storage
    lock and **fails closed** (no write) when the lock is unavailable, instead of
    racing a concurrent keepalive with a lockless write."""
    path = tmp_path / "storage_state.json"
    _patch_lock_unavailable(monkeypatch)
    outcome = storage_mod.replace_from_remint(path, _captured_state(), carry_account=True)
    assert outcome.lock_unavailable
    assert not path.exists()  # nothing written without the lock


def test_replace_from_remint_filters_domains_but_keeps_trusted_subdomains(
    tmp_path: Path,
) -> None:
    """The write-time filter runs INSIDE the writer: an unallowlisted-domain
    cookie never reaches disk, while trusted Google subdomains
    (``*.googleusercontent.com`` / ``drive.google.com``) survive
    (main's preserve-trusted-roots behavior)."""
    path = tmp_path / "storage_state.json"
    captured = {
        "cookies": [
            {"name": "SID", "value": "v", "domain": ".google.com", "path": "/"},
            {"name": "MEDIA", "value": "m", "domain": "lh3.googleusercontent.com", "path": "/"},
            {"name": "DRV", "value": "d", "domain": "drive.google.com", "path": "/"},
            # Unallowlisted sibling-product cookie — must be dropped.
            {"name": "YT", "value": "y", "domain": ".youtube.com", "path": "/"},
        ],
        "origins": [],
    }
    outcome = storage_mod.replace_from_remint(path, captured, carry_account=False)
    assert outcome.ok
    names = {c["name"] for c in json.loads(path.read_text(encoding="utf-8"))["cookies"]}
    assert "YT" not in names  # unallowlisted domain filtered out at the chokepoint
    assert {"SID", "MEDIA", "DRV"} <= names  # trusted Google roots preserved


@pytest.mark.parametrize(
    ("typed_status", "compatibility_status"),
    [
        (ReplaceStatus.APPLIED, storage_mod.WriteStatus.OK),
        (ReplaceStatus.LOCK_UNAVAILABLE, storage_mod.WriteStatus.LOCK_UNAVAILABLE),
    ],
)
def test_replace_from_remint_is_one_typed_store_delegation_and_exact_legacy_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    typed_status: ReplaceStatus,
    compatibility_status: storage_mod.WriteStatus,
) -> None:
    path = tmp_path / "custom.json"
    state = _captured_state()
    seen: list[object] = []

    class FakeStore:
        def __init__(self, actual_path: Path) -> None:
            seen.append(actual_path)

        def replace_from_remint(self, request: object) -> ReplaceResult:
            seen.append(request)
            return ReplaceResult(typed_status)

    monkeypatch.setattr(storage_mod, "ProfileStore", FakeStore)
    outcome = storage_mod.replace_from_remint(
        path,
        state,
        carry_account="truthy",  # type: ignore[arg-type]
        include_domains={"mail"},
    )

    assert set(storage_mod._REMINT_RESULT_PROJECTORS) == set(ReplaceStatus)
    assert type(outcome) is storage_mod.WriteOutcome
    assert outcome == storage_mod.WriteOutcome(compatibility_status)
    assert seen[0] is path
    request = seen[1]
    assert isinstance(request.source, ProfileDocument)
    assert request.source.to_json() == state
    assert request.carry_account == "truthy"
    assert request.domain_selection == DomainSelection(frozenset({"mail"}), False)


def test_replace_from_remint_impossible_status_uses_named_contract_violation() -> None:
    result = ReplaceResult(
        ReplaceStatus.REQUIRED_COOKIES_DROPPED,
        missing_required=("SID",),
    )
    with pytest.raises(AssertionError, match="impossible required-cookie status"):
        storage_mod._REMINT_RESULT_PROJECTORS[result.status](result)


def test_native_projection_maps_cover_every_status_and_cookie_disposition_exactly() -> None:
    assert set(storage_mod._REMINT_RESULT_PROJECTORS) == set(ReplaceStatus)
    assert set(storage_mod._LOGIN_RESULT_PROJECTORS) == set(ReplaceStatus)
    assert storage_mod._COOKIE_MERGE_OK_BY_DISPOSITION == {
        profile_store.CookieMergeDisposition.APPLIED: True,
        profile_store.CookieMergeDisposition.NO_CHANGE: True,
        profile_store.CookieMergeDisposition.CONFLICT: False,
        profile_store.CookieMergeDisposition.HARD_FAILURE: False,
    }


# --- replace_from_login: CLI login / import full-replace (b-PR3) -----------


def _login_state(extra: list[dict] | None = None) -> dict:
    """A captured browser state with the required cookies on ``.google.com``."""
    cookies = [
        {"name": "SID", "value": "s", "domain": ".google.com", "path": "/"},
        {"name": "__Secure-1PSIDTS", "value": "p", "domain": ".google.com", "path": "/"},
    ]
    if extra:
        cookies.extend(extra)
    return {"cookies": cookies, "origins": []}


def test_login_write_outcome_is_value_free(tmp_path: Path) -> None:
    """``LoginWriteOutcome`` never carries a cookie VALUE (only names/keys)."""
    # Drive the required-cookies-dropped path with a sentinel-bearing value: the
    # only SID copy sits on a non-allowlisted domain and is dropped.
    path = tmp_path / "storage_state.json"
    state = {
        "cookies": [
            {"name": "SID", "value": "SENTINEL-SECRET", "domain": ".youtube.com", "path": "/"},
            {"name": "__Secure-1PSIDTS", "value": "SENTINEL-SECRET", "domain": ".google.com"},
        ],
        "origins": [],
    }
    outcome = storage_mod.replace_from_login(path, state, include_domains=None)
    assert outcome.required_cookies_dropped
    assert "SENTINEL-SECRET" not in repr(outcome)
    assert not path.exists()


def test_replace_from_login_filters_and_records_include_domains(tmp_path: Path) -> None:
    """Login write: filter runs inside the writer, account is embedded, and the
    opt-in ``include_domains`` set is recorded in the ``notebooklm`` namespace."""
    path = tmp_path / "storage_state.json"
    state = _login_state(
        [
            {"name": "YT", "value": "y", "domain": ".youtube.com", "path": "/"},
            {"name": "MEDIA", "value": "m", "domain": "lh3.googleusercontent.com", "path": "/"},
        ]
    )
    outcome = storage_mod.replace_from_login(
        path,
        state,
        include_domains={"youtube"},
        account=storage_mod.AccountRecord(authuser=2, email="a@example.com"),
    )
    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {c["name"] for c in data["cookies"]}
    assert {
        "SID",
        "__Secure-1PSIDTS",
        "YT",
        "MEDIA",
    } <= names  # youtube opted in, trusted root kept
    ns = data["notebooklm"]
    assert ns["account"] == {"authuser": 2, "email": "a@example.com"}
    assert ns["include_domains"] == ["youtube"]
    assert ns["version"] == 1


def test_replace_from_login_default_drops_youtube_keeps_trusted_roots(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    state = _login_state(
        [
            {"name": "YT", "value": "y", "domain": ".youtube.com", "path": "/"},
            {"name": "MEDIA", "value": "m", "domain": "lh3.googleusercontent.com", "path": "/"},
        ]
    )
    outcome = storage_mod.replace_from_login(
        path, state, include_domains=None, account=storage_mod.CLEAR_ACCOUNT
    )
    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {c["name"] for c in data["cookies"]}
    assert "YT" not in names
    assert {"SID", "MEDIA"} <= names
    assert data["notebooklm"] == {
        "version": 1,
        "account_route_cleared": True,
    }


def test_replace_from_login_required_dropped_writes_nothing(tmp_path: Path) -> None:
    """A required cookie whose only copy sits on a filtered domain => the writer
    returns REQUIRED_COOKIES_DROPPED and writes NOTHING (#2086 contract)."""
    path = tmp_path / "storage_state.json"
    state = {
        "cookies": [
            {"name": "SID", "value": "s", "domain": ".youtube.com", "path": "/"},
            {"name": "__Secure-1PSIDTS", "value": "p", "domain": ".google.com", "path": "/"},
        ],
        "origins": [],
    }
    outcome = storage_mod.replace_from_login(path, state, include_domains=None)
    assert outcome.required_cookies_dropped
    assert outcome.missing_required == ("SID",)
    assert "__Secure-1PSIDTS" in outcome.present_names
    assert not path.exists()  # nothing written


def test_replace_from_login_keep_account_carries_input_namespace(tmp_path: Path) -> None:
    """KEEP_ACCOUNT (the import default) carries whatever the input state holds —
    import states carry none, so the result carries none."""
    path = tmp_path / "storage_state.json"
    outcome = storage_mod.replace_from_login(
        path, _login_state(), include_domains=None
    )  # default KEEP
    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "notebooklm" not in data  # KEEP + no opt-ins + input had no namespace


def test_replace_from_login_keep_account_promotes_legacy_instead_of_destroying_it(
    tmp_path: Path,
) -> None:
    """BLOCKING regression (#2103 PR-0 review): KEEP_ACCOUNT with nothing to
    carry is NOT an intentional "no account" decision (unlike CLEAR_ACCOUNT) —
    it means the caller (a fresh browser/import jar) never considered the
    account question. Before this fix, ``replace_from_login`` scrubbed the
    legacy sibling ``context.json[account]`` unconditionally after every
    write, so ``notebooklm auth import-cookies`` on a pre-v0.5.0 profile
    PERMANENTLY DESTROYED its only copy of the account binding: nothing was
    embedded in-band (KEEP_ACCOUNT carried the import jar's empty namespace),
    and the legacy record was gone from disk afterward — irrecoverable, not
    even by the ``read_account_metadata`` self-heal (there is nothing left to
    heal from). Must promote the legacy record in-band instead of scrubbing
    it blind."""
    path = tmp_path / "storage_state.json"
    context_path = path.with_name("context.json")
    context_path.write_text(
        json.dumps(
            {
                "account": {"authuser": 3, "email": "legacy@example.com"},
                "notebook_id": "nb-preserved",
            }
        ),
        encoding="utf-8",
    )

    outcome = storage_mod.replace_from_login(
        path, _login_state(), include_domains=None
    )  # default KEEP

    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"] == {"authuser": 3, "email": "legacy@example.com"}
    # Legacy account key scrubbed (promoted, not merely destroyed); other
    # legacy context state preserved.
    context_data = json.loads(context_path.read_text(encoding="utf-8"))
    assert "account" not in context_data
    assert context_data.get("notebook_id") == "nb-preserved"


def test_replace_from_login_clear_account_does_not_resurrect_legacy_binding(
    tmp_path: Path,
) -> None:
    """CLEAR_ACCOUNT is the one intentional "no account" decision — unlike
    KEEP_ACCOUNT-with-nothing-to-carry, promoting the legacy record here would
    resurrect a binding the caller just deliberately cleared."""
    path = tmp_path / "storage_state.json"
    path.with_name("context.json").write_text(
        json.dumps({"account": {"authuser": 3, "email": "legacy@example.com"}}),
        encoding="utf-8",
    )

    outcome = storage_mod.replace_from_login(
        path, _login_state(), include_domains=None, account=storage_mod.CLEAR_ACCOUNT
    )

    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "notebooklm" not in data or "account" not in data.get("notebooklm", {})


def test_replace_from_login_import_backup_inside_lock(tmp_path: Path) -> None:
    """The import flavour takes a pre-overwrite ``.bak`` copy (0600) of an existing
    target inside the lock and returns its path."""
    import sys

    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [{"name": "OLD"}], "origins": []}), encoding="utf-8")
    outcome = storage_mod.replace_from_login(
        path, _login_state(), include_domains=None, include_optional=True, backup=True
    )
    assert outcome.ok
    assert outcome.backup_path == path.with_name("storage_state.json.bak")
    assert outcome.backup_path.exists()
    assert json.loads(outcome.backup_path.read_text())["cookies"] == [{"name": "OLD"}]
    if sys.platform != "win32":
        assert (outcome.backup_path.stat().st_mode & 0o777) == 0o600
    # include_optional recorded in the namespace.
    assert json.loads(path.read_text())["notebooklm"]["include_optional"] is True


def test_replace_from_login_fails_closed_on_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "storage_state.json"
    _patch_lock_unavailable(monkeypatch)
    outcome = storage_mod.replace_from_login(path, _login_state(), include_domains=None)
    assert outcome.lock_unavailable
    assert not path.exists()  # nothing written without the lock


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            ReplaceResult(ReplaceStatus.APPLIED, backup_path=Path("A.json.bak")),
            storage_mod.LoginWriteOutcome(
                storage_mod.LoginWriteStatus.OK,
                backup_path=Path("A.json.bak"),
            ),
        ),
        (
            ReplaceResult(ReplaceStatus.LOCK_UNAVAILABLE),
            storage_mod.LoginWriteOutcome(storage_mod.LoginWriteStatus.LOCK_UNAVAILABLE),
        ),
        (
            ReplaceResult(
                ReplaceStatus.REQUIRED_COOKIES_DROPPED,
                missing_required=("A", "Z"),
                present_names=("B",),
            ),
            storage_mod.LoginWriteOutcome(
                storage_mod.LoginWriteStatus.REQUIRED_COOKIES_DROPPED,
                missing_required=("A", "Z"),
                present_names=("B",),
            ),
        ),
    ],
)
def test_replace_from_login_is_one_typed_store_delegation_and_exhaustive_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: ReplaceResult,
    expected: storage_mod.LoginWriteOutcome,
) -> None:
    path = tmp_path / "custom.json"
    state = _login_state()
    calls: list[tuple[object, ...]] = []

    def native(*args: object, **kwargs: object) -> ReplaceResult:
        calls.append((*args, kwargs))
        return result

    monkeypatch.setattr(storage_mod, "replace_profile_from_login", native)

    outcome = storage_mod.replace_from_login(
        path,
        state,
        include_domains={"mail"},
        include_optional="truthy",  # type: ignore[arg-type]
        account=storage_mod.CLEAR_ACCOUNT,
        backup="truthy",  # type: ignore[arg-type]
        io_policy=object(),
    )

    assert outcome == expected
    assert calls == [
        (
            path,
            state,
            {
                "include_domains": {"mail"},
                "include_optional": "truthy",
                "account_mode": "clear",
                "account_authuser": None,
                "account_email": None,
                "backup": "truthy",
            },
        )
    ]


@pytest.mark.parametrize(
    ("account", "mode", "authuser", "email"),
    [
        (storage_mod.KEEP_ACCOUNT, "keep", None, None),
        (storage_mod.CLEAR_ACCOUNT, "clear", None, None),
        (storage_mod.AccountRecord(7, "user@example.com"), "set", 7, "user@example.com"),
    ],
)
def test_replace_from_login_compatibility_account_translation_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account: storage_mod.AccountArg,
    mode: str,
    authuser: int | None,
    email: str | None,
) -> None:
    calls: list[dict[str, object]] = []

    def native(*args: object, **kwargs: object) -> ReplaceResult:
        calls.append(kwargs)
        return ReplaceResult(ReplaceStatus.APPLIED)

    monkeypatch.setattr(storage_mod, "replace_profile_from_login", native)
    state = _login_state()

    assert storage_mod.replace_from_login(
        tmp_path / "A.json", state, include_domains=None, account=account
    ).ok
    assert calls == [
        {
            "include_domains": None,
            "include_optional": False,
            "account_mode": mode,
            "account_authuser": authuser,
            "account_email": email,
            "backup": False,
        }
    ]


def test_replace_from_login_account_copy_failure_precedes_store_and_parent_creation(
    tmp_path: Path,
) -> None:
    class FailsCopy:
        def __deepcopy__(self, memo: dict[int, object]) -> object:
            raise RuntimeError("bounded account copy failure")

    path = tmp_path / "missing" / "A.json"
    with pytest.raises(RuntimeError, match="bounded account copy failure"):
        storage_mod.replace_from_login(
            path,
            _login_state(),
            include_domains=None,
            account=storage_mod.AccountRecord(FailsCopy(), FailsCopy()),  # type: ignore[arg-type]
        )
    assert not path.parent.exists()


# --- persist_minted_jar: full replace, fails CLOSED ------------------------


def _minted_jar() -> httpx.Cookies:
    jar = httpx.Cookies()
    for name in ("SID", "APISID", "SAPISID"):
        jar.set(name, "v", domain=".google.com", path="/")
    return jar


def test_persist_minted_jar_replaces_cookies_and_rebinds_account(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    # #2103 PR-2 D6: existing storage must already be bound to the minted
    # account, else persist_minted_jar refuses (see the dedicated D6 tests
    # below) — same-account re-mint is the realistic case this test covers.
    path.write_text(
        json.dumps(
            {
                "cookies": [{"name": "OLD", "value": "x", "domain": ".google.com"}],
                "notebooklm": {
                    "version": 1,
                    "account": {"authuser": 0, "email": "minted@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    storage_mod.persist_minted_jar(path, _minted_jar(), email="minted@example.com")
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {c["name"] for c in data["cookies"]}
    assert names == {"SID", "APISID", "SAPISID"}  # replaced, not merged
    assert data["notebooklm"]["account"] == {"authuser": 0, "email": "minted@example.com"}


def test_persist_minted_jar_filters_unallowlisted_but_keeps_rebind(tmp_path: Path) -> None:
    """L4 gap fix (b-PR2): the minted jar is domain-filtered before it reaches
    disk (an unallowlisted cookie is dropped, trusted Google subdomains survive),
    while the rebind to the minted account (authuser=0 + minted email) stays."""
    path = tmp_path / "storage_state.json"
    # #2103 PR-2 D6: same-account re-mint, so the ownership guard is a no-op here.
    path.write_text(
        json.dumps(
            {
                "notebooklm": {
                    "version": 1,
                    "account": {"authuser": 0, "email": "minted@example.com"},
                }
            }
        ),
        encoding="utf-8",
    )
    jar = httpx.Cookies()
    for name in ("SID", "APISID", "SAPISID"):
        jar.set(name, "v", domain=".google.com", path="/")
    jar.set("MEDIA", "m", domain="lh3.googleusercontent.com", path="/")
    # An unallowlisted sibling-product cookie that must NOT reach disk.
    jar.set("YT", "y", domain=".youtube.com", path="/")

    storage_mod.persist_minted_jar(path, jar, email="minted@example.com")
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {c["name"] for c in data["cookies"]}
    assert "YT" not in names  # L4: unallowlisted cookie filtered out at persist
    assert {"SID", "APISID", "SAPISID", "MEDIA"} <= names  # trusted roots survive
    # Rebind semantics unchanged by the added filter.
    assert data["notebooklm"]["account"] == {"authuser": 0, "email": "minted@example.com"}


# --- persist_minted_jar: D6 account-ownership guard (#2103 PR-2) -----------


def test_persist_minted_jar_refuses_a_different_recorded_owner(tmp_path: Path) -> None:
    """The AUTHORITATIVE enforcement: existing storage bound to one account,
    a mint for another -> refused, even without going through
    assert_account_writable's pre-check (this is the "documented low-level
    recipe" bypass D6 closes)."""
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {"notebooklm": {"version": 1, "account": {"authuser": 0, "email": "owner@example.com"}}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(mt_mod.MasterTokenError, match="owner@example.com"):
        storage_mod.persist_minted_jar(path, _minted_jar(), email="attacker@example.com")
    # Refused BEFORE any write: the original owner's cookies are untouched.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "cookies" not in data or data.get("cookies") == []


def test_persist_minted_jar_refuses_an_unrecorded_owner(tmp_path: Path) -> None:
    """Existing storage with NO in-band account metadata -> refused unless
    force (rare after PR-0's promotion, but a fresh cookie-only jar from
    ``import-cookies`` can still lack it)."""
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [{"name": "OLD", "value": "x"}]}), encoding="utf-8")
    with pytest.raises(mt_mod.MasterTokenError, match="no recorded account owner"):
        storage_mod.persist_minted_jar(path, _minted_jar(), email="minted@example.com")


def test_persist_minted_jar_force_overrides_a_different_owner(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {"notebooklm": {"version": 1, "account": {"authuser": 0, "email": "owner@example.com"}}}
        ),
        encoding="utf-8",
    )
    storage_mod.persist_minted_jar(path, _minted_jar(), email="new@example.com", force=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"] == {"authuser": 0, "email": "new@example.com"}


def test_persist_minted_jar_proceeds_when_no_existing_storage(tmp_path: Path) -> None:
    """No existing storage: nothing to protect yet, so the guard is a no-op —
    the first-ever mint into a fresh profile must not require force."""
    path = tmp_path / "storage_state.json"
    assert not path.exists()
    storage_mod.persist_minted_jar(path, _minted_jar(), email="minted@example.com")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"] == {"authuser": 0, "email": "minted@example.com"}


def test_persist_minted_jar_refuse_unknown_owner_false_allows_unrecorded_owner(
    tmp_path: Path,
) -> None:
    """#2103 PR-2 (post-review fix): re-minting from a master token already
    paired with this exact storage_path (``refuse_unknown_owner=False`` —
    what ``remint_from_stored_token`` passes) must NOT require pre-existing
    in-band account metadata. A profile that was never bound to an explicit
    ``--account`` (e.g. cookie-only ``import-cookies``) is the COMMON case,
    not the "rare" one originally assumed — requiring it would break L4
    mid-session self-recovery for exactly that population
    (tests/unit/test_auth_cold_start_recovery.py's fixtures have no account
    metadata at all)."""
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [{"name": "OLD", "value": "x"}]}), encoding="utf-8")
    storage_mod.persist_minted_jar(
        path, _minted_jar(), email="minted@example.com", refuse_unknown_owner=False
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"] == {"authuser": 0, "email": "minted@example.com"}


def test_persist_minted_jar_refuse_unknown_owner_false_still_refuses_different_owner(
    tmp_path: Path,
) -> None:
    """``refuse_unknown_owner=False`` only relaxes the "no metadata at all"
    case — an EXPLICIT different recorded owner is still refused
    unconditionally (this is the actual cross-account-overwrite protection;
    relaxing it would defeat D6 entirely)."""
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {"notebooklm": {"version": 1, "account": {"authuser": 0, "email": "owner@example.com"}}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(mt_mod.MasterTokenError, match="owner@example.com"):
        storage_mod.persist_minted_jar(
            path, _minted_jar(), email="attacker@example.com", refuse_unknown_owner=False
        )


def test_persist_minted_jar_fails_closed_on_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "storage_state.json"
    _patch_lock_unavailable(monkeypatch)
    with pytest.raises(storage_mod.LockUnavailableError):
        storage_mod.persist_minted_jar(path, _minted_jar(), email="minted@example.com")


# --- write_master_token: now locked + atomic, fails CLOSED -----------------


def test_write_master_token_roundtrip_and_mode(tmp_path: Path) -> None:
    import sys

    path = tmp_path / "master_token.json"
    storage_mod.write_master_token(path, email="e@x.com", master_token="aas_et/M", android_id="abc")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "email": "e@x.com",
        "android_id": "abc",
        "master_token": "aas_et/M",
    }
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600  # full-account credential


def test_write_master_token_accepts_storage_state_name_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(b"opaque prior bytes that must not be parsed")
    real_read_text = Path.read_text

    def read_text(target: Path, *args: object, **kwargs: object) -> str:
        if target == path:
            pytest.fail("master-token commit must not read its destination")
        return real_read_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    storage_mod.write_master_token(
        path,
        email="e@x.com",
        master_token="aas_et/M",
        android_id="abc",
    )
    assert path.read_bytes() == (
        b'{\n  "version": 1,\n  "email": "e@x.com",\n'
        b'  "android_id": "abc",\n  "master_token": "aas_et/M"\n}'
    )
    lock = tmp_path / ".storage_state.json.lock"
    assert lock.exists()
    assert not path.with_name(path.name + ".bak").exists()


def test_write_master_token_fails_closed_on_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "master_token.json"
    _patch_master_token_lock_unavailable(monkeypatch)
    with pytest.raises(storage_mod.LockUnavailableError):
        storage_mod.write_master_token(
            path, email="e@x.com", master_token="aas_et/M", android_id="abc"
        )


# --- parent-dir permission self-heal ---------------------------------------


def test_ensure_secure_parent_dir_creates_at_0700(tmp_path: Path) -> None:
    """A freshly-created parent directory is 0700 on POSIX."""
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX permission semantics")
    path = tmp_path / "sub" / "storage_state.json"
    profile_store._ensure_secure_parent_dir(path)
    assert path.parent.is_dir()
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_ensure_secure_parent_dir_retightens_existing_loose_dir(tmp_path: Path) -> None:
    """A PRE-EXISTING parent loosened to 0755 (e.g. by a backup/sync tool) is
    re-tightened to 0700 on the next writer intent — the restored self-heal
    (finding #2 regression). POSIX-only."""
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX permission semantics")
    parent = tmp_path / "creds"
    parent.mkdir(mode=0o700)
    # Simulate a restore/sync tool loosening the directory after the fact.
    import os

    os.chmod(parent, 0o755)
    assert (parent.stat().st_mode & 0o777) == 0o755

    profile_store._ensure_secure_parent_dir(parent / "storage_state.json")

    # Unconditional chmod re-tightened the already-existing directory.
    assert (parent.stat().st_mode & 0o777) == 0o700


def test_writer_intent_retightens_loose_parent_dir(tmp_path: Path) -> None:
    """End-to-end: a writer intent (write_master_token) run against a loosened
    pre-existing parent re-tightens it to 0700 (POSIX-only)."""
    import os
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX permission semantics")
    parent = tmp_path / "creds"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o755)

    storage_mod.write_master_token(
        parent / "master_token.json", email="e@x.com", master_token="aas_et/M", android_id="abc"
    )

    assert (parent.stat().st_mode & 0o777) == 0o700


def test_replace_from_login_failed_write_leaves_legacy_account_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that did nothing must not reach the legacy-account scrub.

    Before the transaction conversion this was STRUCTURAL: both non-OK returns
    sat inside the ``with``, so the post-lock legacy step was unreachable on a
    failed write. Routed through ``in_storage_transaction`` they became the
    transaction's return value, and reachability now rests entirely on the
    explicit post-transaction ``status is not OK`` guard — the only new control
    flow the conversion introduced, and (until this test) the only part of it
    nothing covered: deleting the guard left the whole relevant suite green.

    Without it, a ``CLEAR_ACCOUNT`` login whose required cookies were dropped,
    or that never got the lock, scrubs the legacy ``context.json`` account key
    after writing nothing — ``_drop_legacy_account_key`` removes the file once
    it is empty. That is the #2103 data-loss class re-opened from the other
    side, so the guard is load-bearing rather than tidy.
    """
    dropped_state = {
        "cookies": [
            {"name": "SID", "value": "s", "domain": ".youtube.com", "path": "/"},
            {"name": "__Secure-1PSIDTS", "value": "p", "domain": ".google.com", "path": "/"},
        ],
        "origins": [],
    }
    legacy = {"account": {"authuser": 3, "email": "legacy@example.com"}}

    for label in ("required_dropped", "lock_unavailable"):
        path = tmp_path / label / "storage_state.json"
        path.parent.mkdir(parents=True)
        context_path = path.with_name("context.json")
        context_path.write_text(json.dumps(legacy), encoding="utf-8")

        with monkeypatch.context() as patch:
            if label == "lock_unavailable":
                _patch_lock_unavailable(patch)
                state = _login_state()
            else:
                state = dropped_state
            outcome = storage_mod.replace_from_login(
                path, state, include_domains=None, account=storage_mod.CLEAR_ACCOUNT
            )

        assert outcome.status is not storage_mod.LoginWriteStatus.OK, label
        # The legacy record — and the file itself — must survive a no-op write.
        assert context_path.exists(), f"{label}: context.json was deleted by a failed write"
        assert json.loads(context_path.read_text(encoding="utf-8")) == legacy, label
