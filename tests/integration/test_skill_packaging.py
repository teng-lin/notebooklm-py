"""Packaging smoke tests for skill assets."""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

# wheel-build smoke test; no HTTP, no cassette. Opt out of the
# tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def test_wheel_includes_root_skill_content(tmp_path):
    """The built wheel should carry the canonical repo agent docs into package data."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required for build smoke tests")

    repo_root = Path(__file__).resolve().parents[2]
    build_dir = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(build_dir)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    wheel_path = next(build_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel_path) as wheel:
        packaged_skill = wheel.read("notebooklm/data/SKILL.md").decode("utf-8")
        packaged_codex = wheel.read("notebooklm/data/CODEX.md").decode("utf-8")

    assert packaged_skill.replace("\r", "") == (repo_root / "SKILL.md").read_text(
        encoding="utf-8"
    ).replace("\r", "")
    assert packaged_codex.replace("\r", "") == (repo_root / "AGENTS.md").read_text(
        encoding="utf-8"
    ).replace("\r", "")
