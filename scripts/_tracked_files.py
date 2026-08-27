"""Small shared helper for repository gates that must ignore local scratch files."""

from __future__ import annotations

import subprocess
from pathlib import Path


def tracked_files(repo_root: Path, *, fallback_globs: tuple[str, ...]) -> list[Path]:
    """Return git-tracked files, falling back to globs for synthetic test repos."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return sorted(
            repo_root / relative.decode("utf-8")
            for relative in result.stdout.split(b"\0")
            if relative
        )

    paths: set[Path] = set()
    for pattern in fallback_globs:
        paths.update(path for path in repo_root.glob(pattern) if path.is_file())
    return sorted(paths)


__all__ = ["tracked_files"]
