"""Atomic JSON write helpers.

Shared by :mod:`notebooklm.auth` and :mod:`notebooklm.cli.session_cmd` so both
write sites for ``storage_state.json`` use the same crash- and concurrency-safe
pattern (NamedTemporaryFile in the same directory, ``chmod 0o600``, then
``os.replace``).

Default permission mode is ``0o600`` because the primary caller writes
Playwright storage state containing session cookies, which are credential-
equivalent secrets.

For read-modify-write workflows on shared JSON state (``context.json``,
``config.json``), use :func:`atomic_update_json` — it wraps the read, mutate,
and atomic write inside a cross-process file lock (via the ``filelock``
library) so that two concurrent CLI invocations never lose updates.

The sibling ``<path>.lock`` files that :func:`atomic_update_json` creates are
intentionally left on disk after release: ``filelock`` reuses them across
invocations, and unlinking under contention introduces a TOCTOU race where a
second process could create-and-acquire a fresh lock while the first is mid-
delete. They are zero-byte and cheap.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock

logger = logging.getLogger(__name__)

_WINDOWS_REPLACE_TRANSIENT_WINERRORS = {
    5,  # ERROR_ACCESS_DENIED
    32,  # ERROR_SHARING_VIOLATION
}
_WINDOWS_REPLACE_MAX_ATTEMPTS = 10
_WINDOWS_REPLACE_INITIAL_DELAY_SECONDS = 0.001
_WINDOWS_REPLACE_MAX_DELAY_SECONDS = 0.05


def _is_retryable_windows_replace_error(exc: PermissionError) -> bool:
    if sys.platform != "win32":
        return False
    winerror = getattr(exc, "winerror", None)
    return winerror in _WINDOWS_REPLACE_TRANSIENT_WINERRORS


def replace_file_atomically(temp_path: Path, path: Path) -> None:
    """Replace ``path`` with ``temp_path``, retrying transient Windows races."""
    delay = _WINDOWS_REPLACE_INITIAL_DELAY_SECONDS
    for attempt in range(_WINDOWS_REPLACE_MAX_ATTEMPTS):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError as exc:
            if (
                not _is_retryable_windows_replace_error(exc)
                or attempt == _WINDOWS_REPLACE_MAX_ATTEMPTS - 1
            ):
                raise
            # Windows can transiently deny concurrent replaces of the same
            # destination. The temp file remains the source for a safe retry.
            logger.debug(
                "Transient Windows replace error %s on attempt %d/%d for %s; retrying in %.3fs",
                getattr(exc, "winerror", None),
                attempt + 1,
                _WINDOWS_REPLACE_MAX_ATTEMPTS,
                path,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, _WINDOWS_REPLACE_MAX_DELAY_SECONDS)


def atomic_write_json(path: Path, data: Any, *, mode: int = 0o600) -> None:
    """Write ``data`` as JSON to ``path`` atomically.

    Steps:

    1. Serialize ``data`` to a sibling :class:`tempfile.NamedTemporaryFile` in
       the same directory as ``path`` (same-filesystem for ``os.replace``
       atomicity).
    2. ``chmod`` the temp file to ``mode`` (default ``0o600`` — cookies are
       secrets). Skipped on Windows where POSIX permissions are a no-op and
       can confuse ACLs.
    3. ``os.replace`` the temp file onto ``path`` (atomic on POSIX and Windows),
       with bounded retries for transient Windows replace races.
    4. On any failure: unlink the temp file and re-raise.

    The caller decides whether to log/swallow the exception.
    """

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            # Capture temp path BEFORE write so cleanup-on-failure can still
            # unlink it if write() raises (e.g. ENOSPC, EROFS). Without this,
            # partial temp files would leak into the storage parent dir on
            # every failed save attempt.
            temp_path = Path(temp_file.name)
            json.dump(data, temp_file, indent=2, ensure_ascii=False)
        if sys.platform != "win32":
            # chmod is a no-op on Windows (and can confuse ACLs)
            os.chmod(temp_path, mode)
        replace_file_atomically(temp_path, path)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.debug("Failed to clean up temp file %s: %s", temp_path, cleanup_err)
        raise


def atomic_update_json(
    path: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    mode: int = 0o600,
    timeout: float = 10.0,
    recover_from_corrupt: bool = False,
) -> None:
    """Lock + read + mutate + atomic write of a JSON file.

    Acquires a sibling ``<path>.lock`` file via :class:`filelock.FileLock`
    (cross-platform: POSIX + macOS + Windows), reads the current JSON contents
    (or an empty dict if the file does not exist), passes them to ``mutator``,
    and writes the result back via :func:`atomic_write_json`.

    The lock is held across the entire read-modify-write sequence so that
    two concurrent CLI invocations cannot lose updates by writing stale
    snapshots over each other. The default ``timeout`` is generous enough to
    survive normal contention but bounded so a crashed holder cannot wedge
    the next caller forever — exceeding it raises :class:`filelock.Timeout`.

    Corruption recovery semantics:

    * Valid JSON that decodes to a non-dict value (e.g. ``[1, 2, 3]``) is
      *silently* coerced to ``{}`` before the mutator runs. ``context.json``
      and ``config.json`` are always object-shaped, so this defensive
      normalization matches the legacy behavior of the per-caller helpers.
    * Invalid JSON (``json.JSONDecodeError``) is fatal by default — callers
      must opt in to silent recovery by passing ``recover_from_corrupt=True``.
      When opted in, the mutator runs on an empty dict and the write proceeds
      while the lock is still held. Recovery cannot be done outside the lock
      (e.g. unlink-and-retry) without losing a concurrent process's valid
      write — see PR #465 review threads.

    Args:
        path: Target JSON file. Parent directory is created if missing.
        mutator: Pure function ``current -> updated``. Must return a dict.
            Callers may mutate ``current`` in place and return it.
        mode: POSIX permission bits for the written file (default ``0o600``).
        timeout: Seconds to wait for the lock before raising.
        recover_from_corrupt: When True, an unparseable existing file is
            treated as ``{}`` and overwritten under the same lock. When False
            (default), :class:`json.JSONDecodeError` propagates to the caller.

    Raises:
        filelock.Timeout: If the lock cannot be acquired within ``timeout``.
        json.JSONDecodeError: If the existing file is not valid JSON and
            ``recover_from_corrupt`` is False.
        OSError: From filesystem operations (mkdir, write, replace).
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path), timeout=timeout):
        current: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                if not recover_from_corrupt:
                    raise
                # Treat corrupt file as empty under the same lock — a
                # concurrent valid write committed during this call would
                # land after our atomic_write_json below, so the lock is
                # what guarantees we never clobber a peer's good payload.
                loaded = {}
            current = loaded if isinstance(loaded, dict) else {}
        updated = mutator(current)
        atomic_write_json(path, updated, mode=mode)
