"""The account read path takes no locks and issues no writes (ADR-0033 PR 5.1).

``account.read_account_metadata`` is documented as a fast, lock-free read and
sits on the per-RPC token-route path (``refresh._resolve_token_route_kwargs``
-> ``get_authuser_for_storage`` -> here). It used to call
``promote_legacy_account`` inline whenever the in-band record was absent, which
took the storage WRITE lock — the read wrote. The 2-second promotion deadline
and the per-path warning throttle existed only to compensate for that.

Now the read DERIVES its answer read-only from the legacy sibling and returns;
the durable promotion is a detached single-flight per canonical path. Completed
workers leave the path retryable, and an in-band winner with a stale sibling
schedules the privacy scrub, so interrupted migrations self-heal. This module is
the proof, in four parts:

1. **The derivation is indistinguishable from the promotion** (the
   anti-wrong-account contract). A missed or mangled ``authuser`` routes
   requests to a *different* signed-in Google account, so "we can answer
   without writing" is only true if the answer is byte-identical to what the
   write would have produced. Proven field-by-field over a matrix of legacy
   shapes, including the malformed ones.
2. **Zero locks on the read.** Not "a short lock" — none, on either branch.
3. **Retryable single-flight.** N concurrent readers produce ONE promotion;
   after that worker settles, a later read can retry a transient failure.
4. **All three entry paths reach reconciliation** (per-RPC, CLI, env-auth) —
   the precondition for deleting the deadline and the throttle at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from notebooklm._auth import storage as _auth_storage
from notebooklm._auth.profile_migration import (
    LegacyAccountContext,
    LegacyAccountMigrator,
    LegacyPromotionScheduler,
)
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._auth.storage import (
    get_account_email_for_storage,
    get_authuser_for_storage,
    read_account_metadata,
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

# Legacy ``context.json[account]`` payloads, paired with the record every
# reader must see for them. Deliberately includes the malformed shapes, because
# those are where a second implementation of the sanitizer would drift: a
# ``bool`` is an ``int`` subclass, a negative index is not addressable, a
# whitespace-only email is not an identity, and unknown keys must not leak
# into a record downstream code treats as the account binding.
_LEGACY_MATRIX: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "plain",
        {"authuser": 3, "email": "charlie@example.com"},
        {"authuser": 3, "email": "charlie@example.com"},
    ),
    ("authuser_only", {"authuser": 2}, {"authuser": 2}),
    ("email_only", {"email": "dana@example.com"}, {"authuser": 0, "email": "dana@example.com"}),
    (
        "zero_authuser",
        {"authuser": 0, "email": "e@example.com"},
        {"authuser": 0, "email": "e@example.com"},
    ),
    (
        "negative_authuser",
        {"authuser": -1, "email": "f@example.com"},
        {"authuser": 0, "email": "f@example.com"},
    ),
    ("string_authuser", {"authuser": "4"}, {"authuser": 0}),
    ("bool_authuser", {"authuser": True}, {"authuser": 0}),
    ("blank_email", {"authuser": 5, "email": "   "}, {"authuser": 5}),
    ("non_string_email", {"authuser": 6, "email": 42}, {"authuser": 6}),
    (
        "padded_email",
        {"authuser": 7, "email": "  g@example.com  "},
        {"authuser": 7, "email": "g@example.com"},
    ),
    (
        "extra_keys",
        {"authuser": 8, "email": "h@example.com", "nickname": "h"},
        {"authuser": 8, "email": "h@example.com"},
    ),
]


def _legacy_profile(root: Path, legacy_account: dict[str, Any]) -> Path:
    """Write a legacy-only profile: real cookies in-band, account in the sibling."""
    storage = root / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [{"name": "SID", "value": "v", "domain": ".google.com", "path": "/"}],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    (root / "context.json").write_text(
        json.dumps({"account": legacy_account, "notebook_id": "nb-1"}), encoding="utf-8"
    )
    return storage


def _canonical(storage: Path) -> str:
    """The scheduler key for one profile's single flight."""
    return str(ProfileStore(storage).ordering_key)


