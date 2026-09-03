"""Direct behavior tests for legacy profile account migration components."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from filelock import Timeout

import notebooklm.auth as public_auth
from notebooklm._auth import profile_migration as migration
from notebooklm._auth import storage
from notebooklm._auth.profile_account import (
    ClearAccount,
    DomainSelection,
    KeepAccount,
    ProfileAccount,
    SetAccount,
)
from notebooklm._auth.profile_document import ProfileDocument
from notebooklm._auth.profile_migration import (
    AccountMetadataWriter,
    AlreadyInBand,
    InBandAccount,
    LegacyAccount,
    LegacyAccountContext,
    LegacyAccountMigrator,
    LegacyPromotionScheduler,
    LoginProfileWriter,
    NoAccount,
    NoLegacyRecord,
    Promoted,
    PromotionFailed,
    replace_profile_from_login,
)
from notebooklm._auth.profile_store import (
    LoginWriteRequest,
    ProfileStore,
    ReplaceResult,
    ReplaceStatus,
)


def _document(account: object = None, *, include_account: bool = True) -> ProfileDocument:
    payload: dict[str, Any] = {"cookies": [], "origins": []}
    if include_account:
        payload["notebooklm"] = {"version": 1, "account": account}
    return ProfileDocument.decode(payload)


class _SequencedStore:
    def __init__(self, path: Path, documents: list[ProfileDocument | None]) -> None:
        self.path = path
        self.ordering_key = path.resolve()
        self._documents = iter(documents)
        self.reads = 0
        self.updated: list[tuple[ProfileAccount, bool]] = []
        self.update_result = True
        self.clears = 0
        self.login_result = ReplaceResult(ReplaceStatus.APPLIED)
        self.login_requests: list[LoginWriteRequest] = []

    def _read_account_document(self) -> ProfileDocument | None:
        self.reads += 1
        return next(self._documents)

    def update_account(self, record: ProfileAccount, *, only_if_absent: bool = False) -> bool:
        self.updated.append((record, only_if_absent))
        return self.update_result

    def clear_account(self) -> None:
        self.clears += 1

    def replace_from_login(self, request: LoginWriteRequest) -> ReplaceResult:
        self.login_requests.append(request)
        return self.login_result


class _RecordingContext(LegacyAccountContext):
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result
        self.reads: list[Path] = []
        self.scrubs: list[Path] = []
        self.read_effect = None
        self.scrub_error: BaseException | None = None

    def read(self, storage_path: Path) -> dict[str, Any] | None:
        self.reads.append(storage_path)
        if self.read_effect is not None:
            self.read_effect()
        return self.result

    def scrub(self, storage_path: Path) -> None:
        self.scrubs.append(storage_path)
        if self.scrub_error is not None:
            raise self.scrub_error


def _request(account: KeepAccount | SetAccount | ClearAccount, raw_namespace: object = None):
    payload: dict[str, object] = {"cookies": [], "origins": []}
    if raw_namespace is not None:
        payload["notebooklm"] = raw_namespace
    return LoginWriteRequest(
        source=ProfileDocument.decode(payload),
        domain_selection=DomainSelection(),
        account=account,
    )


def test_result_shapes_are_closed_frozen_slotted_redacted_and_internal():
    secret = "secret-account@example.com"
    account = ProfileAccount(7, secret)
    values = [
        InBandAccount(account),
        LegacyAccount(account),
        NoAccount(),
        Promoted(account),
        AlreadyInBand(),
        NoLegacyRecord(),
        PromotionFailed(account),
    ]

    for value in values:
        assert not hasattr(value, "__dict__")
        assert secret not in repr(value)
        with pytest.raises((FrozenInstanceError, TypeError)):
            setattr(value, "injected", True)  # noqa: B010 - shared frozen-shape assertion

    assert [item.name for item in fields(InBandAccount)] == ["record"]
    assert [item.name for item in fields(PromotionFailed)] == ["authoritative_after_failure"]
    for name in {
        "InBandAccount",
        "LegacyAccountMigrator",
        "LegacyPromotionScheduler",
        "PromotionFailed",
    }:
        assert not hasattr(public_auth, name)
        assert name not in storage.__all__


def test_in_band_projection_is_lossless_ephemeral_and_typed_only(tmp_path):
    raw = {
        "unknown": [1, {"nested": "value"}],
        "authuser": True,
        "email": ["malformed"],
    }
    store = _SequencedStore(tmp_path / "custom.json", [_document(raw)])
    migrator = LegacyAccountMigrator(_RecordingContext())

    result, compatibility = migrator._resolve_with_projection(store)  # type: ignore[arg-type]

    assert result == InBandAccount(ProfileAccount(authuser=0, email=None))
    assert compatibility == raw
    assert list(compatibility) == list(raw)
    compatibility["unknown"][1]["nested"] = "changed"
    assert raw["unknown"][1]["nested"] == "value"
    assert not hasattr(result, "raw")
    assert not hasattr(result, "compatibility")

    typed = LegacyAccountMigrator(_RecordingContext()).resolve(
        _SequencedStore(tmp_path / "custom.json", [_document(raw)])  # type: ignore[arg-type]
    )
    assert typed == InBandAccount(ProfileAccount(0, None))
    assert not hasattr(typed, "raw")


@pytest.mark.parametrize("first", [None, _document({}, include_account=True)])
def test_second_in_band_sample_is_unconditional_when_first_is_absent(tmp_path, first):
    contexts = _RecordingContext(None)
    store = _SequencedStore(tmp_path / "state.json", [first, None])

    result, compatibility = LegacyAccountMigrator(contexts)._resolve_with_projection(  # type: ignore[arg-type]
        store
    )

    assert isinstance(result, NoAccount)
    assert compatibility == {}
    assert store.reads == 2
    assert contexts.reads == [store.path]


def test_non_empty_unknown_in_band_wins_without_legacy_sample(tmp_path):
    contexts = _RecordingContext({"authuser": 9})
    store = _SequencedStore(tmp_path / "state.json", [_document({"unknown": [1, 2]})])

    result, raw = LegacyAccountMigrator(contexts)._resolve_with_projection(store)  # type: ignore[arg-type]

    assert result == InBandAccount(ProfileAccount(0, None))
    assert raw == {"unknown": [1, 2]}
    assert contexts.reads == []
    assert store.reads == 1


def test_account_clear_tombstone_wins_without_sampling_stale_legacy(tmp_path):
    tombstone = ProfileDocument.decode(
        {
            "cookies": [],
            "origins": [],
            "notebooklm": {"version": 1, "account_route_cleared": True},
        }
    )
    contexts = _RecordingContext({"authuser": 7, "email": "stale@example.com"})
    store = _SequencedStore(tmp_path / "state.json", [tombstone])

    result, raw = LegacyAccountMigrator(contexts)._resolve_with_projection(store)  # type: ignore[arg-type]

    assert result == InBandAccount(ProfileAccount(0, None))
    assert raw == {}
    assert contexts.reads == []
    assert store.reads == 1


def test_promotion_between_first_and_legacy_samples_is_found_by_second_read(tmp_path):
    store = _SequencedStore(
        tmp_path / "state.json",
        [None, _document({"authuser": 6, "email": "promoted@example.com"})],
    )
    contexts = _RecordingContext(None)

    result, raw = LegacyAccountMigrator(contexts)._resolve_with_projection(store)  # type: ignore[arg-type]

    assert result == InBandAccount(ProfileAccount(6, "promoted@example.com"))
    assert raw == {"authuser": 6, "email": "promoted@example.com"}


def test_fresh_login_after_legacy_sample_wins_over_stale_legacy(tmp_path):
    store = _SequencedStore(
        tmp_path / "state.json",
        [None, _document({"authuser": 8, "email": "fresh@example.com"})],
    )
    contexts = _RecordingContext({"authuser": 1, "email": "stale@example.com"})

    result, raw = LegacyAccountMigrator(contexts)._resolve_with_projection(store)  # type: ignore[arg-type]

    assert result == InBandAccount(ProfileAccount(8, "fresh@example.com"))
    assert raw["email"] == "fresh@example.com"


def test_in_band_write_with_stale_legacy_copy_is_reconciled_without_overwrite(tmp_path):
    """A crash between embed and scrub self-heals on the next account read (#2228)."""
    storage_path = tmp_path / "storage_state.json"
    storage_path.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [],
                "notebooklm": {
                    "version": 1,
                    "account": {"authuser": 8, "email": "fresh@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps({"account": {"authuser": 1, "email": "stale@example.com"}}),
        encoding="utf-8",
    )
    store = migration.ProfileStore(storage_path)
    migrator = LegacyAccountMigrator()

    resolved, compatibility = migrator._resolve_with_projection(store)

    assert resolved == InBandAccount(ProfileAccount(8, "fresh@example.com"))
    assert compatibility["email"] == "fresh@example.com"
    assert migrator.needs_reconciliation(store.path, resolved) is True
    assert isinstance(migrator.promote(store), AlreadyInBand)
    assert json.loads(storage_path.read_text(encoding="utf-8"))["notebooklm"]["account"] == {
        "authuser": 8,
        "email": "fresh@example.com",
    }
    assert not context_path.exists()
    assert migrator.needs_reconciliation(store.path, resolved) is False


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ({"authuser": True, "email": " "}, {"authuser": 0}),
        ({"authuser": -1, "email": 7}, {"authuser": 0}),
        ({"authuser": 4.0, "email": " x@example.com "}, {"authuser": 0, "email": "x@example.com"}),
        ({"unknown": [1]}, {"authuser": 0}),
    ],
)
def test_legacy_projection_and_promoted_record_share_exact_sanitization(tmp_path, legacy, expected):
    contexts = _RecordingContext(legacy)
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    store = _SequencedStore(path, [None, None])
    migrator = LegacyAccountMigrator(contexts)

    result, compatibility = migrator._resolve_with_projection(store)  # type: ignore[arg-type]
    assert result == LegacyAccount(ProfileAccount(expected["authuser"], expected.get("email")))
    assert compatibility == expected
    assert {key: type(value) for key, value in compatibility.items()} == {
        key: type(value) for key, value in expected.items()
    }

    store.update_result = True
    promoted = migrator.promote(store)  # type: ignore[arg-type]
    assert promoted == Promoted(ProfileAccount(expected["authuser"], expected.get("email")))
    assert store.updated == [(promoted.authoritative, True)]


