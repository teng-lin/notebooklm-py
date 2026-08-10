"""Coverage-focused tests for the ``_auth.account`` / ``_auth.storage`` account branches.

Targets the read/clear/migration helpers and error-handling branches that the
concern-aligned ``test_auth_account.py`` suite does not exercise: malformed /
non-dict storage payloads, the ``_probe_authuser`` non-200 path, legacy
``context.json`` migration cleanup, and the in-band clear adapter's no-op / lock
branches.

ADR-0033 PR 5.2 relocated the account *record* helpers to ``_auth.storage``;
only the NETWORK identity half (``_probe_authuser``, page-email extraction)
still lives in ``_auth.account``. The imports below are split accordingly, so
read the module each subject is imported from rather than assuming ``account``.

New file per ADR-0007: patches owning modules at the bare-name call site rather
than editing the existing concern-aligned test file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from notebooklm._auth import account as _auth_account
from notebooklm._auth import keepalive as _auth_keepalive
from notebooklm._auth import profile_migration as _profile_migration
from notebooklm._auth.account import (
    Account,
    _probe_authuser,
    enumerate_accounts,
    format_authuser_value,
    repair_account_metadata_from_playwright_storage,
)

# Compatibility account adapters remain in ``_auth.storage``. Concrete legacy
# context, migration, and lifecycle tests use their owners in
# ``_auth.profile_migration`` so whitebox patches land where calls resolve.
from notebooklm._auth.profile_migration import LegacyAccountContext
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._auth.storage import (
    clear_account_metadata,
    get_account_email_for_storage,
    promote_legacy_account,
    read_account_metadata,
    read_account_metadata_from_storage_state,
    write_account_metadata,
)
from notebooklm._auth.storage import clear_in_band_account as _clear_in_band_account


class TestProbeAuthuserNon200:
    """``_probe_authuser`` returns ``None`` for non-200 responses (line 131)."""

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        async def fake_get(*args, **kwargs):
            request = httpx.Request("GET", "https://notebooklm.google.com/?authuser=0")
            return httpx.Response(503, content=b"down", request=request)

        class _Client:
            get = staticmethod(fake_get)

        result = await _probe_authuser(_Client(), 0)  # type: ignore[arg-type]
        assert result is None


class TestEnumerateAccountsPokeHook:
    """``enumerate_accounts`` runs the optional poke hook before probing (line 184)."""

    @pytest.mark.asyncio
    async def test_poke_session_hook_invoked(self, monkeypatch):
        poked: list[object] = []

        async def fake_poke(client, storage_path):
            poked.append((client, storage_path))

        async def fake_probe(client, n):
            return "alice@example.com" if n == 0 else "alice@example.com"

        monkeypatch.setattr(_auth_account, "_probe_authuser", fake_probe)

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=2, poke_session=fake_poke)

        assert poked, "poke_session hook was not invoked"
        assert accounts == [Account(authuser=0, email="alice@example.com", is_default=True)]

    @pytest.mark.asyncio
    async def test_poke_session_none_skips_hook(self, monkeypatch):
        # poke_session defaults to None on the bare ``_auth.account`` entry
        # point → the hook is skipped (183->185 false arc).
        async def fake_probe(client, n):
            return "alice@example.com"

        monkeypatch.setattr(_auth_account, "_probe_authuser", fake_probe)

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=2)

        assert accounts == [Account(authuser=0, email="alice@example.com", is_default=True)]


class TestRepairAccountMetadataPokeSession:
    """``repair_account_metadata_from_playwright_storage`` must inject the same
    keepalive poke hook the ``notebooklm.auth`` facade's ``enumerate_accounts``
    wrapper injects (issue found reviewing #2103's auth cross-boundary ledger
    shrink, Phase 5): calling the bare ``_auth.account.enumerate_accounts``
    entry point without ``poke_session`` silently skips the hook (see
    ``TestEnumerateAccountsPokeHook`` above) — a coarse internal function that
    forgets to pass it through would silently drop the keepalive rotation poke
    for this call site with no test failure to catch it.
    """

    @pytest.mark.asyncio
    async def test_poke_session_is_injected(self, tmp_path, monkeypatch):
        calls: list[dict] = []

        async def fake_enumerate(jar, **kwargs):
            calls.append(kwargs)
            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        from notebooklm._auth import cookies as _auth_cookies

        monkeypatch.setattr(_auth_account, "enumerate_accounts", fake_enumerate)
        monkeypatch.setattr(
            _auth_cookies, "build_httpx_cookies_from_storage", lambda path: httpx.Cookies()
        )

        storage_path = tmp_path / "storage_state.json"
        result = await repair_account_metadata_from_playwright_storage(storage_path)

        assert result.written is True
        assert calls and calls[0].get("poke_session") is _auth_keepalive._poke_session


class TestReadInBandAccount:
    """In-band reader malformed / non-dict branches (lines 232-234, 241)."""

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_account_metadata(tmp_path / "missing.json") == {}

    def test_malformed_json_returns_empty(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("not json", encoding="utf-8")
        assert read_account_metadata(storage) == {}

    def test_non_dict_storage_state_returns_empty(self):
        # read_account_metadata_from_storage_state guard: non-dict → {} (line 241).
        assert read_account_metadata_from_storage_state(["not", "a", "dict"]) == {}

    def test_namespace_not_dict_returns_empty(self):
        assert read_account_metadata_from_storage_state({"notebooklm": "oops"}) == {}

    def test_account_not_dict_returns_empty(self):
        assert (
            read_account_metadata_from_storage_state(
                {"notebooklm": {"account": "oops", "version": 1}}
            )
            == {}
        )

    def test_well_formed_account_returned(self):
        assert read_account_metadata_from_storage_state(
            {"notebooklm": {"version": 1, "account": {"authuser": 3}}}
        ) == {"authuser": 3}


class TestReadLegacyAccount:
    """Legacy ``context.json`` reader non-dict branch (line 260)."""

    def test_missing_context_returns_none(self, tmp_path):
        assert LegacyAccountContext().read(tmp_path / "storage_state.json") is None

    def test_malformed_legacy_json_returns_empty(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text("not json", encoding="utf-8")
        assert LegacyAccountContext().read(storage) is None

    def test_non_dict_legacy_payload_returns_empty(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(json.dumps(["x"]), encoding="utf-8")
        assert LegacyAccountContext().read(storage) is None

    def test_legacy_account_returned(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 4}}), encoding="utf-8"
        )
        assert LegacyAccountContext().read(storage) == {"authuser": 4}


class TestPromoteLegacyAccount:
    """``promote_legacy_account`` — the one-shot in-band migration (#2103 PR-0).

    ``test_profile_atomic_write.py::test_legacy_two_file_fixture_reads_cleanly``
    already pins the acceptance-critical success path (embed + scrub + preserve
    non-account context keys). This class covers the branches that test does
    not: no legacy record, in-band-already-present residue cleanup (the privacy
    case — a stale legacy email must not survive a promotion no-op), and
    sanitization of a malformed legacy record.
    """

    def test_missing_storage_file_is_a_noop_never_creates_it(self, tmp_path):
        """MAJOR (#2103 PR-0 review): a plain read must never CREATE
        ``storage_state.json``. Without this guard, promoting into a
        nonexistent storage file synthesizes a cookie-less document
        (``_load_storage_state_for_write``'s missing-file fallback) — and
        ``_app/profile.py``'s ``authenticated=storage.exists()`` check runs
        immediately after calling ``read_account_metadata`` transitively, so
        it would flip from correctly reporting "not authenticated" to
        incorrectly reporting "authenticated" for a profile with zero
        cookies, purely as a side effect of having been looked at (e.g. by
        ``profile list`` or ``auth check``)."""
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 3, "email": "x@example.com"}}),
            encoding="utf-8",
        )

        assert promote_legacy_account(storage) is False
        assert not storage.exists()
        # The legacy fallback still works — read_account_metadata's own
        # existence check happens independently, so the account binding
        # remains visible even though nothing was (or should be) written.
        assert read_account_metadata(storage) == {"authuser": 3, "email": "x@example.com"}
        assert not storage.exists()  # still not created, even by the read

    def test_no_legacy_record_is_a_noop(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        assert promote_legacy_account(storage) is False

    def test_in_band_already_present_scrubs_legacy_residue_without_promoting(self, tmp_path):
        """Both records exist: promotion returns False (nothing NEW promoted),
        but the stale legacy email must still be scrubbed — leaving it at rest
        after a promotion no-op is the exact privacy hazard #2103's PR-0 closes
        on the write side."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "origins": [],
                    "notebooklm": {"version": 1, "account": {"authuser": 7}},
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "stale@example.com"}}),
            encoding="utf-8",
        )

        assert promote_legacy_account(storage) is False

        assert LegacyAccountContext().read(storage) is None
        in_band = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]["account"]
        assert in_band == {"authuser": 7}  # untouched — in-band already won

    def test_promotion_write_takes_the_standard_full_rmw_lock_deadline(self, tmp_path, monkeypatch):
        """Inverse of the pin this replaces (ADR-0033 PR 5.1).

        Promotion used to shorten the storage-lock deadline to 2s because it
        ran INSIDE ``read_account_metadata``, where a 90s acquire would freeze
        an event loop mid-"read". It no longer runs there — the read schedules
        a detached worker — so the override is gone and the standard full-file
        RMW deadline applies. Waiting out real contention is now strictly
        better than giving up, because the one-shot never retries in-process.

        Pinned by capturing what ``ProfileStore.update_account`` receives: no
        deadline override exists on the concrete writer."""
        import inspect

        assert "deadline_seconds" not in inspect.signature(ProfileStore.update_account).parameters

        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "x@example.com"}}),
            encoding="utf-8",
        )

        real_update = ProfileStore.update_account
        captured: dict[str, object] = {}

        def _capture(*args, **kwargs):
            captured["kwargs"] = dict(kwargs)
            return real_update(*args, **kwargs)

        monkeypatch.setattr(ProfileStore, "update_account", _capture)

        assert promote_legacy_account(storage) is True
        assert "deadline_seconds" not in captured["kwargs"]

    def test_strip_failure_after_successful_embed_still_returns_true(self, tmp_path, monkeypatch):
        """The embed's success is independent of the strip's outcome: a
        (hypothetical — ``LegacyAccountContext.scrub`` already swallows every
        realistic OSError/JSONDecodeError internally) exception during the
        cosmetic legacy-key cleanup must not erase a promotion that already
        durably committed, nor propagate up through ``read_account_metadata``
        (which calls this function with no try/except of its own, trusting it
        never to raise)."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 6, "email": "frank@example.com"}}),
            encoding="utf-8",
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated unexpected failure during cleanup")

        monkeypatch.setattr(LegacyAccountContext, "scrub", _boom)

        assert promote_legacy_account(storage) is True
        in_band = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]["account"]
        assert in_band == {"authuser": 6, "email": "frank@example.com"}
        # Also exercise the actual caller contract: read_account_metadata must
        # not raise either, and must see the successfully-embedded record.
        assert read_account_metadata(storage) == {"authuser": 6, "email": "frank@example.com"}

    def test_concurrent_fresh_write_during_promotion_is_not_clobbered(self, tmp_path):
        """The genuine RACE (not just a pre-existing state): a concurrent fresh
        login/account-switch commits its new record in the window AFTER this
        call has already read the stale legacy record but BEFORE it writes.
        The stale write must not win — closes the check-then-act race a prior
        review round found (unlocked pre-check + unconditional overwrite).

        Simulated deterministically via a monkeypatch on ``LegacyAccountContext.read``
        (the first thing the migrator calls) that performs the
        "concurrent" fresh write as a side effect before returning — so by the
        time ``promote_legacy_account`` reaches its own write, a different
        record is already committed, exactly reproducing the race's timing
        without needing real thread synchronization."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 2, "email": "stale@example.com"}}),
            encoding="utf-8",
        )
        real_read_legacy = LegacyAccountContext.read

        def _read_legacy_then_race_a_fresh_write(context, path):
            legacy = real_read_legacy(context, path)
            from notebooklm._auth import storage as _sw

            _sw.update_account_metadata(path, authuser=0, email="fresh@example.com")
            return legacy

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(LegacyAccountContext, "read", _read_legacy_then_race_a_fresh_write)
            assert promote_legacy_account(storage) is False  # lost the race, not "nothing to do"

        in_band = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]["account"]
        assert in_band == {"authuser": 0, "email": "fresh@example.com"}  # winner's data, intact

    def test_sanitizes_malformed_legacy_authuser_and_blank_email(self, tmp_path):
        """Same sanitization ``get_authuser_for_storage`` applies on read: a
        non-int/negative ``authuser`` falls back to 0, a blank email is dropped."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": -1, "email": "   "}}), encoding="utf-8"
        )

        assert promote_legacy_account(storage) is True

        in_band = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]["account"]
        assert in_band == {"authuser": 0}
        assert LegacyAccountContext().read(storage) is None

    def test_promotion_failure_leaves_legacy_record_intact(self, tmp_path, monkeypatch, caplog):
        """Best-effort by design: if the in-band embed raises, the legacy record
        is NOT scrubbed — a failed promotion must not lose the only copy of the
        user's account binding. Also pins that the failure is logged at
        WARNING (not DEBUG): a persistent cause means every subsequent read of
        this profile silently falls back to the legacy record, so an operator
        needs a default-visible signal, not one gated behind -v/--debug."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 4, "email": "dana@example.com"}}),
            encoding="utf-8",
        )

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(ProfileStore, "update_account", _boom)

        with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
            assert promote_legacy_account(storage) is False
        assert LegacyAccountContext().read(storage) == {
            "authuser": 4,
            "email": "dana@example.com",
        }
        assert any(
            r.levelno == logging.WARNING and "disk full" in r.message for r in caplog.records
        )

    def test_promotion_failure_warns_every_time_no_throttle(self, tmp_path, monkeypatch, caplog):
        """The warn-once-per-path throttle is GONE (ADR-0033 PR 5.1).

        It existed because promotion ran on the per-RPC read path, where a
        persistently failing profile would have warned twice per request
        forever. The one-shot makes that structurally impossible — a read
        schedules at most ONE promotion per path per process — so the throttle
        was compensating for a problem that no longer exists, and suppressing
        the second warning now only hides the startup-migration and
        ``replace_from_login`` failures from an operator.

        Pinned by calling promotion directly twice: both failures warn."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 4, "email": "dana@example.com"}}),
            encoding="utf-8",
        )

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(ProfileStore, "update_account", _boom)

        with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
            promote_legacy_account(storage)
            caplog.clear()
            promote_legacy_account(storage)  # same path, still a WARNING

        assert any(
            r.levelno == logging.WARNING and "disk full" in r.message for r in caplog.records
        )
        assert not hasattr(_profile_migration, "_PROMOTION_WARNED_PATHS")

    def test_read_account_metadata_derives_legacy_when_promotion_fails(self, tmp_path, monkeypatch):
        """A failing durable write must not collapse ``read_account_metadata``
        to ``{}`` — that would silently reintroduce the wrong-account-routing
        hazard this migration exists to close, just via a different trigger.

        Since ADR-0033 PR 5.1 the read never waits for the write at all: it
        derives the record read-only and returns, so the write's outcome is
        invisible to it. This test keeps the pin on the *observable* contract
        (never ``{}``, always sanitized exactly as a promotion would embed)
        and additionally drains the worker to show the failure changed
        nothing."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": -1, "email": "  "}}),
            encoding="utf-8",
        )

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(ProfileStore, "update_account", _boom)

        result = read_account_metadata(storage)
        # Never {} — and sanitized (malformed authuser -> 0, blank email dropped)
        # exactly as ``_sanitize_legacy_account_record`` would embed it.
        assert result == {"authuser": 0}
        _profile_migration.LegacyPromotionScheduler.process_default().drain(30.0)
        # The legacy record is untouched — nothing was scrubbed on a failure.
        assert LegacyAccountContext().read(storage) == {"authuser": -1, "email": "  "}
        # And the read still answers identically after the failed attempt.
        assert read_account_metadata(storage) == {"authuser": 0}