def _scheduler() -> LegacyPromotionScheduler:
    return LegacyPromotionScheduler.process_default()


def _drain_promotions_for_tests() -> None:
    _scheduler().drain(30.0)


def _wait_for_active_paths_to_clear(timeout: float = 30.0) -> None:
    """Poll until no promotion worker is active, sharing ``drain``'s budget.

    A fixed short deadline here previously flaked on CI runners under load
    (observed on Windows): the detached worker had not yet run by the time the
    poll gave up, so the assertion that follows saw pre-promotion state instead
    of a genuine failure.
    """
    deadline = time.monotonic() + timeout
    while _scheduler()._active_paths_for_tests() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not _scheduler()._active_paths_for_tests()


def _in_band_on_disk(storage: Path) -> dict[str, Any] | None:
    data = json.loads(storage.read_text(encoding="utf-8"))
    namespace = data.get("notebooklm")
    if not isinstance(namespace, dict):
        return None
    account = namespace.get("account")
    return account if isinstance(account, dict) else None


class _CountingLock:
    """A ``threading.Lock`` stand-in that records every acquisition."""

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self.acquisitions = 0

    def __enter__(self):
        self.acquisitions += 1
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class TestDerivedRecordEqualsPromotedRecord:
    """The anti-wrong-account differential.

    Read-only derivation must produce EXACTLY what a completed promotion
    produces — same keys, same values, same types — or moving the write off
    the read path silently changes which Google account requests route to.
    """

    @pytest.mark.parametrize(
        ("label", "legacy", "expected"),
        _LEGACY_MATRIX,
        ids=[case[0] for case in _LEGACY_MATRIX],
    )
    def test_every_field_matches_before_and_after_the_durable_write(
        self, tmp_path, label, legacy, expected
    ):
        storage = _legacy_profile(tmp_path, legacy)

        # (a) Derived read-only. Deliberately NO assertion that the on-disk
        # record is still absent here: the promotion is detached, so on a fast
        # or loaded machine the worker can legitimately have committed before
        # this line runs. That is a race against the scheduler, not a property
        # of the read. "The read does not write" is a claim about the READER'S
        # THREAD, and it is pinned by
        # ``test_legacy_read_never_enters_the_storage_writer_on_the_readers_thread``.
        derived = read_account_metadata(storage)

        # (b) Let the detached reconciliation commit the durable record.
        _drain_promotions_for_tests()
        promoted = _in_band_on_disk(storage)
        assert promoted is not None, "reconciliation must still make the migration durable"

        # (c) The same read, now served from the genuinely in-band record.
        after = read_account_metadata(storage)

        # Field-for-field on all three, not just == on two of them: identical
        # key sets, identical values, identical value types (an ``authuser``
        # that became ``True`` or ``"3"`` would still compare equal to 1 / 3
        # under a looser assertion).
        for name, record in (("derived", derived), ("promoted", promoted), ("after", after)):
            assert set(record) == set(expected), f"{name}: key set drifted"
            for key, value in expected.items():
                assert record[key] == value, f"{name}[{key}] value drifted"
                assert type(record[key]) is type(value), f"{name}[{key}] type drifted"

    @pytest.mark.parametrize(
        ("label", "legacy", "expected"),
        _LEGACY_MATRIX,
        ids=[case[0] for case in _LEGACY_MATRIX],
    )
    def test_routing_helpers_agree_before_and_after(self, tmp_path, label, legacy, expected):
        """The two helpers the token route actually calls, not just the dict.

        ``get_authuser_for_storage`` / ``get_account_email_for_storage`` are
        what ``_resolve_token_route_kwargs`` uses to build the ``authuser``
        the wire carries, so they are where a wrong-account regression would
        actually surface.
        """
        storage = _legacy_profile(tmp_path, legacy)

        before = (get_authuser_for_storage(storage), get_account_email_for_storage(storage))
        _drain_promotions_for_tests()
        after = (get_authuser_for_storage(storage), get_account_email_for_storage(storage))

        assert before == after
        assert before == (expected["authuser"], expected.get("email"))

    def test_derived_record_is_never_a_raw_legacy_passthrough(self, tmp_path):
        """Explicit inverse of the hazard: the sibling's own extra keys and
        unsanitized values must never reach a caller."""
        storage = _legacy_profile(
            tmp_path, {"authuser": -5, "email": "  x@example.com ", "browser_profile": "Profile 1"}
        )
        result = read_account_metadata(storage)
        assert result == {"authuser": 0, "email": "x@example.com"}
        assert "browser_profile" not in result


