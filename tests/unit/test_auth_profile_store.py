"""Unit tests for the path-owned profile store and cookie transaction spine."""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from notebooklm._auth import profile_store
from notebooklm._auth.cookie_merge import RecoveryObservation
from notebooklm._auth.cookie_types import Cookie, CookieIdentity, CookieJar
from notebooklm._auth.profile_account import DomainSelection, ProfileAccount, StoredSession
from notebooklm._auth.profile_document import ProfileDocument
from notebooklm._auth.profile_store import (
    CookieMergeDisposition,
    CookieMergeResult,
    MintedSessionWriteRequest,
    ProfileStore,
    RemintWriteRequest,
    ReplaceResult,
    ReplaceStatus,
)
from notebooklm._auth.storage_lock import LockRequest, LockState, StorageLockManager
from notebooklm.exceptions import LockUnavailableError


def _cookie(
    value: str,
    *,
    name: str = "SID",
    secure: bool = False,
    expires: int | float | None = None,
) -> Cookie:
    return Cookie(
        name=name,
        domain=".google.com",
        path="/",
        value=value,
        secure=secure,
        expires=expires,
    )


def _row(value: object, *, name: str = "SID", **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "value": value,
        "domain": ".google.com",
        "path": "/",
        **extra,
    }


def _write(path: Path, rows: list[object], **extra: object) -> bytes:
    payload = {"cookies": rows, "origins": [], **extra}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path.read_bytes()


class RecordingLocks:
    def __init__(self, state: LockState = LockState.HELD) -> None:
        self.state = state
        self.requests: list[LockRequest] = []
        self.warning_claimed = False

    @contextlib.contextmanager
    def acquire(self, request: LockRequest):
        self.requests.append(request)
        yield self.state

    def _claim_cookie_warning(self) -> bool:
        if self.warning_claimed:
            return False
        self.warning_claimed = True
        return True


def test_public_shapes_signatures_enum_and_raw_path_are_exact(tmp_path: Path) -> None:
    assert [(member.name, member.value) for member in CookieMergeDisposition] == [
        ("APPLIED", "applied"),
        ("NO_CHANGE", "no_change"),
        ("CONFLICT", "conflict"),
        ("HARD_FAILURE", "hard_failure"),
    ]
    assert str(inspect.signature(ProfileStore)) == (
        "(path: 'Path', *, locks: 'StorageLockManager | None' = None) -> 'None'"
    )
    assert str(inspect.signature(ProfileStore.merge_cookie_observation)) == (
        "(self, observation: 'CookieJar', *, baseline: 'CookieJar', "
        "recovery_observation: 'RecoveryObservation | None' = None) -> 'CookieMergeResult'"
    )
    assert str(inspect.signature(ProfileStore.merge_legacy_cookie_observation)) == (
        "(self, observation: 'CookieJar') -> 'CookieMergeResult'"
    )
    assert str(inspect.signature(ProfileStore.replace_from_remint)) == (
        "(self, request: 'RemintWriteRequest') -> 'ReplaceResult'"
    )
    assert str(inspect.signature(ProfileStore.replace_from_login)) == (
        "(self, request: 'LoginWriteRequest') -> 'ReplaceResult'"
    )
    assert str(inspect.signature(ProfileStore.replace_minted_session)) == (
        "(self, request: 'MintedSessionWriteRequest') -> 'None'"
    )
    assert [(member.name, member.value) for member in ReplaceStatus] == [
        ("APPLIED", "applied"),
        ("LOCK_UNAVAILABLE", "lock_unavailable"),
        ("REQUIRED_COOKIES_DROPPED", "required_cookies_dropped"),
    ]
    assert set(RemintWriteRequest.__slots__) == {
        "source",
        "carry_account",
        "domain_selection",
    }
    assert set(MintedSessionWriteRequest.__slots__) == {
        "cookies",
        "email",
        "force",
        "refuse_unknown_owner",
    }
    assert set(ReplaceResult.__slots__) == {
        "status",
        "missing_required",
        "present_names",
        "backup_path",
    }
    raw = tmp_path / "sub" / ".." / "profile" / "A.json"
    store = ProfileStore(raw)
    assert store.path is raw
    assert store.path == raw
    assert store.ordering_key == raw.expanduser().resolve()
    assert store.path == raw


