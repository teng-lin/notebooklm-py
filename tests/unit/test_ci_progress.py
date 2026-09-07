"""Failure and progress reporting must survive errors without exposing handles."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from scripts._ci_progress import report, safe_test_name

from tests.e2e._progress import E2EProgress

pytest_plugins = ["pytester"]


@pytest.fixture
def workflow_bash():
    if sys.platform == "win32":
        # PATH's bash.exe may be the WSL stub with no installed distribution.
        # Actions uses the Bash bundled alongside Git for Windows.
        git = shutil.which("git")
        executable = Path(git).resolve().parents[1] / "bin" / "bash.exe" if git else None
    else:
        found = shutil.which("bash")
        executable = Path(found) if found else None
    if executable is None or not executable.is_file():
        pytest.skip("workflow shell checks require Bash (Git Bash on Windows)")
    return str(executable)


def test_progress_plugin_runs_with_real_pytest(pytester, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makeconftest(
        "from tests.e2e._progress import E2EProgress\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(E2EProgress())\n"
    )
    pytester.makepyfile(
        "import pytest\n"
        "def test_pass(): pass\n"
        "def test_fail(): assert False, 'example failure'\n"
        "def test_skip(): pytest.skip('example skip')\n"
    )
    result = pytester.runpytest("-s", "-q")
    result.assert_outcomes(passed=1, failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*E2E finished: exit=1 failed=1 passed=1 skipped=1*"])
    assert "failed=1 passed=1 skipped=1" in summary.read_text()


def test_error_is_visible_in_log_annotation_and_summary(monkeypatch, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    report("copy readiness: 50%\nmissing audio", error=True)
    captured = capsys.readouterr()
    assert "missing audio" in captured.err
    assert "::error::copy readiness: 50%25%0Amissing audio" in captured.out
    assert "missing audio" in summary.read_text()


def test_summary_write_failure_preserves_error_diagnostics(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))
    report("original verification failure", error=True)
    assert "original verification failure" in capsys.readouterr().err


@pytest.mark.parametrize(
    "node_id",
    ["secret-id", "tests/e2e/test_one.py::test_one\n::error::injected", "../secret.py::test_one"],
)
def test_untrusted_test_names_are_not_published(node_id):
    assert safe_test_name(node_id) == "unavailable test name"


def test_e2e_progress_handles_reruns_setup_and_teardown_failures(monkeypatch, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    progress = E2EProgress()
    progress.pytest_collection_finish(SimpleNamespace(items=[1, 2, 3]))

    def result(nodeid, when, outcome):
        progress.pytest_runtest_logreport(
            SimpleNamespace(
                nodeid=nodeid,
                when=when,
                outcome=outcome,
                failed=outcome == "failed",
                skipped=outcome == "skipped",
            )
        )

    retry = "tests/e2e/test_one.py::test_retry[private-notebook-id]"
    progress.pytest_runtest_logstart(retry, None)
    progress._report_current()
    result(retry, "call", "rerun")
    progress.pytest_runtest_logfinish(retry, None)
    progress.pytest_runtest_logstart(retry, None)
    result(retry, "call", "passed")
    progress.pytest_runtest_logfinish(retry, None)
    for name, phase in [("test_setup", "setup"), ("test_teardown", "teardown")]:
        nodeid = f"tests/e2e/test_one.py::{name}"
        progress.pytest_runtest_logstart(nodeid, None)
        if phase == "teardown":
            result(nodeid, "call", "passed")
        result(nodeid, phase, "failed")
        progress.pytest_runtest_logfinish(nodeid, None)
    progress.pytest_sessionfinish(None, 1)

    output = capsys.readouterr().out
    rendered = summary.read_text()
    assert "still running:" in output
    assert "failed=2 passed=1 reruns=1" in rendered
    assert "test_setup` | setup" in rendered
    assert "test_teardown` | teardown" in rendered
    assert "private-notebook-id" not in output + rendered


@pytest.mark.parametrize(
    ("lane", "test_filter", "suite"),
    [
        ("readonly", "tests/e2e/test_artifacts.py", "readonly"),
        ("all", "tests/e2e/test_artifacts.py", "omitted"),
        ("all", "", "readonly"),
    ],
)
def test_account_plan_summary_matches_filtered_readonly_selection(
    lane, test_filter, suite, tmp_path, workflow_bash
):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/nightly.yml").read_text())
    command = next(
        step["run"]
        for step in workflow["jobs"]["plan-live-lanes"]["steps"]
        if step.get("name") == "Summarize safe lane selection"
    )
    summary = tmp_path / "summary.md"
    subprocess.run(
        [workflow_bash, "-e", "-o", "pipefail", "-c", command],
        env={
            **os.environ,
            "GITHUB_STEP_SUMMARY": summary.as_posix(),
            "E2E_LANE": lane,
            "TEST_FILTER": test_filter,
            "ENABLED_SLOTS": "A,B",
            "READONLY_SLOT": "B",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert f"| nightly-readonly-windows | Windows | web | {suite} | B |" in summary.read_text()


@pytest.mark.parametrize(
    ("workflow", "job", "step"),
    [
        ("nightly.yml", "e2e", "verifier"),
        ("nightly.yml", "e2e", "sweep"),
        ("nightly.yml", "e2e", "cleanup"),
    ],
)
def test_workflow_streams_output_and_preserves_failure(workflow, job, step, workflow_bash):
    root = Path(__file__).resolve().parents[2]
    data = yaml.safe_load((root / ".github" / "workflows" / workflow).read_text())
    command = next(row["run"] for row in data["jobs"][job]["steps"] if row.get("id") == step)
    command = command.replace("${{ steps.verifier_budget.outputs.timeout }}", "240")
    command = command.replace("${{ matrix.backend }}", "web")
    command = "uv() { printf 'visible progress before failure\\n'; return 6; }\n" + command
    completed = subprocess.run(
        [workflow_bash, "-e", "-o", "pipefail", "-c", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 6
    assert "visible progress before failure" in completed.stdout
