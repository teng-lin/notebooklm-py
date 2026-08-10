"""Direct account-intent tests for the path-owned profile store."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from notebooklm._auth import profile_store as store_mod
from notebooklm._auth.profile_account import ProfileAccount
from notebooklm._auth.profile_migration import LegacyAccountMigrator
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._auth.storage_lock import LockRequest, LockState
from notebooklm.exceptions import LockUnavailableError


class RecordingLocks:
    def __init__(self, state: LockState = LockState.HELD) -> None:
        self.state = state
        self.requests: list[LockRequest] = []

    @contextlib.contextmanager
    def acquire(self, request: LockRequest) -> Iterator[LockState]:
        self.requests.append(request)
        yield self.state


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_in_band_projection(path: Path) -> dict[str, Any]:
    """Take one raw in-band sample through the canonical migration projection."""
    document = ProfileStore(path)._read_account_document()
    projected = LegacyAccountMigrator._project_in_band(document)
    return {} if projected is None else projected[1]


@pytest.mark.parametrize(
    ("account", "typed"),
    [
        (None, None),
        ("bad", None),
        ({}, None),
        ({"unknown": [1, {"x": 2}]}, ProfileAccount(0, None)),
        ({"authuser": True, "email": "  "}, ProfileAccount(0, None)),
        ({"authuser": -1, "email": 3}, ProfileAccount(0, None)),
        ({"authuser": 2.0, "email": " x@example.com "}, ProfileAccount(0, "x@example.com")),
        ({"authuser": "2", "email": "x@example.com"}, ProfileAccount(0, "x@example.com")),
        ({"authuser": 3, "email": " user@example.com "}, ProfileAccount(3, "user@example.com")),
    ],
)
def test_read_account_typed_presence_matrix(
    tmp_path: Path,
    account: object,
    typed: ProfileAccount | None,
) -> None:
    path = tmp_path / "profile.json"
    namespace = {} if account is None else {"account": account}
    _write(path, {"cookies": [], "origins": [], "notebooklm": namespace})
    locks = RecordingLocks()

    assert ProfileStore(path, locks=locks).read_account() == typed
    assert locks.requests == []


def test_raw_adapter_preserves_exact_account_mapping_and_isolation(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    raw = {
        "authuser": True,
        "email": "  raw@example.com  ",
        "nested": {"rows": [1, 2]},
    }
    _write(path, {"notebooklm": {"account": raw}})

    first = _read_in_band_projection(path)
    second = _read_in_band_projection(path)

    assert first == raw
    assert second == raw
    assert first is not second
    first["nested"]["rows"].append(3)
    assert second["nested"] == {"rows": [1, 2]}
    assert json.loads(path.read_text(encoding="utf-8"))["notebooklm"]["account"] == raw


@pytest.mark.parametrize("payload", [[], "root", 3, None])
def test_account_read_non_object_root_is_silent_absence(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    payload: object,
) -> None:
    path = tmp_path / "profile.json"
    _write(path, payload)

    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        assert ProfileStore(path).read_account() is None
        assert _read_in_band_projection(path) == {}
    assert caplog.records == []


def test_account_read_missing_and_corrupt_policies(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = tmp_path / "missing.json"
    assert ProfileStore(missing).read_account() is None
    assert _read_in_band_projection(missing) == {}

    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        assert ProfileStore(path).read_account() is None
    assert [record.getMessage() for record in caplog.records] == [
        f"in-band account read failed at {path}: Expecting property name enclosed in double "
        "quotes: line 1 column 2 (char 1)"
    ]


def test_account_read_invalid_utf8_escapes_without_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "profile.json"
    path.write_bytes(b"\xff")
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        with pytest.raises(UnicodeDecodeError):
            ProfileStore(path).read_account()
        with pytest.raises(UnicodeDecodeError):
            _read_in_band_projection(path)
    assert caplog.records == []


def test_account_read_is_fresh_and_uncached(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    _write(path, {"notebooklm": {"account": {"authuser": 1}}})
    store = ProfileStore(path)
    assert store.read_account() == ProfileAccount(1, None)
    _write(path, {"notebooklm": {"account": {"authuser": 2}}})
    assert store.read_account() == ProfileAccount(2, None)
    assert set(vars(store)) == {"_path", "_locks"}


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (ProfileAccount(True, None), {"authuser": True}),
        (ProfileAccount(-1, ""), {"authuser": -1}),
        (ProfileAccount(2.5, " "), {"authuser": 2.5, "email": " "}),
        (ProfileAccount("3", "a@example.com"), {"authuser": "3", "email": "a@example.com"}),
    ],
)
def test_update_preserves_permissive_record_values(
    tmp_path: Path,
    record: ProfileAccount,
    expected: dict[str, object],
) -> None:
    path = tmp_path / "profile.json"
    assert ProfileStore(path).update_account(record) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "cookies": [],
        "origins": [],
        "notebooklm": {"version": 1, "account": expected},
    }


def test_update_preserves_unrelated_document_and_order(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    original = {
        "opaque": [1, {"x": True}],
        "notebooklm": {"before": 1, "version": "old", "account": "bad", "after": 2},
        "cookies": [{"opaque": "row"}],
        "origins": ["origin"],
    }
    _write(path, original)

    assert ProfileStore(path).update_account(ProfileAccount(4, "x@example.com")) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload) == list(original)
    assert payload["opaque"] == original["opaque"]
    assert payload["cookies"] == original["cookies"]
    assert payload["origins"] == original["origins"]
    assert list(payload["notebooklm"]) == ["before", "version", "account", "after"]
    assert payload["notebooklm"] == {
        "before": 1,
        "version": 1,
        "account": {"authuser": 4, "email": "x@example.com"},
        "after": 2,
    }


@pytest.mark.parametrize(
    ("current", "wrote"),
    [({}, True), (None, True), ("bad", True), ({"unknown": 1}, False)],
)
def test_update_only_if_absent_uses_non_empty_mapping_presence(
    tmp_path: Path,
    current: object,
    wrote: bool,
) -> None:
    path = tmp_path / "profile.json"
    _write(path, {"cookies": [], "origins": [], "notebooklm": {"account": current}})

    assert (
        ProfileStore(path).update_account(
            ProfileAccount(7, "legacy@example.com"), only_if_absent=True
        )
        is wrote
    )
    account = json.loads(path.read_text(encoding="utf-8"))["notebooklm"]["account"]
    assert account == (
        {"unknown": 1} if not wrote else {"authuser": 7, "email": "legacy@example.com"}
    )


def test_update_corruption_and_shape_failures_are_exact(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_info:
        ProfileStore(path).update_account(ProfileAccount(1))
    assert str(exc_info.value).startswith(f"storage state at {path} is corrupted: ")
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)
    assert path.read_text(encoding="utf-8") == "{broken"

    _write(path, [1, 2])
    with pytest.raises(RuntimeError) as exc_info:
        ProfileStore(path).update_account(ProfileAccount(1))
    assert str(exc_info.value) == f"storage state at {path} has unexpected shape: list"
    assert json.loads(path.read_text(encoding="utf-8")) == [1, 2]


def test_conditional_account_update_applies_only_to_exact_document(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    _write(
        path,
        {
            "cookies": [
                {"name": "SID", "value": "a", "domain": ".google.com", "path": "/"},
            ],
            "origins": [],
        },
    )
    store = ProfileStore(path)
    expected = store.read_document()

    assert store._update_account_if_document_unchanged(
        ProfileAccount(1, "first@example.com"),
        expected=expected,
    )
    committed = path.read_bytes()
    assert not store._update_account_if_document_unchanged(
        ProfileAccount(2, "stale@example.com"),
        expected=expected,
    )
    assert path.read_bytes() == committed
    assert store.read_account() == ProfileAccount(1, "first@example.com")


def test_stale_legacy_promotion_cannot_cross_account_clear_tombstone(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    _write(
        path,
        {
            "cookies": [],
            "notebooklm": {"version": 1, "account_route_cleared": True},
        },
    )

    assert not ProfileStore(path).update_account(
        ProfileAccount(7, "stale@example.com"),
        only_if_absent=True,
    )
    assert _read_in_band_projection(path) == {}


def test_clear_missing_profile_blocks_already_sampled_legacy_promotion(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    store = ProfileStore(path)

    store.clear_account()

    assert not store.update_account(
        ProfileAccount(7, "stale@example.com"),
        only_if_absent=True,
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "cookies": [],
        "origins": [],
        "notebooklm": {"version": 1, "account_route_cleared": True},
    }


def test_update_lock_and_commit_failures_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "profile.json"
    _write(path, {"cookies": [], "origins": []})
    missed = RecordingLocks(LockState.UNAVAILABLE)
    with pytest.raises(LockUnavailableError):
        ProfileStore(path, locks=missed).update_account(ProfileAccount(1))
    assert len(missed.requests) == 1
    assert missed.requests[0] == LockRequest(
        path=path.with_name(f".{path.name}.lock"),
        blocking=False,
        operation="write_account_metadata",
        deadline_seconds=90.0,
    )

    failure = OSError("commit failed")
    calls: list[tuple[Path, object]] = []

    def fail_commit(commit_path: Path, payload: object) -> None:
        calls.append((commit_path, payload))
        raise failure

    monkeypatch.setattr(store_mod, "_commit_profile_json", fail_commit)
    with pytest.raises(OSError) as exc_info:
        ProfileStore(path).update_account(ProfileAccount(1))
    assert exc_info.value is failure
    assert len(calls) == 1


def test_update_truthiness_failure_precedes_lock_and_parent(tmp_path: Path) -> None:
    class ExplodingEmail:
        def __bool__(self) -> bool:
            raise LookupError("truth failed")

    path = tmp_path / "missing" / "profile.json"
    locks = RecordingLocks()
    record = ProfileAccount(1, ExplodingEmail())  # type: ignore[arg-type]
    with pytest.raises(LookupError, match="truth failed"):
        ProfileStore(path, locks=locks).update_account(record)
    assert not path.parent.exists()
    assert locks.requests == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], []),
        (
            {"notebooklm": "bad"},
            {"notebooklm": {"version": 1, "account_route_cleared": True}},
        ),
        (
            {"notebooklm": {"version": 1}},
            {"notebooklm": {"version": 1, "account_route_cleared": True}},
        ),
    ],
)
def test_clear_records_an_authoritative_absence(
    tmp_path: Path,
    payload: object,
    expected: object,
) -> None:
    path = tmp_path / "profile.json"
    _write(path, payload)
    ProfileStore(path).clear_account()
    assert json.loads(path.read_text(encoding="utf-8")) == expected


def test_clear_removes_version_only_namespace_and_preserves_siblings(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    _write(path, {"root": 1, "notebooklm": {"version": "odd", "account": {"authuser": 2}}})
    ProfileStore(path).clear_account()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "root": 1,
        "notebooklm": {"version": 1, "account_route_cleared": True},
    }

    _write(
        path,
        {"root": 1, "notebooklm": {"version": 1, "account": {}, "unknown": [1, 2]}},
    )
    ProfileStore(path).clear_account()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "root": 1,
        "notebooklm": {
            "version": 1,
            "account_route_cleared": True,
            "unknown": [1, 2],
        },
    }


def test_clear_missing_lock_miss_and_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "missing" / "profile.json"
    locks = RecordingLocks()
    assert ProfileStore(path, locks=locks).clear_account() is None
    assert [request.operation for request in locks.requests] == ["clear_account_metadata"]
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "cookies": [],
        "origins": [],
        "notebooklm": {"version": 1, "account_route_cleared": True},
    }

    _write(path, {"notebooklm": {"account": {"authuser": 1}}})
    missed = RecordingLocks(LockState.UNAVAILABLE)
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        assert ProfileStore(path, locks=missed).clear_account() is None
    assert [record.getMessage() for record in caplog.records] == [
        f"in-band account clear skipped: storage lock unavailable at {path.with_name(f'.{path.name}.lock')}"
    ]

    failure = OSError("commit failed")
    calls = 0

    def fail_commit(commit_path: Path, payload: object) -> None:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(store_mod, "_commit_profile_json", fail_commit)
    with pytest.raises(OSError) as exc_info:
        ProfileStore(path).clear_account()
    assert exc_info.value is failure
    assert calls == 1


def test_clear_read_failures_keep_exact_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "profile.json"
    _write(path, {"notebooklm": {"account": {"authuser": 1}}})
    failure = OSError("read failed")

    def fail_read(self: Path, *, encoding: str) -> str:
        raise failure

    monkeypatch.setattr(Path, "read_text", fail_read)
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        assert ProfileStore(path).clear_account() is None
    assert [record.getMessage() for record in caplog.records] == [
        f"in-band account clear skipped at {path}: {failure}"
    ]
