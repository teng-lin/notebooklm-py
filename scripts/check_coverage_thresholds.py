"""Assert pyproject.toml `fail_under` matches `.github/workflows/test.yml` `--cov-fail-under`.

Phase 1 / T5: prevents the drift bug that was discovered during the multi-agent
audit. CI claimed 70%, pyproject claimed 90% — until they were aligned manually.

Usage:
    python scripts/check_coverage_thresholds.py
    python scripts/check_coverage_thresholds.py --pyproject custom/pyproject.toml --workflow custom/test.yml

Exit codes:
    0  Thresholds match.
    1  Drift detected.
    2  Argument error / missing field.
"""

from __future__ import annotations

import argparse
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]
    except ImportError:
        print(
            "tomli is required on Python 3.10. Install with: uv pip install tomli",
            file=sys.stderr,
        )
        sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pyproject", default="pyproject.toml")
    ap.add_argument("--workflow", default=".github/workflows/test.yml")
    args = ap.parse_args()

    try:
        with open(args.pyproject, "rb") as f:
            pp = tomllib.load(f)
    except FileNotFoundError:
        print(f"pyproject.toml not found: {args.pyproject}", file=sys.stderr)
        return 2

    try:
        pyproject_threshold = pp["tool"]["coverage"]["report"]["fail_under"]
    except KeyError:
        print(
            f"No [tool.coverage.report] fail_under in {args.pyproject}",
            file=sys.stderr,
        )
        return 2

    try:
        with open(args.workflow) as f:
            yml = f.read()
    except FileNotFoundError:
        print(f"Workflow not found: {args.workflow}", file=sys.stderr)
        return 2

    match = re.search(r"--cov-fail-under=(\d+)", yml)
    if not match:
        print(f"No --cov-fail-under in {args.workflow}", file=sys.stderr)
        return 2
    ci_threshold = int(match.group(1))

    if pyproject_threshold != ci_threshold:
        print(
            f"DRIFT: pyproject.toml fail_under={pyproject_threshold} but "
            f"{args.workflow} --cov-fail-under={ci_threshold}",
            file=sys.stderr,
        )
        return 1

    print(f"OK: both at {pyproject_threshold}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
