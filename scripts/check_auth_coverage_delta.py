#!/usr/bin/env python3
"""Compare merge-base/head auth coverage for every patch-migration-affected module."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_POLICY = Path("tests/fixtures/policies/auth_behavior_scenarios.json")
LIFECYCLE_POLICY = Path("tests/fixtures/policies/auth_lifecycle_cleanup.json")
ALLOWANCE_POLICY = Path("tests/fixtures/policies/auth_coverage_allowances.json")
IN_SCOPE_PREFIXES = (
    "src/notebooklm/_auth/",
    "src/notebooklm/_browser/",
    "src/notebooklm/auth.py",
    "tests/",
    "scripts/audit_auth_",
    "scripts/check_auth_",
    ".github/workflows/test.yml",
    "docs/adr/",
    "docs/development.md",
)


class CoverageDeltaError(RuntimeError):
    def __init__(self, message: str, *, losses: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.losses = losses


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CoverageDeltaError(f"{path}: expected an object")
    return value


def _git(*args: str, cwd: Path = REPO_ROOT) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        raise CoverageDeltaError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def changed_paths(base: str) -> set[str]:
    return set(filter(None, _git("diff", "--name-only", base, "--").splitlines()))


def _policy_at(workspace: Path, path: Path) -> dict[str, Any]:
    full = workspace / path
    if not full.is_file():
        name = (
            "allowances"
            if "allowance" in path.name
            else "operations"
            if "lifecycle" in path.name
            else "scenarios"
        )
        return {"version": 1, name: []}
    return _load(full)


def _policy_at_revision(base: str, path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "show", f"{base}:{path.as_posix()}"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        name = (
            "allowances"
            if "allowance" in path.name
            else "operations"
            if "lifecycle" in path.name
            else "scenarios"
        )
        return {"version": 1, name: []}
    return json.loads(result.stdout)


def validate_allowances(
    policy: dict[str, Any],
    *,
    base_sha: str,
    scenario_ids: set[str] | dict[str, set[str]],
    today: date | None = None,
) -> list[dict[str, Any]]:
    if policy.get("version") != 1 or not isinstance(policy.get("allowances"), list):
        raise CoverageDeltaError("allowance policy must be version 1 with an allowances list")
    today = today or datetime.now(timezone.utc).date()
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    for row in policy["allowances"]:
        required = {
            "id",
            "path",
            "kind",
            "base_coordinate",
            "head_coordinate",
            "disposition",
            "scenario_id",
            "rationale",
            "owner",
            "valid_for_base_sha",
            "authored_on",
            "expires_on",
        }
        if set(row) != required:
            raise CoverageDeltaError(f"allowance row has wrong fields: {set(row) ^ required}")
        if row["id"] in ids:
            raise CoverageDeltaError(f"duplicate allowance id: {row['id']}")
        ids.add(row["id"])
        if row["valid_for_base_sha"] != base_sha:
            raise CoverageDeltaError(f"{row['id']}: valid_for_base_sha is not the merge base")
        authored = date.fromisoformat(row["authored_on"])
        expires = date.fromisoformat(row["expires_on"])
        if authored > today:
            raise CoverageDeltaError(f"{row['id']}: authored_on is in the future")
        if (expires - authored).days > 14 or expires < authored:
            raise CoverageDeltaError(f"{row['id']}: expiry must be within 14 days")
        if expires < today:
            raise CoverageDeltaError(f"{row['id']}: allowance expired")
        if row["scenario_id"] not in scenario_ids:
            raise CoverageDeltaError(f"{row['id']}: scenario link does not exist")
        if isinstance(scenario_ids, dict) and row["path"] not in scenario_ids[row["scenario_id"]]:
            raise CoverageDeltaError(f"{row['id']}: scenario does not cover the allowance path")
        if row["kind"] not in {"statement", "branch"}:
            raise CoverageDeltaError(f"{row['id']}: invalid coordinate kind")
        if row["disposition"] not in {"mapped", "removed"}:
            raise CoverageDeltaError(f"{row['id']}: invalid disposition")
        if any(token in str(row["path"]) for token in ("*", "?", "[")):
            raise CoverageDeltaError(f"{row['id']}: wildcard paths are forbidden")
        if not str(row["path"]).startswith("src/notebooklm/"):
            raise CoverageDeltaError(f"{row['id']}: path is outside production source")
        base_coordinate = row["base_coordinate"]
        head_coordinate = row["head_coordinate"]
        if row["kind"] == "statement":
            valid_base = isinstance(base_coordinate, int) and not isinstance(base_coordinate, bool)
            valid_head = isinstance(head_coordinate, int) and not isinstance(head_coordinate, bool)
        else:
            valid_base = (
                isinstance(base_coordinate, list)
                and len(base_coordinate) == 2
                and all(
                    isinstance(item, int) and not isinstance(item, bool) for item in base_coordinate
                )
            )
            valid_head = (
                isinstance(head_coordinate, list)
                and len(head_coordinate) == 2
                and all(
                    isinstance(item, int) and not isinstance(item, bool) for item in head_coordinate
                )
            )
        if not valid_base:
            raise CoverageDeltaError(f"{row['id']}: invalid base coordinate")
        if row["disposition"] == "mapped" and not valid_head:
            raise CoverageDeltaError(
                f"{row['id']}: mapped allowance needs an exact head coordinate"
            )
        if row["disposition"] == "removed" and head_coordinate is not None:
            raise CoverageDeltaError(f"{row['id']}: removed allowance head coordinate must be null")
        if not str(row["rationale"]).strip() or not str(row["owner"]).strip():
            raise CoverageDeltaError(f"{row['id']}: rationale and owner are required")
        rows.append(row)
    return rows


def _module_path(package: str, module: str) -> str:
    if package == "notebooklm.auth":
        return "src/notebooklm/auth.py"
    return f"src/{package.replace('.', '/')}/{module}.py"


def _collect_projections(workspace: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.audit_auth_patch_sites import build_projection, collect_sites
    from scripts.audit_auth_shared_mutations import (
        build_projection as build_shared,
    )
    from scripts.audit_auth_shared_mutations import (
        collect_mutations,
    )

    tests = workspace / "tests"
    projections = [
        build_projection(collect_sites(tests, workspace / "src/notebooklm/_auth")),
        build_projection(
            collect_sites(
                tests,
                workspace / "src/notebooklm/_browser",
                package_dotted="notebooklm._browser",
            )
        ),
        build_projection(
            collect_sites(
                tests,
                workspace / "src/notebooklm/auth.py",
                package_dotted="notebooklm.auth",
            )
        ),
    ]
    shared = build_shared(
        collect_mutations(
            tests,
            {
                "notebooklm._auth": workspace / "src/notebooklm/_auth",
                "notebooklm._browser": workspace / "src/notebooklm/_browser",
            },
        )
    )
    return projections, shared


def _all_joint(projections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for projection in projections for row in projection.get("joint_sites", [])]


def _changed_policy_rows(
    base_policy: dict[str, Any], head_policy: dict[str, Any], collection: str
) -> list[dict[str, Any]]:
    old = {
        json.dumps(row, sort_keys=True, separators=(",", ":")): row
        for row in base_policy.get(collection, [])
    }
    new = {
        json.dumps(row, sort_keys=True, separators=(",", ":")): row
        for row in head_policy.get(collection, [])
    }
    return [*(old[key] for key in old.keys() - new), *(new[key] for key in new.keys() - old)]


def derive_affected_paths(
    *,
    changed: set[str],
    base_workspace: Path,
    head_workspace: Path,
    base_revision: str,
) -> set[str]:
    affected = {
        path
        for path in changed
        if path.startswith("src/notebooklm/_auth/")
        or path.startswith("src/notebooklm/_browser/")
        or path == "src/notebooklm/auth.py"
    }
    base_projections, base_shared = _collect_projections(base_workspace)
    head_projections, head_shared = _collect_projections(head_workspace)
    old_rows = _all_joint(base_projections)
    new_rows = _all_joint(head_projections)
    identity_fields = (
        "package",
        "module",
        "attribute",
        "idiom",
        "path",
        "owner_qualname",
        "owner_kind",
    )
    old = {tuple(row[field] for field in identity_fields): int(row["count"]) for row in old_rows}
    new = {tuple(row[field] for field in identity_fields): int(row["count"]) for row in new_rows}
    for identity in set(old) | set(new):
        if old.get(identity) != new.get(identity):
            affected.add(_module_path(str(identity[0]), str(identity[1])))
    shared_fields = (
        "package",
        "owner",
        "attribute",
        "idiom",
        "path",
        "owner_qualname",
        "owner_kind",
    )
    old_shared = {
        tuple(row[field] for field in shared_fields): int(row["count"])
        for row in base_shared.get("mutations", [])
    }
    new_shared = {
        tuple(row[field] for field in shared_fields): int(row["count"])
        for row in head_shared.get("mutations", [])
    }
    for identity in set(old_shared) | set(new_shared):
        if old_shared.get(identity) != new_shared.get(identity):
            owner_tail = str(identity[1])[len(str(identity[0])) + 1 :]
            affected.add(_module_path(str(identity[0]), owner_tail.split(".", 1)[0]))
    changed_tests = {path for path in changed if path.startswith("tests/") and path.endswith(".py")}
    for row in (*old_rows, *new_rows):
        if row["path"] in changed_tests:
            affected.add(_module_path(row["package"], row["module"]))
    for projection in (base_shared, head_shared):
        for row in projection.get("mutations", []):
            if row["path"] in changed_tests:
                tail = row["owner"][len(row["package"]) + 1 :].split(".", 1)[0]
                affected.add(_module_path(row["package"], tail))
    if any(
        path.startswith("scripts/audit_auth_")
        or path.startswith("tests/_baselines/")
        or path.startswith("tests/fixtures/baselines/auth_")
        for path in changed
    ):
        for row in (*old_rows, *new_rows):
            affected.add(_module_path(row["package"], row["module"]))
        for projection in (base_shared, head_shared):
            for row in projection.get("mutations", []):
                tail = row["owner"][len(row["package"]) + 1 :].split(".", 1)[0]
                affected.add(_module_path(row["package"], tail))
    policy_collections = (
        (SCENARIO_POLICY, "scenarios"),
        (LIFECYCLE_POLICY, "operations"),
        (ALLOWANCE_POLICY, "allowances"),
    )
    for path, collection in policy_collections:
        base_policy = _policy_at_revision(base_revision, path)
        head_policy = _policy_at(head_workspace, path)
        for row in _changed_policy_rows(base_policy, head_policy, collection):
            affected.update(row.get("affected_paths", []))
            if isinstance(row.get("path"), str):
                affected.add(row["path"])
            if isinstance(row.get("source_path"), str):
                affected.add(row["source_path"])
    return {path for path in affected if path.startswith("src/notebooklm/")}


def _line_map(base_text: str, head_text: str) -> dict[int, int]:
    """Map unchanged lines using Git's zero-context diff hunk boundaries."""
    old = base_text.splitlines()
    new = head_text.splitlines()
    with tempfile.TemporaryDirectory(prefix="auth-coverage-diff-") as temporary:
        root = Path(temporary)
        old_path = root / "base.py"
        new_path = root / "head.py"
        old_path.write_text(base_text, encoding="utf-8")
        new_path.write_text(head_text, encoding="utf-8")
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-index",
                "--unified=0",
                "--no-color",
                "--",
                str(old_path),
                str(new_path),
            ],
            text=True,
            capture_output=True,
        )
    if result.returncode not in {0, 1}:
        raise CoverageDeltaError(result.stderr.strip() or "git zero-context diff failed")
    hunks = [
        tuple(int(value or "1") for value in match.groups())
        for match in re.finditer(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
            result.stdout,
            flags=re.MULTILINE,
        )
    ]
    mapping: dict[int, int] = {}
    old_cursor = new_cursor = 1
    for old_start, old_count, new_start, new_count in hunks:
        # A zero-count range is an inter-line anchor: ``-3,0`` means an
        # insertion *after* old line 3, while ``+3,0`` means a deletion after
        # new line 3. Convert both sides to the first changed-line cursor
        # before comparing their unchanged prefixes. This also maps the
        # boundary anchors ``-0,0`` / ``+0,0`` to cursor 1.
        old_change = old_start + (old_count == 0)
        new_change = new_start + (new_count == 0)
        unchanged = old_change - old_cursor
        if unchanged != new_change - new_cursor:
            raise CoverageDeltaError("git diff produced inconsistent unchanged ranges")
        for offset in range(unchanged):
            mapping[old_cursor + offset] = new_cursor + offset
        old_cursor = old_change + old_count
        new_cursor = new_change + new_count
    remaining = len(old) - old_cursor + 1
    if remaining != len(new) - new_cursor + 1:
        raise CoverageDeltaError("git diff produced inconsistent trailing ranges")
    for offset in range(max(remaining, 0)):
        mapping[old_cursor + offset] = new_cursor + offset
    return mapping