class TestReadTakesNoLocks:
    """Requirement: the per-RPC read takes ZERO locks in the common case."""

    def test_in_band_fast_path_takes_no_lock_and_schedules_nothing(self, tmp_path, monkeypatch):
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "origins": [],
                    "notebooklm": {"version": 1, "account": {"authuser": 4, "email": "i@x.com"}},
                }
            ),
            encoding="utf-8",
        )
        counting = _CountingLock()
        scheduler = LegacyPromotionScheduler()
        scheduler._registry_lock = counting
        monkeypatch.setattr(LegacyPromotionScheduler, "_process_default_scheduler", scheduler)

        def _fail(*args, **kwargs):  # pragma: no cover - assertion callback
            raise AssertionError("the read must not enter the storage writer")

        monkeypatch.setattr(ProfileStore, "update_account", _fail)

        for _ in range(5):
            assert read_account_metadata(storage) == {"authuser": 4, "email": "i@x.com"}

        assert counting.acquisitions == 0
        assert not scheduler._scheduled_paths_for_tests()
        assert not scheduler._workers_for_tests()

    def test_empty_profile_fast_path_takes_no_lock(self, tmp_path, monkeypatch):
        """No in-band record AND no legacy sibling — the fresh-profile case."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        counting = _CountingLock()
        scheduler = LegacyPromotionScheduler()
        scheduler._registry_lock = counting
        monkeypatch.setattr(LegacyPromotionScheduler, "_process_default_scheduler", scheduler)

        assert read_account_metadata(storage) == {}
        assert counting.acquisitions == 0

    def test_legacy_read_never_enters_the_storage_writer_on_the_readers_thread(
        self, tmp_path, monkeypatch
    ):
        """The one case that DOES write proves the write is off-thread.

        The scheduling lock is taken (once, briefly) on this branch; the
        storage lock is not, on this thread, ever.
        """
        storage = _legacy_profile(tmp_path, {"authuser": 9, "email": "j@example.com"})
        real_update = ProfileStore.update_account
        threads_seen: list[threading.Thread] = []

        def _record(*args, **kwargs):
            threads_seen.append(threading.current_thread())
            return real_update(*args, **kwargs)

        monkeypatch.setattr(ProfileStore, "update_account", _record)
        reader = threading.current_thread()

        assert read_account_metadata(storage) == {"authuser": 9, "email": "j@example.com"}
        # The claim is about WHICH THREAD writes, not about whether the write
        # has landed yet. Asserting ``threads_seen == []`` here would also
        # forbid the detached worker from having finished already — a race
        # against the scheduler that fails on fast/loaded machines while
        # reporting "the durable write happened on the reader's thread", which
        # is the opposite of what actually happened.
        assert all(t is not reader for t in threads_seen), (
            "the durable write happened on the reader's thread"
        )

        _drain_promotions_for_tests()
        assert threads_seen, "the durable write never happened at all"
        assert all(t is not reader for t in threads_seen)

    def test_worker_is_a_daemon_so_it_cannot_wedge_interpreter_shutdown(self, tmp_path):
        storage = _legacy_profile(tmp_path, {"authuser": 1})
        workers: list[threading.Thread] = []

        def _thread_factory(**kwargs):
            worker = threading.Thread(**kwargs)
            workers.append(worker)
            return worker

        scheduler = LegacyPromotionScheduler(thread_factory=_thread_factory)
        assert scheduler.schedule(ProfileStore(storage), LegacyAccountMigrator())
        worker = workers[0]
        assert worker.daemon is True
        worker.join(30.0)


class TestPromotionRacingTheReader:
    """The reader samples two files; a promotion may commit between the samples.

    Promotion is embed-then-strip across ``storage_state.json`` and the sibling
    ``context.json``, under two different locks, so at no INSTANT is the binding
    absent from both. That is not enough for a reader, which does not sample the
    two files at the same instant: an in-band sample taken before the embed plus
    a sibling sample taken after the strip observes a state that never existed on
    disk. Returning ``{}`` there means ``authuser=0`` and routes requests to a
    different signed-in Google account — the #2103 hazard reached by timing.

    Deterministic by construction: the promotion is driven from inside the
    reader's own sibling read, so it does not depend on scheduling. The bug this
    pins was originally caught by CI flaking on four runners.
    """

    def test_promotion_committing_between_the_two_samples_keeps_the_binding(
        self, tmp_path, monkeypatch
    ):
        storage = _legacy_profile(tmp_path, {"authuser": 7, "email": "race@example.com"})
        real_read_legacy = LegacyAccountContext.read
        promoted_during_read: list[bool] = []

        promoting: list[bool] = []

        def _promote_mid_read(context: LegacyAccountContext, path: Path) -> dict[str, Any] | None:
            """Let a full promotion land, THEN take the sibling sample.

            This is the exact interleaving. The caller already sampled in-band
            and found it absent; by the time it samples the sibling, both the
            embed and the strip have happened, so the sibling reads empty. The
            reader is now holding two samples that were never simultaneously
            true, and neither one carries the binding.

            ``promoting`` is a re-entrancy guard: ``promote_legacy_account``
            reads the sibling itself, and those nested reads must see the real
            pre-strip record or the promotion has nothing to promote.
            """
            if promoted_during_read or promoting:
                return real_read_legacy(context, path)
            promoting.append(True)
            try:
                promoted_during_read.append(True)
                assert _auth_storage.promote_legacy_account(path) is True
            finally:
                promoting.pop()
            legacy = real_read_legacy(context, path)
            # The premise of the interleaving: this sample is empty.
            assert legacy is None, "the strip did not happen; test would be vacuous"
            return legacy

        monkeypatch.setattr(LegacyAccountContext, "read", _promote_mid_read)

        record = read_account_metadata(storage)

        assert promoted_during_read, "the interleaving never happened; test is vacuous"
        assert record == {"authuser": 7, "email": "race@example.com"}, (
            "a promotion landing between the in-band and sibling samples dropped "
            "the account binding — authuser would fall back to 0 and route to a "
            "different Google account"
        )

    def test_a_genuinely_empty_profile_still_reads_empty(self, tmp_path):
        """The re-read must not invent a record where there never was one."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        assert read_account_metadata(storage) == {}