def test_default_and_injected_lock_manager_identity(tmp_path: Path) -> None:
    default = ProfileStore(tmp_path / "default.json")
    injected = StorageLockManager()
    isolated = ProfileStore(tmp_path / "isolated.json", locks=injected)
    assert set(vars(default)) == {"_path", "_locks"}
    assert default._locks is profile_store._STORAGE_LOCKS  # noqa: SLF001
    assert default._locks is StorageLockManager.process_default()  # noqa: SLF001
    assert isolated._locks is injected  # noqa: SLF001


@pytest.mark.parametrize(
    "result",
    [
        CookieMergeResult(
            CookieMergeDisposition.HARD_FAILURE,
            advances_ordering=False,
            committed=False,
        ),
        CookieMergeResult(
            CookieMergeDisposition.HARD_FAILURE,
            advances_ordering=False,
            committed=None,
        ),
        CookieMergeResult(
            CookieMergeDisposition.NO_CHANGE,
            advances_ordering=True,
            committed=False,
            next_baseline=CookieJar(),
        ),
        CookieMergeResult(
            CookieMergeDisposition.APPLIED,
            advances_ordering=True,
            committed=True,
            next_baseline=CookieJar((_cookie("secret"),)),
        ),
        CookieMergeResult(
            CookieMergeDisposition.CONFLICT,
            advances_ordering=True,
            committed=False,
            next_baseline=CookieJar(),
            rejected=frozenset({CookieIdentity("SID", ".google.com", "/")}),
        ),
    ],
)
def test_result_valid_shapes_are_frozen_slotted_redacted_and_isolated(
    result: CookieMergeResult,
) -> None:
    assert set(CookieMergeResult.__slots__) == {
        "disposition",
        "advances_ordering",
        "committed",
        "next_baseline",
        "rejected",
    }
    with pytest.raises(FrozenInstanceError):
        result.committed = False  # type: ignore[misc]
    assert "secret" not in repr(result)
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "disposition": CookieMergeDisposition.HARD_FAILURE,
            "advances_ordering": True,
            "committed": False,
        },
        {
            "disposition": CookieMergeDisposition.HARD_FAILURE,
            "advances_ordering": False,
            "committed": True,
        },
        {
            "disposition": CookieMergeDisposition.NO_CHANGE,
            "advances_ordering": True,
            "committed": True,
            "next_baseline": CookieJar(),
        },
        {
            "disposition": CookieMergeDisposition.APPLIED,
            "advances_ordering": True,
            "committed": False,
            "next_baseline": CookieJar(),
        },
        {
            "disposition": CookieMergeDisposition.CONFLICT,
            "advances_ordering": True,
            "committed": False,
            "next_baseline": CookieJar(),
        },
        {
            "disposition": CookieMergeDisposition.NO_CHANGE,
            "advances_ordering": 1,
            "committed": False,
            "next_baseline": CookieJar(),
        },
        {
            "disposition": CookieMergeDisposition.NO_CHANGE,
            "advances_ordering": True,
            "committed": 0,
            "next_baseline": CookieJar(),
        },
    ],
)
def test_result_invalid_shapes_raise_bounded_value_free_errors(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)) as excinfo:
        CookieMergeResult(**kwargs)  # type: ignore[arg-type]
    assert "secret" not in str(excinfo.value)


def test_result_defensively_copies_baseline_and_rejected() -> None:
    items = [_cookie("secret")]
    rejected = {CookieIdentity("APISID", ".google.com", "/")}
    result = CookieMergeResult(
        CookieMergeDisposition.CONFLICT,
        advances_ordering=True,
        committed=False,
        next_baseline=CookieJar(items),
        rejected=frozenset(rejected),
    )
    items.clear()
    rejected.clear()
    assert len(result.next_baseline or ()) == 1
    assert result.rejected == frozenset({CookieIdentity("APISID", ".google.com", "/")})


