"""Tests for the Phase 1 CI audit scripts.

Covers `scripts/check_workflow_permissions.py` and `scripts/check_coverage_thresholds.py`.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# check_workflow_permissions.py
# ---------------------------------------------------------------------------


def test_workflow_permissions_passes_on_real_state():
    """Current .github/workflows passes the check (Phase 1 added the blocks)."""
    result = _run([str(SCRIPTS / "check_workflow_permissions.py")])
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"


def test_workflow_permissions_detects_missing_block(tmp_path):
    """Synthetic workflow without top-level permissions → exit 1."""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "bad.yml").write_text(
        textwrap.dedent(
            """\
            name: Bad
            on:
              push:
                branches: [main]
            jobs:
              x:
                runs-on: ubuntu-latest
                steps:
                  - run: echo hi
            """
        )
    )
    result = _run([str(SCRIPTS / "check_workflow_permissions.py"), "--workflow-dir", str(wf_dir)])
    assert result.returncode == 1
    assert "bad.yml" in result.stderr


@pytest.mark.parametrize(
    "body",
    [
        "permissions: write-all\n",  # inline string value
        "permissions: read-all\n",  # inline — still not a scoped block
        "permissions: {}\n",  # inline empty map
        "permissions:\n  contents: write\n",  # non-read value
        "permissions:\n  contents: read\n  issues: write\n",  # mixed
        "permissions:\n  unknown-scope: read\n",  # unknown scope
    ],
)
def test_workflow_permissions_rejects_bypass_attempts(tmp_path, body):
    """Inline strings, write-all, mixed scopes, and unknown scopes all fail."""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "sneaky.yml").write_text(
        "name: Sneaky\non: push\n" + body + "jobs:\n  x:\n    runs-on: ubuntu-latest\n"
    )
    result = _run([str(SCRIPTS / "check_workflow_permissions.py"), "--workflow-dir", str(wf_dir)])
    assert result.returncode == 1
    assert "sneaky.yml" in result.stderr


def test_workflow_permissions_accepts_quoted_values(tmp_path):
    """`contents: 'read'` and `contents: "read"` are valid YAML and must pass."""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "quoted.yml").write_text(
        "name: Quoted\non: push\n"
        "permissions:\n"
        "  contents: 'read'\n"
        '  issues: "none"\n'
        "jobs:\n  x:\n    runs-on: ubuntu-latest\n"
    )
    result = _run([str(SCRIPTS / "check_workflow_permissions.py"), "--workflow-dir", str(wf_dir)])
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_workflow_permissions_globs_yaml_extension(tmp_path):
    """`.yaml` files are also scanned (GitHub Actions accepts both)."""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "missing.yaml").write_text(
        "name: NoPerms\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n"
    )
    result = _run([str(SCRIPTS / "check_workflow_permissions.py"), "--workflow-dir", str(wf_dir)])
    assert result.returncode == 1
    assert "missing.yaml" in result.stderr


def test_workflow_permissions_allowlist(tmp_path):
    """codeql.yml is allowlisted; a workflow without block but named codeql.yml passes."""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "codeql.yml").write_text(
        textwrap.dedent(
            """\
            name: CodeQL
            on: push
            jobs:
              analyze:
                runs-on: ubuntu-latest
                permissions:
                  security-events: write
                steps:
                  - run: echo hi
            """
        )
    )
    result = _run([str(SCRIPTS / "check_workflow_permissions.py"), "--workflow-dir", str(wf_dir)])
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# check_coverage_thresholds.py
# ---------------------------------------------------------------------------


def test_coverage_thresholds_passes_on_real_state():
    """Current pyproject.toml + test.yml agree on the coverage threshold."""
    result = _run([str(SCRIPTS / "check_coverage_thresholds.py")])
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"


def test_coverage_thresholds_ignores_commented_cov_fail_under(tmp_path):
    """A commented `# --cov-fail-under=70` must not shadow the active value."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        textwrap.dedent(
            """\
            [tool.coverage.report]
            fail_under = 90
            """
        )
    )
    workflow = tmp_path / "test.yml"
    # The commented line should be ignored; the live line agrees with pyproject.
    workflow.write_text(
        "jobs:\n"
        "  x:\n"
        "    steps:\n"
        "      # Historical: --cov-fail-under=70\n"
        "      - run: pytest --cov-fail-under=90\n"
    )
    result = _run(
        [
            str(SCRIPTS / "check_coverage_thresholds.py"),
            "--pyproject",
            str(pyproject),
            "--workflow",
            str(workflow),
        ]
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_coverage_thresholds_catches_divergent_second_occurrence(tmp_path):
    """Multiple --cov-fail-under occurrences must all match (not just the first)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.coverage.report]\nfail_under = 90\n")
    workflow = tmp_path / "test.yml"
    # First occurrence matches; second diverges. Old re.search would miss it.
    workflow.write_text(
        "jobs:\n"
        "  a:\n    steps:\n      - run: pytest --cov-fail-under=90\n"
        "  b:\n    steps:\n      - run: pytest --cov-fail-under=70\n"
    )
    result = _run(
        [
            str(SCRIPTS / "check_coverage_thresholds.py"),
            "--pyproject",
            str(pyproject),
            "--workflow",
            str(workflow),
        ]
    )
    assert result.returncode == 1
    assert "DRIFT" in result.stderr
    assert "--cov-fail-under=70" in result.stderr


def test_coverage_thresholds_detects_drift(tmp_path):
    """Synthetic mismatched thresholds → exit 1 with drift message."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        textwrap.dedent(
            """\
            [tool.coverage.report]
            fail_under = 90
            """
        )
    )
    workflow = tmp_path / "test.yml"
    workflow.write_text("jobs:\n  x:\n    steps:\n      - run: pytest --cov-fail-under=70\n")

    result = _run(
        [
            str(SCRIPTS / "check_coverage_thresholds.py"),
            "--pyproject",
            str(pyproject),
            "--workflow",
            str(workflow),
        ]
    )
    assert result.returncode == 1
    assert "DRIFT" in result.stderr
    assert "fail_under=90" in result.stderr
    assert "--cov-fail-under=70" in result.stderr
