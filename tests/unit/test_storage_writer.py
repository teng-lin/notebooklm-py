"""Unit tests for the canonical ``storage_writer`` (refactor (b), b-PR1).

Covers the relocated intent-shaped API's per-intent lock-failure policy and the
value-free outcome contract. The CAS ``merge_cookie_delta`` body is exercised
verbatim by the existing 51-test CAS save-race suite (via the
``save_cookies_to_storage`` delegate) and is not re-tested here.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import pytest

from notebooklm._auth import master_token as mt_mod
from notebooklm._auth import storage as storage_mod
from notebooklm._auth import storage_writer as sw


@contextlib.contextmanager
def _unavailable_lock(lock_path, *, blocking, log_prefix):
    yield "unavailable"


def _patch_lock_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the unified ``_file_lock`` primitive to report the sentinel as
    permanently unavailable (infrastructure failure)."""
    monkeypatch.setattr(storage_mod, "_file_lock", _unavailable_lock)


# --- value-free outcome contract -------------------------------------------


def test_write_outcome_is_value_free() -> None:
    """``WriteOutcome`` carries only an enum status — never any payload."""
    ok = sw.WriteOutcome(sw.WriteStatus.OK)
    bad = sw.WriteOutcome(sw.WriteStatus.LOCK_UNAVAILABLE)
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
    sw.update_account_metadata(path, authuser=2, email="a@example.com")
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
    sw.update_account_metadata(path, authuser=0, email="new@example.com")

    # The belated stale write a slow promoter attempts — must be rejected.
    wrote = sw.update_account_metadata(
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

    wrote = sw.update_account_metadata(path, authuser=3, email="x@example.com", only_if_absent=True)

    assert wrote is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"] == {"authuser": 3, "email": "x@example.com"}


def test_update_account_metadata_fails_closed_on_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    _patch_lock_unavailable(monkeypatch)
    with pytest.raises(sw.LockUnavailableError):
        sw.update_account_metadata(path, authuser=1, email="a@example.com")
    # Fail-closed: the file must be untouched (no partial account write).
    assert "notebooklm" not in json.loads(path.read_text(encoding="utf-8"))


# --- clear_in_band_account: best-effort, swallows lock unavailability -------


def test_clear_in_band_account_swallows_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "storage_state.json"
    sw.update_account_metadata(path, authuser=1, email="a@example.com")  # seed a record
    _patch_lock_unavailable(monkeypatch)
    # Best-effort: no raise, and the record is left intact.
    sw.clear_in_band_account(path)
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
    outcome = sw.replace_from_remint(path, _captured_state(), carry_account=True)
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
    outcome = sw.replace_from_remint(path, _captured_state(), carry_account=False)
    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {c["name"] for c in data["cookies"]} == {"SID", "SAPISID"}
    assert "notebooklm" not in data  # stale binding dropped


def test_replace_from_remint_takes_storage_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[capture-2] lock-contract: the re-mint write serializes on the storage
    lock and **fails closed** (no write) when the lock is unavailable, instead of
    racing a concurrent keepalive with a lockless write."""
    path = tmp_path / "storage_state.json"
    _patch_lock_unavailable(monkeypatch)
    outcome = sw.replace_from_remint(path, _captured_state(), carry_account=True)
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
    outcome = sw.replace_from_remint(path, captured, carry_account=False)
    assert outcome.ok
    names = {c["name"] for c in json.loads(path.read_text(encoding="utf-8"))["cookies"]}
    assert "YT" not in names  # unallowlisted domain filtered out at the chokepoint
    assert {"SID", "MEDIA", "DRV"} <= names  # trusted Google roots preserved


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
    outcome = sw.replace_from_login(path, state, include_domains=None)
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
    outcome = sw.replace_from_login(
        path,
        state,
        include_domains={"youtube"},
        account=sw.AccountRecord(authuser=2, email="a@example.com"),
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
    outcome = sw.replace_from_login(path, state, include_domains=None, account=sw.CLEAR_ACCOUNT)
    assert outcome.ok
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {c["name"] for c in data["cookies"]}
    assert "YT" not in names
    assert {"SID", "MEDIA"} <= names
    # CLEAR + no opt-ins => no notebooklm namespace at all.
    assert "notebooklm" not in data


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
    outcome = sw.replace_from_login(path, state, include_domains=None)
    assert outcome.required_cookies_dropped
    assert outcome.missing_required == ("SID",)
    assert "__Secure-1PSIDTS" in outcome.present_names
    assert not path.exists()  # nothing written


def test_replace_from_login_keep_account_carries_input_namespace(tmp_path: Path) -> None:
    """KEEP_ACCOUNT (the import default) carries whatever the input state holds —
    import states carry none, so the result carries none."""
    path = tmp_path / "storage_state.json"
    outcome = sw.replace_from_login(path, _login_state(), include_domains=None)  # default KEEP
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

    outcome = sw.replace_from_login(path, _login_state(), include_domains=None)  # default KEEP

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

    outcome = sw.replace_from_login(
        path, _login_state(), include_domains=None, account=sw.CLEAR_ACCOUNT
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
    outcome = sw.replace_from_login(
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
    outcome = sw.replace_from_login(path, _login_state(), include_domains=None)
    assert outcome.lock_unavailable
    assert not path.exists()  # nothing written without the lock


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
    sw.persist_minted_jar(path, _minted_jar(), email="minted@example.com")
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

    sw.persist_minted_jar(path, jar, email="minted@example.com")
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
        sw.persist_minted_jar(path, _minted_jar(), email="attacker@example.com")
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
        sw.persist_minted_jar(path, _minted_jar(), email="minted@example.com")


def test_persist_minted_jar_force_overrides_a_different_owner(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {"notebooklm": {"version": 1, "account": {"authuser": 0, "email": "owner@example.com"}}}
        ),
        encoding="utf-8",
    )
    sw.persist_minted_jar(path, _minted_jar(), email="new@example.com", force=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"] == {"authuser": 0, "email": "new@example.com"}


def test_persist_minted_jar_proceeds_when_no_existing_storage(tmp_path: Path) -> None:
    """No existing storage: nothing to protect yet, so the guard is a no-op —
    the first-ever mint into a fresh profile must not require force."""
    path = tmp_path / "storage_state.json"
    assert not path.exists()
    sw.persist_minted_jar(path, _minted_jar(), email="minted@example.com")
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
    sw.persist_minted_jar(
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
        sw.persist_minted_jar(
            path, _minted_jar(), email="attacker@example.com", refuse_unknown_owner=False
        )


def test_persist_minted_jar_fails_closed_on_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "storage_state.json"
    _patch_lock_unavailable(monkeypatch)
    with pytest.raises(sw.LockUnavailableError):
        sw.persist_minted_jar(path, _minted_jar(), email="minted@example.com")


# --- write_master_token: now locked + atomic, fails CLOSED -----------------


def test_write_master_token_roundtrip_and_mode(tmp_path: Path) -> None:
    import sys

    path = tmp_path / "master_token.json"
    sw.write_master_token(path, email="e@x.com", master_token="aas_et/M", android_id="abc")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "email": "e@x.com",
        "android_id": "abc",
        "master_token": "aas_et/M",
    }
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600  # full-account credential


def test_write_master_token_fails_closed_on_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "master_token.json"
    _patch_lock_unavailable(monkeypatch)
    with pytest.raises(sw.LockUnavailableError):
        sw.write_master_token(path, email="e@x.com", master_token="aas_et/M", android_id="abc")


# --- bounded acquire tristate ----------------------------------------------


def test_acquire_storage_lock_held_then_released(tmp_path: Path) -> None:
    lock_path = tmp_path / ".storage_state.json.lock"
    with sw._acquire_storage_lock(lock_path, log_prefix="test") as state:
        assert state == "held"
    # After release the same-process acquire succeeds again (in-process lock freed).
    with sw._acquire_storage_lock(lock_path, log_prefix="test") as state:
        assert state == "held"


def test_acquire_storage_lock_times_out_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under persistent contention the bounded acquire yields 'unavailable'
    within the deadline rather than blocking forever."""
    lock_path = tmp_path / ".storage_state.json.lock"

    @contextlib.contextmanager
    def always_contended(lp, *, blocking, log_prefix):
        yield "contended"

    monkeypatch.setattr(storage_mod, "_file_lock", always_contended)
    with sw._acquire_storage_lock(lock_path, log_prefix="test", deadline_seconds=0.05) as state:
        assert state == "unavailable"


# --- parent-dir permission self-heal ---------------------------------------


def test_ensure_secure_parent_dir_creates_at_0700(tmp_path: Path) -> None:
    """A freshly-created parent directory is 0700 on POSIX."""
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX permission semantics")
    path = tmp_path / "sub" / "storage_state.json"
    sw._ensure_secure_parent_dir(path)
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

    sw._ensure_secure_parent_dir(parent / "storage_state.json")

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

    sw.write_master_token(
        parent / "master_token.json", email="e@x.com", master_token="aas_et/M", android_id="abc"
    )

    assert (parent.stat().st_mode & 0o777) == 0o700
