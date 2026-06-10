"""CLI exit-path marker enforcement.

``ClickException`` and its envelope-bypassing subclasses (``UsageError``,
``BadParameter``, ``MissingParameter``, ``NoSuchOption``, ``BadArgumentUsage``,
``FileError``) and raw ``raise SystemExit`` bypass the typed
``{"error": true, "code": ...}`` JSON envelope owned by ``error_handler.py``:
Click prints ``Error:`` / ``Usage:`` to stderr and exits the process. Every such
site OUTSIDE ``error_handler.py`` must carry an inline marker comment naming why,
so each one stays a conscious, documented choice that cannot silently break
``--json`` consumers.

``click.Abort`` and ``click.exceptions.Exit`` are deliberately EXCLUDED: they
are control-flow (user-abort / explicit exit code), not error-message exits, so
the gate neither detects nor requires a marker on them (issue #1307).

The markers follow the ``# noqa`` / ``# type: ignore`` convention -- the
reason lives at the call site, so (unlike the previous ``(file, line)``
allowlist) they are immune to line shifts in unrelated code and need no central
list to regenerate (issue #1298):

* ``ClickException(...)`` and subclasses  ->  ``# cli-input-validation: <reason>``
* ``raise SystemExit(...)``               ->  ``# cli-raw-exit: <reason>``

A marker may sit on any physical line spanned by its call, so multi-line calls
can carry it on the opening or the closing line.

The gate keeps its full original strength:

* a NEW unmarked site fails ``test_*_sites_are_marked`` -- you cannot add an
  un-audited exit path;
* markers are matched 1:1 to call sites (see :func:`_match_markers`), so a
  single marker cannot satisfy two calls that share a physical line -- each
  audited call needs its own reason, exactly as the old per-row allowlist did;
* a STALE marker (one no call can claim) fails
  ``test_no_stale_or_empty_*_markers`` -- the annotations cannot rot, which is
  the guarantee the old "no stale allowlist entries" half provided;
* every marker must carry a non-empty reason.

Raw ``SystemExit`` is governed the same way as ``click.ClickException`` -- it is
allowed where a ``# cli-raw-exit:`` marker documents it -- and additionally
stays bounded by ``MAX_RAW_SYSEXIT_SITES``. This is a deliberate, conscious
relaxation of the previous gate, where ``ALLOWED_RAW_SYSEXIT_SITES = []`` made
*any* raw ``SystemExit`` outside ``error_handler.py`` an unconditional failure.
The canonical raw exits still live in ``error_handler.py``; the marker + ceiling
keep new ones rare and individually justified rather than forbidden outright.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

from tests._fixtures.cli_exit_markers import (
    Span,
    marker_reasons,
    marker_reasons_for,
    match_markers,
    parse_cli_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_ROOT = REPO_ROOT / "src" / "notebooklm" / "cli"

CLICK_EXCEPTION_MARKER = "cli-input-validation:"
RAW_SYSEXIT_MARKER = "cli-raw-exit:"

#: ``ClickException`` and the subclasses that bypass the JSON error envelope
#: identically (Click prints ``Error:`` / ``Usage:`` to stderr and exits). Each
#: requires a ``# cli-input-validation:`` marker. ``Abort`` / ``exceptions.Exit``
#: are control-flow, not error-message exits, and are intentionally absent
#: (issue #1307).
ENVELOPE_BYPASSING_CLICK_EXCEPTIONS = frozenset(
    {
        "ClickException",
        "UsageError",
        "BadParameter",
        "MissingParameter",
        "NoSuchOption",
        "BadArgumentUsage",
        "FileError",
    }
)

# Defense-in-depth ceiling: raw ``SystemExit`` outside ``error_handler.py``
# must stay rare even when individually marked.
MAX_RAW_SYSEXIT_SITES = 5


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _cli_files() -> list[Path]:
    """Every ``cli/*.py`` except ``error_handler.py`` (which owns the exits)."""
    return [p for p in sorted(CLI_ROOT.rglob("*.py")) if p.name != "error_handler.py"]


def _click_bare_exception_bindings(tree: ast.AST) -> set[str]:
    """Local names bound to a family member via ``from click import ...``.

    Only these may match the bare ``<Name>`` form -- a locally defined class or
    a same-named import from another module must NOT be linted as a Click exit
    (the gate's "reject unrelated identifiers" goal). Honors ``as`` aliases.
    """
    bindings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "click":
            for alias in node.names:
                if alias.name in ENVELOPE_BYPASSING_CLICK_EXCEPTIONS:
                    bindings.add(alias.asname or alias.name)
    return bindings


def _is_envelope_bypassing_click_exception(func: ast.AST, *, bare_bindings: set[str]) -> bool:
    """True for a ``click``-rooted or click-bound bare call in the family.

    Resolves the dotted call path via :func:`_call_name` and accepts:

    * a bare ``<Name>`` *only* when it was bound by ``from click import <Name>``
      in this module (see :func:`_click_bare_exception_bindings`), and
    * any ``click``-rooted chain whose leaf is in the family --
      ``click.UsageError`` *and* the canonical ``click.exceptions.UsageError``.

    Rejects a differently-rooted chain (``other.UsageError``), a bare name not
    imported from ``click``, and any leaf outside the family, so the
    deliberately-excluded control-flow exits ``click.Abort`` /
    ``click.exceptions.Exit`` never match (issue #1307).
    """
    name = _call_name(func)
    if not name:
        return False
    parts = name.split(".")
    if len(parts) == 1:
        return parts[0] in bare_bindings
    return parts[0] == "click" and parts[-1] in ENVELOPE_BYPASSING_CLICK_EXCEPTIONS


def _click_exception_spans(tree: ast.AST) -> list[Span]:
    spans: list[Span] = []
    bare_bindings = _click_bare_exception_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_envelope_bypassing_click_exception(
            node.func, bare_bindings=bare_bindings
        ):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _raw_sysexit_spans(tree: ast.AST) -> list[Span]:
    spans: list[Span] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if isinstance(exc, ast.Call) and _call_name(exc.func) == "SystemExit":
            # Anchor to the ``SystemExit(...)`` call node, not the enclosing
            # ``Raise``: a marker on a multi-line ``raise ... from <cause>`` tail
            # (outside the call) must not count (symmetric with click below).
            spans.append((exc.lineno, exc.end_lineno or exc.lineno))
        elif isinstance(exc, ast.Name) and exc.id == "SystemExit":
            # Bare ``raise SystemExit`` (no parens) is an ``ast.Name``, not a
            # ``Call``; it would otherwise bypass the gate entirely. The marker
            # sits on the raise statement, so anchor to the ``Raise`` span.
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _audit(
    spanner: Callable[[ast.AST], list[Span]],
    marker: str,
) -> tuple[list[str], list[str], list[str], int]:
    """Return ``(unmarked, stale, empty_reason, total)`` across all CLI files."""
    unmarked: list[str] = []
    stale: list[str] = []
    empty_reason: list[str] = []
    total = 0
    for path in _cli_files():
        _source, tree = parse_cli_file(path)  # cached: read + parse once per session
        rel = path.relative_to(REPO_ROOT).as_posix()
        reasons = marker_reasons_for(path, marker)
        spans = spanner(tree)
        total += len(spans)

        unmarked_idx, orphan_lines = match_markers(spans, set(reasons))
        unmarked.extend(f"{rel}:{spans[i][0]}" for i in unmarked_idx)
        stale.extend(f"{rel}:{line}" for line in sorted(orphan_lines))
        # Empty-reason is checked on CLAIMED markers only; an empty orphan is
        # already reported (more actionably) as stale.
        for line in sorted(set(reasons) - orphan_lines):
            if not reasons[line]:
                empty_reason.append(f"{rel}:{line}")
    return unmarked, stale, empty_reason, total


def _format(sites: list[str]) -> str:
    return "\n".join(f"  {site}" for site in sorted(sites))


def test_click_exception_sites_are_marked() -> None:
    """Every ``ClickException`` (and envelope-bypassing subclass) call is marked."""
    unmarked, _stale, _empty, _total = _audit(_click_exception_spans, CLICK_EXCEPTION_MARKER)
    assert not unmarked, (
        "Unmarked ClickException (or envelope-bypassing subclass) call sites. Each "
        "bypasses the JSON error envelope (see error_handler.py) and must carry an "
        f"inline `# {CLICK_EXCEPTION_MARKER} <reason>` comment:\n" + _format(unmarked)
    )


def test_no_stale_or_empty_click_exception_markers() -> None:
    """``# cli-input-validation:`` markers must sit on a call and name a reason."""
    _unmarked, stale, empty, _total = _audit(_click_exception_spans, CLICK_EXCEPTION_MARKER)
    assert not stale, (
        f"Stale `# {CLICK_EXCEPTION_MARKER}` markers (not on a ClickException or "
        "envelope-bypassing subclass call) -- delete them:\n" + _format(stale)
    )
    assert not empty, (
        f"`# {CLICK_EXCEPTION_MARKER}` markers with no reason -- add one:\n" + _format(empty)
    )


def test_raw_system_exit_sites_are_marked_and_bounded() -> None:
    """Raw ``SystemExit`` outside ``error_handler.py`` stays bounded and marked."""
    unmarked, _stale, _empty, total = _audit(_raw_sysexit_spans, RAW_SYSEXIT_MARKER)
    assert total <= MAX_RAW_SYSEXIT_SITES, (
        f"Too many raw SystemExit sites outside error_handler.py ({total} > "
        f"{MAX_RAW_SYSEXIT_SITES}); route new exits through "
        "exit_with_code()/_output_error()."
    )
    assert not unmarked, (
        "Unmarked raw SystemExit call sites. Each must carry an inline "
        f"`# {RAW_SYSEXIT_MARKER} <reason>` comment:\n" + _format(unmarked)
    )


def test_no_stale_or_empty_raw_system_exit_markers() -> None:
    """``# cli-raw-exit:`` markers must sit on a call and name a reason."""
    _unmarked, stale, empty, _total = _audit(_raw_sysexit_spans, RAW_SYSEXIT_MARKER)
    assert not stale, (
        f"Stale `# {RAW_SYSEXIT_MARKER}` markers (not on a raise SystemExit "
        "call) -- delete them:\n" + _format(stale)
    )
    assert not empty, f"`# {RAW_SYSEXIT_MARKER}` markers with no reason -- add one:\n" + _format(
        empty
    )


def test_match_markers_is_per_site_one_to_one() -> None:
    """A single marker cannot satisfy two calls (the per-site 1:1 guarantee).

    Guards the regression both reviewers flagged: union-coverage would let one
    marker on a shared line green-light two un-audited overlapping calls.
    """
    # Two calls sharing one line + one marker -> exactly one stays unmarked.
    unmarked, orphan = match_markers([(1, 1), (1, 1)], {1})
    assert len(unmarked) == 1
    assert orphan == set()

    # One marker inside an outer span but "stolen" by an overlapping inner call
    # leaves the outer call unmarked rather than silently satisfied.
    unmarked, orphan = match_markers([(1, 3), (2, 2)], {2})
    assert len(unmarked) == 1
    assert orphan == set()

    # Distinct marker per call -> all satisfied, nothing orphaned.
    unmarked, orphan = match_markers([(1, 2), (4, 5)], {1, 4})
    assert unmarked == []
    assert orphan == set()

    # Optimal assignment: an outer span must NOT steal the inner span's only
    # marker when an alternative exists (would false-positive under a naive
    # left-endpoint sort). Both are satisfiable -> neither flagged.
    unmarked, orphan = match_markers([(1, 5), (2, 2)], {2, 3})
    assert unmarked == []
    assert orphan == set()

    # A marker no call can claim is reported as stale.
    unmarked, orphan = match_markers([(1, 1)], {1, 9})
    assert unmarked == []
    assert orphan == {9}

    # Degenerate inputs: nothing to check; a marker with no call is orphaned;
    # a call with no marker is unmarked.
    assert match_markers([], set()) == ([], set())
    assert match_markers([], {5}) == ([], {5})
    assert match_markers([(1, 1)], set()) == ([0], set())


def test_raw_sysexit_spans_detects_bare_and_called() -> None:
    """Both ``raise SystemExit(1)`` and bare ``raise SystemExit`` are detected.

    Bare ``raise SystemExit`` is an ``ast.Name`` (not a ``Call``); missing it
    would let a raw exit bypass the gate (gemini-code-assist, PR #1299).
    """
    tree = ast.parse("def called():\n    raise SystemExit(1)\ndef bare():\n    raise SystemExit\n")
    assert sorted(lo for lo, _hi in _raw_sysexit_spans(tree)) == [2, 4]


def test_click_exception_spans_detects_subclasses_and_bare_import() -> None:
    """Envelope-bypassing subclasses are detected in both attribute + bare form.

    The gate widened from literal ``click.ClickException`` to the whole
    envelope-bypassing family, in the ``click.<Name>`` attribute form, the
    canonical ``click.exceptions.<Name>`` chain, and the bare ``<Name>`` form --
    but only when bound by ``from click import <Name>`` (issue #1307). The
    control-flow exit ``click.exceptions.Exit``, a same-named attribute on a
    different root (``other.UsageError``), and a bare name not imported from
    ``click`` (``BadParameter`` here, never imported) must NOT match.
    """
    tree = ast.parse(
        "import click\n"
        "from click import UsageError\n"
        "raise click.UsageError('x')\n"
        "raise click.BadParameter('x')\n"
        "raise UsageError('x')\n"
        "raise click.exceptions.Exit(0)\n"
        "raise other.UsageError('x')\n"
        "raise click.exceptions.UsageError('x')\n"
        "raise BadParameter('x')\n"
    )
    assert sorted(lo for lo, _hi in _click_exception_spans(tree)) == [3, 4, 5, 8]


def test_parse_cli_file_is_memoized_and_byte_identical() -> None:
    """The single-pass cache must reuse one read/parse/tokenize per file.

    Each audit pass calls ``parse_cli_file`` / ``marker_reasons_for`` once per
    CLI file, and the gate runs four passes. Caching on the path collapses that
    to one read + parse + tokenize per file per session (issue #1302) -- but
    only if it stays behavior-identical to the source-keyed helpers it replaces.
    """
    path = _cli_files()[0]

    # Same path -> the *identical* cached (source, tree); not a re-parse.
    source, tree = parse_cli_file(path)
    again_source, again_tree = parse_cli_file(path)
    assert again_tree is tree
    assert again_source is source
    assert source == path.read_text(encoding="utf-8")

    # Path-keyed reasons are byte-identical to the source-keyed helper for
    # every marker family the gate audits.
    for marker in (CLICK_EXCEPTION_MARKER, RAW_SYSEXIT_MARKER):
        assert marker_reasons_for(path, marker) == marker_reasons(source, marker)
