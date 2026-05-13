"""Assert non-publish workflows have a top-level `permissions:` block.

Phase 1 / T4: prevents supply-chain blast radius from default-permissive
GITHUB_TOKEN scope on workflows that don't need it.

Usage:
    python scripts/check_workflow_permissions.py
    python scripts/check_workflow_permissions.py --workflow-dir custom/path

Exit codes:
    0  All non-allowlisted workflows have a top-level permissions block.
    1  One or more workflows are missing the block.
    2  Argument error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Workflows that intentionally rely on job-level permissions or default scopes.
# codeql.yml: needs `security-events: write` (job-scoped is the standard).
# publish.yml / testpypi-publish.yml: write to PyPI; permissions live at job level.
# rpc-health.yml: opens issues on failure (already has its own permissions block).
ALLOWLIST = {
    "codeql.yml",
    "publish.yml",
    "rpc-health.yml",
    "testpypi-publish.yml",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workflow-dir",
        default=".github/workflows",
        help="Directory containing workflow YAML files",
    )
    args = ap.parse_args()

    workflow_dir = Path(args.workflow_dir)
    if not workflow_dir.is_dir():
        print(f"Not a directory: {workflow_dir}", file=sys.stderr)
        return 2

    # Assert each non-allowlisted workflow has a top-level `permissions:`
    # block whose body declares only read scopes. Reject inline strings
    # like `permissions: write-all`, `permissions: read-all`, or
    # `permissions: {}` — anything that isn't an explicit, scoped block
    # of read-only keys.
    #
    # We avoid a yaml dependency by parsing the small structural shape we
    # care about: header line `permissions:` (top-level, nothing else
    # on the line after the colon except optional comment) followed by
    # indented `<scope>: read` or `<scope>: none` lines.
    bad: list[str] = []
    issues: dict[str, str] = {}
    workflow_files = sorted(list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml")))
    for path in workflow_files:
        if path.name in ALLOWLIST:
            continue
        lines = path.read_text().splitlines()
        header_idx = _find_top_level_permissions_header(lines)
        if header_idx is None:
            bad.append(str(path))
            issues[str(path)] = "no top-level `permissions:` block"
            continue
        ok, reason = _validate_block_body(lines, header_idx)
        if not ok:
            bad.append(str(path))
            issues[str(path)] = reason

    if bad:
        for p in bad:
            print(f"{p}: {issues[p]}", file=sys.stderr)
        return 1

    print("OK: all non-allowlisted workflows have a scoped permissions block")
    return 0


# Scopes that may appear with `read` or `none` (or `write` only on allowlisted
# workflows — but allowlisted ones never reach this validator). Sourced from
# GitHub Actions workflow-syntax + GITHUB_TOKEN reference docs.
_ALLOWED_READ_SCOPES = frozenset(
    {
        "actions",
        "artifact-metadata",
        "attestations",
        "checks",
        "code-scanning",
        "contents",
        "deployments",
        "discussions",
        "environments",
        "id-token",
        "issues",
        "labels",
        "merge-queues",
        "metadata",
        "migrations",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-contacts",
        "security-events",
        "statuses",
        "vulnerability-alerts",
    }
)


def _find_top_level_permissions_header(lines: list[str]) -> int | None:
    """Return the index of a top-level `permissions:` header line, or None.

    "Top-level" = column 0 (no indent). The value after the colon must be
    empty (or whitespace + comment): an inline value like `write-all` is
    not a scoped block.
    """
    import re

    header_re = re.compile(r"^permissions:\s*(#.*)?$")
    for i, line in enumerate(lines):
        if header_re.match(line):
            return i
    return None


def _validate_block_body(lines: list[str], header_idx: int) -> tuple[bool, str]:
    """Validate the indented body under `permissions:` allows only read scopes.

    Returns (ok, reason). The body terminates at the next non-indented,
    non-blank line.
    """
    import re

    # Accept optional single/double quotes around the value (valid YAML).
    item_re = re.compile(r"^( +)([a-z][a-z0-9-]*):\s*['\"]?([a-z-]+)['\"]?\s*(#.*)?$")

    body_started = False
    for line in lines[header_idx + 1 :]:
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            break  # end of block
        body_started = True
        m = item_re.match(line)
        if not m:
            return False, f"unparseable body line under permissions: {stripped!r}"
        scope = m.group(2)
        value = m.group(3)
        if scope not in _ALLOWED_READ_SCOPES:
            return False, f"unknown scope under permissions: {scope!r}"
        if value not in ("read", "none"):
            return False, f"scope {scope!r} has non-read value {value!r}"

    if not body_started:
        return False, "permissions: header has no indented body (likely inline value)"
    return True, ""


if __name__ == "__main__":
    sys.exit(main())
