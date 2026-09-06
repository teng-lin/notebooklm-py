"""Pin stdio upload inputs and give backends a private, stable copy."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .._app.source_add import SourceAddValidationError, validate_upload_path


def _open_posix(path: Path) -> tuple[int, Path]:
    """Walk pinned directory descriptors without following any symlink."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        # O_NONBLOCK avoids hanging if a regular file was replaced by a FIFO.
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        return fd, path
    finally:
        os.close(parent)


def _open_windows(path: Path) -> tuple[int, Path]:
    """Hold non-reparse directory handles against rename while opening a file.

    Win32 lacks Python's dir_fd/O_NOFOLLOW interface. Omitting FILE_SHARE_DELETE
    pins each parent name until the leaf is open; OPEN_REPARSE_POINT lets us
    reject junctions/symlinks without traversing them. The final handle supplies
    the normalized long filename, including for NTFS 8.3 aliases.
    """
    if sys.platform != "win32":  # pragma: no cover - platform dispatch below
        raise OSError("Windows file handles are unavailable")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    information = kernel.GetFileInformationByHandleEx
    information.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    information.restype = wintypes.BOOL
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    handles: list[int] = []

    def open_handle(name: Path, *, directory: bool) -> int:
        # Parent handles need only metadata access; the leaf needs GENERIC_READ.
        # FILE_SHARE_READ | FILE_SHARE_WRITE permits normal directory use, while
        # denying delete sharing prevents replacement of each checked parent.
        handle = create(
            str(name),
            0 if directory else 0x80000000,
            3 if directory else 1,
            None,
            3,
            0x00200000 | (0x02000000 if directory else 0),
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(handle)
        attributes = AttributeTag()
        if not information(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise SourceAddValidationError("symlink_disallowed")
        return handle

    try:
        parent = Path(path.anchor)
        open_handle(parent, directory=True)
        for component in path.parts[1:-1]:
            parent /= component
            open_handle(parent, directory=True)
        handle = open_handle(path, directory=False)
        size = final_path(handle, None, 0, 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size)
        length = final_path(handle, buffer, size, 0)
        if not length or length >= size:
            raise OSError("Cannot determine the opened upload's canonical path")
        name = buffer.value
        if name.startswith("\\\\?\\UNC\\"):
            name = "\\\\" + name[8:]
        elif name.startswith("\\\\?\\"):
            name = name[4:]
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handles.pop()  # The CRT descriptor now owns the leaf handle.
        return fd, Path(name)
    finally:
        for handle in reversed(handles):
            close(handle)


@contextmanager
def spool_host_upload(content: str, *, allowed_roots: Sequence[Path]) -> Iterator[Path]:
    """Copy an authorized, securely opened descriptor before yielding to a backend.

    Validation alone cannot authorize a later pathname open: both upload
    backends resolve and open their input again. Only a private spool path may
    survive an await. This context removes it on success, error, or cancellation.
    """
    validate_upload_path(content, False, allowed_roots=allowed_roots)
    # Open the original spelling, so even a junction that resolved to another
    # allowed location still passes through the no-follow component walk.
    path = Path(os.path.abspath(os.path.expanduser(content)))
    try:
        fd, canonical = _open_windows(path) if sys.platform == "win32" else _open_posix(path)
    except OSError as exc:
        raise SourceAddValidationError("not_regular_file") from exc
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise SourceAddValidationError("not_regular_file")
        if sys.platform == "win32":
            validate_upload_path(str(canonical), False, allowed_roots=allowed_roots)
        with tempfile.TemporaryDirectory(prefix="nblm-mcp-host-") as directory:
            destination = Path(directory).resolve() / canonical.name
            output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(output_fd, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            source.close()
            yield destination
