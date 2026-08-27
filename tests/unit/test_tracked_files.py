"""Tests for repository scans that must ignore untracked local scratch."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts._tracked_files import tracked_files


def test_tracked_files_excludes_untracked_scratch(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "docs" / "tracked.md"
    scratch = tmp_path / "docs" / "scratch.md"
    tracked.parent.mkdir()
    tracked.write_text("tracked\n", encoding="utf-8")
    scratch.write_text("untracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "docs/tracked.md"], check=True)

    assert tracked_files(tmp_path, fallback_globs=("docs/**/*.md",)) == [tracked]
