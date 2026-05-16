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
    """Current main has both files in sync — and every installation.md block
    is mirrored or explicitly marked.
    """
    result = _run([])
    assert result.returncode == 0, (
        f"stderr: {result.stderr}\nstdout: {result.stdout}\n"
        "If this fails, either CONTRIBUTING.md, .github/workflows/test.yml, "
        "or docs/installation.md has drifted."
    )


def test_detects_workflow_drift(tmp_path):
    """Synthetic test.yml without the canonical install command → exit 1."""
    workflow = tmp_path / "test.yml"
    workflow.write_text("jobs:\n  x:\n    steps:\n      - run: pip install -e .\n")
    contributing = tmp_path / "CONTRIBUTING.md"
    contributing.write_text(f"# Install\n```bash\n{CANONICAL}\n```\n")

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--skip-block-mirror",
        ]
    )
    assert result.returncode == 1
    assert "test.yml" in result.stderr
    assert "DRIFT" in result.stderr


def test_detects_contributing_drift(tmp_path):
    """Synthetic CONTRIBUTING.md without the canonical command → exit 1."""
    workflow = tmp_path / "test.yml"
    workflow.write_text(f"jobs:\n  x:\n    steps:\n      - run: {CANONICAL}\n")
    contributing = tmp_path / "CONTRIBUTING.md"
    contributing.write_text("# No canonical install here, just prose\n")

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--skip-block-mirror",
        ]
    )
    assert result.returncode == 1
    assert "CONTRIBUTING.md" in result.stderr
    assert "DRIFT" in result.stderr


def test_missing_workflow_file(tmp_path):
    """Missing test.yml → exit 2 (argument error)."""
    contributing = tmp_path / "CONTRIBUTING.md"
    contributing.write_text(CANONICAL)

    result = _run(
        [
            "--workflow",
            str(tmp_path / "missing.yml"),
            "--contributing",
            str(contributing),
            "--skip-block-mirror",
        ]
    )
    assert result.returncode == 2
    assert "not found" in result.stderr.lower()


def test_missing_contributing_file(tmp_path):
    """Missing CONTRIBUTING.md → exit 2 (symmetric to missing workflow)."""
    workflow = tmp_path / "test.yml"
    workflow.write_text(f"jobs:\n  x:\n    steps:\n      - run: {CANONICAL}\n")

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(tmp_path / "missing.md"),
            "--skip-block-mirror",
        ]
    )
    assert result.returncode == 2
    assert "not found" in result.stderr.lower()


# ---------------------------------------------------------------------------
# block-mirror policy
# ---------------------------------------------------------------------------


def _make_synthetic_setup(tmp_path: Path, *, install_md: str, contributing_md: str):
    """Create a synthetic install/contributing pair plus a workflow that already
    contains the canonical install command (so the original Phase-1 check
    passes and the test exercises the block-mirror policy in isolation).
    """
    workflow = tmp_path / "test.yml"
    workflow.write_text(f"jobs:\n  x:\n    steps:\n      - run: {CANONICAL}\n")
    installation = tmp_path / "installation.md"
    installation.write_text(install_md)
    contributing = tmp_path / "CONTRIBUTING.md"
    contributing.write_text(contributing_md)
    return workflow, installation, contributing


def test_block_mirror_unmirrored_block_fails(tmp_path):
    """An installation.md bash block that's neither mirrored nor marked → exit 1."""
    install_md = "# Install\n\n```bash\necho hello\n```\n"
    contributing_md = f"# Contributing\n\n```bash\n{CANONICAL}\n```\n"
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
        ]
    )
    assert result.returncode == 1
    assert "BLOCK-MIRROR DRIFT" in result.stderr
    assert "echo hello" in result.stderr


def test_block_mirror_marker_passes(tmp_path):
    """A bash block preceded by ``<!-- not mirrored: ... -->`` passes."""
    install_md = (
        "# Install\n\n<!-- not mirrored: end-user pip install -->\n```bash\necho hello\n```\n"
    )
    contributing_md = f"# Contributing\n\n```bash\n{CANONICAL}\n```\n"
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
        ]
    )
    assert result.returncode == 0
    assert "1 bash block" in result.stdout