def coverage_losses(
    path: str,
    base_file: dict[str, Any],
    head_file: dict[str, Any],
    line_map: dict[int, int],
) -> list[dict[str, Any]]:
    head_lines = set(head_file.get("executed_lines", []))
    losses: list[dict[str, Any]] = []
    for line in base_file.get("executed_lines", []):
        mapped = line_map.get(line)
        if mapped is None or mapped not in head_lines:
            losses.append(
                {
                    "path": path,
                    "kind": "statement",
                    "base_coordinate": line,
                    "head_coordinate": mapped,
                    "disposition": "removed" if mapped is None else "mapped",
                }
            )
    head_branches = {tuple(branch) for branch in head_file.get("executed_branches", [])}

    def map_arc_point(point: int) -> int | None:
        # coverage.py represents entry/exit arc destinations as the negative
        # line number of the function boundary. Preserve that sign while
        # mapping its underlying source coordinate through the diff.
        if point < 0:
            mapped = line_map.get(-point)
            return -mapped if mapped is not None else None
        if point == 0:
            return 0
        return line_map.get(point)

    for branch in base_file.get("executed_branches", []):
        old_arc = tuple(branch)
        mapped_arc = tuple(map_arc_point(point) for point in old_arc)
        comparable = all(point is not None for point in mapped_arc)
        if not comparable or mapped_arc not in head_branches:
            losses.append(
                {
                    "path": path,
                    "kind": "branch",
                    "base_coordinate": list(old_arc),
                    "head_coordinate": list(mapped_arc) if comparable else None,
                    "disposition": "removed" if not comparable else "mapped",
                }
            )
    return losses


