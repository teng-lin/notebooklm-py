"""Real filesystem races and cleanup at the stdio upload boundary."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from notebooklm._app.source_add import (
    SourceAddValidationError,
    parse_upload_allowed_roots,
    validate_upload_path,
)
from notebooklm.mcp import _hostupload as uploads


@pytest.mark.parametrize("replace_parent", [False, True])
def test_replacement_with_symlink_before_open_is_refused(tmp_path, monkeypatch, replace_parent):
    root = tmp_path / "allowed"
    parent = root / "nested"
    parent.mkdir(parents=True)
    path = parent / "report.pdf"
    path.write_bytes(b"selected document")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "report.pdf"
    secret.write_bytes(b"private data")
    opener = "_open_windows" if sys.platform == "win32" else "_open_posix"
    real_open = getattr(uploads, opener)

    def replace_then_open(validated):
        if replace_parent:
            parent.rename(root / "original")
            parent.symlink_to(outside, target_is_directory=True)
        else:
            path.unlink()
            path.symlink_to(secret)
        return real_open(validated)

    monkeypatch.setattr(uploads, opener, replace_then_open)
    with (
        pytest.raises(SourceAddValidationError),
        uploads.spool_host_upload(str(path), allowed_roots=[root]),
    ):
        pytest.fail("a replaced path must not yield an upload")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows pins the opened name against deletion")
def test_replacement_after_open_cannot_redirect_the_copy(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    path = root / "report.pdf"
    path.write_bytes(b"selected document")
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"private data")
    real_open = uploads._open_posix

    def open_then_replace(validated):
        opened = real_open(validated)
        path.unlink()
        path.symlink_to(secret)
        return opened

    monkeypatch.setattr(uploads, "_open_posix", open_then_replace)
    with uploads.spool_host_upload(str(path), allowed_roots=[root]) as private:
        assert private.read_bytes() == b"selected document"
        assert private != path
    assert not private.exists()
    assert not private.parent.exists()


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_private_copy_is_removed_when_the_consumer_fails(tmp_path, error):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"selected document")
    with (
        pytest.raises(error, match="consumer failed"),
        uploads.spool_host_upload(str(source), allowed_roots=[tmp_path]) as private,
    ):
        assert private.read_bytes() == source.read_bytes()
        if os.name != "nt":
            assert private.stat().st_mode & 0o777 == 0o600
            assert private.parent.stat().st_mode & 0o777 == 0o700
        raise error("consumer failed")
    assert not private.exists()
    assert source.exists()


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("kind", ["missing", "file", "directory", "symlink", "credential"])
def test_denied_targets_have_one_error_without_metadata_probes(
    tmp_path, monkeypatch, configured, kind
):
    root = tmp_path / "allowed"
    root.mkdir()
    target = tmp_path / ("storage_state.json" if kind == "credential" else "outside")
    if kind in ("file", "credential"):
        target.write_text("private data")
    elif kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(root, target_is_directory=True)
    original_stat = Path.stat

    def guarded_stat(path, *args, **kwargs):
        assert path != target, "a denied target must not be probed"
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    with pytest.raises(SourceAddValidationError) as caught:
        validate_upload_path(str(target), False, allowed_roots=[root] if configured else [])
    expected = "path_outside_allowed_root" if configured else "upload_root_not_configured"
    assert caught.value.reason == expected


@pytest.mark.parametrize(
    "name", ["storage_state.json::$DATA", "master_token.json.", "storage_state.json "]
)
def test_credential_stream_and_trailing_aliases_are_refused(tmp_path, name):
    with pytest.raises(SourceAddValidationError) as caught:
        validate_upload_path(str(tmp_path / name), False, allowed_roots=[tmp_path])
    assert caught.value.reason == "credential_path_disallowed"


def test_case_alias_of_home_is_not_an_upload_root(tmp_path, monkeypatch):
    home = tmp_path / "Home"
    home.mkdir()
    alias = home.with_name("hOME")
    if not alias.exists() or not alias.samefile(home):
        pytest.skip("requires a case-insensitive filesystem")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert parse_upload_allowed_roots(str(alias)) == ()


def test_explicit_roots_work_without_an_os_home(tmp_path, monkeypatch):
    def missing_home(cls):
        raise RuntimeError("service UID has no home directory")

    monkeypatch.setattr(Path, "home", classmethod(missing_home))
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path / "credentials"))
    root = tmp_path / "uploads"
    root.mkdir()
    assert parse_upload_allowed_roots(str(root)) == (root.resolve(),)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS short-name aliases")
def test_windows_short_credential_name_is_refused(tmp_path):
    import ctypes
    from ctypes import wintypes

    credential = tmp_path / "storage_state.json"
    credential.write_text("private data")
    short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    short_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    assert short_path(str(credential), buffer, len(buffer))
    alias = tmp_path / Path(buffer.value).name
    if alias.name == credential.name:
        pytest.skip("8.3 short names are disabled on this filesystem")
    with (
        pytest.raises(SourceAddValidationError),
        uploads.spool_host_upload(str(alias), allowed_roots=[tmp_path]),
    ):
        pytest.fail("a credential alias must not yield an upload")