def test_context_read_matrix_and_recursive_isolation(tmp_path, caplog):
    storage_path = tmp_path / "named-anything.json"
    context_path = tmp_path / "context.json"
    contexts = LegacyAccountContext()
    assert contexts.read(storage_path) is None

    context_path.write_text("[]", encoding="utf-8")
    assert contexts.read(storage_path) is None
    context_path.write_text('{"account": {}}', encoding="utf-8")
    assert contexts.read(storage_path) is None
    context_path.write_text('{"account": {"nested": [1]}}', encoding="utf-8")
    first = contexts.read(storage_path)
    assert first == {"nested": [1]}
    first["nested"].append(2)
    assert contexts.read(storage_path) == {"nested": [1]}

    context_path.write_text("{", encoding="utf-8")
    with caplog.at_level("DEBUG", logger="notebooklm.auth"):
        assert contexts.read(storage_path) is None
    assert "account metadata read failed at" in caplog.text

    context_path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        contexts.read(storage_path)


def test_context_scrub_rewrites_or_unlinks_under_exact_sibling_lock(tmp_path, monkeypatch):
    storage_path = tmp_path / "custom-profile-name.json"
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"first": 1, "account": {}, "last": 2}), encoding="utf-8")
    lock_calls: list[tuple[str, float]] = []
    writes: list[tuple[Path, dict[str, Any]]] = []

    class _Lock:
        def __init__(self, path: str, timeout: float) -> None:
            lock_calls.append((path, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _write(path: Path, value: dict[str, Any]) -> None:
        writes.append((path, dict(value)))

    monkeypatch.setattr(migration, "FileLock", _Lock)
    monkeypatch.setattr(migration, "atomic_write_json", _write)
    LegacyAccountContext().scrub(storage_path)

    assert lock_calls == [(str(tmp_path / "context.json.lock"), 10.0)]
    assert writes == [(context_path, {"first": 1, "last": 2})]
    assert list(writes[0][1]) == ["first", "last"]

    monkeypatch.setattr(migration, "atomic_write_json", lambda *_: pytest.fail("must unlink"))
    context_path.write_text('{"account": {"email": "x"}}', encoding="utf-8")
    LegacyAccountContext().scrub(storage_path)
    assert not context_path.exists()


@pytest.mark.parametrize("promoted", [True, False])
def test_promotion_embeds_before_scrub_and_projects_closed_result(tmp_path, promoted):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    events: list[str] = []
    contexts = _RecordingContext({"authuser": 3, "email": " x@example.com "})

    class _Store(_SequencedStore):
        def update_account(self, record, *, only_if_absent=False):
            events.append("embed")
            return promoted

    store = _Store(path, [])
    store.update_result = promoted
    original_scrub = contexts.scrub

    def _scrub(value):
        events.append("scrub")
        original_scrub(value)

    contexts.scrub = _scrub  # type: ignore[method-assign]

    result = LegacyAccountMigrator(contexts).promote(store)  # type: ignore[arg-type]

    assert events == ["embed", "scrub"]
    assert result == (Promoted(ProfileAccount(3, "x@example.com")) if promoted else AlreadyInBand())


def test_promotion_failure_does_not_scrub_or_reread(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    contexts = _RecordingContext({"authuser": 2})

    class _FailingStore(_SequencedStore):
        def update_account(self, record, *, only_if_absent=False):
            raise RuntimeError("writer exploded with secret@example.com")

    store = _FailingStore(path, [])
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        result = LegacyAccountMigrator(contexts).promote(store)  # type: ignore[arg-type]

    assert result == PromotionFailed(None)
    assert contexts.reads == [path]
    assert contexts.scrubs == []
    assert "Legacy account promotion failed for" in caplog.text


def test_promotion_absence_and_baseexception_boundaries(tmp_path):
    contexts = _RecordingContext({"authuser": 1})
    absent = _SequencedStore(tmp_path / "missing.json", [])
    assert LegacyAccountMigrator(contexts).promote(absent) == NoLegacyRecord()  # type: ignore[arg-type]
    assert contexts.reads == []

    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")

    class _FatalStore(_SequencedStore):
        def update_account(self, record, *, only_if_absent=False):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        LegacyAccountMigrator(contexts).promote(_FatalStore(path, []))  # type: ignore[arg-type]


def test_scrub_ordinary_failure_is_logged_but_baseexception_escapes(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    store = _SequencedStore(path, [])
    contexts = _RecordingContext({"authuser": 1})
    contexts.scrub_error = RuntimeError("cleanup secret@example.com")
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        assert isinstance(
            LegacyAccountMigrator(contexts).promote(store),  # type: ignore[arg-type]
            Promoted,
        )
    assert "Legacy account context cleanup failed for" in caplog.text

    contexts.scrub_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        LegacyAccountMigrator(contexts).promote(store)  # type: ignore[arg-type]


def test_scheduler_dedupes_canonical_paths_and_runs_one_daemon_worker(tmp_path):
    release = threading.Event()
    calls: list[Path] = []

    class _Migrator:
        def promote(self, store):
            calls.append(store.path)
            release.wait(10)

    scheduler = LegacyPromotionScheduler()
    one = _SequencedStore(tmp_path / "profile" / "../state.json", [])
    alias = _SequencedStore(tmp_path / "state.json", [])
    migrator = _Migrator()

    assert scheduler.schedule(one, migrator) is True  # type: ignore[arg-type]
    assert scheduler.schedule(alias, migrator) is False  # type: ignore[arg-type]
    workers = scheduler._workers_for_tests()
    assert len(workers) == 1
    worker = next(iter(workers))
    assert worker.name == "notebooklm-account-promotion"
    assert worker.daemon is True
    assert scheduler._active_paths_for_tests() == {str(one.ordering_key)}
    release.set()
    scheduler.drain(10.0)
    assert calls == [one.path]
    assert not scheduler._workers_for_tests()
    assert not scheduler._active_paths_for_tests()
    assert scheduler._scheduled_paths_for_tests() == {str(one.ordering_key)}


def test_scheduler_construction_and_start_failures_are_retryable(tmp_path):
    store = _SequencedStore(tmp_path / "state.json", [])
    migrator = LegacyAccountMigrator(_RecordingContext())

    def _construct_boom(**kwargs):
        raise RuntimeError("construction")

    scheduler = LegacyPromotionScheduler(_construct_boom)
    with pytest.raises(RuntimeError, match="construction"):
        scheduler.schedule(store, migrator)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="construction"):
        scheduler.schedule(store, migrator)  # type: ignore[arg-type]
    assert not scheduler._workers_for_tests()
    assert not scheduler._active_paths_for_tests()

    class _Unstarted:
        def start(self):
            raise RuntimeError("start")

        def join(self, timeout):
            raise RuntimeError("cannot join thread before it is started")

    scheduler = LegacyPromotionScheduler(lambda **kwargs: _Unstarted())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="start"):
        scheduler.schedule(store, migrator)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="start"):
        scheduler.schedule(store, migrator)  # type: ignore[arg-type]
    assert not scheduler._workers_for_tests()
    assert not scheduler._active_paths_for_tests()


def test_scheduler_allows_retry_after_a_worker_failure(tmp_path):
    attempts = 0
    attempted = threading.Event()

    class _FailsOnce:
        def promote(self, store):
            nonlocal attempts
            attempts += 1
            attempted.set()
            if attempts == 1:
                raise RuntimeError("transient lock failure")

    scheduler = LegacyPromotionScheduler()
    store = _SequencedStore(tmp_path / "state.json", [])
    migrator = _FailsOnce()

    assert scheduler.schedule(store, migrator) is True  # type: ignore[arg-type]
    assert attempted.wait(5.0)
    deadline = time.monotonic() + 5.0
    while scheduler._active_paths_for_tests() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not scheduler._active_paths_for_tests()

    attempted.clear()
    assert scheduler.schedule(store, migrator) is True  # type: ignore[arg-type]
    assert attempted.wait(5.0)
    assert scheduler.drain(5.0) is True
    assert attempts == 2


def test_scheduler_worker_contains_baseexception_and_deregisters(tmp_path, caplog):
    class _FatalMigrator:
        def promote(self, store):
            raise KeyboardInterrupt("fatal")

    scheduler = LegacyPromotionScheduler()
    store = _SequencedStore(tmp_path / "state.json", [])
    # WARNING, not DEBUG: `drain` reports a timed-out promotion out loud, so a
    # *crashed* one must not be the quieter of the two (#2223 review).
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        assert scheduler.schedule(store, _FatalMigrator()) is True  # type: ignore[arg-type]
        assert scheduler.drain(10.0) is True
    assert not scheduler._workers_for_tests()
    crashes = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "promotion failed" in record.getMessage()
    ]
    assert len(crashes) == 1
    assert str(store.path) in crashes[0].getMessage()