class TestRetryableSingleFlight:
    """Requirement: N concurrent reads trigger at most ONE promotion."""

    def test_concurrent_reads_of_one_profile_promote_exactly_once(self, tmp_path, monkeypatch):
        storage = _legacy_profile(tmp_path, {"authuser": 3, "email": "k@example.com"})
        real_update = ProfileStore.update_account
        calls = []
        calls_lock = threading.Lock()

        def _count(*args, **kwargs):
            with calls_lock:
                calls.append(1)
            time.sleep(0.05)  # widen the window a duplicate would land in
            return real_update(*args, **kwargs)

        monkeypatch.setattr(ProfileStore, "update_account", _count)

        readers = 8
        start = threading.Barrier(readers)
        results: list[dict[str, Any]] = []
        results_lock = threading.Lock()

        def _read():
            start.wait(timeout=30)
            value = read_account_metadata(storage)
            with results_lock:
                results.append(value)

        threads = [threading.Thread(target=_read) for _ in range(readers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30.0)

        _drain_promotions_for_tests()

        assert len(results) == readers
        assert all(r == {"authuser": 3, "email": "k@example.com"} for r in results)
        assert len(calls) == 1, f"expected exactly one promotion, got {len(calls)}"
        assert sorted(_scheduler()._scheduled_paths_for_tests()) == [_canonical(storage)]

    def test_a_failed_promotion_is_retried_by_a_later_read(self, tmp_path, monkeypatch):
        """A settled failure stays off-thread but no longer poisons the process."""
        storage = _legacy_profile(tmp_path, {"authuser": 2, "email": "l@example.com"})
        attempts = []
        real_update = ProfileStore.update_account

        def _fail_once(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("transient contention")
            return real_update(*args, **kwargs)

        monkeypatch.setattr(ProfileStore, "update_account", _fail_once)

        assert read_account_metadata(storage) == {"authuser": 2, "email": "l@example.com"}
        _wait_for_active_paths_to_clear()

        assert read_account_metadata(storage) == {"authuser": 2, "email": "l@example.com"}
        _drain_promotions_for_tests()

        assert len(attempts) == 2
        assert _in_band_on_disk(storage) == {"authuser": 2, "email": "l@example.com"}

    def test_a_failed_post_write_scrub_is_retried_from_the_in_band_state(
        self, tmp_path, monkeypatch
    ):
        """The embed→scrub crash state must not strand the email at rest (#2228)."""
        storage = _legacy_profile(tmp_path, {"authuser": 2, "email": "privacy@example.com"})
        real_scrub = LegacyAccountContext.scrub
        scrubs = 0

        def _fail_once(context, path):
            nonlocal scrubs
            scrubs += 1
            if scrubs == 1:
                return False
            return real_scrub(context, path)

        monkeypatch.setattr(LegacyAccountContext, "scrub", _fail_once)

        assert read_account_metadata(storage)["email"] == "privacy@example.com"
        _wait_for_active_paths_to_clear()
        assert _in_band_on_disk(storage) == {
            "authuser": 2,
            "email": "privacy@example.com",
        }
        assert "account" in json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))

        assert read_account_metadata(storage)["email"] == "privacy@example.com"
        _drain_promotions_for_tests()

        assert scrubs == 2
        assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8")) == {
            "notebook_id": "nb-1"
        }

    def test_distinct_profiles_each_get_their_own_flight(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        first = _legacy_profile(tmp_path / "a", {"authuser": 1, "email": "a@x.com"})
        second = _legacy_profile(tmp_path / "b", {"authuser": 2, "email": "b@x.com"})

        assert read_account_metadata(first) == {"authuser": 1, "email": "a@x.com"}
        assert read_account_metadata(second) == {"authuser": 2, "email": "b@x.com"}
        _drain_promotions_for_tests()

        assert _in_band_on_disk(first) == {"authuser": 1, "email": "a@x.com"}
        assert _in_band_on_disk(second) == {"authuser": 2, "email": "b@x.com"}


class TestPromotionCannotAffectTheRead:
    """Requirement: a failed or slow promotion changes neither value nor latency."""

    def test_a_slow_promotion_does_not_delay_the_read(self, tmp_path, monkeypatch):
        storage = _legacy_profile(tmp_path, {"authuser": 5, "email": "m@example.com"})
        real_update = ProfileStore.update_account
        released = threading.Event()

        def _slow(*args, **kwargs):
            released.wait(timeout=30)
            return real_update(*args, **kwargs)

        monkeypatch.setattr(ProfileStore, "update_account", _slow)
        try:
            started = time.monotonic()
            assert read_account_metadata(storage) == {"authuser": 5, "email": "m@example.com"}
            elapsed = time.monotonic() - started
            # The worker is parked indefinitely; the read is not. A generous
            # bound — the point is that it does not wait on ``released``.
            assert elapsed < 1.0, f"the read waited on the promotion ({elapsed:.2f}s)"
            # And it keeps answering while the write is still parked.
            assert read_account_metadata(storage) == {"authuser": 5, "email": "m@example.com"}
        finally:
            released.set()
            _drain_promotions_for_tests()

    def test_a_permanently_failing_promotion_never_breaks_the_read(self, tmp_path, monkeypatch):
        storage = _legacy_profile(tmp_path, {"authuser": 6, "email": "n@example.com"})

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated writer explosion")

        monkeypatch.setattr(ProfileStore, "update_account", _boom)

        assert read_account_metadata(storage) == {"authuser": 6, "email": "n@example.com"}
        _drain_promotions_for_tests()
        # Legacy sibling untouched, so the binding is still recoverable, and
        # the read still answers.
        assert read_account_metadata(storage) == {"authuser": 6, "email": "n@example.com"}
        legacy = json.loads((storage.with_name("context.json")).read_text(encoding="utf-8"))
        assert legacy["account"] == {"authuser": 6, "email": "n@example.com"}

    def test_a_crashing_worker_is_contained(self, tmp_path):
        """``_run_promotion_once`` must swallow even what ``promote_legacy_account``
        does not — a detached worker has no caller to raise to, and its
        deregistration must still happen."""

        class ExplodingMigrator:
            def promote(self, _store):
                raise BaseException("not even an Exception")  # noqa: TRY002

        workers: list[threading.Thread] = []

        def _thread_factory(**kwargs):
            worker = threading.Thread(**kwargs)
            workers.append(worker)
            return worker

        scheduler = LegacyPromotionScheduler(thread_factory=_thread_factory)
        storage = _legacy_profile(tmp_path, {"authuser": 1})
        assert scheduler.schedule(ProfileStore(storage), ExplodingMigrator())  # type: ignore[arg-type]
        scheduler.drain(30.0)
        assert not scheduler._workers_for_tests()


class TestInBandAlwaysWins:
    """The re-check that keeps a concurrent login from being overtaken."""

    def test_empty_placeholder_is_promoted_then_scrubbed_end_to_end(self, tmp_path):
        """An empty typed placeholder is absent for read and under-lock promotion."""
        legacy = {"authuser": 6, "email": " legacy@example.com "}
        storage = _legacy_profile(tmp_path, legacy)
        payload = json.loads(storage.read_text(encoding="utf-8"))
        payload["notebooklm"] = {"version": 1, "account": {}}
        storage.write_text(json.dumps(payload), encoding="utf-8")

        expected = {"authuser": 6, "email": "legacy@example.com"}
        assert read_account_metadata(storage) == expected
        _drain_promotions_for_tests()

        assert _in_band_on_disk(storage) == expected
        context = tmp_path / "context.json"
        assert json.loads(context.read_text(encoding="utf-8")) == {"notebook_id": "nb-1"}
        assert read_account_metadata(storage) == expected

    def test_non_empty_unknown_mapping_wins_and_is_not_overwritten(self, tmp_path):
        storage = _legacy_profile(tmp_path, {"authuser": 6, "email": "legacy@example.com"})
        payload = json.loads(storage.read_text(encoding="utf-8"))
        payload["notebooklm"] = {"version": 1, "account": {"unknown": [1, 2]}}
        storage.write_text(json.dumps(payload), encoding="utf-8")

        assert _auth_storage.promote_legacy_account(storage) is False
        assert _in_band_on_disk(storage) == {"unknown": [1, 2]}
        context = tmp_path / "context.json"
        assert json.loads(context.read_text(encoding="utf-8")) == {"notebook_id": "nb-1"}

    def test_in_band_beats_a_stale_legacy_sibling_and_schedules_its_scrub(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "origins": [],
                    "notebooklm": {"version": 1, "account": {"authuser": 9, "email": "new@x.com"}},
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "old@x.com"}}), encoding="utf-8"
        )

        assert read_account_metadata(storage) == {"authuser": 9, "email": "new@x.com"}
        _drain_promotions_for_tests()
        assert _in_band_on_disk(storage) == {"authuser": 9, "email": "new@x.com"}
        assert not (tmp_path / "context.json").exists()

    def test_a_login_landing_during_the_sibling_read_still_wins(self, tmp_path):
        """The narrow window the second in-band check closes.

        A fresh login/account-switch that commits between this read's first
        in-band check and its return must not be overtaken by the stale legacy
        record — that is a wrong-account route. Driven deterministically by
        performing the "concurrent" write as a side effect of the sibling read,
        the same technique ``test_auth_account_coverage`` uses for the writer's
        own check-then-act race.
        """
        storage = _legacy_profile(tmp_path, {"authuser": 1, "email": "old@x.com"})
        real_read_legacy = LegacyAccountContext.read

        def _read_then_race_a_login(context, path):
            legacy = real_read_legacy(context, path)
            _auth_storage.update_account_metadata(path, authuser=8, email="fresh@x.com")
            return legacy

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(LegacyAccountContext, "read", _read_then_race_a_login)
            assert read_account_metadata(storage) == {"authuser": 8, "email": "fresh@x.com"}

        # In-band still wins, while the stale sibling is now scrubbed in the
        # background to close the write-then-scrub crash gap (#2228).
        assert sorted(_scheduler()._scheduled_paths_for_tests()) == [_canonical(storage)]
        _drain_promotions_for_tests()
        assert _in_band_on_disk(storage) == {"authuser": 8, "email": "fresh@x.com"}
        assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8")) == {
            "notebook_id": "nb-1"
        }


