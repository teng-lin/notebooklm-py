"""Direct behavior matrix for path-owned master-token persistence."""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from notebooklm._auth import master_token, master_token_file, storage
from notebooklm._auth.master_token_file import MasterTokenFile, _MasterTokenRead
from notebooklm._auth.master_token_types import MasterToken, _MasterTokenRecordError
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._auth.storage_lock import LockRequest, LockState, StorageLockManager
from notebooklm.exceptions import LockUnavailableError

_MALFORMED = "master_token.json is malformed or an unsupported version."


class RecordingLocks(StorageLockManager):
    def __init__(
        self,
        state: LockState = LockState.HELD,
        *,
        events: list[str] | None = None,
        on_enter: Callable[[LockRequest], None] | None = None,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self.requests: list[LockRequest] = []
        self.events = events if events is not None else []
        self.on_enter = on_enter
        self.on_exit = on_exit

    @contextlib.contextmanager
    def acquire(self, request: LockRequest):
        self.requests.append(request)
        self.events.append("lock-enter")
        if self.on_enter is not None:
            self.on_enter(request)
        try:
            yield self.state
        finally:
            if self.on_exit is not None:
                self.on_exit()
            self.events.append("lock-exit")


def _token() -> MasterToken:
    return MasterToken(email="person@example.com", android_id="android-1", secret="secret")


def _record(**extra: object) -> dict[str, object]:
    return {
        "version": 1,
        "email": "person@example.com",
        "android_id": "android-1",
        "master_token": "secret",
        **extra,
    }


def test_value_file_and_private_pair_shapes_are_exact(tmp_path: Path) -> None:
    assert dataclasses.is_dataclass(_MasterTokenRead)
    assert _MasterTokenRead.__dataclass_params__.frozen is True
    assert _MasterTokenRead.__dataclass_params__.repr is False
    assert _MasterTokenRead.__slots__ == ("token", "raw")
    assert [item.name for item in dataclasses.fields(_MasterTokenRead)] == ["token", "raw"]
    assert str(inspect.signature(MasterTokenFile)) == (
        "(path: 'Path', *, locks: 'StorageLockManager | None' = None) -> 'None'"
    )
    assert str(inspect.signature(MasterTokenFile._read_with_raw)) == (
        "(self) -> '_MasterTokenRead | None'"
    )
    assert str(inspect.signature(MasterTokenFile._read_present_with_raw)) == (
        "(self) -> '_MasterTokenRead'"
    )
    assert str(inspect.signature(MasterTokenFile.read)) == "(self) -> 'MasterToken | None'"
    assert str(inspect.signature(MasterTokenFile.write)) == (
        "(self, token: 'MasterToken') -> 'None'"
    )

    locks = RecordingLocks()
    token_file = MasterTokenFile(tmp_path / "token.json", locks=locks)
    assert vars(token_file) == {"_path": tmp_path / "token.json", "_locks": locks}
    pair = _MasterTokenRead(_token(), _record())
    assert "secret" not in repr(pair)
    assert "person@example.com" not in repr(pair)


def test_default_lock_manager_is_selected_at_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = RecordingLocks()
    calls = 0

    def process_default() -> RecordingLocks:
        nonlocal calls
        calls += 1
        return marker

    monkeypatch.setattr(
        master_token_file.StorageLockManager,
        "process_default",
        process_default,
    )

    first = MasterTokenFile(tmp_path / "one.json")
    second = MasterTokenFile(tmp_path / "two.json")

    assert calls == 2
    assert first._locks is marker  # noqa: SLF001
    assert second._locks is marker  # noqa: SLF001


def test_private_read_probes_reads_parses_and_decodes_once_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "master_token.json"
    raw = _record(unknown={"nested": True})
    path.write_text(json.dumps(raw), encoding="utf-8")
    events: list[str] = []
    real_exists = Path.exists
    real_read_text = Path.read_text
    real_loads = json.loads

    def exists(candidate: Path) -> bool:
        if candidate == path:
            events.append("exists")
        return real_exists(candidate)

    def read_text(candidate: Path, *args: object, **kwargs: object) -> str:
        if candidate == path:
            events.append(f"read:{kwargs.get('encoding')}")
        return real_read_text(candidate, *args, **kwargs)

    def loads(content: str) -> object:
        events.append("parse")
        return real_loads(content)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(master_token_file.json, "loads", loads)

    result = MasterTokenFile(path)._read_with_raw()  # noqa: SLF001

    assert result is not None
    assert result.token == _token()
    assert result.raw == raw
    assert events == ["exists", "read:utf-8", "parse"]


def test_present_only_private_read_never_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "master_token.json"
    path.write_text(json.dumps(_record()), encoding="utf-8")
    token_file = MasterTokenFile(path)

    def forbidden_probe(_candidate: Path) -> bool:
        pytest.fail("the present-only helper must not probe existence")

    monkeypatch.setattr(Path, "exists", forbidden_probe)

    result = token_file._read_present_with_raw()  # noqa: SLF001

    assert result.token == _token()
    assert result.raw == _record()


def test_absent_private_and_typed_reads_do_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "absent.json"

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("absent read continued past the existence probe")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(master_token_file.json, "loads", forbidden)
    token_file = MasterTokenFile(path)

    assert token_file._read_with_raw() is None  # noqa: SLF001
    assert token_file.read() is None


def test_public_absent_probes_once_without_constructing_file_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "absent.json"
    probes = 0

    def exists(candidate: Path) -> bool:
        nonlocal probes
        assert candidate == path
        probes += 1
        return False

    def forbidden_constructor(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("an absent legacy read must not construct MasterTokenFile")

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(master_token, "MasterTokenFile", forbidden_constructor)

    assert master_token.read_master_token(path) is None
    assert probes == 1


def test_public_probe_error_escapes_same_object_without_constructing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token.json"
    error = OSError("probe failed")

    def fail_probe(candidate: Path) -> bool:
        assert candidate == path
        raise error

    monkeypatch.setattr(Path, "exists", fail_probe)

    with pytest.raises(OSError) as captured:
        master_token.read_master_token(path)

    assert captured.value is error
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_public_present_malformed_record_projects_master_token_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"version": 999}), encoding="utf-8")

    with pytest.raises(master_token.MasterTokenError) as captured:
        master_token.read_master_token(path)

    assert str(captured.value) == _MALFORMED


