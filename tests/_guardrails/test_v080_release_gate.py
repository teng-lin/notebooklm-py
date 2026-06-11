"""Release gate: at the v0.8.0 flip, every deferred break must be flipped — no orphans.

``scripts/check_deprecation_targets.py`` forces only the *warning-message*
removals (a warn whose stated removal target equals the shipping version is
incoherent — issue #1214). This gate adds the **behavioral** half: when
``pyproject.toml`` reaches 0.8.0, the ``NOTEBOOKLM_FUTURE_ERRORS`` preview
machinery and the deprecation shims must be **gone** (every preview-gated branch
flipped to its default), and the :data:`V080_BREAKING_CHANGES` break table must
be **empty**. Otherwise a behavioral break (#1290 / #1342 / #1362 / #1344) —
which carries no warning message and so is invisible to the deprecation-targets
gate — could ship un-flipped and silently un-enforced.

The gate is **bidirectional** around the 0.8.0 bump (its pivot is the
``pyproject.toml`` version, so it is dormant during 0.7.x and *activates*
automatically at the bump):

* **Below 0.8.0** — the runway is live: the preview/deprecation machinery and a
  non-empty break table MUST be present. You cannot remove the runway (or empty
  the table) before the flip ships.
* **At/after 0.8.0** — the flip shipped: the machinery MUST be gone and the table
  MUST be empty. Every break flipped, no orphans.

This turns "remember to flip everything in lockstep" into a CI invariant
(umbrella #1346; ADR-0019). It is intentionally distinct from — and complementary
to — the deprecation-coverage gate (which verifies *prep-time* runways exist) and
``check_deprecation_targets.py`` (which catches self-referential warn versions).
"""

from __future__ import annotations

import re
from pathlib import Path

# Sibling-relative import keeps this guard independent of pytest import-mode
# details and avoids depending on the absolute ``tests._guardrails`` package
# name.
from .test_v080_deprecation_coverage import (
    PROJECT_ROOT,
    SRC_ROOT,
    V080_BREAKING_CHANGES,
)

# The release that flips the breaking half of ADR-0019. The gate's behavior
# pivots here: < this version is "runway live", >= is "flip shipped".
_FLIP_VERSION = (0, 8, 0)

# The v0.8.0 preview/deprecation machinery, as code-level markers (a call site or
# a ``def``/``class`` definition). Each exists while the runway is live and must
# be deleted at the flip (#1365). Mapped to a human reason for legible failures.
# NOTE: ``future_errors_enabled()`` (with parens) matches both the resolver
# ``def`` and every gate call site; the ``def``/``class`` prefixes anchor the
# others to their definitions so a passing mention in a docstring/name doesn't
# count. Comments are stripped before scanning (see :func:`_src_code`).
_MACHINERY: dict[str, str] = {
    "future_errors_enabled()": (
        "the NOTEBOOKLM_FUTURE_ERRORS preview-gate (resolver + every "
        "#1247/#1251/#1254/#1290/#1342/#1362 gate site)"
    ),
    "class MappingCompatMixin": "the dict-style compat bridge (#1251)",
    "def warn_get_returns_none": "the get()-returns-None warn runway (#1247)",
    "def deprecated_kwarg": "the kwarg-alias warn runway (#1254)",
}


def _version_tuple(pyproject: Path) -> tuple[int, int, int]:
    """Parse ``(major, minor, patch)`` from ``pyproject.toml``'s ``version``."""
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"(\d+)\.(\d+)\.(\d+)', text)
    if match is None:  # pragma: no cover - guard; pyproject always has a version
        raise AssertionError('could not parse version = "X.Y.Z" from pyproject.toml')
    return (int(match[1]), int(match[2]), int(match[3]))


def _strip_comments(text: str) -> str:
    """Drop ``#`` line-comments so a marker named only in a comment doesn't count."""
    return re.sub(r"#.*$", "", text, flags=re.MULTILINE)


def _src_code() -> str:
    """Comment-stripped concatenation of every ``src/notebooklm`` module."""
    return "\n".join(
        _strip_comments(path.read_text(encoding="utf-8")) for path in sorted(SRC_ROOT.rglob("*.py"))
    )


