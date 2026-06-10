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
``src/notebooklm/cli/**/*.py`` file except ``error_handler.py`` (which owns
the quiet-bypassing error helpers themselves). The scan mirrors ``_cli_files()``
in the sibling gate ``tests/_guardrails/test_error_handler_allowlist.py`` so the two
CLI exit-path gates cover an identical file set -- including everything under
``cli/services/`` -- and a quiet-bypassing error-path call cannot hide in a
non-``*_cmd.py`` module (issue #1301).

Error-path heuristic
--------------------
A call site is considered to live on an *error path* when ANY of the
following hold (the test fails on a quiet-bypassing helper at that site
unless the site carries a waiver marker):

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
A genuinely-intended quiet-aware call on an error path is waived with an
inline ``# quiet-ok: <reason>`` marker comment on any physical line spanned
by the call (the ``# noqa`` convention). This replaces the old
``QUIET_WAIVED_SITES`` ``(module, function, line)`` table, which failed CI
whenever an unrelated edit shifted a waived line (issue #1298). The test
fails on:

- a new (un-waived) error-path site using ``cli_print`` / ``emit_status``,
- a stale ``# quiet-ok:`` marker that no longer sits on a quiet-bypassing
  error-path call (the source moved or the call was fixed), and
- a ``# quiet-ok:`` marker with no reason text.

This keeps the waivers minimal and prevents them from rotting, the same
guarantees the old drift check provided.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from tests._fixtures.cli_exit_markers import Span, marker_reasons, match_markers

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_ROOT = REPO_ROOT / "src" / "notebooklm" / "cli"

# Helpers that honor ``--quiet``. Calling either on an error path silently
# eats the diagnostic, which is the bug this test prevents.
QUIET_AWARE_HELPERS = frozenset({"cli_print", "emit_status"})

# Inline marker that waives an intentional quiet-aware error-path call.
QUIET_OK_MARKER = "quiet-ok:"


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


def _walk_with_ancestors(tree: ast.AST) -> Iterator[tuple[ast.AST, list[ast.AST]]]:
    """Yield ``(node, ancestors)`` for every node in *tree*."""
    stack: list[tuple[ast.AST, list[ast.AST]]] = [(tree, [])]
    while stack:
        node, ancestors = stack.pop()
        yield node, ancestors
        next_ancestors = ancestors + [node]
        for child in ast.iter_child_nodes(node):
            stack.append((child, next_ancestors))


# ---------------------------------------------------------------------------
# Marker scanning
# ---------------------------------------------------------------------------


def _cli_files() -> list[Path]:
    """Every ``cli/**/*.py`` except ``error_handler.py`` (which owns the helpers).

    Identical file set to ``_cli_files()`` in the sibling gate
    ``tests/_guardrails/test_error_handler_allowlist.py`` so both CLI exit-path gates
    audit the same modules (issue #1301).
    """
    return [p for p in sorted(CLI_ROOT.rglob("*.py")) if p.name != "error_handler.py"]


def _audit_quiet_markers() -> tuple[list[str], list[str], list[str]]:
    """Return ``(unmarked, stale, empty_reason)`` across every CLI module.

    * ``unmarked`` -- error-path quiet-bypass calls with no ``# quiet-ok:``
      marker (formatted ``module::func:line``).
    * ``stale`` -- ``# quiet-ok:`` markers no such call can claim.
    * ``empty_reason`` -- claimed ``# quiet-ok:`` markers with no reason text.
    """
    unmarked: list[str] = []
    stale: list[str] = []
    empty_reason: list[str] = []
    for path in _cli_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()
        reasons = marker_reasons(source, QUIET_OK_MARKER)

        # ``spans`` and ``labels`` are index-aligned so an unmarked call maps
        # back to its OWN label even if two qualifying calls share a start line
        # (a dict keyed by lineno would collapse them and mislabel — PR #1299).
        spans: list[Span] = []
        labels: list[str] = []
        for node, ancestors in _walk_with_ancestors(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id not in QUIET_AWARE_HELPERS:
                continue
            if not _is_error_path(ancestors):
                continue
            spans.append((node.lineno, node.end_lineno or node.lineno))
            labels.append(f"{rel}::{_enclosing_function_name(ancestors)}:{node.lineno}")

        unmarked_idx, orphan_lines = match_markers(spans, set(reasons))
        unmarked.extend(labels[i] for i in unmarked_idx)
        stale.extend(f"{rel}:{line}" for line in sorted(orphan_lines))
        for line in sorted(set(reasons) - orphan_lines):
            if not reasons[line]:
                empty_reason.append(f"{rel}:{line}")
    return unmarked, stale, empty_reason


def _format(sites: list[str]) -> str:
    return "\n".join(f"  {site}" for site in sorted(sites))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_new_quiet_bypassing_error_sites() -> None:
    """Fail when a new error-path site uses ``cli_print`` / ``emit_status``.

    Errors must use ``output_error(...)`` (or ``json_error_response(...)``)
    which bypass ``--quiet`` and route to stderr. Quiet-aware helpers
    silently swallow the diagnostic and are forbidden on error paths unless
    waived with an inline ``# quiet-ok: <reason>`` marker.
    """
    unmarked, _stale, _empty = _audit_quiet_markers()
    assert not unmarked, (
        "New error-path uses of cli_print/emit_status detected.\n"
        "Errors must use output_error(...) (cli.error_handler) or "
        "json_error_response(...) (cli.rendering); both bypass --quiet.\n"
        "If the quiet-aware call is intentional, waive it with an inline "
        f"`# {QUIET_OK_MARKER} <reason>` comment.\nSites:\n" + _format(unmarked)
    )


def test_no_stale_or_empty_quiet_markers() -> None:
    """``# quiet-ok:`` markers must sit on a quiet-bypass call and name a reason.

    A stale marker means the source moved or the violation was fixed -- the
    waiver must be deleted so the list stays minimal and trustworthy.
    """
    _unmarked, stale, empty = _audit_quiet_markers()
    assert not stale, (
        f"Stale `# {QUIET_OK_MARKER}` markers (no longer on an error-path "
        "quiet-bypass call) -- delete them:\n" + _format(stale)
    )
    assert not empty, f"`# {QUIET_OK_MARKER}` markers with no reason -- add one:\n" + _format(empty)