class TestLegacyAccountContextScrub:
    """``LegacyAccountContext.scrub`` migration branches."""

    def test_no_context_file_is_noop(self, tmp_path):
        # context.json missing → early return (line ~348-349).
        LegacyAccountContext().scrub(tmp_path / "storage_state.json")

    def test_malformed_context_json_skipped(self, tmp_path):
        # Read under lock raises → debug log + return (line ~357-359).
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text("not json", encoding="utf-8")
        LegacyAccountContext().scrub(storage)  # no raise
        assert (tmp_path / "context.json").read_text(encoding="utf-8") == "not json"

    def test_non_dict_or_missing_account_key_returns(self, tmp_path):
        # data is a dict but no account key → return without write (line 360-361).
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(json.dumps({"notebook_id": "nb"}), encoding="utf-8")
        LegacyAccountContext().scrub(storage)
        assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8")) == {
            "notebook_id": "nb"
        }

    def test_account_key_removed_but_other_state_preserved(self, tmp_path):
        # account dropped, remaining state rewritten (line 363-364).
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"notebook_id": "nb", "account": {"authuser": 1}}),
            encoding="utf-8",
        )
        LegacyAccountContext().scrub(storage)
        assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8")) == {
            "notebook_id": "nb"
        }

    def test_account_only_context_file_unlinked(self, tmp_path):
        # When account was the sole key, the file is removed (line 366).
        storage = tmp_path / "storage_state.json"
        context = tmp_path / "context.json"
        context.write_text(json.dumps({"account": {"authuser": 1}}), encoding="utf-8")
        LegacyAccountContext().scrub(storage)
        assert not context.exists()

    def test_oserror_from_lock_is_swallowed(self, tmp_path, monkeypatch):
        # Best-effort migration: an OSError acquiring the lock is swallowed
        # (lines 367-369).
        storage = tmp_path / "storage_state.json"
        context = tmp_path / "context.json"
        context.write_text(json.dumps({"account": {"authuser": 1}}), encoding="utf-8")

        class _BoomLock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                raise OSError("lock unavailable")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(_profile_migration, "FileLock", _BoomLock)
        LegacyAccountContext().scrub(storage)  # swallows OSError, no raise
        # Untouched because the lock failed before any read/write.
        assert json.loads(context.read_text(encoding="utf-8")) == {"account": {"authuser": 1}}