# --- Pure detector (no I/O) — the real gate and the self-checks share it -------
def _orphans(
    version: tuple[int, int, int],
    src_code: str,
    breaks: tuple[object, ...],
) -> list[str]:
    """Return no-orphan violations for ``(version, src, table)``; empty == healthy.

    The version pivots the direction: below :data:`_FLIP_VERSION` the machinery
    and a non-empty table must be PRESENT (runway live); at/after it they must be
    ABSENT / empty (flip complete).
    """
    present = sorted(marker for marker in _MACHINERY if marker in src_code)
    violations: list[str] = []
    if version < _FLIP_VERSION:
        missing = sorted(set(_MACHINERY) - set(present))
        if missing:
            violations.append(
                "v0.8.0 runway machinery removed before the flip (pyproject is "
                f"still pre-0.8.0): {missing}. Re-add it, or bump pyproject to 0.8.0 "
                "if the flip is shipping."
            )
        if not breaks:
            violations.append(
                "V080_BREAKING_CHANGES is empty before the 0.8.0 flip — the break "
                "table must stay populated until each break actually ships."
            )
    else:
        if present:
            detail = {marker: _MACHINERY[marker] for marker in present}
            violations.append(
                f"pyproject is at v{'.'.join(map(str, version))} (>= 0.8.0) but the "
                f"preview/deprecation machinery is still in src/: {detail}. Flip every "
                "gate to its v0.8.0 default and delete the shims (#1365)."
            )
        if breaks:
            issues = [getattr(change, "issue", change) for change in breaks]
            violations.append(
                f"pyproject is at v{'.'.join(map(str, version))} (>= 0.8.0) but "
                f"V080_BREAKING_CHANGES still lists {issues} — these breaks were not "
                "flipped (orphans). Remove each from the table as its flip ships."
            )
    return violations


def test_no_orphaned_v080_breaks_at_release() -> None:
    """Every v0.8.0 break is handled in lockstep with the version bump (no orphans)."""
    version = _version_tuple(PROJECT_ROOT / "pyproject.toml")
    violations = _orphans(version, _src_code(), V080_BREAKING_CHANGES)
    assert not violations, "v0.8.0 release-gate violation(s):\n  - " + "\n  - ".join(violations)


# --- Self-checks: the detector must be non-vacuous in BOTH directions ----------
# A crafted "runway live" source (all four machinery markers present) and a
# static non-empty stand-in table. The dummy is deliberately *decoupled* from the
# real ``V080_BREAKING_CHANGES`` (it is NOT ``[:1]`` of it): at the 0.8.0 flip the
# real table is emptied, which would otherwise make these self-checks vacuous /
# spuriously fail. ``_orphans`` reads only truthiness + ``.issue`` (via getattr
# fallback), so a bare sentinel string is a valid stand-in entry.
_LIVE_SRC = "\n".join(_MACHINERY)
_NONEMPTY_TABLE: tuple[object, ...] = ("dummy-break-sentinel",)


def test_selfcheck_pre_flip_healthy_when_runway_intact() -> None:
    assert _orphans((0, 7, 0), _LIVE_SRC, _NONEMPTY_TABLE) == []


def test_selfcheck_pre_flip_flags_early_machinery_removal() -> None:
    assert _orphans((0, 7, 0), "", _NONEMPTY_TABLE)


def test_selfcheck_pre_flip_flags_early_table_emptying() -> None:
    assert _orphans((0, 7, 0), _LIVE_SRC, ())


def test_selfcheck_post_flip_healthy_when_fully_flipped() -> None:
    assert _orphans((0, 8, 0), "", ()) == []


def test_selfcheck_post_flip_flags_leftover_machinery() -> None:
    assert _orphans((0, 8, 0), _LIVE_SRC, ())


def test_selfcheck_post_flip_flags_unflipped_breaks() -> None:
    assert _orphans((0, 8, 0), "", _NONEMPTY_TABLE)