class TestAllThreeEntryPathsReachReconciliation:
    """The precondition for deleting the 2s deadline and the warn throttle.

    Those two compensations existed because promotion sat on the read path.
    Deleting them is only safe if the replacement — detached reconciliation —
    actually covers every path that used to trigger promotion.
    """

    def test_per_rpc_token_route_derives_and_schedules(self, tmp_path):
        """``refresh._resolve_token_route_kwargs`` is the per-RPC entry."""
        from notebooklm._auth import refresh as _refresh

        storage = _legacy_profile(tmp_path, {"authuser": 3, "email": "route@example.com"})
        kwargs = _refresh._resolve_token_route_kwargs(storage, authuser=None, account_email=None)
        assert kwargs == {"authuser": 3, "account_email": "route@example.com"}
        assert sorted(_scheduler()._scheduled_paths_for_tests()) == [_canonical(storage)]
        _drain_promotions_for_tests()
        assert _in_band_on_disk(storage) == {"authuser": 3, "email": "route@example.com"}

    def test_cli_read_derives_and_schedules(self, tmp_path):
        """The CLI reads synchronously, with no event loop running.

        This is why reconciliation uses a thread and not an ``asyncio`` flight:
        ``_auth.single_flight`` needs ``asyncio.get_running_loop()`` and would
        leave this entry path — ``profile list``, ``auth check``,
        ``login --refresh`` — without any durable promotion at all.
        """
        import asyncio

        from notebooklm._app.profile import gather_profile_list

        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()  # precondition: genuinely no loop here

        storage = _legacy_profile(tmp_path, {"authuser": 4, "email": "cli@example.com"})
        entries, active = gather_profile_list(
            list_profiles=lambda: ["default"],
            resolve_profile=lambda: "default",
            get_storage_path=lambda *, profile: storage,
            read_account_metadata=read_account_metadata,
        )
        assert active == "default"
        assert [(e.name, e.account, e.authenticated) for e in entries] == [
            ("default", "cli@example.com", True)
        ]
        assert sorted(_scheduler()._scheduled_paths_for_tests()) == [_canonical(storage)]
        _drain_promotions_for_tests()
        assert _in_band_on_disk(storage) == {"authuser": 4, "email": "cli@example.com"}

    def test_env_auth_read_needs_no_promotion_at_all(self, tmp_path, monkeypatch):
        """``NOTEBOOKLM_AUTH_JSON`` (#2083) carries its account record in-band
        by construction: there is no profile directory and therefore no legacy
        sibling to promote. The path is covered by being vacuous, not by
        scheduling anything — pinned so a future change that gives env-auth a
        storage path notices it now owes this path reconciliation."""
        from notebooklm._auth import refresh as _refresh

        payload = {
            "cookies": [],
            "origins": [],
            "notebooklm": {"version": 1, "account": {"authuser": 5, "email": "env@example.com"}},
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(payload))

        assert read_account_metadata(None) == {}
        kwargs = _refresh._resolve_token_route_kwargs(None, authuser=None, account_email=None)
        assert kwargs == {"authuser": 5, "account_email": "env@example.com"}
        assert not _scheduler()._scheduled_paths_for_tests()
        assert not _scheduler()._workers_for_tests()