class TestClearInBandAccount:
    """``_clear_in_band_account`` no-op / branch coverage
    (lines 447, 469-471, 473, 481, 483-484)."""

    def test_missing_file_is_noop(self, tmp_path):
        _clear_in_band_account(tmp_path / "missing.json")  # early return

    def test_malformed_json_skipped(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("not json", encoding="utf-8")
        _clear_in_band_account(storage)  # debug-log + return, no raise
        assert storage.read_text(encoding="utf-8") == "not json"

    def test_non_dict_payload_returns(self, tmp_path):
        # data loads but is not a dict → return (line 481).
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps(["x"]), encoding="utf-8")
        _clear_in_band_account(storage)
        assert json.loads(storage.read_text(encoding="utf-8")) == ["x"]

    def test_namespace_missing_account_key_records_clear(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps({"cookies": [], "notebooklm": {"version": 1}}), encoding="utf-8"
        )
        _clear_in_band_account(storage)
        assert json.loads(storage.read_text(encoding="utf-8"))["notebooklm"] == {
            "version": 1,
            "account_route_cleared": True,
        }

    def test_account_clear_records_authoritative_absence(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=2, email="bob@example.com")
        assert "notebooklm" in json.loads(storage.read_text(encoding="utf-8"))
        _clear_in_band_account(storage)
        data = json.loads(storage.read_text(encoding="utf-8"))
        assert data["notebooklm"] == {
            "version": 1,
            "account_route_cleared": True,
        }

    def test_account_cleared_keeps_namespace_with_extra_keys(self, tmp_path):
        # namespace carries an extra (non-version) key → namespace retained.
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "notebooklm": {
                        "version": 1,
                        "account": {"authuser": 1},
                        "extra": "keep-me",
                    },
                }
            ),
            encoding="utf-8",
        )
        _clear_in_band_account(storage)
        namespace = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]
        assert "account" not in namespace
        assert namespace["extra"] == "keep-me"


