"""Enforce ``--quiet`` error-path policy in CLI command modules.

The CLI exposes a root ``--quiet`` flag whose contract is documented in
``cli/rendering.py``: it suppresses *status* prose only. **Errors are never
silenced.** The two quiet-aware helpers (``cli_print`` and ``emit_status``)
short-circuit under ``--quiet`` -- so calling either of them on an error path
silently swallows the diagnostic the user needed.

The contract therefore is:

- Status / success prose may use ``cli_print(...)`` or ``emit_status(...)``
  (both honor ``--quiet``).
- Error sites must use ``output_error(...)`` / ``_output_error(...)`` (from
  ``cli.error_handler``) or ``json_error_response(...)`` (from
  ``cli.rendering``). Both of those bypass ``--quiet`` and route to stderr (or
  emit a structured JSON envelope) so the failure is always observable.

This module enforces that contract structurally via an AST walk over every
``src/notebooklm/cli/*_cmd.py`` file.

Error-path heuristic
--------------------
A call site is considered to live on an *error path* when ANY of the
following hold (the test fails on a quiet-bypassing helper at that site
unless the site is explicitly waived):

1. **Inside a ``Try`` ``ExceptHandler`` body** -- by definition the program
   is currently handling an exception.
2. **Inside an ``If.body`` whose ``test`` references an exception/error
   identifier** -- e.g. ``if error:`` or ``if exc is not None:``. The
   identifier check walks every ``Name`` / ``Attribute`` in the test and
   matches the substrings ``error`` / ``fail`` / ``exception`` (case-
   insensitive) plus the bare exception conventions ``e`` / ``exc`` / ``err``.
3. **Inside a function whose name** contains ``error``, ``fail``, or starts
   with ``_handle_`` (e.g. ``_handle_auth_error``).

Soft-failure status prose (e.g. ``if not success:``) is intentionally NOT
flagged: the heuristic is conservative so it stays low-noise. Authors who
*want* an error-path-grade diagnostic can switch to ``output_error`` on
their own; this test only blocks the unambiguous regression -- a
quiet-bypassing helper landing inside a path that already names an
exception/error.

Waiver mechanism
----------------
Each pre-existing violation is listed in :data:`QUIET_WAIVED_SITES` with a
rationale. The test fails on:

- a new (un-waived) error-path site using ``cli_print`` / ``emit_status``,
- drift -- a waived ``(module, function, line)`` tuple that no longer maps
  to a quiet-bypassing call.

This guarantees that the waiver list shrinks toward zero over time and
cannot silently rot.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_ROOT = REPO_ROOT / "src" / "notebooklm" / "cli"

# Helpers that honor ``--quiet``. Calling either on an error path silently
# eats the diagnostic, which is the bug this test prevents.
QUIET_AWARE_HELPERS = frozenset({"cli_print", "emit_status"})

# Pre-existing error-path violations grandfathered in at the time this test
# was introduced. Keys are ``(module, function, line)`` -- the file path is
# repo-relative POSIX; the function is the innermost enclosing
# ``def``/``async def`` name; the line is the call site's ``lineno``.
#
# Drift detection: each entry MUST still resolve to a quiet-bypassing call
# at the recorded line, in the recorded function, in the recorded module.
# If the source moves or the call is fixed, delete the waiver.
QUIET_WAIVED_SITES: dict[tuple[str, str, int], str] = {
    (
        "src/notebooklm/cli/chat_cmd.py",
        "_run",
        344,
    ): (
        "TODO(quiet-policy): note-save failure inside `ask` reports the "
        "underlying exception via `emit_status` so the warning is colored "
        "and routed alongside other chat status text. Switching to "
        "`output_error` here would also `SystemExit(1)`, aborting the "
        "outer chat response payload; the user explicitly opted into "
        "`--save-as-note` as a *secondary* action, so we surface the "
        "note-save failure without killing the chat output. Revisit when "
        "the chat-cmd save-as-note path gains a structured non-fatal "
        "error channel."
    ),
}


# ---------------------------------------------------------------------------
# AST walk
# ---------------------------------------------------------------------------


def _enclosing_function_name(ancestors: list[ast.AST]) -> str:
    """Return the innermost enclosing function name (or ``<module>``)."""
    for anc in reversed(ancestors):
        if isinstance(anc, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return anc.name
    return "<module>"


def _name_references_error(test_node: ast.AST) -> bool:
    """True if any identifier in *test_node* names an exception/error.

    Walks every ``Name`` and ``Attribute`` in the subtree. Matches if the
    identifier (case-folded) contains ``error`` / ``fail`` / ``exception``
    or is one of the conventional bare-exception names ``e``, ``exc``,
    ``err``.
    """
    conventional = {"e", "exc", "err"}
    for node in ast.walk(test_node):
        if isinstance(node, ast.Name):
            name = node.id.lower()
            if name in conventional:
                return True
            if "error" in name or "fail" in name or "exception" in name:
                return True
        elif isinstance(node, ast.Attribute):
            attr = node.attr.lower()
            if "error" in attr or "fail" in attr or "exception" in attr:
                return True
    return False


def _is_error_path(ancestors: list[ast.AST]) -> bool:
    """Apply the documented heuristic to the ancestor chain.

    Returns True if the call site sits inside any of:

    1. an ``ExceptHandler`` body,
    2. an ``If.body`` whose ``test`` references an error/exception
       identifier (see :func:`_name_references_error`),
    3. a function whose name signals error handling.
    """
    # Function-name signal.
    for anc in ancestors:
        if isinstance(anc, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fname = anc.name.lower()
            if "error" in fname or "fail" in fname or fname.startswith("_handle_"):
                return True

    # ExceptHandler signal.
    for anc in ancestors:
        if isinstance(anc, ast.ExceptHandler):
            return True

    # If.body-with-error-test signal. We need to distinguish ``If.body``
    # from ``If.orelse`` because only the body fires when ``test`` is truthy.
    # Walk pairs (parent, child) in the ancestor chain. Both slices are one
    # shorter than ``ancestors`` and must stay equal-length.
    for parent, child in zip(ancestors[:-1], ancestors[1:], strict=True):
        if isinstance(parent, ast.If) and child in parent.body:
            if _name_references_error(parent.test):
                return True

    return False


def _walk_with_ancestors(tree: ast.AST):
    """Yield ``(node, ancestors)`` for every node in *tree*."""
    stack: list[tuple[ast.AST, list[ast.AST]]] = [(tree, [])]
    while stack:
        node, ancestors = stack.pop()
        yield node, ancestors
        next_ancestors = ancestors + [node]
        for child in ast.iter_child_nodes(node):
            stack.append((child, next_ancestors))


def _collect_quiet_bypass_error_sites() -> set[tuple[str, str, int]]:
    """Walk every ``cli/*_cmd.py`` and return error-path quiet-aware calls."""
    sites: set[tuple[str, str, int]] = set()
    for path in sorted(CLI_ROOT.glob("*_cmd.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        for node, ancestors in _walk_with_ancestors(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id not in QUIET_AWARE_HELPERS:
                continue
            if not _is_error_path(ancestors):
                continue
            sites.add((rel_path, _enclosing_function_name(ancestors), node.lineno))
    return sites


def _format_sites(sites: set[tuple[str, str, int]]) -> str:
    return "\n".join(f"  {module}::{func}:{line}" for module, func, line in sorted(sites))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_new_quiet_bypassing_error_sites() -> None:
    """Fail when a new error-path site uses ``cli_print`` / ``emit_status``.

    Errors must use ``output_error(...)`` (or ``json_error_response(...)``)
    which bypass ``--quiet`` and route to stderr. Quiet-aware helpers
    silently swallow the diagnostic and are forbidden on error paths.
    """
    actual = _collect_quiet_bypass_error_sites()
    waived = set(QUIET_WAIVED_SITES.keys())
    unwaived = actual - waived
    assert not unwaived, (
        "New error-path uses of cli_print/emit_status detected.\n"
        "Errors must use output_error(...) (cli.error_handler) or "
        "json_error_response(...) (cli.rendering); both bypass --quiet.\n"
        "Sites:\n" + _format_sites(unwaived)
    )


def test_waiver_list_has_no_drift() -> None:
    """Every waived site must still exist in source at the recorded line.

    Drift means a waived ``(module, function, line)`` tuple no longer maps
    to a quiet-bypassing helper in source -- either the source moved or
    someone already fixed the violation. Either way, the waiver must be
    deleted so the list stays minimal and trustworthy.
    """
    actual = _collect_quiet_bypass_error_sites()
    waived = set(QUIET_WAIVED_SITES.keys())
    stale = waived - actual
    assert not stale, (
        "Stale QUIET_WAIVED_SITES entries (no longer match source):\n"
        + _format_sites(stale)
        + "\nDelete these waivers from QUIET_WAIVED_SITES."
    )
