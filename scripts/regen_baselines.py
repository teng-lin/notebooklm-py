#!/usr/bin/env python3
"""Regenerate the committed test baselines from live code (ADR-0022).

A *baseline* is a committed snapshot of a value the code already derives — e.g.
``notebooklm.types.__all__``, the collected public surface of the ungated public
modules, or the CLI command tree. They live in ``tests/fixtures/baselines/`` (plus
the pre-existing ``tests/fixtures/cli_contract_baseline.json``) and are registered
in ``tests/_baselines/registry.py``.

This is the discoverable wrapper around the dev-only ``--update-baselines`` pytest
flag: it runs the registry-driven freeze test in *update* mode, which rewrites each
committed file from ``derive()``. After running, review the ``git diff`` — every
changed line is a deliberate, reviewed acknowledgement of a public-surface change.

    python scripts/regen_baselines.py

**Dev-only-regen invariant (ADR-0022):** CI never regenerates — it only diffs the
committed files against ``derive()``. This script (and the underlying fixture)
refuses to run when a CI environment is detected, so it cannot silently rewrite
baselines in automation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The single freeze test that performs the rewrite under ``--update-baselines``.
_BASELINE_FREEZE_TEST = (
    "tests/_guardrails/test_public_surface_manifest.py::test_baseline_matches_committed_file"
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if os.environ.get("CI", "").strip():
        print(
            "refusing to regenerate baselines in CI: CI only diffs (ADR-0022). "
            "Run this locally and commit the result.",
            file=sys.stderr,
        )
        return 2

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        _BASELINE_FREEZE_TEST,
        "--update-baselines",
        "-q",
        "-p",
        "no:cacheprovider",
        *argv,
    ]
    print("regenerating baselines:", " ".join(cmd), file=sys.stderr)
    result = subprocess.run(cmd, cwd=_PROJECT_ROOT)
    if result.returncode == 0:
        print(
            "baselines regenerated; review `git diff` — each change is a "
            "deliberate public-surface acknowledgement.",
            file=sys.stderr,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
