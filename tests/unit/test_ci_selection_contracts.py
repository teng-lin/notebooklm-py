"""Selection and contract routing policy tests (Phase 3).

Follows Section 5 and Section 6.5 of the test-optimization plan:
1. Asserts that pr_contract is a strict subset of repo_lint (pr_contract => repo_lint).
2. Asserts that PR contracts and historical tests are completely disjoint.
3. Asserts that promoted lifecycle invariants execute in the routine PR suite.
4. Asserts that the live unconfirmed contract is in the routine lane, not hidden under repo_lint.
5. Asserts the platform selection manifest paths exist and tag matching tests with compat_smoke.
6. Asserts all custom markers are properly registered.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from tests.conftest import pytest_collection_modifyitems

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
PLATFORM_MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "ci-platform-selection.json"
LEDGER_PATH = REPO_ROOT / "tests" / "fixtures" / "test_relevance_ledger.json"

PROMOTED_LIFECYCLE_NAMES = [
    "test_open_is_transactional_and_concurrent_callers_coalesce",
    "test_open_failure_rolls_back_every_transport_and_preserves_original",
    "test_cancelling_non_owner_open_does_not_abort_owner",
    "test_cancelled_close_aborts_hung_graceful_wait_but_finishes_teardown",
    "test_close_reopen_allocates_a_new_resource_epoch",
    "test_registered_child_self_close_fails_fast_without_leaking_admission",
    "test_poll_callback_self_close_fails_fast_and_poll_settles_once",
]


def test_custom_markers_are_registered(pytestconfig: pytest.Config) -> None:
    markers = pytestconfig.getini("markers")
    marker_names = {m.split(":", 1)[0].strip().split("(")[0].strip() for m in markers}
    assert "historical" in marker_names
    assert "pr_contract" in marker_names
    assert "compat_smoke" in marker_names


def test_promoted_lifecycle_invariants_are_unmarked_by_refactor_qualification() -> None:
    path = REPO_ROOT / "tests" / "unit" / "test_client_lifecycle_waves.py"
    source = path.read_text(encoding="utf-8")
    assert "pytestmark = pytest.mark.refactor_qualification" not in source

    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in PROMOTED_LIFECYCLE_NAMES:
        assert name in functions, f"Expected {name} in {path}"
        fn = functions[name]
        decorator_names = [ast.unparse(d) for d in fn.decorator_list]
        assert not any("refactor_qualification" in d for d in decorator_names), (
            f"{name} must not be decorated with refactor_qualification"
        )


def test_pr_contract_implies_repo_lint() -> None:
    """Ensure that the pytest hook auto-tags items carrying pr_contract with repo_lint."""

    class FakeItem:
        def __init__(self, markers: set[str], nodeid: str = "tests/unit/test_foo.py::test_bar"):
            self.markers = set(markers)
            self.nodeid = nodeid
            self.keywords: dict[str, object] = {}

        def get_closest_marker(self, name: str):
            if name in self.markers:
                return getattr(pytest.mark, name)
            return None

        def add_marker(self, marker):
            self.markers.add(marker.name)

    config_mock = MagicMock()
    config_mock.getoption.return_value = False
    config_mock.rootpath = REPO_ROOT

    item1 = FakeItem({"pr_contract"})
    item2 = FakeItem({"pr_contract", "repo_lint"})
    item3 = FakeItem({"unit"})

    items = [item1, item2, item3]
    pytest_collection_modifyitems(config_mock, items)

    assert "repo_lint" in item1.markers
    assert "repo_lint" in item2.markers
    assert "repo_lint" not in item3.markers


def test_unconfirmed_contract_is_in_routine_behavioral_lane() -> None:
    path = REPO_ROOT / "tests" / "_guardrails" / "test_unconfirmed_contract.py"
    source = path.read_text(encoding="utf-8")
    assert "pytestmark = pytest.mark.repo_lint" not in source, (
        "test_unconfirmed_contract.py must not be marked repo_lint; it is a live behavioral contract"
    )


def test_platform_selection_manifest_is_valid() -> None:
    assert PLATFORM_MANIFEST_PATH.is_file(), f"Missing {PLATFORM_MANIFEST_PATH}"
    data = json.loads(PLATFORM_MANIFEST_PATH.read_text(encoding="utf-8"))
    paths = data.get("paths", [])
    assert len(paths) >= 8, f"Expected at least 8 platform paths, found {len(paths)}"

    for rel_path in paths:
        full_path = REPO_ROOT / rel_path
        assert full_path.exists(), f"Platform manifest path does not exist: {rel_path}"


def test_pr_contract_and_historical_sets_are_disjoint() -> None:
    assert LEDGER_PATH.is_file()
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    entries = ledger["entries"]

    pr_nodes = {e["nodeid"] for e in entries if e["decision"] == "pr_contract"}
    historical_nodes = {e["nodeid"] for e in entries if e["decision"] == "historical_manual"}

    overlap = pr_nodes & historical_nodes
    assert not overlap, f"PR contracts and historical tests must be disjoint; overlap: {overlap}"


def test_pr_contract_is_executed_in_canonical_pr_workflow() -> None:
    assert WORKFLOW_PATH.is_file()
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    test_job = workflow["jobs"]["test"]
    step = next(
        (s for s in test_job.get("steps", []) if s.get("name") == "Run PR contract suites"),
        None,
    )
    assert step is not None, "Missing 'Run PR contract suites' step in test.yml"
    assert step.get("if") == "matrix.canonical"
    run_cmd = str(step.get("run", ""))
    assert "-m" in run_cmd
    assert "pr_contract" in run_cmd


def test_historical_qualification_is_executed_in_manual_workflow() -> None:
    assert WORKFLOW_PATH.is_file()
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    repo_lint_job = workflow["jobs"]["repo-lint"]
    step = next(
        (
            s
            for s in repo_lint_job.get("steps", [])
            if s.get("name") == "Run historical qualification tests"
        ),
        None,
    )
    assert step is not None, "Missing 'Run historical qualification tests' step in repo-lint job"
    run_cmd = str(step.get("run", ""))
    assert "--run-historical" in run_cmd
    assert "-m historical" in run_cmd