def test_public_present_read_probes_constructs_reads_parses_and_decodes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token.json"
    raw = _record(extra={"kept": True})
    path.write_text(json.dumps(raw), encoding="utf-8")
    events: list[str] = []
    real_exists = Path.exists
    real_read_text = Path.read_text
    real_loads = json.loads

    def exists(candidate: Path) -> bool:
        if candidate == path:
            events.append("probe")
        return real_exists(candidate)

    def read_text(candidate: Path, *args: object, **kwargs: object) -> str:
        assert candidate == path
        events.append(f"read:{kwargs.get('encoding')}")
        return real_read_text(candidate, *args, **kwargs)

    def loads(content: str) -> object:
        events.append("parse")
        return real_loads(content)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(master_token_file.json, "loads", loads)

    assert master_token.read_master_token(path) == raw
    assert events == ["probe", "read:utf-8", "parse"]


def test_typed_read_projects_only_token_from_one_private_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = _MasterTokenRead(_token(), _record(secret_extra="keep raw"))
    calls = 0

    def read_once(_self: MasterTokenFile) -> _MasterTokenRead:
        nonlocal calls
        calls += 1
        return pair

    monkeypatch.setattr(MasterTokenFile, "_read_with_raw", read_once)

    assert MasterTokenFile(tmp_path / "token.json").read() is pair.token
    assert calls == 1


def test_actual_typed_read_preserves_permissive_legacy_values(tmp_path: Path) -> None:
    path = tmp_path / "master_token.json"
    path.write_text(
        json.dumps(
            {
                "version": True,
                "email": ["person@example.com"],
                "android_id": {"id": 7},
                "master_token": 123,
                "unknown": {"nested": True},
            }
        ),
        encoding="utf-8",
    )

    token = MasterTokenFile(path).read()

    assert token is not None
    assert token.email == ["person@example.com"]
    assert token.android_id == {"id": 7}
    assert token.secret == 123


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="non-object"),
        pytest.param({"version": 99}, id="wrong-version"),
        pytest.param({"version": 1}, id="missing-fields"),
        pytest.param({**_record(), "master_token": ""}, id="falsy-field"),
    ],
)
def test_private_and_typed_reads_surface_lower_shape_error(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "master_token.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(_MasterTokenRecordError) as private_error:
        MasterTokenFile(path)._read_with_raw()  # noqa: SLF001
    assert str(private_error.value) == _MALFORMED

    with pytest.raises(_MasterTokenRecordError) as typed_error:
        MasterTokenFile(path).read()
    assert str(typed_error.value) == _MALFORMED


@pytest.mark.parametrize("payload", [[], {"version": 99}, {"version": 1}])
def test_public_malformed_projection_has_completely_empty_exception_chain(
    tmp_path: Path, payload: object
) -> None:
    path = tmp_path / "master_token.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(master_token.MasterTokenError) as captured:
        master_token.read_master_token(path)

    assert str(captured.value) == _MALFORMED
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is False


@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(OSError("disk unavailable"), "disk unavailable", id="os-error"),
        pytest.param(
            json.JSONDecodeError("bad syntax", "{", 0),
            "bad syntax",
            id="json-error",
        ),
    ],
)
def test_public_unreadable_projection_preserves_exact_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    message: str,
) -> None:
    def fail(_self: MasterTokenFile) -> _MasterTokenRead:
        raise error

    path = tmp_path / "token.json"
    path.touch()
    monkeypatch.setattr(MasterTokenFile, "_read_present_with_raw", fail)

    with pytest.raises(master_token.MasterTokenError) as captured:
        master_token.read_master_token(path)

    assert str(captured.value) == f"Unreadable master_token.json: {error}"
    assert message in str(captured.value)
    assert captured.value.__cause__ is error
    assert captured.value.__context__ is error
    assert captured.value.__suppress_context__ is True


