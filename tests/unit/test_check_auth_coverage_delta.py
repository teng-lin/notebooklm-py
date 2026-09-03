from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_auth_coverage_delta as check


def test_unchanged_covered_statement_and_branch_must_remain_covered() -> None:
    base = {"executed_lines": [1, 2], "executed_branches": [[1, 2]]}
    head = {"executed_lines": [1], "executed_branches": []}
    assert check.coverage_losses("src/notebooklm/_auth/x.py", base, head, {1: 1, 2: 2}) == [
        {
            "path": "src/notebooklm/_auth/x.py",
            "kind": "statement",
            "base_coordinate": 2,
            "head_coordinate": 2,
            "disposition": "mapped",
        },
        {
            "path": "src/notebooklm/_auth/x.py",
            "kind": "branch",
            "base_coordinate": [1, 2],
            "head_coordinate": [1, 2],
            "disposition": "mapped",
        },
    ]


def test_coordinate_mapping_tracks_only_unchanged_lines() -> None:
    mapping = check._line_map("one\ntwo\nthree\n", "zero\none\nchanged\nthree\n")
    assert mapping == {1: 2, 3: 4}


@pytest.mark.parametrize(
    ("base", "head", "expected"),
    [
        ("a\nb\n", "x\na\nb\n", {1: 2, 2: 3}),
        ("a\nb\nc\nd\n", "a\nb\nc\nx\nd\n", {1: 1, 2: 2, 3: 3, 4: 5}),
        ("a\nb\n", "a\nb\nx\n", {1: 1, 2: 2}),
        ("a\nb\nc\nd\n", "a\nc\nd\n", {1: 1, 3: 2, 4: 3}),
        ("a\nb\n", "a\n", {1: 1}),
        ("a\nb\n", "", {}),
    ],
)
def test_zero_context_insert_delete_anchors_map_unchanged_lines(
    base: str, head: str, expected: dict[int, int]
) -> None:
    assert check._line_map(base, head) == expected


def test_negative_coverage_arc_endpoints_preserve_their_signed_line_mapping() -> None:
    base = {"executed_lines": [2], "executed_branches": [[2, -1]]}
    head = {"executed_lines": [3], "executed_branches": [[3, -2]]}
    assert check.coverage_losses("src/notebooklm/_auth/x.py", base, head, {1: 2, 2: 3}) == []


def test_cleanup_only_allowance_deletion_seeds_non_vacuous_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    monkeypatch.setattr(check, "_collect_projections", lambda workspace: ([], {"mutations": []}))
    monkeypatch.setattr(
        check,
        "_policy_at_revision",
        lambda revision, path: {
            "version": 1,
            "allowances": [
                {
                    "path": "src/notebooklm/_auth/refresh.py",
                    "source_path": "src/notebooklm/_auth/refresh.py",
                }
            ]
            if "allowance" in path.name
            else [],
        },
    )
    monkeypatch.setattr(
        check,
        "_policy_at",
        lambda workspace, path: {"version": 1, "allowances": [], "scenarios": [], "operations": []},
    )
    affected = check.derive_affected_paths(
        changed={"tests/fixtures/policies/auth_coverage_allowances.json"},
        base_workspace=base,
        head_workspace=head,
        base_revision="abc123",
    )
    assert affected == {"src/notebooklm/_auth/refresh.py"}


def test_unchanged_policy_rows_do_not_seed_coverage_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    row = {"affected_paths": ["src/notebooklm/_auth/recovery.py"]}
    monkeypatch.setattr(check, "_collect_projections", lambda workspace: ([], {"mutations": []}))

    def policy(path: Path) -> dict[str, object]:
        collection = "operations" if "lifecycle" in path.name else "allowances"
        return {"version": 1, collection: [row] if collection == "operations" else []}

    monkeypatch.setattr(check, "_policy_at_revision", lambda revision, path: policy(path))
    monkeypatch.setattr(check, "_policy_at", lambda workspace, path: policy(path))
    assert (
        check.derive_affected_paths(
            changed={"docs/adr/0033-auth-consolidation-policy.md"},
            base_workspace=base,
            head_workspace=head,
            base_revision="abc123",
        )
        == set()
    )


def test_unmatched_loss_and_stale_allowance_both_fail(tmp_path: Path) -> None:
    path = "src/notebooklm/_auth/x.py"
    for workspace in (tmp_path / "base", tmp_path / "head"):
        source = workspace / path
        source.parent.mkdir(parents=True)
        source.write_text("covered\n", encoding="utf-8")
    base_cov = {"files": {path: {"executed_lines": [1], "executed_branches": []}}}
    head_cov = {"files": {path: {"executed_lines": [], "executed_branches": []}}}
    with pytest.raises(check.CoverageDeltaError, match="unexplained") as raised:
        check.check_delta(
            base_coverage=base_cov,
            head_coverage=head_cov,
            affected={path},
            base_workspace=tmp_path / "base",
            head_workspace=tmp_path / "head",
            allowances=[],
        )
    assert raised.value.losses == [
        {
            "path": path,
            "kind": "statement",
            "base_coordinate": 1,
            "head_coordinate": 1,
            "disposition": "mapped",
        }
    ]


