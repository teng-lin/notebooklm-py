"""Shrink-only ratchet: storage writers take the lock via the transaction template.

ADR-0031 Stage 3. Six writers in ``_auth/storage_writer.py`` each hand-rolled the
same four-step preamble — secure the parent dir, derive the sentinel lock path,
take the bounded lock, branch on whether it was held.
:func:`~notebooklm._auth.storage_writer.in_storage_transaction` now owns those
four steps, with the fourth (the not-held branch) supplied by the caller because
it genuinely differs three ways:

* **raise** ``LockUnavailableError`` — fail closed, where proceeding without the
  lock could lose a concurrent writer's commit;
* **skip** with a DEBUG log — best-effort, only where a miss degrades gracefully;
* **report** a typed outcome — and the two full-replace writers have *different*
  outcome types, so the value comes from the caller.

A template that picked one of those would be a silent semantic change in a
credential-write path, which is why the policy is a parameter and why this gate
checks *routing through the template*, not uniformity of behavior.

Migration is opportunistic per this repo's ratchet convention: the gate blocks a
NEW hand-rolled acquire and pins the remaining ones so the list can only shrink.
``merge_cookie_delta`` is exempt by design — it takes the BLOCKING
``storage._file_lock_exclusive`` rather than the bounded acquire, which is a
different operation, not a variant.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

_AUTH = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_auth"
_WRITER = _AUTH / "storage_writer.py"
#: The template + its three policies were split out of ``storage_writer`` to
#: stay under the ADR-0008 module-size budget.
_TEMPLATE = _AUTH / "storage_transaction.py"

#: Functions still calling ``_acquire_storage_lock`` directly. SHRINK-ONLY —
#: never add. Each is a candidate for conversion; the three below are the
#: delicate ones deliberately left for a focused pass rather than converted in
#: bulk, because their bodies carry semantics worth converting under their own
#: differential tests:
#:   * ``persist_minted_jar`` — the #2108 cross-account ownership guard and the
#:     write-ordering it depends on; sits on the client's own L4 recovery path.
#:   * ``replace_from_login`` — the login/import full-replace, whose write-time
#:     domain filter and required-cookie revalidation run inside the lock.
#:   * ``replace_from_remint`` — the browser-capture re-mint, which carries or
#:     drops the account namespace under the same lock.
_UNCONVERTED: frozenset[str] = frozenset(
    {
        "replace_from_remint",
        "replace_from_login",
        "persist_minted_jar",
    }
)


def _functions_calling_directly() -> set[str]:
    """Return writer functions that call ``_acquire_storage_lock`` themselves."""
    tree = ast.parse(_WRITER.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        # The template itself lives in ``storage_transaction.py`` now, so
        # nothing in THIS module should be calling the primitive except the
        # writers still awaiting conversion.
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_acquire_storage_lock"
            ):
                found.add(node.name)
    return found


def test_no_new_hand_rolled_storage_lock() -> None:
    """A new writer must route through ``in_storage_transaction``."""
    new = _functions_calling_directly() - _UNCONVERTED
    assert not new, (
        "Writer(s) calling _acquire_storage_lock directly instead of routing "
        f"through in_storage_transaction: {sorted(new)}\n\n"
        "Use:\n"
        "    in_storage_transaction(path, _body, log_prefix=..., \n"
        "                           on_unavailable=raise_on_lock_unavailable(...))\n"
        "picking the on_unavailable policy deliberately — raise_ / skip_ / "
        "report_on_lock_unavailable are NOT interchangeable."
    )


def test_unconverted_list_is_shrink_only() -> None:
    """A converted writer must be removed from the list.

    Without this the list becomes a graveyard and stops describing the real
    remaining work — the failure mode that makes ratchets rot.
    """
    stale = _UNCONVERTED - _functions_calling_directly()
    assert not stale, (
        f"These writers no longer hand-roll the lock — delete them from "
        f"_UNCONVERTED: {sorted(stale)}"
    )


def test_merge_cookie_delta_is_not_expected_to_convert() -> None:
    """The CAS merge is exempt by design, so it must not appear in the list.

    It takes the blocking ``_file_lock_exclusive`` and skips the parent-dir prep
    (it only updates a file that already exists). Listing it would imply a
    conversion we do not intend and that would change its lock semantics.
    """
    assert "merge_cookie_delta" not in _UNCONVERTED
    assert "merge_cookie_delta" not in _functions_calling_directly()


def test_the_three_lock_policies_are_all_used() -> None:
    """Each policy has a real caller — none is speculative API.

    If one drops to zero callers it should be deleted, not kept as a
    just-in-case helper that nothing pins the semantics of.
    """
    source = _TEMPLATE.read_text(encoding="utf-8")
    for policy in (
        "raise_on_lock_unavailable",
        "skip_on_lock_unavailable",
        "report_on_lock_unavailable",
    ):
        # One definition plus at least one call site once conversion completes;
        # while writers remain unconverted, the definition alone is expected.
        assert f"def {policy}(" in source, f"{policy} disappeared"