class TestClearAccountMetadataFacade:
    """``clear_account_metadata`` covers in-band + legacy paths together."""

    def test_none_storage_path_is_noop(self):
        clear_account_metadata(None)

    def test_clears_both_in_band_and_legacy(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=1, email="alice@example.com")
        (tmp_path / "context.json").write_text(
            json.dumps({"notebook_id": "nb", "account": {"authuser": 1}}),
            encoding="utf-8",
        )

        clear_account_metadata(storage)

        assert json.loads(storage.read_text(encoding="utf-8"))["notebooklm"] == {
            "version": 1,
            "account_route_cleared": True,
        }
        assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8")) == {
            "notebook_id": "nb"
        }


class TestAccountEmailAndAuthuserValue:
    """Residual branches in email/authuser-value resolution (lines 317, 329->331)."""

    def test_get_account_email_returns_none_when_absent(self, tmp_path):
        # No metadata at all → falls through to None (line 317).
        assert get_account_email_for_storage(tmp_path / "storage_state.json") is None

    def test_get_account_email_returns_none_for_blank_email(self, tmp_path):
        # Persisted email is whitespace-only → stripped to "" → None (line 317).
        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=1, email="alice@example.com")
        # Overwrite the in-band email with a blank string.
        data = json.loads(storage.read_text(encoding="utf-8"))
        data["notebooklm"]["account"]["email"] = "   "
        storage.write_text(json.dumps(data), encoding="utf-8")
        assert get_account_email_for_storage(storage) is None

    def test_format_authuser_value_blank_email_falls_back_to_index(self):
        # Whitespace-only email is ignored; integer index is used (329->331).
        assert format_authuser_value(2, "   ") == "2"

    def test_format_authuser_value_prefers_real_email(self):
        assert format_authuser_value(0, "bob@example.com") == "bob@example.com"

    def test_format_authuser_value_default_index(self):
        assert format_authuser_value() == "0"


