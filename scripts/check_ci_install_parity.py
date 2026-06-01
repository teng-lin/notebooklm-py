"""Assert install-doc parity.

Two checks:

1. **Canonical install presence:** the canonical install command —
   ``uv sync --frozen --extra browser --extra dev --extra markdown`` — must
   appear verbatim in both ``.github/workflows/test.yml`` and
   ``CONTRIBUTING.md``. The exact wording is deliberate (per
   ``docs/installation.md``): the broader ``--all-extras`` form pulls in
   ``cookies`` (and ``ai``), which fails on Python 3.13/3.14.

2. **Block-mirror policy:** every fenced ``bash`` code block
   in ``docs/installation.md`` (the canonical install guide) must EITHER
   appear verbatim in ``CONTRIBUTING.md``, OR be marked with
   ``<!-- not mirrored: <reason> -->`` on the line directly before the
   opening fence in ``installation.md``. This forces a reviewer to *think*
   about parity each time they edit the install docs — a stale block
   silently drifting into ``installation.md`` without a corresponding
   contributor-doc update is the failure mode this guards.

Usage:
    python scripts/check_ci_install_parity.py
    python scripts/check_ci_install_parity.py --workflow X --contributing Y --installation Z
    python scripts/check_ci_install_parity.py --skip-block-mirror   # original check only

Exit codes:
    0  All checks pass.
    1  Drift detected.
    2  Argument error / file not found.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

CANONICAL_INSTALL_CMD = "uv sync --frozen --extra browser --extra dev --extra markdown"

# Regex to find ``<!-- not mirrored: ... -->`` markers. The reason text is
# stripped — it's documentation for humans, not a key.
_MARKER_RE = re.compile(r"<!--\s*not\s+mirrored\s*:\s*(.+?)\s*-->", re.IGNORECASE)


@dataclass(frozen=True)
class _BashBlock:
    """A fenced ``bash`` code block extracted from a markdown source."""

    start_line: int  # 1-based line number of the opening ``` fence
    body: str  # block contents (no fences), exact characters


def _extract_bash_blocks(text: str) -> list[_BashBlock]:
    """Extract every fenced ``bash`` block from a markdown document.

    Indented blocks (e.g. inside numbered lists) are recognized: the script
    looks for a fence opener that begins with ``bash`` after any indentation.
    The block body is captured with the leading indentation stripped from
    each line so a verbatim search against another doc isn't foiled by
    purely-cosmetic indent differences.
    """
    blocks: list[_BashBlock] = []
    lines = text.splitlines(keepends=False)
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.rstrip() == "```bash":
            start = i + 1  # 1-based for human-friendly error messages
            body_lines: list[str] = []
            j = i + 1
            while j < len(lines):
                ln = lines[j]
                ln_stripped = ln.lstrip()
                ln_indent = len(ln) - len(ln_stripped)
                if ln_stripped.rstrip() == "```" and ln_indent == indent:
                    # Tolerate trailing whitespace on the closing fence so a
                    # file with stray spaces after ``` doesn't silently
                    # capture the rest of the document into one giant block.
                    break
                # Strip the opener's indent so the captured body is comparable
                # against blocks elsewhere that may use different indentation.
                if ln.startswith(" " * indent):
                    body_lines.append(ln[indent:])
                else:
                    body_lines.append(ln)
                j += 1
            blocks.append(_BashBlock(start_line=start, body="\n".join(body_lines)))
            i = j + 1
            continue
        i += 1
    return blocks


def _has_not_mirrored_marker(text: str, block: _BashBlock) -> bool:
    """Return True if a ``<!-- not mirrored: ... -->`` marker precedes ``block``.

    The marker must appear on the line directly above the opening fence
    (blank lines between are tolerated so the marker can sit above a
    heading or short paragraph without being forced inline with the fence).
    """
    lines = text.splitlines(keepends=False)
    fence_idx = block.start_line - 1
    k = fence_idx - 1
    while k >= 0 and lines[k].strip() == "":
        k -= 1
    if k < 0:
        return False
    return _MARKER_RE.search(lines[k]) is not None


def _check_canonical_install_presence(workflow_path: Path, contributing_path: Path) -> int:
    if not workflow_path.is_file():
        print(f"File not found: {workflow_path}", file=sys.stderr)
        return 2
    if not contributing_path.is_file():
        print(f"File not found: {contributing_path}", file=sys.stderr)
        return 2

    workflow_text = workflow_path.read_text(encoding="utf-8")
    contributing_text = contributing_path.read_text(encoding="utf-8")

    if CANONICAL_INSTALL_CMD not in workflow_text:
        print(
            f"DRIFT: {workflow_path} is missing the canonical install command:\n"
            f"  '{CANONICAL_INSTALL_CMD}'",
            file=sys.stderr,
        )
        return 1
    if CANONICAL_INSTALL_CMD not in contributing_text:
        print(
            f"DRIFT: {contributing_path} is missing the canonical install command:\n"
            f"  '{CANONICAL_INSTALL_CMD}'",
            file=sys.stderr,
        )
        return 1

    print(f"OK: both files use '{CANONICAL_INSTALL_CMD}'")
    return 0


def _check_block_mirror_policy(installation_path: Path, contributing_path: Path) -> int:
    """Every bash block in installation.md must be mirrored or explicitly marked."""
    if not installation_path.is_file():
        print(f"File not found: {installation_path}", file=sys.stderr)
        return 2
    if not contributing_path.is_file():
        print(f"File not found: {contributing_path}", file=sys.stderr)
        return 2

    installation_text = installation_path.read_text(encoding="utf-8")
    contributing_text = contributing_path.read_text(encoding="utf-8")

    blocks = _extract_bash_blocks(installation_text)
    if not blocks:
        print(f"WARN: no fenced ``bash`` blocks found in {installation_path}", file=sys.stderr)
        return 0

    # Compare against the SET of fenced bash blocks in CONTRIBUTING.md, not
    # the raw text. A naive substring check would let an installation block
    # pass when its body coincidentally appears inside prose, an unrelated
    # block, or a longer command — exactly the false-positive failure mode
    # that lets stale install docs slip through unnoticed.
    contributing_block_bodies = {b.body for b in _extract_bash_blocks(contributing_text)}

    failures: list[str] = []
    for block in blocks:
        if block.body in contributing_block_bodies:
            continue
        if _has_not_mirrored_marker(installation_text, block):
            continue
        first_line = block.body.splitlines()[0] if block.body else "(empty)"
        failures.append(
            f"  {installation_path}:{block.start_line} — block not mirrored "
            f"in {contributing_path.name} and missing "
            f"'<!-- not mirrored: <reason> -->' marker.\n"
            f"    first line: {first_line!r}"
        )

    if failures:
        print(
            "BLOCK-MIRROR DRIFT in install docs:\n"
            + "\n".join(failures)
            + "\n  Fix: either copy the block verbatim into "
            + str(contributing_path)
            + ", or add a '<!-- not mirrored: <reason> -->' line directly above "
            "the opening fence in " + str(installation_path) + ".",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: all {len(blocks)} bash block(s) in {installation_path.name} "
        "are mirrored or explicitly marked"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflow", default=str(repo_root / ".github/workflows/test.yml"))
    ap.add_argument("--contributing", default=str(repo_root / "CONTRIBUTING.md"))
    ap.add_argument("--installation", default=str(repo_root / "docs/installation.md"))
    ap.add_argument(
        "--skip-block-mirror",
        action="store_true",
        help="Run only the original canonical-install presence check.",
    )
    args = ap.parse_args(argv)

    workflow = Path(args.workflow)
    contributing = Path(args.contributing)
    installation = Path(args.installation)

    rc = _check_canonical_install_presence(workflow, contributing)
    if rc != 0:
        return rc

    if args.skip_block_mirror:
        return 0

    return _check_block_mirror_policy(installation, contributing)


if __name__ == "__main__":
    sys.exit(main())