def test_reads_are_fresh_isolated_and_project_route_session(tmp_path: Path) -> None:
    path = tmp_path / "A.json"
    _write(
        path,
        [_row("first")],
        notebooklm={
            "account": {"authuser": 3, "email": " route@example.com "},
            "include_domains": ["drive"],
            "include_optional": True,
        },
    )
    store = ProfileStore(path)
    first = store.read_document()
    first_json = first.to_json()
    first_json["cookies"] = []
    _write(
        path,
        [_row("second")],
        notebooklm={
            "account": {"authuser": 4},
            "include_domains": ["youtube", 1],
            "include_optional": False,
        },
    )
    second = store.read_document()
    assert first.to_json()["cookies"][0]["value"] == "first"  # type: ignore[index]
    assert second.to_json()["cookies"][0]["value"] == "second"  # type: ignore[index]
    session = store.read_session()
    assert isinstance(session, StoredSession)
    assert next(iter(session.cookies)).value == "second"
    assert session.account == ProfileAccount(authuser=4)
    assert session.domain_selection == DomainSelection(frozenset({"youtube"}), False)


@pytest.mark.parametrize(
    ("raw", "error"),
    [("{", json.JSONDecodeError), ("[]", ValueError), ('{"cookies": {}}', None)],
)
def test_read_document_preserves_raw_read_decode_results(
    tmp_path: Path, raw: str, error: type[Exception] | None
) -> None:
    path = tmp_path / "A.json"
    path.write_text(raw, encoding="utf-8")
    store = ProfileStore(path)
    if error is None:
        assert isinstance(store.read_document(), ProfileDocument)
    else:
        with pytest.raises(error):
            store.read_document()


def test_bounded_lock_order_request_projection_and_body_exception_distinction(
    tmp_path: Path,
) -> None:
    locks = RecordingLocks()
    store = ProfileStore(tmp_path / "nested" / "A.json", locks=locks)  # type: ignore[arg-type]
    calls: list[str] = []
    assert (
        store._under_bounded_lock(  # noqa: SLF001
            operation="writer",
            body=lambda: calls.append("body") or "done",
            on_unavailable=lambda path: calls.append("unavailable") or "miss",
            deadline_seconds=12.5,
        )
        == "done"
    )
    assert calls == ["body"]
    assert store.path.parent.exists()
    assert locks.requests == [
        LockRequest(
            path=store.path.with_name(".A.json.lock"),
            blocking=False,
            operation="writer",
            deadline_seconds=12.5,
        )
    ]

    marker = LockUnavailableError("body failure")
    unavailable_calls: list[Path] = []

    def fail() -> None:
        raise marker

    with pytest.raises(LockUnavailableError) as excinfo:
        store._under_bounded_lock(  # noqa: SLF001
            operation="writer",
            body=fail,
            on_unavailable=lambda path: unavailable_calls.append(path),
        )
    assert excinfo.value is marker
    assert unavailable_calls == []


def test_bounded_lock_miss_calls_policy_without_body(tmp_path: Path) -> None:
    locks = RecordingLocks(LockState.UNAVAILABLE)
    store = ProfileStore(tmp_path / "A.json", locks=locks)  # type: ignore[arg-type]
    body_calls: list[None] = []
    lock_paths: list[Path] = []
    result = store._under_bounded_lock(  # noqa: SLF001
        operation="writer",
        body=lambda: body_calls.append(None),
        on_unavailable=lambda path: lock_paths.append(path) or "miss",
    )
    assert result == "miss"
    assert body_calls == []
    assert lock_paths == [tmp_path / ".A.json.lock"]


