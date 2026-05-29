"""Assert CLAUDE.md repo-structure paths are fresh in both directions.

Prevents silent drift in the hand-maintained file map:

* documented paths must still exist; and
* direct ``src/notebooklm`` modules/packages must be documented or explicitly
  omitted with a reason.

Usage:
    python scripts/check_claude_md_freshness.py
    python scripts/check_claude_md_freshness.py --claude-md path/to/CLAUDE.md

Exit codes:
    0  CLAUDE.md is fresh.
    1  One or more paths are stale or missing from the map.
    2  Argument error or CLAUDE.md not found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_DOCUMENTED_ROOTS = ("src/notebooklm", "tests")
_OMISSIONS_HEADING = "### Repository Structure Intentional Omissions"
_OMISSION_BULLET_RE = re.compile(r"^\s*[-*]\s+")
_OMISSION_PATH_RE = re.compile(r"^\s*[-*]\s+`(?P<path>src/notebooklm/[^`]+)`")
_IGNORED_PATH_RE = re.compile(
    r"^\s*[-*]\s+`(?P<path>src/notebooklm/[^`]+)`\s+(?:--|-|:)\s+(?P<reason>.+?)\s*$"
)


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
        if full_path.startswith(_DOCUMENTED_ROOTS):
            paths.append(full_path)

    return sorted(set(paths))


def _repository_structure_section(text: str) -> str:
    section = _section_after_heading(text, "### Repository Structure")
    next_heading = re.search(r"^\s*##\s+", section, flags=re.MULTILINE)
    if next_heading is not None:
        section = section[: next_heading.start()]
    return section


def _extract_intentional_omissions(text: str) -> dict[str, str]:
    """Return intentionally omitted ``src/notebooklm`` paths and their reasons."""
    omissions: dict[str, str] = {}
    for line in _intentional_omissions_section(text).splitlines():
        if "`src/notebooklm/" not in line:
            continue
        match = _IGNORED_PATH_RE.match(line)
        if match is None:
            continue
        path = match.group("path").rstrip("/")
        reason = match.group("reason").strip()
        if reason:
            omissions[path] = reason
    return omissions


def _extract_unreasoned_omissions(text: str) -> list[str]:
    """Return omission bullets that mention a path but do not provide a reason."""
    unreasoned: list[str] = []
    for line in _intentional_omissions_section(text).splitlines():
        if _OMISSION_PATH_RE.match(line) and _IGNORED_PATH_RE.match(line) is None:
            unreasoned.append(line.strip())
    return unreasoned


def _extract_omission_bullet_path(line: str) -> str | None:
    match = _OMISSION_PATH_RE.match(line)
    if match is None:
        return None
    return match.group("path").rstrip("/")


def _intentional_omissions_section(text: str) -> str:
    section = _section_after_heading(text, _OMISSIONS_HEADING)
    next_heading = re.search(r"^\s*###\s+", section, flags=re.MULTILINE)
    if next_heading is not None:
        section = section[: next_heading.start()]
    return section


def _section_after_heading(text: str, heading: str) -> str:
    match = re.search(rf"^\s*{re.escape(heading)}\s*$", text, flags=re.MULTILINE)
    if match is None:
        return ""
    return text[match.end() :]


def _top_level_notebooklm_modules(repo_root: Path) -> list[str]:
    """Return direct ``src/notebooklm`` Python modules and packages."""
    package_root = repo_root / "src" / "notebooklm"
    if not package_root.is_dir():
        return []

    paths: list[str] = []
    for child in package_root.iterdir():
        if child.name == "__pycache__":
            continue
        if (child.is_file() and child.suffix == ".py") or (
            child.is_dir() and (child / "__init__.py").is_file()
        ):
            paths.append(child.relative_to(repo_root).as_posix())
    return sorted(paths)


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
    text = _repository_structure_section(claude.read_text(encoding="utf-8"))

    paths = _extract_paths(text)
    omissions = _extract_intentional_omissions(text)
    unreasoned_omissions = _extract_unreasoned_omissions(text)
    unreasoned_paths = {
        path for line in unreasoned_omissions if (path := _extract_omission_bullet_path(line))
    }
    missing = [p for p in paths if not (repo_root / p).exists()]
    top_level_modules = _top_level_notebooklm_modules(repo_root)
    undocumented = [
        p
        for p in top_level_modules
        if p not in paths and p not in omissions and p not in unreasoned_paths
    ]
    stale_omissions = [p for p in omissions if not (repo_root / p).exists()]

    if missing or undocumented or stale_omissions or unreasoned_omissions:
        if missing:
            print("Stale CLAUDE.md path references:", file=sys.stderr)
            for p in missing:
                print(f"  {p}", file=sys.stderr)
        if undocumented:
            print("Undocumented top-level src/notebooklm modules/packages:", file=sys.stderr)
            for p in undocumented:
                print(f"  {p}", file=sys.stderr)
        if stale_omissions:
            print("Stale CLAUDE.md intentional omissions:", file=sys.stderr)
            for p in stale_omissions:
                print(f"  {p}", file=sys.stderr)
        if unreasoned_omissions:
            print("Intentional omissions without parseable reasons:", file=sys.stderr)
            for line in unreasoned_omissions:
                print(f"  {line}", file=sys.stderr)
        return 1
    print(
        f"OK: {len(paths)} documented paths resolve; "
        f"{len(top_level_modules)} top-level modules/packages "
        f"are documented or intentionally omitted"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
