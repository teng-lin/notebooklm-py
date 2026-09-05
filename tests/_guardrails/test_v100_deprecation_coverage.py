"""Every announced v1.0 break has a live warning or docs runway."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._v100_breaks import V100_BREAKING_CHANGES, BreakingChange, DocsRunway, Runway

pytestmark = pytest.mark.repo_lint

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"


def _problems(changes: dict[str, BreakingChange] | object) -> list[str]:
    items = changes.items()  # type: ignore[union-attr]
    docs = (PROJECT_ROOT / "docs" / "deprecations.md").read_text(encoding="utf-8")
    problems: list[str] = []
    for key, change in items:
        has_runway = change.runway is not None
        has_exemption = bool(change.exemption and change.exemption.strip())
        if has_runway == has_exemption:
            problems.append(f"{key}: must set exactly one of runway/exemption")
            continue
        runway = change.runway
        if isinstance(runway, DocsRunway):
            if f"| {runway.anchor} |" not in docs:
                problems.append(f"{key}: missing docs row {runway.anchor!r}")
        elif isinstance(runway, Runway):
            path = SRC_ROOT / runway.module
            if not path.is_file() or runway.needle not in path.read_text(encoding="utf-8"):
                problems.append(f"{key}: missing live runway {runway.module}:{runway.needle}")
    return problems


def test_v100_registry_is_nonempty_unique_and_fully_runwayed() -> None:
    assert V100_BREAKING_CHANGES
    assert not _problems(V100_BREAKING_CHANGES), _problems(V100_BREAKING_CHANGES)


def test_v100_registry_literal_has_no_duplicate_row_ids() -> None:
    path = Path(__file__).with_name("_v100_breaks.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "V100_BREAKING_CHANGES"
    )
    assert isinstance(assignment.value, ast.Call)
    table = assignment.value.args[0]
    assert isinstance(table, ast.Dict)
    keys = [node.value for node in table.keys if isinstance(node, ast.Constant)]
    assert len(keys) == len(set(keys)) == len(V100_BREAKING_CHANGES)


def test_v100_detector_rejects_a_silent_break() -> None:
    assert _problems({"silent": BreakingChange("silent")})


def test_v100_detector_rejects_a_claimed_missing_runway() -> None:
    change = BreakingChange("missing", Runway(module="client.py", symbol="def missing_v100"))
    assert _problems({"missing": change})