def test_public_unicode_decode_error_escapes_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    def fail(_self: MasterTokenFile) -> _MasterTokenRead:
        raise error

    path = tmp_path / "token.json"
    path.touch()
    monkeypatch.setattr(MasterTokenFile, "_read_present_with_raw", fail)

    with pytest.raises(UnicodeDecodeError) as captured:
        master_token.read_master_token(path)
    assert captured.value is error


def test_removed_after_successful_probe_is_public_unreadable_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token.json"
    path.write_text("present at probe", encoding="utf-8")

    def removed(_self: Path, *args: object, **kwargs: object) -> str:
        raise FileNotFoundError("removed after probe")

    monkeypatch.setattr(Path, "read_text", removed)

    with pytest.raises(master_token.MasterTokenError) as captured:
        master_token.read_master_token(path)
    assert isinstance(captured.value.__cause__, FileNotFoundError)


def test_public_raw_adapter_returns_fresh_captured_dict_without_reencoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _record(unknown={"nested": [1, 2]}, duplicate="last")
    pair = _MasterTokenRead(_token(), raw)
    calls = 0

    def paired_read(_self: MasterTokenFile) -> _MasterTokenRead:
        nonlocal calls
        calls += 1
        return pair

    def forbidden_encode(_token: MasterToken) -> dict[str, Any]:
        pytest.fail("raw compatibility must not re-encode the typed value")

    path = tmp_path / "token.json"
    path.touch()
    monkeypatch.setattr(master_token.MasterTokenFile, "_read_present_with_raw", paired_read)
    monkeypatch.setattr(master_token_file, "_master_token_to_legacy_record", forbidden_encode)

    first = master_token.read_master_token(path)
    second = master_token.read_master_token(path)

    assert first == raw
    assert second == raw
    assert first is not raw and second is not raw and first is not second
    assert first is not None and first["unknown"] is raw["unknown"]
    assert calls == 2


def test_actual_raw_adapter_retains_unknowns_types_and_duplicate_last_wins(tmp_path: Path) -> None:
    path = tmp_path / "master_token.json"
    path.write_text(
        '{"version": true, "email": ["person@example.com"], '
        '"android_id": {"id": 7}, "master_token": 1, '
        '"unknown": {"nested": true}, "duplicate": "first", "duplicate": "last"}',
        encoding="utf-8",
    )

    assert master_token.read_master_token(path) == {
        "version": True,
        "email": ["person@example.com"],
        "android_id": {"id": 7},
        "master_token": 1,
        "unknown": {"nested": True},
        "duplicate": "last",
    }


def test_write_order_request_payload_and_release_are_exact(tmp_path: Path) -> None:
    path = tmp_path / "credential.json"
    lock_path = tmp_path / ".credential.json.lock"
    events: list[str] = []

    def observe_enter(request: LockRequest) -> None:
        assert request.path == lock_path
        assert path.parent.is_dir()
        assert not path.exists()

    def observe_exit() -> None:
        assert json.loads(path.read_text(encoding="utf-8")) == _record()

    locks = RecordingLocks(events=events, on_enter=observe_enter, on_exit=observe_exit)

    MasterTokenFile(path, locks=locks).write(_token())

    assert events == ["lock-enter", "lock-exit"]
    assert path.read_bytes() == (
        b'{\n  "version": 1,\n  "email": "person@example.com",\n'
        b'  "android_id": "android-1",\n  "master_token": "secret"\n}'
    )
    assert locks.requests == [
        LockRequest(
            path=lock_path,
            blocking=False,
            operation="write_master_token",
            deadline_seconds=90.0,
        )
    ]


@pytest.mark.parametrize("state", [LockState.CONTENDED, LockState.UNAVAILABLE])
def test_write_fails_closed_for_both_nonheld_states(
    tmp_path: Path,
    state: LockState,
) -> None:
    path = tmp_path / "master_token.json"
    locks = RecordingLocks(state)
    with pytest.raises(LockUnavailableError) as captured:
        MasterTokenFile(path, locks=locks).write(_token())

    assert str(captured.value) == (
        f"write_master_token: storage lock unavailable at {tmp_path / '.master_token.json.lock'}"
    )
    assert not path.exists()
    assert locks.events == ["lock-enter", "lock-exit"]