class TestLegacyScrubMalformedUnderLock:
    """``LegacyAccountContext.scrub`` malformed-read-under-lock branch."""

    def test_malformed_json_under_lock_is_skipped(self, tmp_path):
        # The file exists with content that survives the first existence check
        # but fails json parsing while holding the lock → debug-log + return.
        storage = tmp_path / "storage_state.json"
        context = tmp_path / "context.json"
        context.write_text("{not valid json", encoding="utf-8")
        LegacyAccountContext().scrub(storage)  # no raise
        assert context.read_text(encoding="utf-8") == "{not valid json"


class TestWriteAccountMetadataNamespaceReplace:
    """``write_account_metadata`` namespace handling (lines 410, 410->412)."""

    def test_non_dict_namespace_is_replaced(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps({"cookies": [], "origins": [], "notebooklm": "corrupt"}),
            encoding="utf-8",
        )
        write_account_metadata(storage, authuser=3, email="carol@example.com")
        namespace = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]
        assert namespace["version"] == 1
        assert namespace["account"] == {"authuser": 3, "email": "carol@example.com"}

    def test_existing_dict_namespace_is_updated_in_place(self, tmp_path):
        # File already carries a valid ``notebooklm`` dict namespace → the
        # ``isinstance`` guard is True so the reassignment is skipped (410->412).
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "origins": [],
                    "notebooklm": {"version": 1, "extra": "keep", "account": {"authuser": 0}},
                }
            ),
            encoding="utf-8",
        )
        write_account_metadata(storage, authuser=5, email="dave@example.com")
        namespace = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]
        assert namespace["account"] == {"authuser": 5, "email": "dave@example.com"}
        assert namespace["extra"] == "keep"


