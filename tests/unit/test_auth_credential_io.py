"""Tests for the sealed typed credential commit spine."""

from __future__ import annotations

import inspect
import logging
import os
import stat
from pathlib import Path

import pytest

from notebooklm import _atomic_io
from notebooklm._auth import credential_io


def test_commit_signatures_and_none_returns_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = "(path: 'Path', payload: 'Mapping[str, object]') -> 'None'"
    assert str(inspect.signature(credential_io._commit_json_unchecked)) == expected
    assert str(inspect.signature(credential_io._commit_profile_json)) == expected
    assert str(inspect.signature(credential_io._commit_master_token_json)) == expected

    calls: list[tuple[Path, object]] = []

    def commit(path: Path, payload: object) -> None:
        calls.append((path, payload))

    monkeypatch.setattr(credential_io, "_atomic_write_json_unchecked", commit)
    path = tmp_path / "raw.json"
    payload: dict[str, object] = {"secret": "value"}
    assert credential_io._commit_json_unchecked(path, payload) is None
    assert calls == [(path, payload)]
    assert calls[0][0] is path
    assert calls[0][1] is payload


@pytest.mark.parametrize(
    "wrapper",
    [credential_io._commit_profile_json, credential_io._commit_master_token_json],
)
def test_typed_wrappers_forward_exact_objects_once(
    wrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, object]] = []

    def commit(path: Path, payload: object) -> None:
        calls.append((path, payload))

    monkeypatch.setattr(credential_io, "_commit_json_unchecked", commit)
    path = tmp_path / "custom.json"
    payload: dict[str, object] = {"first": "秘密", "second": 2}
    assert wrapper(path, payload) is None
    assert calls == [(path, payload)]
    assert calls[0] == (path, payload)
    assert calls[0][0] is path
    assert calls[0][1] is payload


def test_profile_commit_preserves_pretty_utf8_bytes_order_mode_and_no_newline(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "A.json"
    payload = {"first": "秘密", "second": {"nested": True}}
    reads: list[Path] = []
    real_read_text = Path.read_text
    real_replace = _atomic_io.os.replace
    replaces: list[tuple[object, object]] = []

    def read_text(target: Path, *args: object, **kwargs: object) -> str:
        reads.append(target)
        return real_read_text(target, *args, **kwargs)

    def replace(source: object, destination: object) -> None:
        replaces.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(_atomic_io.os, "replace", replace)
    with caplog.at_level(logging.DEBUG):
        credential_io._commit_profile_json(path, payload)
    assert path.read_bytes() == (
        b'{\n  "first": "\xe7\xa7\x98\xe5\xaf\x86",\n  "second": {\n    "nested": true\n  }\n}'
    )
    assert not path.read_bytes().endswith(b"\n")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_name(path.name + ".bak").exists()
    assert reads == []
    assert len(replaces) == 1
    assert replaces[0][1] == path
    assert caplog.records == []


def test_master_token_wrapper_accepts_storage_state_name_public_writer_rejects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    payload = {
        "version": 1,
        "email": "user@example.com",
        "android_id": "android",
        "master_token": "secret-token",
    }
    with pytest.raises(ValueError, match="must not be called"):
        _atomic_io.atomic_write_json(path, payload)
    credential_io._commit_master_token_json(path, payload)
    assert path.read_text(encoding="utf-8") == (
        '{\n  "version": 1,\n  "email": "user@example.com",\n'
        '  "android_id": "android",\n  "master_token": "secret-token"\n}'
    )


def test_spine_preserves_exception_identity_and_does_not_create_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = OSError("secret exception text")

    def fail(path: Path, payload: object) -> None:
        raise marker

    monkeypatch.setattr(credential_io, "_atomic_write_json_unchecked", fail)
    with pytest.raises(OSError) as excinfo:
        credential_io._commit_profile_json(tmp_path / "missing" / "A.json", {"secret": "x"})
    assert excinfo.value is marker


def test_typed_commit_does_not_automatically_create_parent(tmp_path: Path) -> None:
    parent = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        credential_io._commit_profile_json(parent / "A.json", {"secret": "x"})
    assert not parent.exists()


def test_raw_spine_has_no_capability_escape_values() -> None:
    assert credential_io._commit_json_unchecked.__module__ == "notebooklm._auth.credential_io"
    assert credential_io._commit_profile_json.__module__ == "notebooklm._auth.credential_io"
    assert credential_io._commit_master_token_json.__module__ == "notebooklm._auth.credential_io"
