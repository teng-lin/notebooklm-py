"""Human-facing version string, annotated with the short git commit.

The commit resolves in two ways, in order:

1. ``_commit.py`` — baked in at build time by ``hatch_build.py`` when the build
   ran from a git checkout, so an installed package knows its commit with no
   ``.git`` nearby (released wheels and ``pip install git+…@main`` both go
   through that path; ``pip install .`` does too when the build backend can see
   ``.git``).
2. A live ``git rev-parse`` — for running straight from a source checkout
   (editable / ``PYTHONPATH``) where the build hook never ran.

Neither present (build couldn't see git) → bare version, no hash.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from . import __version__

# src/notebooklm/_version_info.py -> parents[2] == repo root (holds .git).
# Slice (not [2]) yields () when the install lives too shallow to have a
# parents[2] — avoids an IndexError at import time on a pathological layout.
_REPO_ROOT = next(iter(Path(__file__).resolve().parents[2:3]), None)


def _embedded_commit() -> str | None:
    """The commit baked in by the build hook, or ``None`` when absent."""
    try:
        from ._commit import COMMIT
    except ImportError:
        return None
    return COMMIT or None


def _live_commit() -> str | None:
    """First 8 chars of HEAD from a git checkout, or ``None``."""
    if _REPO_ROOT is None or not (_REPO_ROOT / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


@lru_cache(maxsize=1)
def version_string() -> str:
    """``"0.8.0 (5d748a26)"`` when the commit is known, else ``"0.8.0"``."""
    rev = _embedded_commit() or _live_commit()
    return f"{__version__} ({rev})" if rev else __version__


if __name__ == "__main__":  # ponytail: smallest runnable check for the branch
    v = version_string()
    assert v.startswith(__version__), v
    print(v)
