"""Coverage assertion for the RPC-health canary.

``scripts/check_rpc_health.py`` already enumerates every ``RPCMethod`` and
prints a per-method row, but until now there was no CI guard that *every*
new enum entry is either (a) actively probed by the canary or (b)
explicitly classified as intentionally-not-probed. Without that guard, a
new entry can land silently and stay unmonitored.

This test pins the classification: every ``RPCMethod`` member must be in
exactly one of four categories:

1. **Probed** — ``get_test_params`` returns non-None test params AND the
   method is not short-circuited by ``ALWAYS_SKIP_METHODS``, so the
   canary will exercise the RPC and confirm its ID still echoes back.
2. **MUTATING_SKIP_LIST** — create/update/delete/generate writes. These
   either mutate state (only safe in ``--full`` mode against a throwaway
   notebook) or kick off long-running server-side tasks. They are NEVER
   probed in the read-only quick canary; full mode handles them via
   ``setup_temp_resources`` / ``cleanup_temp_resources``.
3. **PATH_NOT_METHOD_SKIP** — entries that hold a URL path string rather
   than a batchexecute RPC ID. They cannot be probed via the RPC pipeline.
4. **UNAVAILABLE_SKIP_LIST** — RPCs that exist in the enum but are not
   currently exercisable (e.g. server-side not fully rolled out). Listed
   so a future rollout can move them back into the probe set.

If a new ``RPCMethod`` is added without classification, this test fails
with a message naming the unclassified member so the contributor must
make an explicit decision.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from notebooklm.rpc.types import RPCMethod

# Load scripts/check_rpc_health.py as a module. The ``scripts`` directory
# is not a package, so we go through importlib rather than a normal import.
# Registering the module in ``sys.modules`` before exec is required so
# ``@dataclass`` inside the script can resolve forward references back to
# itself during class construction. We use a namespaced key
# (``scripts.check_rpc_health``) instead of a bare ``check_rpc_health``
# to avoid colliding with any other test that loads the same script via
# its own importlib spec (see tests/unit/test_check_rpc_health.py).
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_rpc_health.py"
_MODULE_KEY = "scripts.check_rpc_health"
_spec = importlib.util.spec_from_file_location(_MODULE_KEY, _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_rpc_health = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_KEY] = check_rpc_health
_spec.loader.exec_module(check_rpc_health)


# A representative notebook ID — only used to drive ``get_test_params``
# down its notebook-required branches. No network calls are made.
_DUMMY_NOTEBOOK_ID = "dummy-notebook-id-for-classification-only"


# ---------------------------------------------------------------------------
# Explicit skip lists — each entry MUST be justified with a comment so that
# reviewers can audit the decision at a glance.
# ---------------------------------------------------------------------------

MUTATING_SKIP_LIST: frozenset[str] = frozenset(
    {
        # Creates a new notebook — only safe inside --full mode against a
        # throwaway notebook (handled by setup_temp_resources).
        "CREATE_NOTEBOOK",
        # Permanently deletes a notebook — only safe in --full cleanup.
        "DELETE_NOTEBOOK",
        # Adds a text/url source to a notebook — write op, --full only.
        "ADD_SOURCE",
        # Registers a file upload as a source — write op, --full only.
        "ADD_SOURCE_FILE",
        # Removes a source from a notebook — write op, --full only.
        "DELETE_SOURCE",
        # Generates a new artifact (audio/video/quiz/report/…) — expensive
        # write op. Tested via --full setup using flashcards (fastest path).
        "CREATE_ARTIFACT",
        # Permanently deletes an artifact — write op, --full only.
        "DELETE_ARTIFACT",
        # Creates a new note — write op, --full only.
        "CREATE_NOTE",
        # Permanently deletes a note — write op, --full only.
        "DELETE_NOTE",
        # Kicks off a fast-research task on the server — long-running write
        # op. Tested via --full setup to verify the RPC ID still echoes.
        "START_FAST_RESEARCH",
        # Kicks off a deep-research task — takes minutes to hours. Never
        # probed in either quick or full mode (see ALWAYS_SKIP_METHODS in
        # scripts/check_rpc_health.py).
        "START_DEEP_RESEARCH",
    }
)


# Reserved for ``RPCMethod`` members that hold a URL-path string rather than
# a batchexecute RPC ID. None currently exist — the streamed-chat path was
# relocated to a module-level constant in ``rpc/types.py`` — but
# this category remains so a future path-shaped entry can be classified
# without re-introducing the whole skip-list scaffolding.
PATH_NOT_METHOD_SKIP: frozenset[str] = frozenset()


UNAVAILABLE_SKIP_LIST: frozenset[str] = frozenset()
"""Reserved for ``RPCMethod`` members that exist but aren't currently
exercisable by the canary (e.g. unreleased / rolled-back Google features).
Empty for now; the scaffolding is preserved so a future "exists but cannot
probe" entry can be classified without re-introducing the constant."""


def _probed_method_names() -> frozenset[str]:
    """Return the set of method names the canary actively exercises.

    A method counts as "probed" only when BOTH conditions hold:
    1. ``get_test_params`` returns a non-None parameter list for it.
    2. It is NOT short-circuited by ``ALWAYS_SKIP_METHODS`` in the script
       (which runs before ``get_test_params`` in ``check_method``).

    Without the second check, a method that has params *but* is also in
    ALWAYS_SKIP would be silently mis-classified as probed even though
    the canary never actually calls it.
    """
    always_skip_names = {m.name for m in check_rpc_health.ALWAYS_SKIP_METHODS}
    probed: set[str] = set()
    for notebook_id in (None, _DUMMY_NOTEBOOK_ID):
        for method in RPCMethod:
            if method.name in always_skip_names:
                continue
            params = check_rpc_health.get_test_params(method, notebook_id)
            if params is not None:
                probed.add(method.name)
    return frozenset(probed)


