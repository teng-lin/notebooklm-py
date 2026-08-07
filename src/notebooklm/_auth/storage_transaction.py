"""The storage-write transaction template (ADR-0031 Stage 3).

Six writers in :mod:`notebooklm._auth.storage_writer` each hand-rolled the same
four-step preamble — secure the parent dir, derive the sentinel lock path, take
the bounded lock, and branch on whether it was held. Only the last step differs,
and it differs in three genuinely incompatible ways, so the policy is a
parameter rather than a decision baked into the template: a version that picked
one behavior would be a silent semantic change in a credential-write path.

``merge_cookie_delta`` deliberately does NOT use this. It takes the BLOCKING
``storage._file_lock_exclusive`` rather than the bounded acquire, and skips the
parent-dir prep because it only ever updates a file that already exists. Its
lock semantics are a different operation, not a variant of this one.

Split out of ``storage_writer.py`` to stay under the ADR-0008 module-size
budget. The atomic write itself stays in ``storage_writer`` — this module never
imports the ``_atomic_io`` primitives, preserving the single-writer boundary
``tests/_guardrails/test_storage_writer_boundary.py`` enforces.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..exceptions import LockUnavailableError
from .paths import _storage_state_lock_path
from .storage import _LOCK_ACQUIRE_DEADLINE_SECONDS

logger = logging.getLogger("notebooklm.auth")


class _LockUnavailablePolicy(Protocol):
    """What a writer does when the storage lock could not be acquired."""

    def __call__(self, lock_path: Path) -> Any: ...


def raise_on_lock_unavailable(operation: str) -> _LockUnavailablePolicy:
    """Fail closed: raise :class:`LockUnavailableError`.

    For writers where proceeding without the lock risks losing a concurrent
    writer's commit — the account-metadata write and both master-token
    persists.
    """

    def _policy(lock_path: Path) -> Any:
        raise LockUnavailableError(f"{operation}: storage lock unavailable at {lock_path}")

    return _policy


def skip_on_lock_unavailable(message: str) -> _LockUnavailablePolicy:
    """Best-effort: log at DEBUG and do nothing.

    Only correct where skipping degrades gracefully rather than corrupting —
    today just the in-band account clear, whose miss leaves the legacy reader
    still able to resolve the record.
    """

    def _policy(lock_path: Path) -> Any:
        logger.debug(message, lock_path)
        return None

    return _policy


def report_on_lock_unavailable(outcome: Any) -> _LockUnavailablePolicy:
    """Return a caller-supplied typed outcome describing the miss.

    The two full-replace writers each have their OWN outcome type
    (:class:`WriteOutcome` vs :class:`LoginWriteOutcome`), so the value is
    supplied by the caller rather than constructed here.
    """

    def _policy(lock_path: Path) -> Any:
        return outcome

    return _policy


def in_storage_transaction(
    path: Path,
    body: Callable[[], Any],
    *,
    log_prefix: str,
    on_unavailable: _LockUnavailablePolicy,
    deadline_seconds: float = _LOCK_ACQUIRE_DEADLINE_SECONDS,
) -> Any:
    """Run ``body()`` under the bounded storage lock for ``path``.

    Owns the four steps every writer repeated: secure-parent-dir prep, lock-path
    derivation, the bounded acquire, and the not-held branch. ``body`` returns
    the writer's own return value, so an early ``return`` inside it (the
    ``only_if_absent`` short-circuit, for instance) propagates unchanged.

    The lock is held for the whole of ``body``, including its ``atomic_write_json``
    — the read-decide-write sequence must not be re-entered by a concurrent
    writer partway through.
    """
    from .storage_writer import _acquire_storage_lock, _ensure_secure_parent_dir

    _ensure_secure_parent_dir(path)
    lock_path = _storage_state_lock_path(path)
    with _acquire_storage_lock(
        lock_path, log_prefix=log_prefix, deadline_seconds=deadline_seconds
    ) as state:
        if state != "held":
            return on_unavailable(lock_path)
        return body()
