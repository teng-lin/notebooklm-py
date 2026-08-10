"""Direct contract tests for the path-owned browser re-mint replacement."""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from notebooklm._auth import profile_store
from notebooklm._auth.profile_account import DomainSelection
from notebooklm._auth.profile_document import ProfileDocument
from notebooklm._auth.profile_store import (
    ProfileStore,
    RemintWriteRequest,
    ReplaceResult,
    ReplaceStatus,
)
from notebooklm._auth.storage_lock import LockRequest, LockState


def _source(*rows: object, **extra: object) -> ProfileDocument:
    return ProfileDocument.decode({"cookies": list(rows), "origins": ["discard"], **extra})


def _row(name: str = "SID", value: object = "secret", **extra: object) -> dict[str, object]:
    return {"name": name, "value": value, "domain": ".google.com", "path": "/", **extra}


class RecordingLocks:
    def __init__(self, state: LockState = LockState.HELD, before_yield=None) -> None:
        self.state = state
        self.before_yield = before_yield
        self.requests: list[LockRequest] = []
        self.released = False

    @contextlib.contextmanager
    def acquire(self, request: LockRequest):
        self.requests.append(request)
        if self.before_yield is not None:
            self.before_yield()
        try:
            yield self.state
        finally:
            self.released = True


def test_value_shapes_signatures_validation_and_redaction() -> None:
    assert str(inspect.signature(ProfileStore.replace_from_remint)) == (
        "(self, request: 'RemintWriteRequest') -> 'ReplaceResult'"
    )
    assert str(inspect.signature(RemintWriteRequest)) == (
        "(source: 'ProfileDocument', carry_account: 'bool', "
        "domain_selection: 'DomainSelection' = DomainSelection(include_domains=frozenset(), "
        "include_optional=False)) -> None"
    )
    assert [(member.name, member.value) for member in ReplaceStatus] == [
        ("APPLIED", "applied"),
        ("LOCK_UNAVAILABLE", "lock_unavailable"),
        ("REQUIRED_COOKIES_DROPPED", "required_cookies_dropped"),
    ]
    request = RemintWriteRequest(_source(_row()), "truthy")  # type: ignore[arg-type]
    assert set(RemintWriteRequest.__slots__) == {
        "source",
        "carry_account",
        "domain_selection",
    }
    assert "SID" not in repr(request) and "secret" not in str(request)
    result = ReplaceResult(ReplaceStatus.APPLIED)
    assert set(ReplaceResult.__slots__) == {
        "status",
        "missing_required",
        "present_names",
        "backup_path",
    }
    assert (result.missing_required, result.present_names, result.backup_path) == ((), (), None)
    locked = ReplaceResult(ReplaceStatus.LOCK_UNAVAILABLE)
    assert (locked.missing_required, locked.present_names, locked.backup_path) == ((), (), None)
    assert "secret" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.status = ReplaceStatus.LOCK_UNAVAILABLE  # type: ignore[misc]
    with pytest.raises(TypeError, match="status must be a ReplaceStatus"):
        ReplaceResult("applied")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="source must be a ProfileDocument"):
        RemintWriteRequest({}, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="domain_selection must be a DomainSelection"):
        RemintWriteRequest(_source(), False, object())  # type: ignore[arg-type]


@pytest.mark.parametrize("state", [LockState.CONTENDED, LockState.UNAVAILABLE])
def test_lock_miss_is_typed_and_runs_zero_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: LockState
) -> None:
    locks = RecordingLocks(state)
    path = tmp_path / "custom" / "A.json"
    monkeypatch.setattr(
        profile_store,
        "filter_storage_state_cookies_by_domain_policy",
        lambda *args, **kwargs: pytest.fail("filter ran"),
    )
    monkeypatch.setattr(
        profile_store, "_commit_profile_json", lambda *args: pytest.fail("commit ran")
    )
    result = ProfileStore(path, locks=locks).replace_from_remint(
        RemintWriteRequest(_source(_row()), True)
    )
    assert result == ReplaceResult(ReplaceStatus.LOCK_UNAVAILABLE)
    assert len(locks.requests) == 1
    request = locks.requests[0]
    assert request.path == path.with_name(".A.json.lock")
    assert request.operation == "replace_from_remint"
    assert request.blocking is False and request.deadline_seconds == 90.0
    assert not path.exists()


