"""Unit tests for unified atomic profile-state write (P1-20).

The pre-P1-20 layout stored Google auth state across two files:

* ``storage_state.json`` — Playwright cookie/origin state.
* ``context.json`` — sibling JSON with ``{"account": {"authuser", "email"}}``
  and CLI notebook/conversation context.

Each file was atomically written on its own. Between the two writes there was
a window in which an external reader (e.g. a long-running ``notebooklm chat``
in a sibling shell) could see either the new cookies bundled with the old
account metadata, or vice versa. Tier-9 P1-20 closes this by bundling the
``account`` record INTO ``storage_state.json`` under a ``notebooklm``
namespace key:

    {
      "cookies": [...],
      "origins": [...],
      "notebooklm": {"version": 1, "account": {"authuser": 1, "email": "..."}}
    }

A single ``atomic_write_json`` is now the only commit point for the
(cookies, account) pair. ``context.json`` keeps holding non-account CLI
state (``notebook_id``, ``conversation_id``); the account key, if still
present from legacy installs, is migrated lazily on next write.

This module covers:

1. Round-trip of the new unified record.
2. Migration: a legacy two-file fixture reads cleanly under the new reader.
3. Migration: writing account metadata after a legacy read drops the
   ``account`` key from ``context.json`` (preserving other CLI state).
4. Torn-write fault injection: if ``storage_state.json`` write fails after
   cookies + account were serialized into the same temp file, the original
   on-disk file is preserved untouched (no half-written state).
5. Login/import replacement keeps this same single profile commit while its
   legacy promote-or-scrub reconciliation remains a later sibling-file step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from notebooklm._auth.profile_migration import LegacyPromotionScheduler
from notebooklm._auth.storage import (
    clear_account_metadata,
    get_account_email_for_storage,
    get_authuser_for_storage,
    promote_legacy_account,
    read_account_metadata,
    write_account_metadata,
)


def _drain_promotions_for_tests() -> None:
    LegacyPromotionScheduler.process_default().drain(30.0)


def _write_storage_state(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_storage_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _context_path(storage_path: Path) -> Path:
    return storage_path.with_name("context.json")


def test_write_account_metadata_lands_inline_in_storage_state(tmp_path: Path) -> None:
    """The post-P1-20 writer puts account inside storage_state.json."""
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(
        storage_path,
        {
            "cookies": [{"name": "SID", "value": "v", "domain": ".google.com", "path": "/"}],
            "origins": [],
        },
    )
    write_account_metadata(storage_path, authuser=1, email="alice@example.com")
    payload = _read_storage_state(storage_path)
    assert payload["notebooklm"]["version"] == 1
    assert payload["notebooklm"]["account"] == {"authuser": 1, "email": "alice@example.com"}


def test_read_account_metadata_prefers_in_band_record(tmp_path: Path) -> None:
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(
        storage_path,
        {
            "cookies": [],
            "origins": [],
            "notebooklm": {
                "version": 1,
                "account": {"authuser": 2, "email": "bob@example.com"},
            },
        },
    )
    assert get_authuser_for_storage(storage_path) == 2
    assert get_account_email_for_storage(storage_path) == "bob@example.com"


def test_legacy_two_file_fixture_reads_cleanly(tmp_path: Path) -> None:
    """ACCEPTANCE-CRITICAL: existing two-file profile reads correctly, no caller
    action required.

    Since the master-token-relocation PR-0 (issue #2103), ``read_account_metadata``
    never returns a raw pass-through of the sibling ``context.json[account]``
    record — a standing legacy-sibling fallback on every read meant a missed
    ``authuser`` could silently route requests to a *different* signed-in Google
    account (a #2103-class hazard), so the fallback was removed rather than
    patched at each call site.

    In its place, ``read_account_metadata`` DERIVES the record from the legacy
    sibling read-only, through the same sanitizer promotion embeds with — so
    THIS call, the very first read of any kind (``get_authuser_for_storage``,
    ``get_account_email_for_storage``, ``read_account_metadata`` directly, or
    any future caller), returns the correct value immediately. No caller
    anywhere has to remember an extra promotion step; that is the point of
    putting it at the chokepoint instead of at each of the read helpers built
    on it.

    Since ADR-0033 PR 5.1 the *durable* half is detached: the read schedules a
    one-shot background promotion and does not wait for it, so this test
    drains before asserting on disk state. The correct read values above are
    asserted BEFORE the drain, which is the actual acceptance property — a
    user's binding survives even if the write never lands.

    If this breaks, an existing user loses their account binding
    (authuser/email) on the next CLI run.
    """
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(
        storage_path,
        {
            "cookies": [{"name": "SID", "value": "v", "domain": ".google.com", "path": "/"}],
            "origins": [],
        },
    )
    # Legacy sibling context.json with account but no in-band record.
    _context_path(storage_path).write_text(
        json.dumps(
            {
                "account": {"authuser": 3, "email": "charlie@example.com"},
                "notebook_id": "nb-123",
            }
        ),
        encoding="utf-8",
    )

    # The very first read already self-heals — no separate promotion call, and
    # no waiting on the durable write either.
    assert get_authuser_for_storage(storage_path) == 3
    assert get_account_email_for_storage(storage_path) == "charlie@example.com"
    assert read_account_metadata(storage_path) == {
        "authuser": 3,
        "email": "charlie@example.com",
    }

    # Now let the detached one-shot finish and assert the durable half.
    _drain_promotions_for_tests()
    in_band = json.loads(storage_path.read_text(encoding="utf-8"))["notebooklm"]["account"]
    assert in_band == {"authuser": 3, "email": "charlie@example.com"}
    # Non-account legacy context state (notebook_id) survives the promotion.
    legacy_after = json.loads(_context_path(storage_path).read_text(encoding="utf-8"))
    assert "account" not in legacy_after
    assert legacy_after.get("notebook_id") == "nb-123"

    # Idempotent: nothing left to promote, and the migrated values are stable.
    assert promote_legacy_account(storage_path) is False
    assert read_account_metadata(storage_path) == {
        "authuser": 3,
        "email": "charlie@example.com",
    }


def test_migration_on_write_removes_legacy_account_key_only(tmp_path: Path) -> None:
    """After upgrade write, ``context.json`` keeps non-account state.

    ``context.json`` also holds ``notebook_id`` / ``conversation_id`` —
    migration must NOT clobber those.
    """
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(storage_path, {"cookies": [], "origins": []})
    _context_path(storage_path).write_text(
        json.dumps(
            {
                "account": {"authuser": 4, "email": "dana@example.com"},
                "notebook_id": "nb-456",
                "conversation_id": "conv-789",
            }
        ),
        encoding="utf-8",
    )

    # Trigger a unified write; this should migrate the legacy record.
    write_account_metadata(storage_path, authuser=5, email="erin@example.com")

    in_band = _read_storage_state(storage_path)["notebooklm"]["account"]
    assert in_band == {"authuser": 5, "email": "erin@example.com"}

    # Non-account context state preserved.
    legacy = json.loads(_context_path(storage_path).read_text(encoding="utf-8"))
    assert "account" not in legacy
    assert legacy.get("notebook_id") == "nb-456"
    assert legacy.get("conversation_id") == "conv-789"


def test_in_band_account_overrides_legacy_account(tmp_path: Path) -> None:
    """When both forms exist, in-band wins — because legacy is never consulted.

    Since #2103's PR-0, ``read_account_metadata`` has no legacy-fallback read at
    all, so this isn't a precedence contest the in-band record wins; the stale
    legacy record is simply invisible to the reader.
    """
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(
        storage_path,
        {
            "cookies": [],
            "origins": [],
            "notebooklm": {
                "version": 1,
                "account": {"authuser": 7, "email": "new@example.com"},
            },
        },
    )
    _context_path(storage_path).write_text(
        json.dumps({"account": {"authuser": 1, "email": "stale@example.com"}}),
        encoding="utf-8",
    )

    assert get_authuser_for_storage(storage_path) == 7
    assert get_account_email_for_storage(storage_path) == "new@example.com"


def test_clear_account_metadata_clears_in_band(tmp_path: Path) -> None:
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(
        storage_path,
        {
            "cookies": [],
            "origins": [],
            "notebooklm": {
                "version": 1,
                "account": {"authuser": 9, "email": "zoe@example.com"},
            },
        },
    )
    clear_account_metadata(storage_path)
    # Either the namespace is gone, or its account is gone — either way the
    # reader reports no account. The file itself remains valid JSON.
    _read_storage_state(storage_path)  # sanity-check the file still parses
    assert read_account_metadata(storage_path) == {}
    assert get_authuser_for_storage(storage_path) == 0
    assert get_account_email_for_storage(storage_path) is None


def test_clear_account_metadata_clears_legacy_two_file(tmp_path: Path) -> None:
    """Backward compat: clearing still removes legacy ``context.json`` account."""
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(storage_path, {"cookies": [], "origins": []})
    _context_path(storage_path).write_text(
        json.dumps(
            {
                "account": {"authuser": 8, "email": "leah@example.com"},
                "notebook_id": "nb-keep",
            }
        ),
        encoding="utf-8",
    )
    clear_account_metadata(storage_path)
    # Non-account context preserved.
    legacy = json.loads(_context_path(storage_path).read_text(encoding="utf-8"))
    assert "account" not in legacy
    assert legacy.get("notebook_id") == "nb-keep"


def test_torn_write_fault_injection_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACCEPTANCE-CRITICAL: writer crash between cookies+metadata is recoverable.

    If we crash during the unified write, the original on-disk
    ``storage_state.json`` must remain valid and unchanged — no half-written
    state mixing new cookies with old account or vice versa.
    """
    storage_path = tmp_path / "storage_state.json"
    original_payload = {
        "cookies": [{"name": "SID", "value": "old-v", "domain": ".google.com", "path": "/"}],
        "origins": [],
        "notebooklm": {
            "version": 1,
            "account": {"authuser": 1, "email": "original@example.com"},
        },
    }
    _write_storage_state(storage_path, original_payload)

    # Inject a crash mid-write: the next ``os.replace`` raises.
    import notebooklm._atomic_io as atomic_io_mod

    original_replace = atomic_io_mod.os.replace

    def _boom(src: Any, dst: Any) -> None:
        raise OSError("simulated crash during atomic replace")

    monkeypatch.setattr(atomic_io_mod.os, "replace", _boom)

    with pytest.raises(OSError, match="simulated crash"):
        write_account_metadata(storage_path, authuser=99, email="never@example.com")

    monkeypatch.setattr(atomic_io_mod.os, "replace", original_replace)

    # Original file untouched: reader sees the OLD record consistently.
    assert _read_storage_state(storage_path) == original_payload
    assert get_authuser_for_storage(storage_path) == 1
    assert get_account_email_for_storage(storage_path) == "original@example.com"

    # No torn-write temp files leaked beside the storage file. The filelock
    # sentinel (``.storage_state.json.lock``) is expected to persist — filelock
    # >= 3.29 no longer unlinks it on release — and is not a torn-write leak.
    leftover_temps = [
        p for p in tmp_path.glob(".storage_state.json.*") if p.name != ".storage_state.json.lock"
    ]
    assert leftover_temps == [], f"Temp file leaked: {leftover_temps}"


def test_torn_write_reader_never_sees_mixed_state(tmp_path: Path) -> None:
    """Property: at any observation point, reader sees old XOR new — never mixed.

    Atomicity comes from ``atomic_write_json``'s single ``os.replace``: until
    the rename commits, the reader sees only the previous on-disk version.
    This test asserts the contract is exercised by the unified write path —
    after a successful write, both cookies and account come from the same
    commit; if the write fails, neither rolls forward.
    """
    storage_path = tmp_path / "storage_state.json"
    # Round 1: write old state.
    write_account_metadata(storage_path, authuser=10, email="round1@example.com")
    # Sanity: the file now exists with the round-1 record.
    payload_1 = _read_storage_state(storage_path)
    assert payload_1["notebooklm"]["account"]["email"] == "round1@example.com"

    # Round 2: overwrite with a new account record.
    write_account_metadata(storage_path, authuser=20, email="round2@example.com")
    payload_2 = _read_storage_state(storage_path)
    # The reader sees exactly one record — never a merge.
    assert payload_2["notebooklm"]["account"] == {
        "authuser": 20,
        "email": "round2@example.com",
    }
    # The non-account file structure round-trips unchanged.
    assert payload_2.get("cookies") == payload_1.get("cookies")


def test_unified_format_version_is_pinned_to_one(tmp_path: Path) -> None:
    """Pin the version number so any future bump is intentional."""
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(storage_path, {"cookies": [], "origins": []})
    write_account_metadata(storage_path, authuser=1, email="v@example.com")
    payload = _read_storage_state(storage_path)
    assert payload["notebooklm"]["version"] == 1


def test_write_without_email_omits_email_field(tmp_path: Path) -> None:
    """Default-account login: authuser=0, no email — record omits email."""
    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(storage_path, {"cookies": [], "origins": []})
    write_account_metadata(storage_path, authuser=0, email=None)
    payload = _read_storage_state(storage_path)
    assert payload["notebooklm"]["account"] == {"authuser": 0}


def test_write_preserves_cookies_and_origins(tmp_path: Path) -> None:
    """Writing account metadata MUST NOT touch cookies / origins."""
    storage_path = tmp_path / "storage_state.json"
    cookies = [
        {"name": "SID", "value": "v1", "domain": ".google.com", "path": "/"},
        {"name": "HSID", "value": "v2", "domain": ".google.com", "path": "/"},
    ]
    origins = [
        {"origin": "https://notebooklm.google.com", "localStorage": [{"name": "k", "value": "v"}]}
    ]
    _write_storage_state(storage_path, {"cookies": cookies, "origins": origins})
    write_account_metadata(storage_path, authuser=1, email="alice@example.com")
    payload = _read_storage_state(storage_path)
    assert payload["cookies"] == cookies
    assert payload["origins"] == origins


def test_storage_state_mutators_share_one_lock_file(tmp_path: Path) -> None:
    """All ``storage_state.json`` mutators must serialize on the SAME flock file.

    Tier-0 data-loss regression: cookie saves and account-metadata writes once
    used DIFFERENT lock files and could lose updates under concurrency. After
    the storage-writer refactor every mutator serializes on the project-internal
    shared ``StorageLockManager`` (cookie saves via ``ProfileStore``'s blocking
    primitive, account/clear/replacement via its bounded transaction). This test
    removes the sentinel before each real mutation and asserts every operation
    recreates the identical dotted ``.storage_state.json.lock`` sibling. The sibling
    ``context.json.lock`` (still ``filelock``, taken by
    ``LegacyAccountContext.scrub``) uses a different mechanism and is not captured
    here.
    """
    import httpx

    from notebooklm._auth.storage import (
        persist_minted_jar,
        replace_from_remint,
        save_cookies_to_storage,
    )

    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(storage_path, {"cookies": [], "origins": []})

    # Canonical name is the dotted, hidden sibling (storage.py contract).
    expected = storage_path.with_name(f".{storage_path.name}.lock").expanduser().resolve()
    context_lock = storage_path.with_name(".context.json.lock").expanduser().resolve()

    def _locks_taken_by(mutator) -> set[Path]:  # type: ignore[no-untyped-def]
        for candidate in tmp_path.iterdir():
            if candidate.name.endswith(".lock") and candidate.is_file():
                candidate.unlink()
        mutator()
        observed = {
            candidate.resolve()
            for candidate in tmp_path.iterdir()
            if candidate.name.endswith(".lock") and candidate.resolve() != context_lock
        }
        assert expected in observed
        return observed

    # Assert PER MUTATOR that it created the storage lock — so "a mutator takes
    # no lock" is caught, not masked by another mutator's earlier sentinel.
    account_write_locks = _locks_taken_by(
        lambda: write_account_metadata(storage_path, authuser=1, email="alice@example.com")
    )
    account_clear_locks = _locks_taken_by(lambda: clear_account_metadata(storage_path))
    cookie_save_locks = _locks_taken_by(
        lambda: save_cookies_to_storage(httpx.Cookies(), path=storage_path, original_snapshot={})
    )
    remint_locks = _locks_taken_by(
        lambda: replace_from_remint(
            storage_path,
            {
                "cookies": [{"name": "SID", "value": "v", "domain": ".google.com", "path": "/"}],
                "origins": [],
            },
            carry_account=True,
        )
    )
    minted = httpx.Cookies()
    minted.set("SID", "v", domain=".google.com", path="/")
    minted_locks = _locks_taken_by(
        lambda: persist_minted_jar(
            storage_path,
            minted,
            email="alice@example.com",
            refuse_unknown_owner=False,
        )
    )

    assert account_write_locks == {expected}, (
        f"write_account_metadata must take exactly the shared lock; got {account_write_locks}"
    )
    assert account_clear_locks == {expected}, (
        f"clear_account_metadata must take exactly the shared lock; got {account_clear_locks}"
    )
    assert cookie_save_locks == {expected}, (
        f"save_cookies_to_storage must take exactly the shared lock; got {cookie_save_locks}"
    )
    assert remint_locks == {expected}, (
        f"replace_from_remint must take exactly the shared lock; got {remint_locks}"
    )
    assert minted_locks == {expected}, (
        f"persist_minted_jar must take exactly the shared lock; got {minted_locks}"
    )