class TestLegacyScrubTocTouRecheck:
    """Inner existence re-check return under the lock (line 354)."""

    def test_file_vanishes_after_lock_acquired(self, tmp_path, monkeypatch):
        storage = tmp_path / "storage_state.json"
        context = tmp_path / "context.json"
        context.write_text(json.dumps({"account": {"authuser": 1}}), encoding="utf-8")

        original_exists = Path.exists
        calls = {"n": 0}

        def flaky_exists(self):
            # First call (outer guard) sees the file; second call (inner
            # re-check under the lock) reports it gone, exercising the TOCTOU
            # return at line 354.
            if self == context:
                calls["n"] += 1
                if calls["n"] >= 2:
                    return False
            return original_exists(self)

        monkeypatch.setattr(Path, "exists", flaky_exists)
        LegacyAccountContext().scrub(storage)  # returns without touching the file
        # File is left intact because the inner re-check short-circuited.
        assert json.loads(context.read_text(encoding="utf-8")) == {"account": {"authuser": 1}}


class TestClearInBandLockFailure:
    """Best-effort lock-unavailable handling in ``_clear_in_band_account``.

    The in-band clear now delegates to ``storage.clear_in_band_account``,
    which serializes on the unified storage lock manager. When the
    lock is unavailable the clear stays best-effort (swallows, never raises) and
    leaves the file untouched — the legacy reader still resolves the record.
    """

    def test_lock_unavailable_is_swallowed(self, tmp_path, monkeypatch):
        import contextlib

        from notebooklm._auth import profile_store as _profile_store
        from notebooklm._auth.storage_lock import LockState

        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=1)

        class UnavailableLocks:
            @contextlib.contextmanager
            def acquire(self, request):
                yield LockState.UNAVAILABLE

        monkeypatch.setattr(_profile_store, "_STORAGE_LOCKS", UnavailableLocks())
        # Should swallow the lock-unavailable outcome and not raise.
        _clear_in_band_account(storage)
        # File untouched because the lock was unavailable before any write.
        assert "notebooklm" in json.loads(storage.read_text(encoding="utf-8"))
