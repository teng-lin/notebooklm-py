"""Direct contract tests for the path-owned login/import replacement."""

from __future__ import annotations

import contextlib
import copy
import inspect
import json
import logging
import os
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from notebooklm._auth import profile_store
from notebooklm._auth.profile_account import (
    ClearAccount,
    DomainSelection,
    KeepAccount,
    ProfileAccount,
    SetAccount,
)
from notebooklm._auth.profile_document import ProfileDocument
from notebooklm._auth.profile_store import (
    LoginWriteRequest,
    ProfileStore,
    ReplaceResult,
    ReplaceStatus,
)
from notebooklm._auth.storage_lock import LockRequest, LockState


def _row(
    name: str,
    value: object = "secret",
    *,
    domain: str = ".google.com",
    path: str = "/",
    **extra: object,
) -> dict[str, object]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        **extra,
    }


def _source(*, namespace: object = None, extra_rows: list[object] | None = None) -> ProfileDocument:
    payload: dict[str, object] = {
        "cookies": [
            _row("SID", "sid-secret"),
            _row("__Secure-1PSIDTS", "psidts-secret"),
            *(extra_rows or []),
        ],
        "origins": [{"origin": "https://example.invalid"}],
        "untrusted-root": "drop-me",
    }
    if namespace is not None:
        payload["notebooklm"] = namespace
    return ProfileDocument.decode(payload)


def _request(
    *,
    source: ProfileDocument | None = None,
    selection: DomainSelection = DomainSelection(),
    account: KeepAccount | SetAccount | ClearAccount = KeepAccount(),
    backup: object = False,
) -> LoginWriteRequest:
    return LoginWriteRequest(
        source=source or _source(),
        domain_selection=selection,
        account=account,
        backup=backup,  # type: ignore[arg-type]
    )


class RecordingLocks:
    def __init__(self, state: LockState = LockState.HELD) -> None:
        self.state = state
        self.requests: list[LockRequest] = []
        self.events: list[str] = []

    @contextlib.contextmanager
    def acquire(self, request: LockRequest):
        self.requests.append(request)
        self.events.append("enter")
        try:
            yield self.state
        finally:
            self.events.append("exit")


def test_request_result_and_method_shapes_are_exact_redacted_and_closed() -> None:
    assert str(inspect.signature(LoginWriteRequest)) == (
        "(source: 'ProfileDocument', domain_selection: 'DomainSelection', "
        "account: 'AccountDirective', backup: 'bool' = False) -> None"
    )
    assert str(inspect.signature(ProfileStore.replace_from_login)) == (
        "(self, request: 'LoginWriteRequest') -> 'ReplaceResult'"
    )
    assert [(member.name, member.value) for member in ReplaceStatus] == [
        ("APPLIED", "applied"),
        ("LOCK_UNAVAILABLE", "lock_unavailable"),
        ("REQUIRED_COOKIES_DROPPED", "required_cookies_dropped"),
    ]
    assert set(LoginWriteRequest.__slots__) == {
        "source",
        "domain_selection",
        "account",
        "backup",
    }
    assert set(ReplaceResult.__slots__) == {
        "status",
        "missing_required",
        "present_names",
        "backup_path",
    }

    secret = "request-secret@example.com"
    request = _request(account=SetAccount(ProfileAccount(3, secret)), backup="truthy")
    assert secret not in repr(request)
    assert secret not in str(request)
    assert request.backup == "truthy"
    with pytest.raises(FrozenInstanceError):
        request.backup = False  # type: ignore[misc]

    applied = ReplaceResult(ReplaceStatus.APPLIED, backup_path=Path("A.json.bak"))
    locked = ReplaceResult(ReplaceStatus.LOCK_UNAVAILABLE)
    rejected = ReplaceResult(
        ReplaceStatus.REQUIRED_COOKIES_DROPPED,
        missing_required=("B", "SID"),
        present_names=("A",),
    )
    assert applied.backup_path == Path("A.json.bak")
    assert locked.missing_required == locked.present_names == ()
    assert rejected.missing_required == ("B", "SID")
    assert (applied.ok, applied.lock_unavailable, applied.required_cookies_dropped) == (
        True,
        False,
        False,
    )
    assert (locked.ok, locked.lock_unavailable, locked.required_cookies_dropped) == (
        False,
        True,
        False,
    )
    assert (rejected.ok, rejected.lock_unavailable, rejected.required_cookies_dropped) == (
        False,
        False,
        True,
    )
    assert {name for name, value in vars(ReplaceResult).items() if isinstance(value, property)} == {
        "ok",
        "lock_unavailable",
        "required_cookies_dropped",
    }
    with pytest.raises(FrozenInstanceError):
        applied.status = ReplaceStatus.LOCK_UNAVAILABLE  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": object()},
        {"status": ReplaceStatus.APPLIED, "missing_required": ("SID",)},
        {"status": ReplaceStatus.APPLIED, "present_names": ("SID",)},
        {"status": ReplaceStatus.LOCK_UNAVAILABLE, "backup_path": Path("x")},
        {"status": ReplaceStatus.REQUIRED_COOKIES_DROPPED},
        {
            "status": ReplaceStatus.REQUIRED_COOKIES_DROPPED,
            "missing_required": ("SID", "SID"),
        },
        {
            "status": ReplaceStatus.REQUIRED_COOKIES_DROPPED,
            "missing_required": ("SID", "A"),
        },
        {
            "status": ReplaceStatus.REQUIRED_COOKIES_DROPPED,
            "missing_required": ("SID",),
            "present_names": ("SID",),
        },
        {
            "status": ReplaceStatus.REQUIRED_COOKIES_DROPPED,
            "missing_required": (1,),
        },
    ],
)
def test_invalid_result_states_raise_bounded_errors(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)) as excinfo:
        ReplaceResult(**kwargs)  # type: ignore[arg-type]
    assert "secret" not in str(excinfo.value)