def test_drain_reports_and_warns_when_a_worker_outlives_the_budget(tmp_path, caplog):
    """An incomplete drain must be observable, not silent (#2223).

    Before the fix ``drain`` joined each worker and returned regardless: a
    process could exit with the promotion unfinished and nothing anywhere --
    no return value, no log, no exception -- said so.
    """
    release = threading.Event()

    class _StuckMigrator:
        def promote(self, store):
            release.wait(30)

    scheduler = LegacyPromotionScheduler()
    # ``path`` and ``ordering_key`` differ here (the key is ``.resolve()``d), so
    # the assertion below pins that the warning names the profile the user sees
    # rather than the internal dedupe key.
    store = _SequencedStore(tmp_path / "profile" / ".." / "storage_state.json", [])
    assert str(store.path) != str(store.ordering_key)
    try:
        assert scheduler.schedule(store, _StuckMigrator()) is True  # type: ignore[arg-type]
        with caplog.at_level("WARNING", logger="notebooklm.auth"):
            assert scheduler.drain(0.05) is False
        stragglers = [
            record.getMessage()
            for record in caplog.records
            if "still running when the shared" in record.getMessage()
        ]
        assert len(stragglers) == 1
        # The repo's redacting handler clears ``LogRecord.args``, so the
        # rendered message is what a reader actually gets -- assert on it.
        assert str(store.path) in stragglers[0]
        assert "0.1s exit budget" in stragglers[0]
        assert "NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT" in stragglers[0]
        assert "context.json" in stragglers[0]
    finally:
        release.set()
        assert scheduler.drain(30.0) is True


