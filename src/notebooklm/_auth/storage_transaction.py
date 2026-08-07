"""The storage-write transaction template (ADR-0031 Stage 3).

Six writers in :mod:`notebooklm._auth.storage_writer` each hand-rolled the same
four-step preamble — secure the parent dir, derive the sentinel lock path, take
the bounded lock, and branch on whether it was held. **Three of those six route
through this template today**; the remaining three are pinned in the shrink-only
ratchet ``tests/_guardrails/test_storage_transaction_ratchet.py`` and convert in
a later pass. Only the last step differs, and it differs in three genuinely
incompatible ways, so the policy is a parameter rather than a decision baked
into the template: a version that picked one behavior would be a silent
semantic change in a credential-write path.

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


#
# THE POLICIES — two intents, three constructors
# ----------------------------------------------
# There are only TWO intents here, and it is worth stating which is which,
# because the surface count of three invites the wrong mental model:
#
#   MUST-KNOW  the write mattered; a caller that proceeds as though it happened
#              is wrong. Five writers. A master token that was not persisted
#              means the mint was wasted; account metadata that was not written
#              means routing silently targets the wrong Google account; a login
#              that was not persisted means the user believes they are signed in.
#
#   TOLERABLE  the write was cleanup; missing it degrades gracefully. One writer.
#
# MUST-KNOW has *two constructors* only because the writers' return channels
# differ in what they can express — not because the intent differs:
#
#   ``-> None``            no channel at all                      -> raise
#   ``-> bool``            ``False`` already means "deliberately   -> raise
#                          skipped (only_if_absent)", so reusing
#                          it would conflate *chose not to* with
#                          *could not*
#   ``-> WriteOutcome``    a rich enum with room for a distinct    -> report
#   ``-> LoginWriteOutcome`` LOCK_UNAVAILABLE status
#
# Each choice is locally forced. The inconsistency lives one level up, in
# writers that do morally identical things having different return types.
# Unifying that means giving every MUST-KNOW writer a rich outcome type, which
# is a breaking change for callers that today catch ``OSError``/``TimeoutError``
# around ``persist_minted_jar`` and ``update_account_metadata`` — a deprecation
# runway, not a refactor stage. Tracked in ADR-0031.


class _LockUnavailablePolicy(Protocol):
    """What a writer does when the storage lock could not be acquired."""

    def __call__(self, lock_path: Path) -> Any: ...


def raise_on_lock_unavailable(operation: str) -> _LockUnavailablePolicy:
    """MUST-KNOW, via exception — for writers with no usable return channel.

    Used where the return type is ``None`` (``persist_minted_jar``,
    ``write_master_token``) or a ``bool`` whose ``False`` already carries a
    different meaning (``update_account_metadata``).
    """

    def _policy(lock_path: Path) -> Any:
        raise LockUnavailableError(f"{operation}: storage lock unavailable at {lock_path}")

    return _policy


def report_on_lock_unavailable(outcome: Any) -> _LockUnavailablePolicy:
    """MUST-KNOW, via return value — for writers with a rich outcome type.

    Same intent as :func:`raise_on_lock_unavailable`; different mechanism only
    because the caller has somewhere unambiguous to put it. The two full-replace
    writers have their OWN outcome types (:class:`WriteOutcome` vs
    :class:`LoginWriteOutcome`), so the value comes from the caller.

    .. note::
       This has **no caller yet** — ``replace_from_login`` and
       ``replace_from_remint`` are the only writers whose return type can carry
       a distinct lock-unavailable status, and both are still unconverted. That
       is pinned rather than merely noted: the ratchet asserts zero callers
       while they are unconverted, and at least one once they are, so this
       helper cannot quietly outlive its reason to exist.
    """

    def _policy(lock_path: Path) -> Any:
        return outcome

    return _policy


def skip_on_lock_unavailable(message: str) -> _LockUnavailablePolicy:
    """TOLERABLE — log at DEBUG and do nothing.

    Args:
        message: a logging format string with **exactly one** ``%s``, which
            receives the lock path. A message with no placeholder (or more than
            one) raises inside ``logging``, which swallows it and prints to
            stderr instead of logging — an unpleasant failure to trace back,
            since it surfaces nowhere near this call.

    The only genuinely different intent, and it has exactly one user today:
    ``clear_in_band_account``. Its justification is functional — a missed clear
    leaves the legacy reader still able to resolve the account record.

    .. note::
       That justification is narrower than the operation's motive. Clearing the
       in-band account is **privacy**-motivated ("a stale key must not leave the
       account email at rest" — see ``auth.py``), and a swallowed failure leaves
       precisely that email on disk until the next successful write. Functional
       degradation is graceful; the privacy miss is silent. Rare — it needs 90 s
       of lock contention or a lock-infrastructure failure — but the swallow is
       justified on a different axis than the one that matters most here.
       Flagged in ADR-0031 rather than changed unilaterally, since promoting it
       to MUST-KNOW would make a best-effort cleanup able to fail a caller.
    """

    def _policy(lock_path: Path) -> Any:
        logger.debug(message, lock_path)
        return None

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