def test_commit_failure_escapes_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    locks = RecordingLocks(events=events)
    error = RuntimeError("commit failed")

    def commit(_path: Path, _payload: dict[str, Any]) -> None:
        events.append("commit")
        raise error

    monkeypatch.setattr(master_token_file, "_commit_master_token_json", commit)

    with pytest.raises(RuntimeError) as captured:
        MasterTokenFile(tmp_path / "master_token.json", locks=locks).write(_token())

    assert captured.value is error
    assert events == ["lock-enter", "commit", "lock-exit"]


def test_non_token_rejection_precedes_parent_lock_and_commit(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "master_token.json"
    locks = RecordingLocks()

    with pytest.raises(TypeError) as captured:
        MasterTokenFile(path, locks=locks).write(object())  # type: ignore[arg-type]

    assert captured.value.args == ("token must be an exact MasterToken instance",)
    assert locks.requests == []
    assert not path.parent.exists()


def test_actual_write_is_canonical_atomic_secure_and_does_not_read_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    parent = tmp_path / "credentials"
    parent.mkdir(mode=0o700)
    if sys.platform != "win32":
        os.chmod(parent, 0o755)
    path = parent / "storage_state.json"
    path.write_bytes(b"opaque corrupt destination")
    real_read_text = Path.read_text

    def read_text(candidate: Path, *args: object, **kwargs: object) -> str:
        if candidate == path:
            pytest.fail("token write read its destination")
        return real_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    caplog.set_level("DEBUG", logger="notebooklm.auth")

    MasterTokenFile(path).write(_token())

    assert path.read_bytes() == (
        b'{\n  "version": 1,\n  "email": "person@example.com",\n'
        b'  "android_id": "android-1",\n  "master_token": "secret"\n}'
    )
    assert (parent / ".storage_state.json.lock").exists()
    assert not path.with_name(path.name + ".bak").exists()
    assert not caplog.records
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
        assert parent.stat().st_mode & 0o777 == 0o700


def test_missing_parent_chain_is_created_securely(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "credentials" / "master_token.json"

    MasterTokenFile(path).write(_token())

    assert path.exists()
    if sys.platform != "win32":
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_arbitrary_relative_path_is_never_canonicalized(
    tmp_path: Path,
) -> None:
    (tmp_path / "relative").mkdir()
    path = tmp_path / "relative" / ".." / "custom-token.json"
    locks = RecordingLocks()

    MasterTokenFile(path, locks=locks).write(_token())

    assert json.loads(path.read_text(encoding="utf-8")) == _record()
    assert locks.requests[0].path == tmp_path / "relative" / ".." / ".custom-token.json.lock"
    assert ".." in locks.requests[0].path.parts


def test_profile_store_derives_lazily_uses_same_locks_and_retains_no_file(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage_state.json"
    token_path = tmp_path / "master_token.json"
    locks = RecordingLocks()
    store = ProfileStore(storage_path, locks=locks)

    assert store.read_master_token() is None
    store.write_master_token(_token())
    assert store.read_master_token() == _token()

    assert json.loads(token_path.read_text(encoding="utf-8")) == _record()
    assert [request.path for request in locks.requests] == [tmp_path / ".master_token.json.lock"]
    assert vars(store) == {"_path": storage_path, "_locks": locks}


def test_storage_writer_adapter_constructs_exact_value_and_delegates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "arbitrary.token"
    constructed: list[FakeTokenFile] = []

    class FakeTokenFile:
        def __init__(self, candidate: Path) -> None:
            self.path = candidate
            self.tokens: list[MasterToken] = []
            constructed.append(self)

        def write(self, token: MasterToken) -> None:
            self.tokens.append(token)

    monkeypatch.setattr(storage, "MasterTokenFile", FakeTokenFile)

    result = storage.write_master_token(
        path,
        email="person@example.com",
        master_token="secret",
        android_id="android-1",
    )

    assert result is None
    assert len(constructed) == 1
    assert constructed[0].path is path
    assert constructed[0].tokens == [_token()]


def test_public_writer_preserves_call_time_storage_module_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "arbitrary.token"
    calls: list[tuple[Path, dict[str, str]]] = []

    def writer(candidate: Path, **kwargs: str) -> None:
        calls.append((candidate, kwargs))

    monkeypatch.setattr(storage, "write_master_token", writer)

    result = master_token.write_master_token(
        path,
        email="person@example.com",
        master_token="secret",
        android_id="android-1",
    )

    assert result is None
    assert calls == [
        (
            path,
            {
                "email": "person@example.com",
                "master_token": "secret",
                "android_id": "android-1",
            },
        )
    ]