def test_an_unfinished_exit_drain_actually_reaches_the_users_stderr(tmp_path: Path) -> None:
    """The whole fix is a log line, so prove it survives interpreter shutdown.

    A unit test can assert ``drain`` calls ``logger.warning``, and still leave
    #2223 fully regressed: ``logging`` registers its own ``atexit`` shutdown at
    import time, so whether our hook runs *before* handlers are torn down is a
    LIFO ordering property of the real interpreter, not of the function. If
    that ordering ever changes, or the logger loses its default WARNING floor,
    the user is back to complete silence with every other test still green.

    So this spawns a real process, leaves a promotion permanently stuck, and
    asserts the warning is on stderr after it exits.
    """
    home = tmp_path / "home"
    profile = home / "profiles" / "default"
    profile.mkdir(parents=True)
    storage = profile / "storage_state.json"
    storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    env = {
        **os.environ,
        "NOTEBOOKLM_HOME": str(home),
        "PYTHONPATH": str(_SRC_ROOT),
        "NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT": "0.2",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import threading\n"
            "from pathlib import Path\n"
            "from notebooklm._auth.profile_migration import LegacyPromotionScheduler\n"
            "from notebooklm._auth.profile_store import ProfileStore\n"
            "class Stuck:\n"
            "    def promote(self, store):\n"
            "        threading.Event().wait(120)\n"
            f"store = ProfileStore(Path({str(storage)!r}))\n"
            "LegacyPromotionScheduler.process_default().schedule(store, Stuck())\n",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "was still running when the shared" in result.stderr, (
        "the exit drain gave up without telling the user anything: " + repr(result.stderr)
    )
    assert str(storage) in result.stderr, "the warning must name the profile at risk"


def test_short_lived_process_still_lands_the_durable_promotion(tmp_path: Path) -> None:
    """A real process must migrate and scrub before it exits.

    This is a SUBPROCESS test on purpose. Every in-process test here drains the
    worker explicitly, so all of them passed while the durable half was, in
    practice, dead: measured on the first draft, a real ``notebooklm profile
    list`` against a legacy-only profile migrated it 0 times out of 6. The read
    was correct every time — what silently never happened was the promotion and
    the privacy scrub that removes a stale account email from ``context.json``
    at rest. Only spawning a process that exits can see that.
    """
    home = tmp_path / "home"
    profile = home / "profiles" / "default"
    profile.mkdir(parents=True)
    storage = profile / "storage_state.json"
    context = profile / "context.json"
    storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    context.write_text(
        json.dumps({"account": {"authuser": 3, "email": "alice@example.com"}}),
        encoding="utf-8",
    )

    env = {**os.environ, "NOTEBOOKLM_HOME": str(home), "PYTHONPATH": str(_SRC_ROOT)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "from notebooklm._auth.storage import read_account_metadata\n"
            f"read_account_metadata(Path({str(storage)!r}))\n",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    embedded = json.loads(storage.read_text(encoding="utf-8"))
    assert (embedded.get("notebooklm") or {}).get("account") == {
        "authuser": 3,
        "email": "alice@example.com",
    }, "the durable promotion did not land before the process exited"
    scrubbed = not context.exists() or "account" not in json.loads(
        context.read_text(encoding="utf-8") or "{}"
    )
    assert scrubbed, "the stale account email was left in context.json at rest"