def test_block_mirror_verbatim_in_contributing_passes(tmp_path):
    """A bash block whose body appears verbatim in CONTRIBUTING.md passes."""
    install_md = "# Install\n\n```bash\necho hello\necho world\n```\n"
    contributing_md = (
        f"# Contributing\n\n```bash\n{CANONICAL}\n```\n\n"
        "## Mirrored from installation.md\n\n```bash\necho hello\necho world\n```\n"
    )
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
        ]
    )
    assert result.returncode == 0


def test_block_mirror_marker_blank_line_above_passes(tmp_path):
    """The marker is allowed to sit above blank lines before the fence."""
    install_md = (
        "# Install\n\n<!-- not mirrored: tolerated blank gap -->\n\n\n```bash\necho hello\n```\n"
    )
    contributing_md = f"```bash\n{CANONICAL}\n```\n"
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
        ]
    )
    assert result.returncode == 0


def test_block_mirror_indented_block_in_list_recognized(tmp_path):
    """Bash blocks inside numbered/bulleted lists (3-space indent) parse correctly."""
    install_md = (
        "# Install\n\n"
        "1. Step one:\n"
        "   <!-- not mirrored: numbered-list step -->\n"
        "   ```bash\n"
        "   echo indented\n"
        "   ```\n"
    )
    contributing_md = f"```bash\n{CANONICAL}\n```\n"
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
        ]
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_block_mirror_substring_only_in_prose_fails(tmp_path):
    """Body must appear inside a fenced bash block in CONTRIBUTING.md, not in prose.

    The previous (looser) substring check would let an installation block
    pass when its body coincidentally appeared inside an unrelated prose
    paragraph or a different code fence. This test pins the stricter
    behavior: only fenced bash bodies count as a mirror.
    """
    install_md = "# Install\n\n```bash\necho hello\n```\n"
    contributing_md = (
        f"# Contributing\n\n```bash\n{CANONICAL}\n```\n\n"
        # Same body text, but inside prose (not a fenced bash block) — must NOT count.
        "Some prose mentioning `echo hello` inline.\n"
    )
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )
    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
        ]
    )
    assert result.returncode == 1
    assert "BLOCK-MIRROR DRIFT" in result.stderr


def test_block_mirror_no_bash_blocks_warns_and_passes(tmp_path):
    """When installation.md has no bash blocks at all, WARN to stderr and pass."""
    install_md = "# Install\n\nJust prose, no fenced bash blocks here.\n"
    contributing_md = f"```bash\n{CANONICAL}\n```\n"
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )
    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
        ]
    )
    assert result.returncode == 0
    assert "no fenced ``bash`` blocks" in result.stderr


def test_block_mirror_trailing_whitespace_on_closing_fence(tmp_path):
    """A closing fence with trailing whitespace must still terminate the block."""
    # Note: trailing spaces after the closing ```. Without rstrip tolerance the
    # parser would slurp the rest of the document into one giant block.
    install_md = (
        "# Install\n\n"
        "<!-- not mirrored: synthetic -->\n"
        "```bash\necho hello\n```   \n\n"
        "## Next section (must remain outside the block)\n"
    )
    contributing_md = f"```bash\n{CANONICAL}\n```\n"
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )
    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
        ]
    )
    assert result.returncode == 0


def test_skip_block_mirror_flag_bypasses_extension(tmp_path):
    """``--skip-block-mirror`` runs only the original Phase-1 check."""
    install_md = "# Install\n\n```bash\necho not-mirrored\n```\n"
    contributing_md = f"```bash\n{CANONICAL}\n```\n"
    workflow, installation, contributing = _make_synthetic_setup(
        tmp_path, install_md=install_md, contributing_md=contributing_md
    )

    result = _run(
        [
            "--workflow",
            str(workflow),
            "--contributing",
            str(contributing),
            "--installation",
            str(installation),
            "--skip-block-mirror",
        ]
    )
    assert result.returncode == 0