def _allowance_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    base_coordinate = row["base_coordinate"]
    head_coordinate = row["head_coordinate"]
    if isinstance(base_coordinate, list):
        base_coordinate = tuple(base_coordinate)
    if isinstance(head_coordinate, list):
        head_coordinate = tuple(head_coordinate)
    return (
        row["path"],
        row["kind"],
        base_coordinate,
        head_coordinate,
        row["disposition"],
    )


def check_delta(
    *,
    base_coverage: dict[str, Any],
    head_coverage: dict[str, Any],
    affected: set[str],
    base_workspace: Path,
    head_workspace: Path,
    allowances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    losses: list[dict[str, Any]] = []
    for path in sorted(affected):
        base_file = base_coverage.get("files", {}).get(path)
        head_file = head_coverage.get("files", {}).get(path)
        if base_file is None:
            if head_file is None:
                raise CoverageDeltaError(f"coverage JSON is missing affected source file {path}")
            continue
        if head_file is None:
            head_file = {"executed_lines": [], "executed_branches": []}
        base_source = base_workspace / path
        head_source = head_workspace / path
        mapping = _line_map(
            base_source.read_text(encoding="utf-8") if base_source.is_file() else "",
            head_source.read_text(encoding="utf-8") if head_source.is_file() else "",
        )
        losses.extend(coverage_losses(path, base_file, head_file, mapping))
    allowance_map = {_allowance_identity(row): row for row in allowances}
    if len(allowance_map) != len(allowances):
        raise CoverageDeltaError("duplicate coverage allowance coordinate")
    loss_map = {_allowance_identity(row): row for row in losses}
    unmatched = sorted(set(loss_map) - set(allowance_map), key=str)
    stale = sorted(set(allowance_map) - set(loss_map), key=str)
    if unmatched:
        raise CoverageDeltaError(
            f"unexplained covered-coordinate losses: {unmatched}", losses=losses
        )
    if stale:
        raise CoverageDeltaError(f"stale coverage allowances: {stale}", losses=losses)
    return losses


def coverage_report(
    base_coverage: dict[str, Any], head_coverage: dict[str, Any], affected: set[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(affected):
        old = base_coverage.get("files", {}).get(path, {})
        new = head_coverage.get("files", {}).get(path, {})
        result.append(
            {
                "path": path,
                "base": old.get("summary", {}),
                "head": new.get("summary", {}),
                "base_covered_statements": len(old.get("executed_lines", [])),
                "head_covered_statements": len(new.get("executed_lines", [])),
                "base_covered_branches": len(old.get("executed_branches", [])),
                "head_covered_branches": len(new.get("executed_branches", [])),
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-json", type=Path, required=True)
    parser.add_argument("--head-json", type=Path, required=True)
    parser.add_argument("--changed-since", required=True)
    parser.add_argument("--base-workspace", type=Path, default=REPO_ROOT)
    parser.add_argument("--head-workspace", type=Path, default=REPO_ROOT)
    parser.add_argument("--scenario-policy", type=Path, default=SCENARIO_POLICY)
    parser.add_argument("--allowance-policy", type=Path, default=ALLOWANCE_POLICY)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report: dict[str, Any] = {
        "status": "error",
        "affected_paths": [],
        "files": [],
        "losses": [],
    }
    try:
        changed = changed_paths(args.changed_since)
        in_scope = any(path.startswith(IN_SCOPE_PREFIXES) for path in changed)
        if not in_scope:
            report = {"status": "out-of-scope", "affected_paths": [], "losses": []}
        else:
            affected = derive_affected_paths(
                changed=changed,
                base_workspace=args.base_workspace,
                head_workspace=args.head_workspace,
                base_revision=args.changed_since,
            )
            if not affected:
                raise CoverageDeltaError("in-scope change produced an empty coverage scope")
            report["affected_paths"] = sorted(affected)
            scenario = _load(args.scenario_policy)
            scenario_ids = {
                row["id"]: set(row.get("affected_paths", []))
                for row in scenario.get("scenarios", [])
            }
            allowance_policy = _load(args.allowance_policy)
            allowances = validate_allowances(
                allowance_policy,
                base_sha=args.changed_since,
                scenario_ids=scenario_ids,
            )
            base_coverage = _load(args.base_json)
            head_coverage = _load(args.head_json)
            report = {
                "status": "checking",
                "affected_paths": sorted(affected),
                "files": coverage_report(base_coverage, head_coverage, affected),
                "losses": [],
            }
            losses = check_delta(
                base_coverage=base_coverage,
                head_coverage=head_coverage,
                affected=affected,
                base_workspace=args.base_workspace,
                head_workspace=args.head_workspace,
                allowances=allowances,
            )
            report = {
                "status": "ok",
                "affected_paths": sorted(affected),
                "files": coverage_report(base_coverage, head_coverage, affected),
                "losses": losses,
            }
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.report:
            args.report.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
    except (CoverageDeltaError, ValueError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        if isinstance(exc, CoverageDeltaError) and exc.losses is not None:
            report["losses"] = exc.losses
        if args.report:
            args.report.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
