"""Atomic JSON write helpers.

Shared by auth storage/account/capture writers and public CLI helpers (via
:mod:`notebooklm.io`) so JSON state writes use the same crash- and
concurrency-safe pattern (NamedTemporaryFile in the same directory,
``chmod 0o600``, ``flush`` + ``fsync`` of the temp file, ``os.replace``, then a
best-effort ``fsync`` of the parent directory).

The write is both **rename-atomic** (a reader sees either the old or the new
file, never a partial one) and, on POSIX, **fsync-durable** (the bytes are
forced to stable storage before the rename), so a power loss / kernel panic
after :func:`atomic_write_json` returns cannot lose the file data. The
parent-directory fsync hardens the directory entry too but is best-effort: it
silently degrades on Windows and on filesystems that reject a directory fsync,
since by then the (fsynced) data has already been committed by the rename.

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

Lock-path derivation contract
-----------------------------

:func:`atomic_update_json` derives its lock as ``<name>.lock`` via
``path.with_suffix(path.suffix + ".lock")`` (e.g. ``config.json`` ->
``config.json.lock``). This pattern is **deliberately distinct** from the
*dotted* ``.<name>.lock`` sentinel that the canonical ``storage_state.json``
mutators serialize on (``_auth.paths._storage_state_lock_path`` ->
``.storage_state.json.lock``; see #1215). Because the two patterns yield two
*different* files, routing a ``storage_state.json`` path through
``atomic_update_json`` would acquire the *wrong* lock and silently re-introduce
the lost-update race that #1215 closed.

To enforce that contract by construction rather than by hand-synced string
literals, :func:`atomic_update_json` rejects ``storage_state.json`` paths up
front (see :data:`_STORAGE_STATE_FILENAME`). The ``config.json`` /
``context.json`` callers keep their existing ``<name>.lock`` files unchanged;
only ``storage_state.json`` is special-cased. Cookie/account writers must use
the dedicated locked writers in :mod:`notebooklm._auth`
(``save_cookies_to_storage`` / ``write_account_metadata`` /
``_clear_in_band_account``), which all share ``_storage_state_lock_path``.
"""

from __future__ import annotations

import errno
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

# Filename whose mutators MUST serialize on the canonical dotted
# ``.storage_state.json.lock`` sentinel (``_auth.paths._storage_state_lock_path``,
# #1215). ``atomic_update_json`` derives a *non-dotted* ``<name>.lock`` instead,
# so it rejects this name rather than acquire a divergent lock file and
# re-introduce the lost-update race. Kept as a plain literal here to avoid an
# import edge from this leaf module into ``notebooklm._auth``. Stored
# already-casefolded so the guard can compare ``path.name.casefold()`` against
# it directly (case-insensitive filesystems resolve casing variants to the same
# file).
_STORAGE_STATE_FILENAME = "storage_state.json"

# Errnos that mean "this fd/filesystem does not support fsync" rather than
# "the writeback failed". Only these are swallowed so durability degrades
# gracefully on filesystems that reject fsync (e.g. some network/virtual
# mounts, or a directory fd on filesystems that disallow dir fsync). A *real*
# writeback failure (EIO, ENOSPC, …) is NOT in this set and must propagate so
# we never replace a good file with non-durable data.
_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    e
    for e in (
        getattr(errno, "EINVAL", None),  # fsync not valid for this fd (e.g. dir)
        getattr(errno, "ENOTSUP", None),  # operation not supported
        getattr(errno, "EOPNOTSUPP", None),  # operation not supported (alias)
        getattr(errno, "ENOSYS", None),  # fsync not implemented
        getattr(errno, "EROFS", None),  # read-only filesystem
    )
    if e is not None
)


def _is_unsupported_fsync_error(exc: OSError) -> bool:
    """True if ``exc`` means fsync is *unsupported* here (vs. a writeback error)."""
    return exc.errno in _FSYNC_UNSUPPORTED_ERRNOS


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


