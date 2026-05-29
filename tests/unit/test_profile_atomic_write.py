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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from notebooklm._auth.account import (
    clear_account_metadata,
    get_account_email_for_storage,
    get_authuser_for_storage,
    read_account_metadata,
    write_account_metadata,
)


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
    """ACCEPTANCE-CRITICAL: existing two-file profile loads under new reader.

    If this migration test breaks for any existing user, they will lose their
    account binding (authuser/email) on the next CLI run — Tier-9 P1-20 PR
    body must surface this.
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

    assert get_authuser_for_storage(storage_path) == 3
    assert get_account_email_for_storage(storage_path) == "charlie@example.com"
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
    """When both forms exist, in-band wins."""
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

    # No temp files leaked beside the storage file.
    leftover_temps = list(tmp_path.glob(".storage_state.json.*"))
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


def _capture_account_lock_path(monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Record the ``storage_state.json`` lock path account.py passes to ``FileLock``.

    The captured contextmanager is a no-op so the surrounding read-modify-write
    still completes against the real file. ``write_account_metadata`` also calls
    ``_drop_legacy_account_key``, which locks the sibling ``context.json.lock``;
    that path is filtered out so it can't clobber the value we assert on. Paths
    are canonicalized (``.expanduser().resolve()``) because they back
    cross-process locking, where distinct spellings of the same file must
    compare equal.
    """
    import contextlib

    from notebooklm._auth import account as account_mod

    seen: dict[str, Path] = {}

    @contextlib.contextmanager
    def _fake_filelock(path: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        resolved = Path(path).expanduser().resolve()
        if "storage_state.json" in resolved.name:
            seen["path"] = resolved
        yield

    monkeypatch.setattr(account_mod, "FileLock", _fake_filelock)
    return seen


def _capture_storage_lock_path(monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Record the lock path storage.py passes to ``_file_lock_exclusive``.

    Paths are canonicalized to match ``_capture_account_lock_path`` so the two
    captures compare equal even if their spellings differ.
    """
    import contextlib

    from notebooklm._auth import storage as storage_mod

    seen: dict[str, Path] = {}

    @contextlib.contextmanager
    def _fake_exclusive(lock_path: Path):  # type: ignore[no-untyped-def]
        seen["path"] = Path(lock_path).expanduser().resolve()
        yield

    monkeypatch.setattr(storage_mod, "_file_lock_exclusive", _fake_exclusive)
    return seen


def test_storage_state_mutators_share_one_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All ``storage_state.json`` mutators must serialize on the SAME flock file.

    Tier-0 data-loss regression: ``save_cookies_to_storage`` (storage.py) used
    the dotted ``.storage_state.json.lock`` sibling while ``account.py``'s
    metadata writers used the non-dotted ``storage_state.json.lock`` — a
    DIFFERENT file. Cookie-save and account-metadata writes therefore locked on
    distinct files and could lose updates under concurrency. Assert both paths
    derive an identical lock filename for the same ``storage_state.json``.
    """
    import httpx

    from notebooklm._auth.storage import save_cookies_to_storage

    storage_path = tmp_path / "storage_state.json"
    _write_storage_state(storage_path, {"cookies": [], "origins": []})

    # account.py: write_account_metadata
    account_seen = _capture_account_lock_path(monkeypatch)
    write_account_metadata(storage_path, authuser=1, email="alice@example.com")
    account_write_lock = account_seen["path"]

    # account.py: _clear_in_band_account (via clear_account_metadata)
    account_seen.clear()
    clear_account_metadata(storage_path)
    account_clear_lock = account_seen["path"]

    # storage.py: save_cookies_to_storage (the canonical cookie-save writer)
    storage_seen = _capture_storage_lock_path(monkeypatch)
    save_cookies_to_storage(
        httpx.Cookies(),
        path=storage_path,
        original_snapshot={},
    )
    storage_cookie_lock = storage_seen["path"]

    # Canonical name is the dotted, hidden sibling (storage.py contract).
    # Resolve to match the canonicalized captures above.
    expected = storage_path.with_name(f".{storage_path.name}.lock").expanduser().resolve()
    assert storage_cookie_lock == expected
    assert account_write_lock == expected
    assert account_clear_lock == expected
    # And, transitively, all three agree on the exact same file on disk.
    assert account_write_lock == account_clear_lock == storage_cookie_lock
