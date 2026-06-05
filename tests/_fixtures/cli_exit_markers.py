"""Shared marker-scanning helpers for the CLI exit-path lint gates.

Both ``tests/_guardrails/test_error_handler_allowlist.py`` (``# cli-input-validation:``
/ ``# cli-raw-exit:`` markers on ``click.ClickException`` / raw ``SystemExit``
calls) and ``tests/unit/cli/test_quiet_enforcement.py`` (``# quiet-ok:`` waivers
on error-path ``cli_print`` / ``emit_status`` calls) scan inline marker comments
and match them 1:1 to call sites. The scan + match logic lives here so a fix to
either cannot drift between the two gates (PR #1299 review).

The exit-path gate audits each CLI file under several marker families and across
two assertions per family, so a naive helper re-reads + re-``ast.parse``s +
re-``tokenize``s every file once per audit pass. The per-path memoized
:func:`parse_cli_file` / :func:`marker_reasons_for` helpers collapse that to a
single read + parse + tokenize per file per test session -- the CLI sources do
not change mid-run, so caching on the path is sound (issue #1302).
"""

from __future__ import annotations

import ast
import functools
import io
import tokenize
from pathlib import Path

#: ``(start_line, end_line)`` of a call, inclusive (1-based, as ``ast`` reports).
Span = tuple[int, int]


def _comment_bodies(source: str) -> tuple[tuple[int, str], ...]:
    """Return ``(lineno, body)`` for every comment token in *source*.

    ``body`` is the comment with its leading ``#``\\ (s) and surrounding
    whitespace stripped. Using ``tokenize`` ensures only real comment tokens
    are seen -- never a marker-looking substring inside a string literal.
    """
    bodies: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            bodies.append((tok.start[0], tok.string.lstrip("#").strip()))
    return tuple(bodies)


def _reasons_from_bodies(bodies: tuple[tuple[int, str], ...], marker: str) -> dict[int, str]:
    """Map ``lineno -> reason`` for each comment ``body`` that starts with *marker*."""
    return {
        lineno: body.removeprefix(marker).strip()
        for lineno, body in bodies
        if body.startswith(marker)
    }


def marker_reasons(source: str, marker: str) -> dict[int, str]:
    """Map ``lineno -> reason`` for each ``# <marker> <reason>`` comment.

    Uses ``tokenize`` so the marker is only recognized inside a real comment
    token, never inside a string literal that happens to contain the text.
    """
    return _reasons_from_bodies(_comment_bodies(source), marker)


def parse_cli_file(path: Path) -> tuple[str, ast.Module]:
    """Read and ``ast.parse`` *path* once, caching ``(source, tree)`` per path.

    Memoized for the test session: the exit-path gate audits each CLI file
    under multiple marker families and assertions, and the source does not
    change mid-run, so each file is read + parsed exactly once (issue #1302).

    Paths are canonicalized before they reach the cache so a relative and an
    absolute reference to the same physical file share one entry.
    """
    return _parse_cli_file_cached(path.expanduser().resolve())


@functools.cache
def _parse_cli_file_cached(path: Path) -> tuple[str, ast.Module]:
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(path))


@functools.cache
def _comment_bodies_for(path: Path) -> tuple[tuple[int, str], ...]:
    """Tokenize *path* (canonicalized) once, caching its comment pairs."""
    source, _tree = _parse_cli_file_cached(path)
    return _comment_bodies(source)


def marker_reasons_for(path: Path, marker: str) -> dict[int, str]:
    """Path-keyed :func:`marker_reasons`: tokenize each file once per session.

    Byte-identical to ``marker_reasons(parse_cli_file(path)[0], marker)`` but
    reuses the cached single tokenize pass across every marker family.
    """
    return _reasons_from_bodies(_comment_bodies_for(path.expanduser().resolve()), marker)


def match_markers(spans: list[Span], marker_lines: set[int]) -> tuple[list[int], set[int]]:
    """Greedily assign each marker line to at most one call span.

    Returns ``(unmarked_indices, orphan_lines)``: indices into ``spans`` for
    calls with no dedicated marker, and marker lines no span could claim (the
    source moved, the call was deleted, or a call carries a redundant second
    marker -- the stale-marker signal that keeps the annotations from rotting).

    Indices (not the span tuples) are returned so a caller can map an unmarked
    call back to its OWN label via a parallel list even when two distinct calls
    share an identical span.

    Each marker satisfies a single span (claimed, then removed from the pool),
    so a lone marker on a line shared by two overlapping call spans leaves the
    second span unmarked -- every audited call needs its own marker, matching
    the per-site strength of the removed 1:1 allowlist.

    Spans are processed by ascending end line (then start) and claim the lowest
    available marker: the textbook optimal greedy for assigning each interval a
    distinct point. A naive left-endpoint sort could let an outer span steal the
    only marker an overlapping inner span needs and red CI on validly-marked
    code -- the exact false-positive class these gates exist to remove.
    """
    unclaimed = set(marker_lines)
    unmarked: list[int] = []
    for idx in sorted(range(len(spans)), key=lambda i: (spans[i][1], spans[i][0])):
        lo, hi = spans[idx]
        claim = next((line for line in range(lo, hi + 1) if line in unclaimed), None)
        if claim is None:
            unmarked.append(idx)
        else:
            unclaimed.discard(claim)
    return unmarked, unclaimed
