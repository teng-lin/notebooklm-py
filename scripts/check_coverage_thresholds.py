"""Assert pyproject.toml `fail_under` matches `.github/workflows/test.yml` `--cov-fail-under`.

Prevents the two values from drifting (e.g. CI passing at 70% while pyproject
demands 90%, or vice versa) by failing CI whenever they disagree.

When ``--coverage-json`` is provided, additionally enforces per-file floors
declared in ``[tool.notebooklm.per_file_coverage_floors]``. Coverage.py's
``[tool.coverage.report]`` only supports a global ``fail_under``, so individual
files that lag the project-wide 90% are guarded by this script.

Usage:
    python scripts/check_coverage_thresholds.py
    python scripts/check_coverage_thresholds.py --pyproject custom/pyproject.toml --workflow custom/test.yml
    python scripts/check_coverage_thresholds.py --coverage-json coverage.json

Exit codes:
    0  Thresholds match (and any per-file floors are met).
    1  Drift detected, OR a per-file floor breached.
    2  Argument error / missing field / coverage.json missing or malformed.
"""

from __future__ import annotations

import argparse
import json
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


def _check_global_drift(pyproject_path: str, workflow_path: str) -> int:
    """Compare pyproject ``fail_under`` against CI ``--cov-fail-under``."""
    try:
        with open(pyproject_path, "rb") as f:
            pp = tomllib.load(f)
    except FileNotFoundError:
        print(f"pyproject.toml not found: {pyproject_path}", file=sys.stderr)
        return 2

    try:
        pyproject_threshold = pp["tool"]["coverage"]["report"]["fail_under"]
    except KeyError:
        print(
            f"No [tool.coverage.report] fail_under in {pyproject_path}",
            file=sys.stderr,
        )
        return 2

    try:
        with open(workflow_path) as f:
            yml = f.read()
    except FileNotFoundError:
        print(f"Workflow not found: {workflow_path}", file=sys.stderr)
        return 2

    # Scan line-by-line and ignore commented YAML lines so a stale
    # `# --cov-fail-under=90` doesn't shadow a real drift in the executed
    # command. Collect ALL occurrences so a workflow with multiple jobs
    # cannot smuggle a divergent threshold past the check.
    thresholds: list[int] = []
    pattern = re.compile(r"(?<!\S)--cov-fail-under(?:=|\s+)(\d+)(?!\S)")
    for line in yml.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for m in pattern.finditer(stripped):
            thresholds.append(int(m.group(1)))

    if not thresholds:
        print(f"No --cov-fail-under in {workflow_path}", file=sys.stderr)
        return 2

    for ci_threshold in thresholds:
        if pyproject_threshold != ci_threshold:
            print(
                f"DRIFT: pyproject.toml fail_under={pyproject_threshold} but "
                f"{workflow_path} --cov-fail-under={ci_threshold}",
                file=sys.stderr,
            )
            return 1

    print(f"OK: {len(thresholds)} occurrence(s), all at {pyproject_threshold}%")
    return 0


def _check_per_file_floors(pyproject_path: str, coverage_json_path: str) -> int:
    """Enforce per-file floors from ``[tool.notebooklm.per_file_coverage_floors]``.

    Floors live in pyproject.toml so they're checked into the repo and bumped
    via PR (the commit message documents the new minimum). The script reads
    ``coverage.json`` produced by ``pytest --cov ... --cov-report=json:...``
    and fails if any guarded file's ``percent_covered`` is below its floor.
    """
    try:
        with open(pyproject_path, "rb") as f:
            pp = tomllib.load(f)
    except FileNotFoundError:
        print(f"pyproject.toml not found: {pyproject_path}", file=sys.stderr)
        return 2

    floors = pp.get("tool", {}).get("notebooklm", {}).get("per_file_coverage_floors")
    if floors is None:
        # Missing table is fine — nothing to enforce.
        print(f"OK: no [tool.notebooklm.per_file_coverage_floors] in {pyproject_path}")
        return 0
    if not isinstance(floors, dict):
        # Misconfiguration (e.g. someone wrote a string or list instead of a
        # table). Fail fast rather than silently returning OK.
        print(
            f"[tool.notebooklm.per_file_coverage_floors] must be a TOML table in "
            f"{pyproject_path}, got {type(floors).__name__}",
            file=sys.stderr,
        )
        return 2
    if not floors:
        # Empty table — explicitly opted into "enforce nothing right now".
        print(f"OK: no [tool.notebooklm.per_file_coverage_floors] in {pyproject_path}")
        return 0

    try:
        with open(coverage_json_path) as f:
            cov = json.load(f)
    except FileNotFoundError:
        print(f"coverage.json not found: {coverage_json_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"coverage.json malformed: {exc}", file=sys.stderr)
        return 2

    files = cov.get("files")
    if not isinstance(files, dict):
        # Includes both the missing-key case (``cov.get`` returns None) and
        # the wrong-shape case (e.g. accidentally a list); both indicate a
        # malformed ``coverage.json`` and should fail before any per-file
        # comparison runs.
        print(
            f"coverage.json 'files' must be an object map in {coverage_json_path}, "
            f"got {type(files).__name__}",
            file=sys.stderr,
        )
        return 2

    failures: list[str] = []
    missing: list[str] = []
    for path, floor in sorted(floors.items()):
        entry = files.get(path)
        if entry is None:
            # A guarded file with no measurement is itself a CI failure —
            # otherwise a rename or accidental deletion would silently drop
            # the floor and any later regression would slip through.
            missing.append(path)
            continue
        try:
            actual = float(entry["summary"]["percent_covered"])
            target = float(floor)
        except (KeyError, TypeError, ValueError) as exc:
            print(
                f"coverage.json entry for {path!r} could not be compared "
                f"(actual={entry.get('summary', {}).get('percent_covered')!r}, "
                f"floor={floor!r}): {exc}",
                file=sys.stderr,
            )
            return 2
        if actual < target:
            failures.append(f"  {path}: {actual:.2f}% < floor {floor}%")

    if missing:
        print(
            "MISSING from coverage.json (renamed/deleted? update "
            "[tool.notebooklm.per_file_coverage_floors] or restore the file):\n  "
            + "\n  ".join(missing),
            file=sys.stderr,
        )
        return 1

    if failures:
        print(
            "PER-FILE COVERAGE FLOOR BREACH:\n" + "\n".join(failures),
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(floors)} per-file floor(s) all met")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pyproject", default="pyproject.toml")
    ap.add_argument("--workflow", default=".github/workflows/test.yml")
    ap.add_argument(
        "--coverage-json",
        default=None,
        help=(
            "Optional path to coverage.json (produced by `pytest --cov "
            "--cov-report=json:coverage.json`). When present, also enforces "
            "[tool.notebooklm.per_file_coverage_floors] from pyproject.toml."
        ),
    )
    args = ap.parse_args(argv)

    drift_rc = _check_global_drift(args.pyproject, args.workflow)
    if drift_rc != 0:
        return drift_rc

    if args.coverage_json:
        return _check_per_file_floors(args.pyproject, args.coverage_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
