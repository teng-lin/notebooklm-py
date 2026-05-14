"""Assert CI install step matches CONTRIBUTING.md.

Phase 6 / P6.4: ensures that contributors are using the same install path as CI.
The canonical install command — ``uv sync --frozen --extra browser --extra dev
--extra markdown`` — must appear verbatim in both files. The exact wording is
deliberate (per ``docs/installation.md``): the broader ``--all-extras`` form
pulls in ``cookies`` (and ``ai``), which fails on Python 3.13/3.14.

Usage:
    python scripts/check_ci_install_parity.py
    python scripts/check_ci_install_parity.py --workflow custom/test.yml --contributing CONTRIBUTING.md

Exit codes:
    0  Both files contain the canonical install command.
    1  Drift detected (one or both files missing the command).
    2  Argument error / file not found.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CANONICAL_INSTALL_CMD = "uv sync --frozen --extra browser --extra dev --extra markdown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    repo_root = Path(__file__).resolve().parent.parent
    ap.add_argument("--workflow", default=str(repo_root / ".github/workflows/test.yml"))
    ap.add_argument("--contributing", default=str(repo_root / "CONTRIBUTING.md"))
    args = ap.parse_args(argv)

    workflow = Path(args.workflow)
    contributing = Path(args.contributing)

    if not workflow.is_file():
        print(f"File not found: {workflow}", file=sys.stderr)
        return 2
    if not contributing.is_file():
        print(f"File not found: {contributing}", file=sys.stderr)
        return 2

    workflow_text = workflow.read_text(encoding="utf-8")
    contributing_text = contributing.read_text(encoding="utf-8")

    ci_has_it = CANONICAL_INSTALL_CMD in workflow_text
    docs_have_it = CANONICAL_INSTALL_CMD in contributing_text

    if not ci_has_it:
        print(
            f"DRIFT: {workflow} is missing the canonical install command:\n"
            f"  '{CANONICAL_INSTALL_CMD}'",
            file=sys.stderr,
        )
        return 1
    if not docs_have_it:
        print(
            f"DRIFT: {contributing} is missing the canonical install command:\n"
            f"  '{CANONICAL_INSTALL_CMD}'",
            file=sys.stderr,
        )
        return 1

    print(f"OK: both files use '{CANONICAL_INSTALL_CMD}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
