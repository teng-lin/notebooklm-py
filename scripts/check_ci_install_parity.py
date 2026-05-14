"""Assert CI install step matches CONTRIBUTING.md.

Phase 6 / P6.4: ensures that contributors are using the same install path as CI.

Usage:
    python scripts/check_ci_install_parity.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).parent.parent
    test_yml = repo_root / ".github/workflows/test.yml"
    contributing_md = repo_root / "CONTRIBUTING.md"

    if not test_yml.is_file():
        print(f"File not found: {test_yml}", file=sys.stderr)
        return 2
    if not contributing_md.is_file():
        print(f"File not found: {contributing_md}", file=sys.stderr)
        return 2

    test_text = test_yml.read_text(encoding="utf-8")
    contributing_text = contributing_md.read_text(encoding="utf-8")

    # Look for the install command
    # In test.yml: uv sync --frozen --all-extras
    # In CONTRIBUTING.md: uv sync --frozen --all-extras

    install_cmd = "uv sync --frozen --extra browser --extra dev --extra markdown"

    ci_has_it = install_cmd in test_text
    docs_have_it = install_cmd in contributing_text

    if not ci_has_it:
        print(f"ERROR: .github/workflows/test.yml is missing '{install_cmd}'", file=sys.stderr)
        return 1
    if not docs_have_it:
        print(f"ERROR: CONTRIBUTING.md is missing '{install_cmd}'", file=sys.stderr)
        return 1

    print(f"OK: CI and CONTRIBUTING.md both use '{install_cmd}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