def test_blocking_cookie_lock_has_no_parent_prep_and_fails_open_warning_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    locks = RecordingLocks(LockState.UNAVAILABLE)
    store = ProfileStore(tmp_path / "missing" / "A.json", locks=locks)  # type: ignore[arg-type]
    monkeypatch.setattr(
        profile_store,
        "_ensure_secure_parent_dir",
        lambda path: pytest.fail("blocking cookie path must not prepare/chmod parent"),
    )
    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        assert (
            store._under_blocking_cookie_lock(  # noqa: SLF001
                operation="save_cookies_to_storage", body=lambda: "ran"
            )
            == "ran"
        )
        assert (
            store._under_blocking_cookie_lock(  # noqa: SLF001
                operation="save_cookies_to_storage", body=lambda: "ran-again"
            )
            == "ran-again"
        )
    assert [(request.blocking, request.deadline_seconds) for request in locks.requests] == [
        (True, None),
        (True, None),
    ]
    assert [record.getMessage() for record in caplog.records] == [
        f"Cross-process file lock unavailable at {tmp_path / 'missing' / '.A.json.lock'}; "
        "cookie saves will proceed without cross-process coordination and rely solely on "
        "snapshot/delta CAS guards. Common causes: NFS without flock support, read-only "
        "parent directory, fd exhaustion. (Logged once per process.)"
    ]


def test_real_blocking_manager_creates_raw_parent_and_0600_sentinel(tmp_path: Path) -> None:
    path = tmp_path / "new-parent" / "A.json"
    store = ProfileStore(path, locks=StorageLockManager())
    result = store.merge_legacy_cookie_observation(CookieJar((_cookie("fresh"),)))
    lock = path.with_name(".A.json.lock")
    assert result == CookieMergeResult(
        CookieMergeDisposition.HARD_FAILURE,
        advances_ordering=False,
        committed=False,
    )
    assert path.parent.is_dir()
    assert lock.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_equivalent_raw_spellings_share_ordering_but_not_request_spelling(tmp_path: Path) -> None:
    direct = tmp_path / "profile" / "A.json"
    alternate = tmp_path / "profile" / "nested" / ".." / "A.json"
    first_locks = RecordingLocks()
    second_locks = RecordingLocks()
    first = ProfileStore(direct, locks=first_locks)  # type: ignore[arg-type]
    second = ProfileStore(alternate, locks=second_locks)  # type: ignore[arg-type]
    assert first.ordering_key == second.ordering_key
    first._under_blocking_cookie_lock(operation="save_cookies_to_storage", body=lambda: None)  # noqa: SLF001
    second._under_blocking_cookie_lock(operation="save_cookies_to_storage", body=lambda: None)  # noqa: SLF001
    assert first_locks.requests[0].path == direct.with_name(".A.json.lock")
    assert second_locks.requests[0].path == alternate.with_name(".A.json.lock")
    assert first_locks.requests[0].path != second_locks.requests[0].path


def test_cookie_no_change_and_identical_decided_document_result_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "A.json"
    canonical = _row(
        "same",
        expires=-1,
        httpOnly=False,
        secure=False,
        sameSite="None",
    )
    original = _write(path, [canonical])
    store = ProfileStore(path, locks=RecordingLocks())  # type: ignore[arg-type]
    unchanged = store.merge_cookie_observation(
        CookieJar((_cookie("same"),)), baseline=CookieJar((_cookie("same"),))
    )
    assert unchanged == CookieMergeResult(
        CookieMergeDisposition.NO_CHANGE,
        advances_ordering=True,
        committed=False,
        next_baseline=CookieJar((_cookie("same"),)),
    )
    assert path.read_bytes() == original

    commits: list[tuple[Path, object]] = []

    def commit(target: Path, payload: object) -> None:
        commits.append((target, payload))

    monkeypatch.setattr(profile_store, "_commit_profile_json", commit)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        applied = store.merge_cookie_observation(
            CookieJar((_cookie("same"),)), baseline=CookieJar()
        )
    assert applied.disposition is CookieMergeDisposition.APPLIED
    assert applied.committed is True
    assert len(commits) == 1
    assert commits[0] == (path, {"cookies": [canonical], "origins": []})
    assert [record.getMessage() for record in caplog.records] == [
        f"Successfully synced 1 refreshed cookies to {path}"
    ]


