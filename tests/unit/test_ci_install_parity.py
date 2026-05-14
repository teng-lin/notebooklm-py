"""Tests for `scripts/check_ci_install_parity.py`.

Phase 6 / P6.4 — drift catcher between ``.github/workflows/test.yml`` and
``CONTRIBUTING.md`` install commands.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_ci_install_parity.py"

# Import the canonical command from the script so the tests can't drift from the
# actual contract (Codex polish review feedback).
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from check_ci_install_parity import CANONICAL_INSTALL_CMD as CANONICAL  # noqa: E402


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        timeout=30,
    )


def test_passes_on_real_repo_state():
    """Current main has both files in sync."""
    result = _run([])
    assert result.returncode == 0, (
        f"stderr: {result.stderr}\nstdout: {result.stdout}\n"
        "If this fails, either CONTRIBUTING.md or .github/workflows/test.yml has drifted."
    )


def test_detects_workflow_drift(tmp_path):
    """Synthetic test.yml without the canonical install command → exit 1."""
    workflow = tmp_path / "test.yml"
    workflow.write_text("jobs:\n  x:\n    steps:\n      - run: pip install -e .\n")
    contributing = tmp_path / "CONTRIBUTING.md"
    contributing.write_text(f"# Install\n```bash\n{CANONICAL}\n```\n")

    result = _run(["--workflow", str(workflow), "--contributing", str(contributing)])
    assert result.returncode == 1
    assert "test.yml" in result.stderr
    assert "DRIFT" in result.stderr


def test_detects_contributing_drift(tmp_path):
    """Synthetic CONTRIBUTING.md without the canonical command → exit 1."""
    workflow = tmp_path / "test.yml"
    workflow.write_text(f"jobs:\n  x:\n    steps:\n      - run: {CANONICAL}\n")
    contributing = tmp_path / "CONTRIBUTING.md"
    contributing.write_text("# No canonical install here, just prose\n")

    result = _run(["--workflow", str(workflow), "--contributing", str(contributing)])
    assert result.returncode == 1
    assert "CONTRIBUTING.md" in result.stderr
    assert "DRIFT" in result.stderr


def test_missing_workflow_file(tmp_path):
    """Missing test.yml → exit 2 (argument error)."""
    contributing = tmp_path / "CONTRIBUTING.md"
    contributing.write_text(CANONICAL)

    result = _run(
        ["--workflow", str(tmp_path / "missing.yml"), "--contributing", str(contributing)]
    )
    assert result.returncode == 2
    assert "not found" in result.stderr.lower()


def test_missing_contributing_file(tmp_path):
    """Missing CONTRIBUTING.md → exit 2 (symmetric to missing workflow)."""
    workflow = tmp_path / "test.yml"
    workflow.write_text(f"jobs:\n  x:\n    steps:\n      - run: {CANONICAL}\n")

    result = _run(["--workflow", str(workflow), "--contributing", str(tmp_path / "missing.md")])
    assert result.returncode == 2
    assert "not found" in result.stderr.lower()