@pytest.mark.parametrize(
    "prior",
    [
        b"not json",
        b"[]",
        b'[{"notebooklm": {}}]',
        b"\xff",
        json.dumps({"notebooklm": {"account": {"authuser": 8}}}).encode(),
    ],
)
def test_carry_false_never_reads_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior: bytes
) -> None:
    path = tmp_path / "storage.json"
    path.write_bytes(prior)
    store = ProfileStore(path, locks=RecordingLocks())
    monkeypatch.setattr(store, "read_document", lambda: pytest.fail("destination read"))
    result = store.replace_from_remint(RemintWriteRequest(_source(_row()), False))
    assert result.status is ReplaceStatus.APPLIED
    assert json.loads(path.read_text()) == {
        "cookies": [_row()],
        "origins": [],
        "notebooklm": {"version": 1, "account_route_cleared": True},
    }


@pytest.mark.parametrize(
    ("payload", "expected_namespace"),
    [
        ({}, None),
        ({"notebooklm": []}, None),
        ({"notebooklm": {}}, {}),
        ({"notebooklm": {"account": {}}}, {"account": {}}),
        (
            {"notebooklm": {"future": [1, {"odd": True}], "version": "raw"}},
            {"future": [1, {"odd": True}], "version": "raw"},
        ),
    ],
)
def test_truthy_carry_preserves_only_the_whole_raw_namespace(
    tmp_path: Path, payload: object, expected_namespace: object
) -> None:
    path = tmp_path / "A.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = ProfileStore(path, locks=RecordingLocks()).replace_from_remint(
        RemintWriteRequest(_source(_row(), notebooklm={"source": "drop"}, top="drop"), 1)  # type: ignore[arg-type]
    )
    assert result.status is ReplaceStatus.APPLIED
    actual = json.loads(path.read_text())
    assert list(actual) == (
        ["cookies", "origins"]
        if expected_namespace is None
        else ["cookies", "origins", "notebooklm"]
    )
    assert actual["cookies"] == [_row()]
    assert actual["origins"] == []
    assert actual.get("notebooklm") == expected_namespace


@pytest.mark.parametrize("prior", [b"not json", b"[]"])
def test_tolerated_carry_read_failures_still_commit(tmp_path: Path, prior: bytes) -> None:
    path = tmp_path / "A.json"
    path.write_bytes(prior)
    result = ProfileStore(path, locks=RecordingLocks()).replace_from_remint(
        RemintWriteRequest(_source(_row()), True)
    )
    assert result.status is ReplaceStatus.APPLIED
    assert json.loads(path.read_text()) == {"cookies": [_row()], "origins": []}


def test_carry_read_oserror_is_silent_and_still_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "A.json"
    path.write_text("{}")
    store = ProfileStore(path, locks=RecordingLocks())
    monkeypatch.setattr(store, "read_document", lambda: (_ for _ in ()).throw(OSError("read")))
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        result = store.replace_from_remint(RemintWriteRequest(_source(_row()), True))
    assert result.status is ReplaceStatus.APPLIED
    assert caplog.records == []
    assert json.loads(path.read_text()) == {"cookies": [_row()], "origins": []}


def test_unicode_carry_failure_precedes_filter_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "A.json"
    path.write_bytes(b"\xff")
    locks = RecordingLocks()
    monkeypatch.setattr(
        profile_store,
        "filter_storage_state_cookies_by_domain_policy",
        lambda *args, **kwargs: pytest.fail("filter ran"),
    )
    with pytest.raises(UnicodeDecodeError):
        ProfileStore(path, locks=locks).replace_from_remint(
            RemintWriteRequest(_source(_row()), True)
        )
    assert locks.released and path.read_bytes() == b"\xff"


def test_truthy_carry_existence_oserror_escapes_before_filter_or_commit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class ExistenceFailurePath(type(Path())):
        def exists(self, *, follow_symlinks: bool = True) -> bool:
            raise OSError("stat failed")

    path = ExistenceFailurePath(tmp_path / "A.json")
    locks = RecordingLocks()
    malformed = {"name": 7, "value": "never-log", "domain": ".google.com"}
    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.auth"),
        pytest.raises(OSError, match="stat failed"),
    ):
        ProfileStore(path, locks=locks).replace_from_remint(
            RemintWriteRequest(_source(malformed), True)
        )
    assert locks.released
    assert caplog.records == []
    assert not Path(path).exists()