def test_drain_returns_true_and_stays_quiet_when_every_worker_finishes(tmp_path, caplog):
    class _Migrator:
        def promote(self, store):
            return None

    scheduler = LegacyPromotionScheduler()
    store = _SequencedStore(tmp_path / "state.json", [])
    assert scheduler.schedule(store, _Migrator()) is True  # type: ignore[arg-type]
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        assert scheduler.drain(30.0) is True
    # A prose-keyed negative assertion would pass forever if the message were
    # reworded; assert no WARNING was emitted at all.
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert not scheduler._workers_for_tests()


def test_drain_budget_is_one_shared_deadline_and_the_first_worker_gets_it_all():
    """Pins BOTH halves of the deadline contract.

    A purely elapsed-time assertion is one-sided: a ``drain`` that gave up
    instantly (``join(0.0)``) would also finish fast and pass. Recording the
    timeout each worker is actually handed pins that the first worker is
    offered the whole budget and later ones inherit only what is left -- which
    is what makes this an overall deadline rather than a per-worker one.
    """
    budget = 0.2
    handed: list[float] = []

    class _FakeWorker:
        """A worker that stays alive, so each join consumes its whole timeout."""

        def __init__(self) -> None:
            self.daemon = True
            self.name = "notebooklm-account-promotion"

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            handed.append(timeout)
            time.sleep(timeout)

        def is_alive(self) -> bool:
            return True

    scheduler = LegacyPromotionScheduler(lambda **kwargs: _FakeWorker())  # type: ignore[arg-type]
    stores = [_SequencedStore(Path(f"/nonexistent/state{index}.json"), []) for index in range(4)]
    for store in stores:
        assert scheduler.schedule(store, LegacyAccountMigrator()) is True  # type: ignore[arg-type]

    assert scheduler.drain(budget) is False

    # Each worker is joined exactly once -- the re-snapshot loop must not spin.
    assert len(handed) == 4, handed
    assert handed[0] == pytest.approx(budget, rel=0.05), handed
    # A per-worker budget would hand the full `budget` to every one of them.
    assert all(value <= budget / 4 for value in handed[1:]), handed
    scheduler._workers.clear()