def test_cookie_partial_and_all_conflict_result_table(tmp_path: Path) -> None:
    path = tmp_path / "A.json"
    _write(path, [_row("old"), _row("sibling", name="APISID", unknown="keep")])
    store = ProfileStore(path, locks=RecordingLocks())  # type: ignore[arg-type]
    partial = store.merge_cookie_observation(
        CookieJar((_cookie("fresh"), _cookie("fresh-api", name="APISID"))),
        baseline=CookieJar((_cookie("old"),)),
    )
    assert partial.disposition is CookieMergeDisposition.CONFLICT
    assert partial.committed is True
    assert partial.rejected == frozenset({CookieIdentity("APISID", ".google.com", "/")})
    rows = json.loads(path.read_text(encoding="utf-8"))["cookies"]
    assert rows[0]["value"] == "fresh"
    assert rows[1] == _row("sibling", name="APISID", unknown="keep")

    all_conflict = store.merge_cookie_observation(
        CookieJar((_cookie("third", name="APISID"),)),
        baseline=CookieJar((_cookie("baseline", name="APISID"),)),
    )
    assert all_conflict.disposition is CookieMergeDisposition.CONFLICT
    assert all_conflict.committed is False
    assert all_conflict.rejected == frozenset({CookieIdentity("APISID", ".google.com", "/")})


def test_cookie_read_and_commit_failures_have_distinct_committed_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = tmp_path / "missing.json"
    store = ProfileStore(missing, locks=RecordingLocks())  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        read_failure = store.merge_legacy_cookie_observation(CookieJar((_cookie("fresh"),)))
    assert read_failure == CookieMergeResult(
        CookieMergeDisposition.HARD_FAILURE,
        advances_ordering=False,
        committed=False,
    )
    assert [record.getMessage() for record in caplog.records] == [
        f"Skipping cookie sync: Storage file not found at {missing}"
    ]

    path = tmp_path / "A.json"
    _write(path, [_row("old")])
    marker = OSError("secret commit error")

    def fail(target: Path, payload: object) -> None:
        raise marker

    monkeypatch.setattr(profile_store, "_commit_profile_json", fail)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        commit_failure = ProfileStore(path, locks=RecordingLocks()).merge_cookie_observation(  # type: ignore[arg-type]
            CookieJar((_cookie("fresh"),)), baseline=CookieJar((_cookie("old"),))
        )
    assert commit_failure == CookieMergeResult(
        CookieMergeDisposition.HARD_FAILURE,
        advances_ordering=False,
        committed=None,
    )
    assert [record.getMessage() for record in caplog.records] == [
        f"Failed to write updated cookies to {path}: OSError"
    ]
    assert "secret" not in repr(commit_failure)


def test_legacy_and_recovery_methods_use_distinct_decisions(tmp_path: Path) -> None:
    path = tmp_path / "A.json"
    _write(path, [_row("old", name="__Secure-1PSIDTS")])
    store = ProfileStore(path, locks=RecordingLocks())  # type: ignore[arg-type]
    legacy = store.merge_legacy_cookie_observation(
        CookieJar((_cookie("fresh", name="__Secure-1PSIDTS"),))
    )
    assert legacy.disposition is CookieMergeDisposition.APPLIED
    assert legacy.rejected == frozenset()

    _write(path, [_row("observed", name="__Secure-1PSIDTS", unknown="drop")])
    recovery = RecoveryObservation(
        {CookieIdentity("__Secure-1PSIDTS", ".google.com", "/"): frozenset({"observed"})}
    )
    recovered = store.merge_cookie_observation(
        CookieJar((_cookie("fresh", name="__Secure-1PSIDTS"),)),
        baseline=CookieJar((_cookie("old", name="__Secure-1PSIDTS"),)),
        recovery_observation=recovery,
    )
    assert recovered.disposition is CookieMergeDisposition.APPLIED
    assert "unknown" not in json.loads(path.read_text(encoding="utf-8"))["cookies"][0]