def test_deleted_covered_statement_requires_its_exact_removed_allowance(tmp_path: Path) -> None:
    path = "src/notebooklm/_auth/x.py"
    base_source = tmp_path / "base" / path
    head_source = tmp_path / "head" / path
    base_source.parent.mkdir(parents=True)
    head_source.parent.mkdir(parents=True)
    base_source.write_text("kept\nremoved\n", encoding="utf-8")
    head_source.write_text("kept\n", encoding="utf-8")
    allowance = {
        "path": path,
        "kind": "statement",
        "base_coordinate": 2,
        "head_coordinate": None,
        "disposition": "removed",
    }

    assert check.check_delta(
        base_coverage={"files": {path: {"executed_lines": [1, 2], "executed_branches": []}}},
        head_coverage={"files": {path: {"executed_lines": [1], "executed_branches": []}}},
        affected={path},
        base_workspace=tmp_path / "base",
        head_workspace=tmp_path / "head",
        allowances=[allowance],
    ) == [allowance]


def test_allowance_must_match_the_exact_mapped_head_coordinate(tmp_path: Path) -> None:
    path = "src/notebooklm/_auth/x.py"
    for workspace in (tmp_path / "base", tmp_path / "head"):
        source = workspace / path
        source.parent.mkdir(parents=True)
        source.write_text("covered\n", encoding="utf-8")
    base_cov = {"files": {path: {"executed_lines": [1], "executed_branches": []}}}
    head_cov = {"files": {path: {"executed_lines": [], "executed_branches": []}}}
    allowance = {
        "path": path,
        "kind": "statement",
        "base_coordinate": 1,
        "head_coordinate": 99,
        "disposition": "mapped",
    }
    with pytest.raises(check.CoverageDeltaError, match="unexplained"):
        check.check_delta(
            base_coverage=base_cov,
            head_coverage=head_cov,
            affected={path},
            base_workspace=tmp_path / "base",
            head_workspace=tmp_path / "head",
            allowances=[allowance],
        )


def test_shared_mutation_delta_seeds_its_production_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    row = {
        "package": "notebooklm._auth",
        "owner": "notebooklm._auth.refresh.Owner.process_default()",
        "attribute": "reset",
        "idiom": "method-or-unknown",
        "path": "tests/unit/test_x.py",
        "owner_qualname": "test_x",
        "owner_kind": "test",
        "count": 1,
    }

    def projections(workspace: Path):
        return [], {"mutations": [row] if workspace == base else []}

    monkeypatch.setattr(check, "_collect_projections", projections)
    monkeypatch.setattr(
        check,
        "_policy_at_revision",
        lambda revision, path: {"version": 1, "scenarios": [], "operations": [], "allowances": []},
    )
    monkeypatch.setattr(
        check,
        "_policy_at",
        lambda workspace, path: {"version": 1, "scenarios": [], "operations": [], "allowances": []},
    )
    assert check.derive_affected_paths(
        changed={"tests/unit/test_x.py"},
        base_workspace=base,
        head_workspace=head,
        base_revision="abc123",
    ) == {"src/notebooklm/_auth/refresh.py"}


def test_failure_report_preserves_file_summaries_and_losses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = "src/notebooklm/_auth/x.py"
    base_workspace = tmp_path / "base"
    head_workspace = tmp_path / "head"
    for workspace in (base_workspace, head_workspace):
        source = workspace / source_path
        source.parent.mkdir(parents=True)
        source.write_text("covered\n", encoding="utf-8")
    base_json = tmp_path / "base.json"
    head_json = tmp_path / "head.json"
    base_json.write_text(
        json.dumps(
            {
                "files": {
                    source_path: {
                        "executed_lines": [1],
                        "executed_branches": [],
                        "summary": {"percent_covered": 100.0},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    head_json.write_text(
        json.dumps(
            {
                "files": {
                    source_path: {
                        "executed_lines": [],
                        "executed_branches": [],
                        "summary": {"percent_covered": 0.0},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    scenario = tmp_path / "scenarios.json"
    allowance = tmp_path / "allowances.json"
    scenario.write_text('{"version": 1, "scenarios": []}\n', encoding="utf-8")
    allowance.write_text('{"version": 1, "allowances": []}\n', encoding="utf-8")
    report = tmp_path / "report.json"
    monkeypatch.setattr(check, "changed_paths", lambda revision: {source_path})
    monkeypatch.setattr(check, "derive_affected_paths", lambda **kwargs: {source_path})
    with pytest.raises(SystemExit):
        check.main(
            [
                "--base-json",
                str(base_json),
                "--head-json",
                str(head_json),
                "--changed-since",
                "abc123",
                "--base-workspace",
                str(base_workspace),
                "--head-workspace",
                str(head_workspace),
                "--scenario-policy",
                str(scenario),
                "--allowance-policy",
                str(allowance),
                "--report",
                str(report),
            ]
        )
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["status"] == "error"
    assert evidence["affected_paths"] == [source_path]
    assert evidence["files"][0]["base"]["percent_covered"] == 100.0
    assert evidence["losses"][0]["base_coordinate"] == 1