def test_drain_warns_for_every_stalled_profile_not_just_the_first(tmp_path, caplog):
    """The worker -> path mapping must be per-worker, not a single hoisted path."""
    release = threading.Event()

    class _StuckMigrator:
        def promote(self, store):
            release.wait(30)

    scheduler = LegacyPromotionScheduler()
    stores = [_SequencedStore(tmp_path / f"state{index}.json", []) for index in range(3)]
    try:
        for store in stores:
            assert scheduler.schedule(store, _StuckMigrator()) is True  # type: ignore[arg-type]
        with caplog.at_level("WARNING", logger="notebooklm.auth"):
            assert scheduler.drain(0.05) is False
        messages = [
            record.getMessage()
            for record in caplog.records
            if "still running when the shared" in record.getMessage()
        ]
        assert len(messages) == len(stores)
        for store in stores:
            assert any(str(store.path) in message for message in messages), store.path
    finally:
        release.set()
        assert scheduler.drain(30.0) is True


def test_drain_zero_never_waits_but_still_reports(tmp_path, caplog):
    """The documented ``0`` setting must not become a silent escape hatch."""
    release = threading.Event()

    class _StuckMigrator:
        def promote(self, store):
            release.wait(30)

    scheduler = LegacyPromotionScheduler()
    store = _SequencedStore(tmp_path / "state.json", [])
    try:
        assert scheduler.schedule(store, _StuckMigrator()) is True  # type: ignore[arg-type]
        started = time.monotonic()
        with caplog.at_level("WARNING", logger="notebooklm.auth"):
            assert scheduler.drain(0.0) is False
        assert time.monotonic() - started < 5.0
        assert any("still running when the shared" in r.getMessage() for r in caplog.records)
    finally:
        release.set()
        assert scheduler.drain(30.0) is True


def test_drain_also_joins_a_worker_scheduled_after_it_started(tmp_path, caplog):
    """A worker registered mid-drain must not escape unjoined and unreported.

    ``drain`` used to take one snapshot and join only that. A promotion
    scheduled after the snapshot was then neither waited for nor warned about,
    and ``drain`` still returned ``True`` -- the exact silence this method
    exists to remove. The late worker here stays stuck on purpose, so a
    single-snapshot drain would report a clean shutdown.
    """
    release = threading.Event()
    scheduler = LegacyPromotionScheduler()
    late_store = _SequencedStore(tmp_path / "late.json", [])

    class _Stuck:
        def promote(self, store):
            release.wait(30)

    class _SchedulesAStuckOne:
        def promote(self, store):
            scheduler.schedule(late_store, _Stuck())  # type: ignore[arg-type]

    first = _SequencedStore(tmp_path / "first.json", [])
    try:
        assert scheduler.schedule(first, _SchedulesAStuckOne()) is True  # type: ignore[arg-type]
        with caplog.at_level("WARNING", logger="notebooklm.auth"):
            assert scheduler.drain(0.3) is False
        messages = [
            record.getMessage()
            for record in caplog.records
            if "still running when the shared" in record.getMessage()
        ]
        assert len(messages) == 1
        assert str(late_store.path) in messages[0]
    finally:
        release.set()
        assert scheduler.drain(30.0) is True