def test_filter_projection_fidelity_optional_and_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "A.json"
    raw = _row(
        expires=-1.0,
        secure="raw-secure",
        httpOnly="raw-http",
        sameSite="Odd",
        unknown={"nested": [1]},
    )
    source = _source(
        raw,
        _row("Y", domain=".youtube.com"),
        _row("EVIL", domain="evilgoogle.com"),
    )
    request = RemintWriteRequest(
        source,
        False,
        DomainSelection(include_domains=frozenset({"youtube"}), include_optional=False),
    )
    committed: list[dict[str, object]] = []

    def _commit(_: Path, payload: dict[str, object]) -> None:
        committed.append(payload)
        payload["cookies"] = []

    monkeypatch.setattr(profile_store, "_commit_profile_json", _commit)
    result = ProfileStore(path, locks=RecordingLocks()).replace_from_remint(request)
    assert result.status is ReplaceStatus.APPLIED
    projection = request.source.to_json()
    assert projection["cookies"][0] == raw  # type: ignore[index]
    assert committed[0]["origins"] == []
    assert committed[0]["notebooklm"] == {
        "version": 1,
        "account_route_cleared": True,
    }


def test_direct_filter_warning_duplicate_and_raw_row_fidelity(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "A.json"
    first = _row("DUP", "old", expires=-1, secure=True, unknown={"keep": [1]})
    later = _row("DUP", "new", expires=-1.0, secure="raw", unknown={"keep": [2]})
    dotted = _row("TWIN", "dot")
    bare = _row("TWIN", "bare", domain="google.com")
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        result = ProfileStore(path, locks=RecordingLocks()).replace_from_remint(
            RemintWriteRequest(
                _source(
                    "opaque-secret",
                    {"name": 7, "value": "never-log", "domain": ".google.com"},
                    {"name": "silent", "value": "never-log", "domain": ""},
                    first,
                    later,
                    dotted,
                    bare,
                    _row("YT", "optional", domain=".youtube.com"),
                ),
                False,
                DomainSelection(frozenset({"youtube"}), False),
            )
        )
    assert result.status is ReplaceStatus.APPLIED
    rows = json.loads(path.read_text())["cookies"]
    assert rows == [later, dotted, bare, _row("YT", "optional", domain=".youtube.com")]
    assert type(rows[0]["expires"]) is float
    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == ("Skipping malformed storage_state cookie entry (not a dict): type=str")
    assert "missing/empty/non-str name" in messages[1]
    assert "never-log" not in " ".join(messages)
    assert messages[-1] == (
        "Cookie DUP: exact-identity duplicate on (.google.com, /); keeping later observation"
    )


def test_filter_failure_escapes_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = RecordingLocks()
    monkeypatch.setattr(
        profile_store,
        "filter_storage_state_cookies_by_domain_policy",
        lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("filter")),
    )
    with pytest.raises(TypeError, match="filter"):
        ProfileStore(tmp_path / "A.json", locks=locks).replace_from_remint(
            RemintWriteRequest(_source(_row()), False)
        )
    assert locks.released


def test_failures_escape_only_after_lock_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = RecordingLocks()
    monkeypatch.setattr(
        profile_store,
        "_commit_profile_json",
        lambda *args: (_ for _ in ()).throw(RuntimeError("commit")),
    )
    with pytest.raises(RuntimeError, match="commit"):
        ProfileStore(tmp_path / "A.json", locks=locks).replace_from_remint(
            RemintWriteRequest(_source(_row()), False)
        )
    assert locks.released


def test_carry_reads_latest_destination_after_acquisition(tmp_path: Path) -> None:
    path = tmp_path / "A.json"
    path.write_text(json.dumps({"notebooklm": {"account": {"authuser": 1}}}))

    def _competing_write() -> None:
        path.write_text(json.dumps({"notebooklm": {"account": {"authuser": 9}}}))

    locks = RecordingLocks(before_yield=_competing_write)
    result = ProfileStore(path, locks=locks).replace_from_remint(
        RemintWriteRequest(_source(_row()), True)
    )
    assert result.status is ReplaceStatus.APPLIED
    assert json.loads(path.read_text())["notebooklm"]["account"]["authuser"] == 9
