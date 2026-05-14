"""Assert CLAUDE.md repo-structure paths actually exist.

Phase 6 / P6.6: prevents silent drift in the hand-maintained file map.

Usage:
    python scripts/check_claude_md_freshness.py
    python scripts/check_claude_md_freshness.py --claude-md path/to/CLAUDE.md

Exit codes:
    0  All listed paths exist.
    1  One or more paths are missing.
    2  Argument error or CLAUDE.md not found.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _extract_paths(text: str) -> list[str]:
    paths: list[str] = []
    stack: list[tuple[int, str]] = []

    for line in text.splitlines():
        # Do not strip yet, we need leading spaces for indent calculation
        trimmed = line.strip()
        if not trimmed or any(trimmed.startswith(p) for p in ("|", "#", "```")):
            continue

        # Determine indentation level and clean the line
        indent = 0
        clean_line = trimmed
        found_marker = False
        for marker in ("├── ", "└── ", "│   "):
            if marker in line:
                # Calculate depth based on the position of the marker
                # We expect 4 spaces per level (or equivalent tree chars)
                pos = line.find(marker)
                indent = (pos // 4) + 1
                clean_line = line.split(marker, 1)[1]
                found_marker = True
                break

        if not found_marker:
            if trimmed.startswith(("src/notebooklm", "tests")):
                indent = 0
                clean_line = trimmed
            else:
                continue

        # Remove comments
        if " # " in clean_line:
            clean_line = clean_line.split(" # ", 1)[0]
        clean_line = clean_line.strip().rstrip("/")

        if not clean_line:
            continue

        # Manage the stack for tree traversal
        while stack and stack[-1][0] >= indent:
            stack.pop()

        stack.append((indent, clean_line))

        full_path = "/".join(segment for _, segment in stack)
        if full_path.startswith(("src/notebooklm", "tests")):
            paths.append(full_path)

    return sorted(set(paths))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-md", default="CLAUDE.md")
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args(argv)

    claude = Path(args.claude_md)
    if not claude.is_file():
        print(f"CLAUDE.md not found: {claude}", file=sys.stderr)
        return 2

    repo_root = Path(args.repo_root).resolve()
    text = claude.read_text(encoding="utf-8")

    # Only look at the Repository Structure section
    if "### Repository Structure" in text:
        text = text.split("### Repository Structure", 1)[1]
        if "## " in text:
            text = text.split("## ", 1)[0]

    paths = _extract_paths(text)
    missing = [p for p in paths if not (repo_root / p).exists()]
    if missing:
        print("Stale CLAUDE.md path references:", file=sys.stderr)
        for p in missing:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"OK: all {len(paths)} CLAUDE.md path references resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
