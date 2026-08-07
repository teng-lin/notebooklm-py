"""Shrink-only ratchet: storage writers take the lock via the transaction template.

ADR-0031 Stage 3. Six writers in ``_auth/storage_writer.py`` USED TO each
hand-roll the same four-step preamble — secure the parent dir, derive the
sentinel lock path, take the bounded lock, branch on whether it was held.
:func:`~notebooklm._auth.storage_transaction.in_storage_transaction` now owns
those four steps. **Three of the six route through it today**; the other three
are pinned in :data:`_UNCONVERTED` below and convert in a later pass. The fourth
step (the not-held branch) is supplied by the caller because it genuinely
differs three ways:

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


#: The three not-held policies the template exposes.
_POLICIES: tuple[str, ...] = (
    "raise_on_lock_unavailable",
    "skip_on_lock_unavailable",
    "report_on_lock_unavailable",
)

#: The writers that will use ``report_on_lock_unavailable`` when they convert —
#: the only two whose return type (``WriteOutcome`` / ``LoginWriteOutcome``) has
#: room for a distinct ``LOCK_UNAVAILABLE`` status. Every other writer returns
#: ``None`` or a ``bool`` already spoken for, so it must raise instead.
_REPORT_POLICY_WRITERS: frozenset[str] = frozenset({"replace_from_login", "replace_from_remint"})


def _policy_call_counts() -> dict[str, int]:
    """Count real call sites of each policy across ``_auth/``.

    Definitions do not count — only ``<policy>(...)`` calls. Counting by AST
    rather than substring keeps a mention inside a docstring or an error
    message (this module puts the policy names in both) from reading as a use.
    """
    counts = dict.fromkeys(_POLICIES, 0)
    for source_file in sorted(_AUTH.glob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in counts:
                    counts[node.func.id] += 1
    return counts


def _functions_calling_directly() -> set[str]:
    """Return writer functions that call ``_acquire_storage_lock`` themselves."""
    tree = ast.parse(_WRITER.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        # ``AsyncFunctionDef`` is NOT a subclass of ``FunctionDef`` — matching
        # only the latter would leave an ``async def`` writer free to hand-roll
        # the acquire without ever tripping this gate. Every storage writer is
        # sync today, so this closes a hole that is empty rather than one that
        # leaks; a ratchet that only covers the shapes in use when it was
        # written stops being a ratchet the moment someone adds a new shape.
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
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


def test_the_three_lock_policies_are_all_defined() -> None:
    """Each policy still exists — deleting one silently is a semantic change."""
    tree = ast.parse(_TEMPLATE.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    missing = set(_POLICIES) - defined
    assert not missing, f"lock policy/policies disappeared from {_TEMPLATE.name}: {sorted(missing)}"


def test_in_use_policies_have_real_callers() -> None:
    """``raise_`` and ``skip_`` are load-bearing, not decoration.

    Counts real call sites across ``_auth/`` rather than asserting the
    definition exists in the file that defines it — the latter is a tautology
    that cannot fail while the function is present, and would stay green if
    every caller in the repo were deleted.
    """
    counts = _policy_call_counts()
    for policy in ("raise_on_lock_unavailable", "skip_on_lock_unavailable"):
        assert counts[policy] >= 1, (
            f"{policy} has no callers left in _auth/. Either a writer regressed "
            f"to hand-rolling its not-held branch, or the policy is now dead and "
            f"should be deleted rather than kept as a just-in-case helper."
        )


def test_report_policy_is_pinned_until_its_writers_convert() -> None:
    """``report_on_lock_unavailable`` is self-retiring, in both directions.

    It has ZERO callers today, which is correct but only *while* the two
    full-replace writers that will use it are unconverted — they are the only
    writers with a rich enough outcome type to report into. So the expectation
    is conditional on :data:`_UNCONVERTED`, not a fixed number:

    * while they are pinned, a caller appearing means someone reached for the
      report policy from a writer whose return channel cannot express it;
    * the moment they convert, zero callers means the conversion quietly used a
      different policy — silently changing fail-closed-by-return into
      fail-closed-by-raise for callers — or that the helper is now dead and
      should be deleted.

    This is what the previous version of this gate *claimed* to check and did
    not: it asserted ``"def <policy>(" in <the file defining it>``, which is a
    tautology. It read as proof that all three policies were live, and that
    reading was wrong — one of them never had a caller at all.
    """
    converted = _REPORT_POLICY_WRITERS - _UNCONVERTED
    callers = _policy_call_counts()["report_on_lock_unavailable"]
    # Each CONVERTED designated writer must call the report policy exactly once;
    # unconverted ones contribute zero. Pinning to the converted count (rather
    # than branching all-or-nothing on ``pending``) is what permits a correct
    # one-writer-at-a-time migration: with one of the two converted, the
    # expectation is 1, not the impossible "zero callers while a converted
    # writer must call it" (CodeRabbit finding on #2152).
    assert callers == len(converted), (
        "report_on_lock_unavailable caller count drifted from its converted "
        f"writers.\n  converted report-policy writers: {sorted(converted) or '(none)'}\n"
        f"  expected callers: {len(converted)}  actual: {callers}\n"
        "If a caller appeared from a writer NOT in the designated set, that "
        "writer's return channel cannot express the report — use "
        "raise_on_lock_unavailable. If a designated writer converted without a "
        "caller appearing, it was converted to the wrong policy (changing "
        "fail-closed-by-return into fail-closed-by-raise, a breaking change "
        "for its callers)."
    )