def test_a_promotion_scheduled_after_the_drain_closed_is_refused_out_loud(tmp_path, caplog):
    """The last window: nobody would ever join a worker started after the drain.

    ``drain`` closes the registry under the same lock as its final emptiness
    check, so a promotion is either joined by the drain or refused here --
    never started with no one left to wait for it, which at interpreter
    shutdown means killed mid-write in silence.
    """
    scheduler = LegacyPromotionScheduler()
    assert scheduler.drain(30.0) is True  # nothing outstanding -> closes

    store = _SequencedStore(tmp_path / "state.json", [])
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        assert scheduler.schedule(store, LegacyAccountMigrator()) is False  # type: ignore[arg-type]

    refusals = [
        record.getMessage()
        for record in caplog.records
        if "was not started because the process is already shutting down" in record.getMessage()
    ]
    assert len(refusals) == 1
    assert str(store.path) in refusals[0]
    # Refused, not silently registered.
    assert not scheduler._workers_for_tests()


def test_the_scheduler_also_closes_when_the_drain_times_out(tmp_path, caplog):
    """Both of ``drain``'s exits must close the registry, not just the tidy one.

    A drain that gives up on a straggler is still the only exit drain there
    will be, so a promotion scheduled after it would be started with nobody
    left to join it -- killed mid-write at shutdown, silently.
    """
    release = threading.Event()

    class _Stuck:
        def promote(self, store):
            release.wait(30)

    scheduler = LegacyPromotionScheduler()
    stuck_store = _SequencedStore(tmp_path / "stuck.json", [])
    try:
        assert scheduler.schedule(stuck_store, _Stuck()) is True  # type: ignore[arg-type]
        assert scheduler.drain(0.05) is False  # times out -> the deadline return

        later = _SequencedStore(tmp_path / "later.json", [])
        with caplog.at_level("WARNING", logger="notebooklm.auth"):
            assert scheduler.schedule(later, LegacyAccountMigrator()) is False  # type: ignore[arg-type]
        assert any(
            "was not started because the process is already shutting down" in r.getMessage()
            for r in caplog.records
        )
        assert str(later.path) in caplog.text
    finally:
        release.set()
        assert scheduler.drain(30.0) is True


def test_reset_reopens_the_scheduler_for_the_next_test(tmp_path):
    scheduler = LegacyPromotionScheduler()
    assert scheduler.drain(30.0) is True
    scheduler._reset_for_tests()
    store = _SequencedStore(tmp_path / "state.json", [])
    assert scheduler.schedule(store, LegacyAccountMigrator()) is True  # type: ignore[arg-type]
    assert scheduler.drain(30.0) is True


def test_scrub_reports_a_lock_timeout_instead_of_swallowing_it(tmp_path, caplog, monkeypatch):
    """``filelock.Timeout`` subclasses ``OSError`` (#2223 review).

    The old ``except OSError`` therefore ate the *lock* timeout at DEBUG, so
    ordinary contention between two notebooklm processes left the user's
    account email at rest in ``context.json`` with no signal whatsoever -- and
    ``drain()`` still reported success.
    """
    assert issubclass(Timeout, OSError), "the bug this guards depends on this subclassing"

    storage_path = tmp_path / "storage_state.json"
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps({"account": {"authuser": 3, "email": "stale@example.com"}}), encoding="utf-8"
    )

    class _AlwaysContended:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            raise Timeout("lock held elsewhere")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(migration, "FileLock", _AlwaysContended)
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        assert LegacyAccountContext().scrub(storage_path) is False

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert str(context_path) in warnings[0]
    assert "account email is still stored" in warnings[0]
    # The stale copy really is still on disk -- the warning is not a false alarm.
    assert "stale@example.com" in context_path.read_text(encoding="utf-8")


def test_scrub_reports_an_ordinary_os_failure_too(tmp_path, caplog, monkeypatch):
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"account": {"authuser": 1}}), encoding="utf-8")

    class _Boom:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            raise PermissionError("no access to the lock file")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(migration, "FileLock", _Boom)
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        assert LegacyAccountContext().scrub(tmp_path / "storage_state.json") is False
    assert any("no access to the lock file" in r.getMessage() for r in caplog.records)


def test_scrub_returns_true_when_the_legacy_copy_is_gone(tmp_path, caplog):
    storage_path = tmp_path / "storage_state.json"
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps({"account": {"authuser": 3, "email": "stale@example.com"}}), encoding="utf-8"
    )
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        assert LegacyAccountContext().scrub(storage_path) is True
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert not context_path.exists()
    # Absent file: still "gone", still quiet.
    assert LegacyAccountContext().scrub(storage_path) is True


