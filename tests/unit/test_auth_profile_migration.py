"""Direct behavior tests for legacy profile account migration components."""

from __future__ import annotations

import json
import threading
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
)
from notebooklm._auth.profile_store import LoginWriteRequest, ReplaceResult, ReplaceStatus


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
    release.set()
    scheduler.drain(10.0)
    assert calls == [one.path]
    assert not scheduler._workers_for_tests()
    assert scheduler._scheduled_paths_for_tests() == {str(one.ordering_key)}


def test_scheduler_construction_and_start_failures_remain_once_only(tmp_path):
    store = _SequencedStore(tmp_path / "state.json", [])
    migrator = LegacyAccountMigrator(_RecordingContext())

    def _construct_boom(**kwargs):
        raise RuntimeError("construction")

    scheduler = LegacyPromotionScheduler(_construct_boom)
    with pytest.raises(RuntimeError, match="construction"):
        scheduler.schedule(store, migrator)  # type: ignore[arg-type]
    assert scheduler.schedule(store, migrator) is False  # type: ignore[arg-type]
    assert not scheduler._workers_for_tests()

    class _Unstarted:
        def start(self):
            raise RuntimeError("start")

        def join(self, timeout):
            raise RuntimeError("cannot join thread before it is started")

    scheduler = LegacyPromotionScheduler(lambda **kwargs: _Unstarted())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="start"):
        scheduler.schedule(store, migrator)  # type: ignore[arg-type]
    assert scheduler.schedule(store, migrator) is False  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="cannot join"):
        scheduler.drain(2.0)


def test_scheduler_worker_contains_baseexception_and_deregisters(tmp_path, caplog):
    class _FatalMigrator:
        def promote(self, store):
            raise KeyboardInterrupt("fatal")

    scheduler = LegacyPromotionScheduler()
    store = _SequencedStore(tmp_path / "state.json", [])
    with caplog.at_level("DEBUG", logger="notebooklm.auth"):
        assert scheduler.schedule(store, _FatalMigrator()) is True  # type: ignore[arg-type]
        scheduler.drain(10.0)
    assert not scheduler._workers_for_tests()
    assert "Background legacy account promotion crashed for" in caplog.text


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
    store = object()
    migrator = SimpleNamespace()
    calls: list[object] = []
    migrator._resolve_with_projection = lambda value: (
        LegacyAccount(ProfileAccount(3, None)),
        {"authuser": 3},
    )

    class _Scheduler:
        def schedule(self, value, owner):
            calls.append((value, owner))
            return True

    monkeypatch.setattr(storage, "ProfileStore", lambda value: store)
    monkeypatch.setattr(storage, "LegacyAccountMigrator", lambda: migrator)
    monkeypatch.setattr(storage.LegacyPromotionScheduler, "process_default", lambda: _Scheduler())

    assert storage.read_account_metadata(path) == {"authuser": 3}
    assert calls == [(store, migrator)]
    assert storage.read_account_metadata(None) == {}


def test_public_drop_compatibility_symbol_is_exact_alias():
    assert storage._drop_legacy_account_key is migration._drop_legacy_account_key
    assert public_auth.drop_legacy_account_key is migration._drop_legacy_account_key