def test_request_snapshots_source_domain_and_shared_cyclic_account_values() -> None:
    namespace = {"account": {"authuser": 7}, "unknown": {"rows": [1]}}
    raw = {
        "cookies": [_row("SID"), _row("__Secure-1PSIDTS")],
        "origins": [],
        "notebooklm": namespace,
    }
    source = ProfileDocument.decode(raw)
    domains = {"youtube"}
    selection = DomainSelection(frozenset(domains), True)
    shared: list[object] = []
    shared.append(shared)
    request = LoginWriteRequest(
        source=source,
        domain_selection=selection,
        account=SetAccount(ProfileAccount(shared, shared)),  # type: ignore[arg-type]
    )

    raw["cookies"] = []
    namespace["unknown"]["rows"].append(2)  # type: ignore[index,union-attr]
    domains.add("maps")
    shared.append("caller mutation")

    assert len(request.source.to_json()["cookies"]) == 2  # type: ignore[arg-type]
    assert request.domain_selection.include_domains == frozenset({"youtube"})
    assert isinstance(request.account, SetAccount)
    authuser = request.account.record.authuser
    email = request.account.record.email
    assert authuser is email
    assert isinstance(authuser, list)
    assert authuser[0] is authuser
    assert "caller mutation" not in authuser


def test_request_rejects_alien_values_and_copy_failure_before_store_activity() -> None:
    class AlienKeep(KeepAccount):
        pass

    class FailsCopy:
        def __deepcopy__(self, memo: dict[int, object]) -> object:
            raise RuntimeError("bounded copy failure")

    with pytest.raises(TypeError, match="source must"):
        LoginWriteRequest(object(), DomainSelection(), KeepAccount())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="domain_selection"):
        LoginWriteRequest(_source(), object(), KeepAccount())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="account directive"):
        LoginWriteRequest(_source(), DomainSelection(), AlienKeep())
    with pytest.raises(RuntimeError, match="bounded copy failure"):
        LoginWriteRequest(
            _source(),
            DomainSelection(),
            SetAccount(ProfileAccount(FailsCopy(), FailsCopy())),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("lock_state", [LockState.CONTENDED, LockState.UNAVAILABLE])
def test_unheld_lock_reports_without_entering_transaction_body(
    tmp_path: Path,
    lock_state: LockState,
) -> None:
    class FilterMustNotRun(ProfileDocument):
        def to_json(self) -> dict[str, object]:
            pytest.fail("lock miss must not enter the filter projection")

    locks = RecordingLocks(lock_state)
    path = tmp_path / "custom" / "A.json"
    source = FilterMustNotRun.decode({"cookies": [], "origins": []})
    result = ProfileStore(path, locks=locks).replace_from_login(_request(source=source))

    assert result == ReplaceResult(ReplaceStatus.LOCK_UNAVAILABLE)
    assert not path.exists()
    assert locks.events == ["enter", "exit"]
    assert len(locks.requests) == 1
    request = locks.requests[0]
    assert request.path == path.with_name(f".{path.name}.lock")
    assert request.operation == "replace_from_login"
    assert request.blocking is False
    assert request.deadline_seconds == 90.0


def test_raw_filter_fidelity_required_gate_and_keep_namespace_are_one_held_body(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    locks = RecordingLocks()
    path = tmp_path / "A.json"
    first_sid = _row("SID", "first", sameSite="Lax", expires=-1, marker={"n": 1})
    later_sid = _row("SID", "later", sameSite="None", expires=-1.0, marker={"n": 2})
    source = _source(
        namespace={"account": None, "version": "raw", "unknown": {"nested": [1]}},
        extra_rows=[
            first_sid,
            later_sid,
            _row("YT", domain=".youtube.com"),
            "malformed",
        ],
    )
    selection = DomainSelection(frozenset({"youtube"}), True)

    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        result = ProfileStore(path, locks=locks).replace_from_login(
            _request(source=source, selection=selection)
        )

    assert result == ReplaceResult(ReplaceStatus.APPLIED)
    assert locks.events == ["enter", "exit"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload) == ["cookies", "origins", "notebooklm"]
    assert payload["origins"] == []
    assert "untrusted-root" not in payload
    rows = payload["cookies"]
    assert isinstance(rows, list)
    sid_rows = [row for row in rows if isinstance(row, dict) and row.get("name") == "SID"]
    assert sid_rows == [later_sid]
    assert type(sid_rows[0]["expires"]) is float
    assert any(isinstance(row, dict) and row.get("name") == "YT" for row in rows)
    assert payload["notebooklm"] == {
        "account": None,
        "version": "raw",
        "unknown": {"nested": [1]},
        "include_domains": ["youtube"],
        "include_optional": True,
    }
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "str" in warnings[0].getMessage()
    assert "secret" not in warnings[0].getMessage()


def test_required_rejection_uses_late_policy_attributes_and_precedes_destination_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "sentinel-secret-profile.json"
    path.write_bytes(b"opaque predecessor")
    locks = RecordingLocks()
    calls: list[str] = []

    monkeypatch.setattr(profile_store._cookie_policy, "MINIMUM_REQUIRED_COOKIES", {"Z", "A"})

    def names(payload: dict[str, Any]) -> set[str]:
        calls.append("policy")
        return {"B"}

    monkeypatch.setattr(profile_store._cookie_policy, "cookie_names_from_storage", names)
    monkeypatch.setattr(
        profile_store.shutil,
        "copy2",
        lambda *args: pytest.fail("backup must not run"),
    )
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        result = ProfileStore(path, locks=locks).replace_from_login(_request(backup=True))

    assert result == ReplaceResult(
        ReplaceStatus.REQUIRED_COOKIES_DROPPED,
        missing_required=("A", "Z"),
        present_names=("B",),
    )
    assert calls == ["policy"]
    assert path.read_bytes() == b"opaque predecessor"
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
    assert messages == [
        f"replace_from_login: 2 required cookie(s) dropped by the write-time domain policy "
        f"for {path}; writing nothing"
    ]
    policy_projection = messages[0].split(" for ", 1)[0]
    assert "A" not in policy_projection
    assert "Z" not in policy_projection
    assert "B" not in policy_projection


def test_set_projection_uses_a_second_shared_memo_copy_and_persists_isolated_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request copy and both write-time projections preserve shared identity."""
    shared: list[object] = [{"nested": [1]}]
    real_deepcopy = copy.deepcopy
    calls: list[object] = []

    def tracked(value: object) -> object:
        calls.append(value)
        return real_deepcopy(value)

    monkeypatch.setattr(profile_store.copy, "deepcopy", tracked)
    request = _request(account=SetAccount(ProfileAccount(shared, shared)))  # type: ignore[arg-type]
    assert len(calls) == 1
    assert isinstance(request.account, SetAccount)
    request_shared = request.account.record.authuser
    assert request_shared is request.account.record.email
    shared.append("caller mutation")

    path = tmp_path / "A.json"
    store = ProfileStore(path, locks=RecordingLocks())
    assert store.replace_from_login(request).status is ReplaceStatus.APPLIED
    assert store.replace_from_login(request).status is ReplaceStatus.APPLIED

    assert len(calls) == 3
    for projected_call in calls:
        assert isinstance(projected_call, tuple)
        assert projected_call[0] is projected_call[1]
    assert isinstance(request_shared, list)
    assert "caller mutation" not in request_shared
    committed = json.loads(path.read_text(encoding="utf-8"))["notebooklm"]["account"]
    assert committed["authuser"] == [{"nested": [1]}]
    assert committed["email"] == committed["authuser"]


def test_held_set_projection_copy_failure_releases_without_backup_or_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailOnProjection:
        def __init__(self, generation: int = 0) -> None:
            self.generation = generation

        def __deepcopy__(self, memo: dict[int, object]) -> object:
            if self.generation:
                raise RuntimeError("bounded projection failure")
            copied = type(self)(1)
            memo[id(self)] = copied
            return copied

    original = FailOnProjection()
    request = _request(
        account=SetAccount(ProfileAccount(original, original)),  # type: ignore[arg-type]
        backup=True,
    )
    locks = RecordingLocks()
    monkeypatch.setattr(
        profile_store.shutil,
        "copy2",
        lambda *args: pytest.fail("backup must not run"),
    )
    path = tmp_path / "A.json"
    with pytest.raises(RuntimeError, match="bounded projection failure"):
        ProfileStore(path, locks=locks).replace_from_login(request)
    assert locks.events == ["enter", "exit"]
    assert not path.exists()


@pytest.mark.parametrize(
    ("account", "namespace", "expected"),
    [
        (KeepAccount(), {}, None),
        (KeepAccount(), {"account": {}}, {"account": {}, "version": 1}),
        (KeepAccount(), ["odd"], None),
        (
            ClearAccount(),
            {"unknown": 1},
            {"account_route_cleared": True, "version": 1},
        ),
        (
            SetAccount(ProfileAccount(True, 17)),  # type: ignore[arg-type]
            {"unknown": 1},
            {"account": {"authuser": True, "email": 17}, "version": 1},
        ),
    ],
)
def test_directives_construct_exact_distinct_namespaces(
    tmp_path: Path,
    account: KeepAccount | SetAccount | ClearAccount,
    namespace: object,
    expected: dict[str, object] | None,
) -> None:
    path = tmp_path / "A.json"
    request = _request(source=_source(namespace=namespace), account=account)
    result = ProfileStore(path, locks=RecordingLocks()).replace_from_login(request)
    assert result.status is ReplaceStatus.APPLIED
    committed = json.loads(path.read_text(encoding="utf-8"))
    if expected is None:
        assert "notebooklm" not in committed
    else:
        assert committed["notebooklm"] == expected


def test_destination_corruption_and_backup_matrix(tmp_path: Path) -> None:
    without_backup = tmp_path / "without" / "A.json"
    without_backup.parent.mkdir()
    without_backup.write_bytes(b"\xff opaque invalid json")
    result = ProfileStore(without_backup, locks=RecordingLocks()).replace_from_login(_request())
    assert result == ReplaceResult(ReplaceStatus.APPLIED)
    assert json.loads(without_backup.read_text(encoding="utf-8"))["cookies"]
    assert not without_backup.with_name("A.json.bak").exists()

    absent = tmp_path / "absent" / "A.json"
    result = ProfileStore(absent, locks=RecordingLocks()).replace_from_login(_request(backup=True))
    assert result == ReplaceResult(ReplaceStatus.APPLIED)

    existing = tmp_path / "existing" / "custom.json"
    existing.parent.mkdir()
    previous = b"not json at all\n"
    existing.write_bytes(previous)
    os.chmod(existing, 0o644)
    result = ProfileStore(existing, locks=RecordingLocks()).replace_from_login(
        _request(backup=True)
    )
    candidate = existing.with_name("custom.json.bak")
    assert result == ReplaceResult(ReplaceStatus.APPLIED, backup_path=candidate)
    assert candidate.read_bytes() == previous
    if os.name != "nt":
        assert candidate.stat().st_mode & 0o777 == 0o600


def test_windows_backup_skips_chmod_and_commit_failure_leaves_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "A.json"
    path.write_bytes(b"predecessor")
    monkeypatch.setattr(profile_store.sys, "platform", "win32")
    monkeypatch.setattr(
        profile_store.shutil,
        "copy2",
        lambda source, destination: destination.write_bytes(source.read_bytes()),
    )
    monkeypatch.setattr(
        profile_store.os,
        "chmod",
        lambda *args: pytest.fail("Windows must not chmod the backup"),
    )

    def fail_commit(path: Path, payload: dict[str, Any]) -> None:
        raise OSError("commit failed")

    monkeypatch.setattr(profile_store, "_commit_profile_json", fail_commit)
    with pytest.raises(OSError, match="commit failed"):
        ProfileStore(path, locks=RecordingLocks()).replace_from_login(_request(backup=True))
    assert path.with_name("A.json.bak").read_bytes() == b"predecessor"


def test_backup_observes_latest_serialized_predecessor(tmp_path: Path) -> None:
    class SerialLocks:
        def __init__(self) -> None:
            self.gate = threading.Lock()

        @contextlib.contextmanager
        def acquire(self, request: LockRequest):
            with self.gate:
                yield LockState.HELD

    path = tmp_path / "A.json"
    path.write_bytes(b"old predecessor")
    locks = SerialLocks()
    started = threading.Event()
    results: list[ReplaceResult] = []

    def login() -> None:
        started.set()
        results.append(ProfileStore(path, locks=locks).replace_from_login(_request(backup=True)))

    with locks.gate:
        path.write_bytes(b"latest predecessor")
        thread = threading.Thread(target=login)
        thread.start()
        assert started.wait(timeout=2)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert results == [
        ReplaceResult(ReplaceStatus.APPLIED, backup_path=path.with_name("A.json.bak"))
    ]
    assert path.with_name("A.json.bak").read_bytes() == b"latest predecessor"