def test_scrub_lock_is_actually_wired_to_the_pinned_constant(tmp_path, monkeypatch):
    """The floor test below is constant-vs-constant; this pins the wiring."""
    seen: list[float] = []
    real_filelock = migration.FileLock

    def _record(path, timeout=None):
        seen.append(timeout)
        return real_filelock(path, timeout=timeout)

    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"account": {"authuser": 1}}), encoding="utf-8")
    monkeypatch.setattr(migration, "FileLock", _record)
    # Move the constant to a value no literal would coincide with: a source that
    # hardcodes the timeout instead of reading the constant fails here.
    monkeypatch.setattr(migration, "_CONTEXT_SCRUB_LOCK_TIMEOUT_SECONDS", 3.75)
    assert LegacyAccountContext().scrub(tmp_path / "storage_state.json") is True
    assert seen == [3.75]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, migration._PROMOTION_EXIT_JOIN_SECONDS),
        ("", migration._PROMOTION_EXIT_JOIN_SECONDS),
        ("   ", migration._PROMOTION_EXIT_JOIN_SECONDS),
        ("0", 0.0),
        ("45", 45.0),
        ("1.5", 1.5),
        # Finite but past threading.TIMEOUT_MAX -> OverflowError from join();
        # clamped rather than refused so "wait as long as possible" still works.
        ("1e12", threading.TIMEOUT_MAX),
    ],
)
def test_promotion_exit_timeout_honours_the_operator_override(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT", raw)
    assert migration._promotion_exit_timeout() == expected


@pytest.mark.parametrize(
    ("raw", "clause"),
    [
        ("abc", "not a number of seconds"),
        ("-1", "finite, non-negative"),
        ("nan", "finite, non-negative"),
        # float() parses these happily; Thread.join() then raises OverflowError
        # straight out of the atexit hook, skipping every remaining worker.
        ("inf", "finite, non-negative"),
        ("-inf", "finite, non-negative"),
        ("1e400", "finite, non-negative"),
    ],
)
def test_promotion_exit_timeout_rejects_unusable_values_out_loud(monkeypatch, caplog, raw, clause):
    monkeypatch.setenv("NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT", raw)
    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        resolved = migration._promotion_exit_timeout()
    assert resolved == migration._PROMOTION_EXIT_JOIN_SECONDS
    # Assert the *discriminating* clause, so collapsing the two rejection
    # branches into one is not silently equivalent.
    assert clause in caplog.text
    assert "NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT" in caplog.text


@pytest.mark.parametrize(
    "raw", ["inf", "-inf", "1e400", "1e12", "9e18", "0", "45", "abc", "-1", "nan", "", "  "]
)
def test_resolved_budget_is_always_safe_to_hand_to_thread_join(monkeypatch, raw):
    """No operator input may turn the exit hook into an OverflowError.

    ``Thread.join`` computes a deadline from its timeout, so a non-finite or
    oversized value raises ``OverflowError: timestamp out of range`` -- out of
    ``atexit``, replacing this feature's diagnostic with a shutdown traceback
    and skipping every remaining worker. This pins the numeric contract that
    makes that unreachable, for accepted and rejected inputs alike.
    """
    monkeypatch.setenv("NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT", raw)
    resolved = migration._promotion_exit_timeout()
    assert math.isfinite(resolved), resolved
    assert 0.0 <= resolved <= threading.TIMEOUT_MAX, resolved


def test_exit_budget_can_outlast_the_scrub_lock_it_waits_on():
    """The exit budget must exceed the promotion's own internal lock wait.

    A promotion queued behind the ``context.json`` lock is healthy work in
    progress, not a hang. If the drain budget is below that lock's timeout, such
    a promotion is abandoned every time by construction — which is how the
    shipped 2.0s budget could lose writes silently against a 10.0s scrub lock
    (#2223).
    """
    assert migration._PROMOTION_EXIT_JOIN_SECONDS > migration._CONTEXT_SCRUB_LOCK_TIMEOUT_SECONDS, (
        "the exit drain gives up before the scrub lock it is waiting on can even time out"
    )


def test_exit_drain_hook_uses_the_resolved_budget(monkeypatch):
    """The ``atexit`` hook must read the override, not the bare constant."""
    seen: list[float] = []
    monkeypatch.setenv("NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT", "7.5")
    monkeypatch.setattr(
        migration.LegacyPromotionScheduler.process_default(),
        "drain",
        lambda timeout: seen.append(timeout) or True,
    )
    migration._drain_promotions_at_exit()
    assert seen == [7.5]


def _login_state() -> dict[str, Any]:
    return {
        "cookies": [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "psidts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
        "origins": [],
        "notebooklm": {
            "version": 1,
            "account": {"authuser": 9, "email": "old@example.com"},
        },
    }


@pytest.mark.parametrize(
    ("mode", "authuser", "email", "expected"),
    [
        ("keep", None, None, {"account": {"authuser": 9, "email": "old@example.com"}}),
        ("clear", None, None, {"account_route_cleared": True}),
        ("set", 3, None, {"account": {"authuser": 3}}),
        ("set", 3, "new@example.com", {"account": {"authuser": 3, "email": "new@example.com"}}),
    ],
)
def test_native_login_replacement_translates_primitive_account_modes(
    tmp_path: Path,
    mode: str,
    authuser: int | None,
    email: str | None,
    expected: dict[str, object],
) -> None:
    path = tmp_path / mode / "storage_state.json"
    result = replace_profile_from_login(
        path,
        _login_state(),
        include_domains={"mail"},
        include_optional=True,
        account_mode=mode,  # type: ignore[arg-type]
        account_authuser=authuser,
        account_email=email,
    )

    assert result.ok
    namespace = json.loads(path.read_text(encoding="utf-8"))["notebooklm"]
    assert {key: namespace[key] for key in expected} == expected
    assert namespace["include_domains"] == ["mail"]
    assert namespace["include_optional"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_mode": "unknown"},
        {"account_mode": "keep", "account_authuser": 1},
        {"account_mode": "keep", "account_email": "x@example.com"},
        {"account_mode": "clear", "account_authuser": 0},
        {"account_mode": "clear", "account_email": "x@example.com"},
        {"account_mode": "set"},
        {"account_mode": "set", "account_email": "x@example.com"},
    ],
)
def test_native_login_replacement_rejects_invalid_account_combinations_before_io(
    tmp_path: Path, kwargs: dict[str, object]
) -> None:
    path = tmp_path / "missing" / "storage_state.json"
    with pytest.raises(ValueError):
        replace_profile_from_login(  # type: ignore[arg-type]
            path,
            _login_state(),
            include_domains=None,
            **kwargs,
        )
    assert not path.parent.exists()


@pytest.mark.parametrize(
    ("status", "expected_calls"),
    [
        (ReplaceStatus.LOCK_UNAVAILABLE, []),
        (ReplaceStatus.REQUIRED_COOKIES_DROPPED, []),
        (ReplaceStatus.APPLIED, ["promote"]),
    ],
)
def test_login_writer_reconciles_only_after_applied(status, expected_calls, tmp_path):
    store = _SequencedStore(tmp_path / "state.json", [])
    if status is ReplaceStatus.REQUIRED_COOKIES_DROPPED:
        store.login_result = ReplaceResult(status, missing_required=("SID",), present_names=())
    else:
        store.login_result = ReplaceResult(status)
    calls: list[str] = []
    migrator = SimpleNamespace(
        promote=lambda value: calls.append("promote"), scrub=lambda value: calls.append("scrub")
    )

    result = LoginProfileWriter(store, migrator).write(_request(KeepAccount()))  # type: ignore[arg-type]

    assert result is store.login_result
    assert calls == expected_calls
    assert store.login_requests


@pytest.mark.parametrize(
    ("directive", "namespace", "expected"),
    [
        (KeepAccount(), None, "promote"),
        (KeepAccount(), {"version": 1}, "promote"),
        (KeepAccount(), {"account": {}}, "scrub"),
        (KeepAccount(), {"account": None}, "scrub"),
        (SetAccount(ProfileAccount(1, "x@example.com")), None, "scrub"),
        (ClearAccount(), None, "scrub"),
    ],
)
def test_login_writer_uses_exact_raw_keep_key_presence(directive, namespace, expected, tmp_path):
    store = _SequencedStore(tmp_path / "state.json", [])
    calls: list[str] = []
    migrator = SimpleNamespace(
        promote=lambda value: calls.append("promote"), scrub=lambda value: calls.append("scrub")
    )
    LoginProfileWriter(store, migrator).write(  # type: ignore[arg-type]
        _request(directive, namespace)
    )
    assert calls == [expected]


def test_account_writer_orders_store_before_scrub_and_stops_on_store_failure(tmp_path):
    events: list[str] = []

    class _Store(_SequencedStore):
        def update_account(self, record, *, only_if_absent=False):
            events.append(f"write:{only_if_absent}")
            return False

        def clear_account(self):
            events.append("clear")

    class _Migrator:
        def scrub(self, store):
            events.append("scrub")

    store = _Store(tmp_path / "state.json", [])
    writer = AccountMetadataWriter(store, _Migrator())  # type: ignore[arg-type]
    assert writer.write(ProfileAccount(2, None)) is None
    assert events == ["write:False", "scrub"]
    events.clear()
    assert writer.clear() is None
    assert events == ["clear", "scrub"]

    def _fail(record, *, only_if_absent=False):
        raise RuntimeError("store failure")

    store.update_account = _fail  # type: ignore[method-assign]
    events.clear()
    with pytest.raises(RuntimeError, match="store failure"):
        writer.write(ProfileAccount(1, None))
    assert events == []


def test_storage_reader_uses_one_core_call_and_schedules_only_legacy(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    calls: list[object] = []
    path.with_name("context.json").write_text(
        json.dumps({"account": {"authuser": 3}}),
        encoding="utf-8",
    )

    class _Scheduler:
        def schedule(self, value, owner):
            calls.append((value, owner))
            return True

    monkeypatch.setattr(storage.LegacyPromotionScheduler, "process_default", lambda: _Scheduler())

    assert storage.read_account_metadata(path) == {"authuser": 3}
    assert len(calls) == 1
    scheduled_store, scheduled_migrator = calls[0]
    assert isinstance(scheduled_store, ProfileStore)
    assert scheduled_store.path == path
    assert isinstance(scheduled_migrator, LegacyAccountMigrator)
    assert storage.read_account_metadata(None) == {}


def test_storage_reader_schedules_stale_copy_cleanup_after_in_band_write(monkeypatch, tmp_path):
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [],
                "notebooklm": {
                    "version": 1,
                    "account": {"authuser": 4, "email": "fresh@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "context.json").write_text(
        json.dumps({"account": {"authuser": 1, "email": "stale@example.com"}}),
        encoding="utf-8",
    )
    calls: list[tuple[object, object]] = []

    class _Scheduler:
        def schedule(self, store, migrator):
            calls.append((store, migrator))
            return True

    monkeypatch.setattr(storage.LegacyPromotionScheduler, "process_default", lambda: _Scheduler())

    assert storage.read_account_metadata(path) == {
        "authuser": 4,
        "email": "fresh@example.com",
    }
    assert len(calls) == 1


def test_public_drop_compatibility_symbol_is_exact_alias():
    assert storage._drop_legacy_account_key is migration._drop_legacy_account_key
    assert public_auth.drop_legacy_account_key is migration._drop_legacy_account_key
