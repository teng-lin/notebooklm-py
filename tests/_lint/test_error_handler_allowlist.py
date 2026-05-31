"""CLI exit-path marker enforcement.

``click.ClickException`` and raw ``raise SystemExit`` bypass the typed
``{"error": true, "code": ...}`` JSON envelope owned by ``error_handler.py``.
Every such site OUTSIDE ``error_handler.py`` must carry an inline marker
comment naming why, so each one stays a conscious, documented choice that
cannot silently break ``--json`` consumers.

The markers follow the ``# noqa`` / ``# type: ignore`` convention -- the
reason lives at the call site, so (unlike the previous ``(file, line)``
allowlist) they are immune to line shifts in unrelated code and need no central
list to regenerate (issue #1298):

* ``click.ClickException(...)``  ->  ``# cli-input-validation: <reason>``
* ``raise SystemExit(...)``      ->  ``# cli-raw-exit: <reason>``

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
import io
import tokenize
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_ROOT = REPO_ROOT / "src" / "notebooklm" / "cli"

CLICK_EXCEPTION_MARKER = "cli-input-validation:"
RAW_SYSEXIT_MARKER = "cli-raw-exit:"

# Defense-in-depth ceiling: raw ``SystemExit`` outside ``error_handler.py``
# must stay rare even when individually marked.
MAX_RAW_SYSEXIT_SITES = 5

Span = tuple[int, int]


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


def _marker_reasons(source: str, marker: str) -> dict[int, str]:
    """Map ``lineno -> reason`` for each ``# <marker> <reason>`` comment.

    Uses ``tokenize`` so the marker is only recognized inside a real comment
    token, never inside a string literal that happens to contain the text.
    """
    reasons: dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        body = tok.string.lstrip("#").strip()
        if body.startswith(marker):
            reasons[tok.start[0]] = body.removeprefix(marker).strip()
    return reasons


def _click_exception_spans(tree: ast.AST) -> list[Span]:
    spans: list[Span] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) == "click.ClickException":
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


def _match_markers(spans: list[Span], marker_lines: set[int]) -> tuple[list[Span], set[int]]:
    """Greedily assign each marker line to at most one call span.

    Returns ``(unmarked_spans, orphan_lines)``. Each marker satisfies a single
    span (claimed, then removed from the pool), so a lone marker on a line shared
    by two overlapping call spans leaves the second span unmarked -- every
    audited call needs its OWN marker, matching the per-site strength of the
    removed 1:1 allowlist. ``orphan_lines`` are markers no span could claim
    (the source moved, the call was deleted, or a call carries a redundant
    second marker) -- the stale-marker signal that keeps the annotations from
    rotting.
    """
    # Process spans by ascending end line (then start) and claim the lowest
    # available marker in each: the textbook optimal greedy for assigning each
    # interval a distinct point. A naive left-endpoint sort could let an outer
    # span steal the only marker an overlapping inner span needs and red CI on
    # validly-marked code -- the exact false-positive class this gate exists to
    # remove.
    unclaimed = set(marker_lines)
    unmarked: list[Span] = []
    for lo, hi in sorted(spans, key=lambda span: (span[1], span[0])):
        claim = next((line for line in range(lo, hi + 1) if line in unclaimed), None)
        if claim is None:
            unmarked.append((lo, hi))
        else:
            unclaimed.discard(claim)
    return unmarked, unclaimed


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
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()
        reasons = _marker_reasons(source, marker)
        spans = spanner(tree)
        total += len(spans)

        unmarked_spans, orphan_lines = _match_markers(spans, set(reasons))
        unmarked.extend(f"{rel}:{lo}" for lo, _hi in unmarked_spans)
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
    """Every ``click.ClickException`` call must carry an input-validation marker."""
    unmarked, _stale, _empty, _total = _audit(_click_exception_spans, CLICK_EXCEPTION_MARKER)
    assert not unmarked, (
        "Unmarked click.ClickException call sites. Each bypasses the JSON error "
        "envelope (see error_handler.py) and must carry an inline "
        f"`# {CLICK_EXCEPTION_MARKER} <reason>` comment:\n" + _format(unmarked)
    )


def test_no_stale_or_empty_click_exception_markers() -> None:
    """``# cli-input-validation:`` markers must sit on a call and name a reason."""
    _unmarked, stale, empty, _total = _audit(_click_exception_spans, CLICK_EXCEPTION_MARKER)
    assert not stale, (
        f"Stale `# {CLICK_EXCEPTION_MARKER}` markers (not on a click.ClickException "
        "call) -- delete them:\n" + _format(stale)
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
    unmarked, orphan = _match_markers([(1, 1), (1, 1)], {1})
    assert len(unmarked) == 1
    assert orphan == set()

    # One marker inside an outer span but "stolen" by an overlapping inner call
    # leaves the outer call unmarked rather than silently satisfied.
    unmarked, orphan = _match_markers([(1, 3), (2, 2)], {2})
    assert len(unmarked) == 1
    assert orphan == set()

    # Distinct marker per call -> all satisfied, nothing orphaned.
    unmarked, orphan = _match_markers([(1, 2), (4, 5)], {1, 4})
    assert unmarked == []
    assert orphan == set()

    # Optimal assignment: an outer span must NOT steal the inner span's only
    # marker when an alternative exists (would false-positive under a naive
    # left-endpoint sort). Both are satisfiable -> neither flagged.
    unmarked, orphan = _match_markers([(1, 5), (2, 2)], {2, 3})
    assert unmarked == []
    assert orphan == set()

    # A marker no call can claim is reported as stale.
    unmarked, orphan = _match_markers([(1, 1)], {1, 9})
    assert unmarked == []
    assert orphan == {9}


def test_raw_sysexit_spans_detects_bare_and_called() -> None:
    """Both ``raise SystemExit(1)`` and bare ``raise SystemExit`` are detected.

    Bare ``raise SystemExit`` is an ``ast.Name`` (not a ``Call``); missing it
    would let a raw exit bypass the gate (gemini-code-assist, PR #1299).
    """
    tree = ast.parse("def called():\n    raise SystemExit(1)\ndef bare():\n    raise SystemExit\n")
    assert sorted(lo for lo, _hi in _raw_sysexit_spans(tree)) == [2, 4]