def _all_skip_lists() -> frozenset[str]:
    """Union of every explicit skip frozenset."""
    return MUTATING_SKIP_LIST | PATH_NOT_METHOD_SKIP | UNAVAILABLE_SKIP_LIST


def test_every_rpc_method_is_probed_or_explicitly_skipped() -> None:
    """Every ``RPCMethod`` member must be probed or in a skip list.

    Fails with a clear message naming any enum entry that is neither
    actively probed by ``scripts/check_rpc_health.py`` nor declared in one
    of the explicit skip frozensets above. A new entry must be classified
    by editing the appropriate constant (and adding a justifying comment).
    """
    probed = _probed_method_names()
    classified = probed | _all_skip_lists()
    all_names = {m.name for m in RPCMethod}
    unclassified = sorted(all_names - classified)
    assert not unclassified, (
        "Unclassified RPCMethod entries detected: "
        f"{unclassified}. Add each one to scripts/check_rpc_health.py's "
        "get_test_params (read-only probe), MUTATING_SKIP_LIST (a write/"
        "expensive op), PATH_NOT_METHOD_SKIP (a URL path), or "
        "UNAVAILABLE_SKIP_LIST (not currently exercisable)."
    )


def test_skip_lists_are_disjoint_from_probed() -> None:
    """A method must not appear in both a skip list and the probe set."""
    probed = _probed_method_names()
    double_classified_mutating = sorted(probed & MUTATING_SKIP_LIST)
    double_classified_path = sorted(probed & PATH_NOT_METHOD_SKIP)
    double_classified_unavailable = sorted(probed & UNAVAILABLE_SKIP_LIST)
    assert not double_classified_mutating, (
        "Methods are both probed AND in MUTATING_SKIP_LIST: "
        f"{double_classified_mutating}. Remove from one list."
    )
    assert not double_classified_path, (
        "Methods are both probed AND in PATH_NOT_METHOD_SKIP: "
        f"{double_classified_path}. Remove from one list."
    )
    assert not double_classified_unavailable, (
        "Methods are both probed AND in UNAVAILABLE_SKIP_LIST: "
        f"{double_classified_unavailable}. Remove from one list."
    )


def test_skip_lists_are_disjoint_from_each_other() -> None:
    """Each skip list entry must belong to exactly one category."""
    pairs = (
        ("MUTATING_SKIP_LIST", "PATH_NOT_METHOD_SKIP", MUTATING_SKIP_LIST & PATH_NOT_METHOD_SKIP),
        (
            "MUTATING_SKIP_LIST",
            "UNAVAILABLE_SKIP_LIST",
            MUTATING_SKIP_LIST & UNAVAILABLE_SKIP_LIST,
        ),
        (
            "PATH_NOT_METHOD_SKIP",
            "UNAVAILABLE_SKIP_LIST",
            PATH_NOT_METHOD_SKIP & UNAVAILABLE_SKIP_LIST,
        ),
    )
    for left, right, overlap in pairs:
        assert not overlap, (
            f"Entries appear in both {left} and {right}: {sorted(overlap)}. Pick one category."
        )


def test_skip_list_entries_reference_real_enum_members() -> None:
    """Catch typos: every skip-list name must match an actual enum member."""
    all_names = {m.name for m in RPCMethod}
    for label, skip_set in (
        ("MUTATING_SKIP_LIST", MUTATING_SKIP_LIST),
        ("PATH_NOT_METHOD_SKIP", PATH_NOT_METHOD_SKIP),
        ("UNAVAILABLE_SKIP_LIST", UNAVAILABLE_SKIP_LIST),
    ):
        stale = sorted(skip_set - all_names)
        assert not stale, (
            f"{label} references non-existent RPCMethod entries: {stale}. Remove or fix the name."
        )


def test_full_mode_only_methods_match_mutating_skip_list() -> None:
    """The script's FULL_MODE_ONLY set should be a subset of MUTATING_SKIP_LIST.

    Every method the script treats as full-mode-only (because it mutates
    state or kicks off long work) must also be declared mutating here, so
    the two views of "this is a write op" stay in lockstep.
    """
    full_mode_names = {m.name for m in check_rpc_health.FULL_MODE_ONLY_METHODS}
    missing = sorted(full_mode_names - MUTATING_SKIP_LIST)
    assert not missing, (
        "FULL_MODE_ONLY_METHODS contains entries not in MUTATING_SKIP_LIST: "
        f"{missing}. Add them to MUTATING_SKIP_LIST with a justifying comment."
    )


def test_always_skip_methods_are_each_classified() -> None:
    """Every ``ALWAYS_SKIP_METHODS`` entry must be in some skip frozenset.

    Closes the symmetric gap: if the script marks a method as always-skip
    (so the canary never exercises it), the test's skip lists must own
    that decision explicitly. Otherwise an entry could be silently
    de-probed in the script without any visible test churn.
    """
    always_skip_names = {m.name for m in check_rpc_health.ALWAYS_SKIP_METHODS}
    classified = _all_skip_lists()
    missing = sorted(always_skip_names - classified)
    assert not missing, (
        "ALWAYS_SKIP_METHODS entries are not in any skip frozenset: "
        f"{missing}. Add each to MUTATING_SKIP_LIST, PATH_NOT_METHOD_SKIP, "
        "or UNAVAILABLE_SKIP_LIST with a justifying comment."
    )
