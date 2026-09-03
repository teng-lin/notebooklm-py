"""Cross-platform command-line builders for subprocess-backed tests."""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Sequence


def platform_command(args: Sequence[str]) -> str:
    """Quote ``args`` with the parser rules production uses on this platform."""
    command = list(args)
    return subprocess.list2cmdline(command) if sys.platform == "win32" else shlex.join(command)
