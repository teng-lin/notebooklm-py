"""Shared marker-scanning helpers for the CLI exit-path lint gates.

Both ``tests/_lint/test_error_handler_allowlist.py`` (``# cli-input-validation:``
/ ``# cli-raw-exit:`` markers on ``click.ClickException`` / raw ``SystemExit``
calls) and ``tests/unit/cli/test_quiet_enforcement.py`` (``# quiet-ok:`` waivers
on error-path ``cli_print`` / ``emit_status`` calls) scan inline marker comments
and match them 1:1 to call sites. The scan + match logic lives here so a fix to
either cannot drift between the two gates (PR #1299 review).
"""

from __future__ import annotations

import io
import tokenize

#: ``(start_line, end_line)`` of a call, inclusive (1-based, as ``ast`` reports).
Span = tuple[int, int]


def marker_reasons(source: str, marker: str) -> dict[int, str]:
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
