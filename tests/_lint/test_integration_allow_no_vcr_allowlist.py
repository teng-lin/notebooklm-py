"""Lint the temporary integration ``allow_no_vcr`` baseline."""

from __future__ import annotations

import subprocess
import sys


def test_integration_allow_no_vcr_allowlists_are_current() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/test_taxonomy_inventory.py", "--check"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr + result.stdout