def _fsync_dir(directory: Path) -> None:
    """``fsync`` a directory fd so a freshly-committed rename is durable.

    On POSIX, ``os.replace`` only updates the directory entry in the page
    cache; the new entry can be lost on power loss / kernel panic until the
    *directory* itself is flushed.

    This step runs *after* the rename has already committed the file data
    (which was itself fsynced before the replace), so it is **best-effort and
    never raises**: a power-loss before this returns at worst loses only the
    directory-entry update, not the file data. We therefore:

    * skip Windows entirely (it cannot open a directory fd), and
    * swallow filesystems that reject ``fsync`` on a directory fd.

    A *real* directory writeback failure is logged at ``warning`` (it points at
    a genuinely sick filesystem) but still not raised, because raising here
    would falsely report a committed write as failed.
    """
    if sys.platform == "win32" or not hasattr(os, "fsync"):
        # Windows cannot open a directory for reading (``os.open`` raises
        # PermissionError), so directory fsync is unavailable. Return early to
        # avoid a guaranteed-to-fail syscall + exception on every write.
        return
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        # On POSIX we just wrote a temp file into this dir and replaced into it,
        # so failing to re-open it (EACCES, EMFILE, EIO, …) is anomalous. The
        # rename already committed the fsynced data, so do not raise, but
        # surface it.
        logger.warning("Could not open parent dir %s for fsync: %s", directory, exc)
        return
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if _is_unsupported_fsync_error(exc):
            # Filesystem rejects fsync on a directory fd (common on some
            # network/virtual mounts). Expected; degrade quietly.
            logger.debug("Parent dir %s does not support fsync: %s", directory, exc)
        else:
            # Real writeback error on an already-committed rename. The file data
            # is durable; only the dir entry may be at risk. Surface loudly but
            # do not raise — the write itself succeeded.
            logger.warning("Failed to fsync parent dir %s: %s", directory, exc)
    finally:
        try:
            os.close(dir_fd)
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("Failed to close parent dir fd for %s: %s", directory, exc)


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
    """Write ``data`` as JSON to ``path`` atomically and durably.

    Steps:

    1. Serialize ``data`` to a sibling :class:`tempfile.NamedTemporaryFile` in
       the same directory as ``path`` (same-filesystem for ``os.replace``
       atomicity).
    2. ``fchmod`` the temp file to ``mode`` (default ``0o600`` — cookies are
       secrets). Done before the fsync so the permission bits are flushed too.
       Skipped on Windows where POSIX permissions are a no-op and can confuse
       ACLs.
    3. ``flush`` + ``os.fsync`` the temp file so its bytes reach stable storage
       *before* the rename. Without this, the rename can commit while the data
       is still only in the OS page cache, so a crash in the post-replace
       window can leave ``path`` pointing at an inode with no data (truncated /
       zero-length file). A *real* fsync writeback failure (``EIO``, ``ENOSPC``,
       …) aborts the write (temp unlinked, target preserved); only a genuine
       "fsync unsupported" error degrades to a flush-only write.
    4. ``os.replace`` the temp file onto ``path`` (atomic on POSIX and Windows),
       with bounded retries for transient Windows replace races.
    5. Best-effort ``fsync`` of the *parent directory* so the new directory
       entry is itself durable, not just the file data (POSIX only — silently
       skipped on Windows and on filesystems that reject a directory fsync).
    6. On any failure before the replace commits: unlink the temp file and
       re-raise.

    Durability note: steps 3 and 5 make the write *fsync-durable* on POSIX, so
    a power loss / kernel panic after this call returns cannot lose a
    previously-committed ``path``. The parent-directory fsync (step 5) is
    best-effort and never raises — see :func:`_fsync_dir` — because the rename
    has already committed the (fsynced) file data by that point.

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
                # chmod is a no-op on Windows (and can confuse ACLs). Done
                # BEFORE fsync so the permission-bit change is itself flushed
                # to stable storage along with the data.
                os.fchmod(temp_file.fileno(), mode)
            # Force the JSON bytes (and the fchmod metadata) to stable storage
            # before the rename so the post-replace crash window cannot expose a
            # truncated/empty file.
            temp_file.flush()
            if hasattr(os, "fsync"):
                try:
                    os.fsync(temp_file.fileno())
                except OSError as exc:
                    if not _is_unsupported_fsync_error(exc):
                        # Real writeback failure (EIO, ENOSPC, …): the data is
                        # NOT durable. Re-raise so the outer handler unlinks the
                        # temp file and preserves the existing target rather than
                        # replacing it with non-durable bytes.
                        raise
                    # Filesystem genuinely does not support fsync; flush already
                    # pushed bytes to the OS, so degrade to rename-atomic.
                    logger.debug("Temp file %s does not support fsync: %s", temp_path, exc)
        replace_file_atomically(temp_path, path)
        # Rename committed: make the directory entry durable too. Best-effort,
        # POSIX-only, never raises — see _fsync_dir.
        _fsync_dir(path.parent)
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

    .. warning::
        ``storage_state.json`` paths are **rejected** with :class:`ValueError`.
        This helper's ``<name>.lock`` derivation diverges from the canonical
        dotted ``.storage_state.json.lock`` sentinel that every
        ``storage_state.json`` mutator shares (``_storage_state_lock_path``,
        #1215); acquiring the wrong lock would silently re-introduce a
        lost-update race. Cookie/account writers must use the dedicated locked
        writers in :mod:`notebooklm._auth` instead. See the module docstring's
        *Lock-path derivation contract* section.

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
        ValueError: If ``path`` names ``storage_state.json`` — its lock
            derivation diverges from the canonical ``_storage_state_lock_path``;
            use the dedicated :mod:`notebooklm._auth` writers instead.
        filelock.Timeout: If the lock cannot be acquired within ``timeout``.
        json.JSONDecodeError: If the existing file is not valid JSON and
            ``recover_from_corrupt`` is False.
        OSError: From filesystem operations (mkdir, write, replace).
    """
    # Case-insensitive match: on macOS (APFS/HFS+ default) and Windows (NTFS),
    # ``Storage_State.json`` resolves to the same file as ``storage_state.json``,
    # so a case-sensitive ``==`` would let a casing variant slip past the guard
    # and re-introduce the divergent-lock race. ``casefold`` is the robust
    # Unicode-aware lowercaser for this comparison.
    if path.name.casefold() == _STORAGE_STATE_FILENAME:
        # Echo the caller's actual filename (which may be a casing variant on a
        # case-insensitive filesystem) so the error matches what they passed,
        # while still naming the canonical lock for context.
        raise ValueError(
            f"atomic_update_json must not be called with a {path.name!r} "
            f"({_STORAGE_STATE_FILENAME!r}) path: its '<name>.lock' lock derivation "
            "diverges from the canonical dotted '.storage_state.json.lock' sentinel "
            "(_storage_state_lock_path, #1215), so it would acquire the wrong lock "
            "and risk a lost-update race. Use the dedicated notebooklm._auth writers "
            "(save_cookies_to_storage / write_account_metadata / _clear_in_band_account) "
            "instead."
        )
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
