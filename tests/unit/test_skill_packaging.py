"""Packaging smoke tests for skill assets."""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _REPO_ROOT / "plugin" / "skills" / "notebooklm"


def _build_wheel(tmp_path: Path) -> Path:
    """Build the wheel into ``tmp_path / "dist"`` and return its path."""
    build_dir = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(build_dir)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return next(build_dir.glob("*.whl"))


def _normalize(text: str) -> str:
    return text.replace("\r", "")


def test_wheel_includes_root_skill_content(tmp_path):
    """The built wheel should carry the whole plugin/skills/notebooklm/ tree
    + AGENTS.md into package data (notebooklm/data/skill/** and
    notebooklm/data/CODEX.md)."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required for build smoke tests")

    wheel_path = _build_wheel(tmp_path)

    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        packaged_skill_entry = wheel.read("notebooklm/data/skill/SKILL.md").decode("utf-8")
        packaged_launcher = wheel.read("notebooklm/data/skill/scripts/nlm").decode("utf-8")
        packaged_codex = wheel.read("notebooklm/data/CODEX.md").decode("utf-8")

    # SKILL.md is byte-equal to the checked-in source.
    assert _normalize(packaged_skill_entry) == _normalize(
        (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    )
    # The launcher script made it in too, byte-equal.
    assert _normalize(packaged_launcher) == _normalize(
        (_SKILL_DIR / "scripts" / "nlm").read_text(encoding="utf-8")
    )
    # At least one references/*.md file is present.
    reference_entries = [
        name
        for name in names
        if name.startswith("notebooklm/data/skill/references/") and name.endswith(".md")
    ]
    assert reference_entries, f"no references/*.md packaged; wheel had: {sorted(names)}"

    assert _normalize(packaged_codex) == _normalize(
        (_REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    )


def test_wheel_skill_tree_is_byte_equal_to_source(tmp_path):
    """Every file under the packaged ``notebooklm/data/skill/`` tree is
    byte-equal to its ``plugin/skills/notebooklm/`` source counterpart, and
    no source file is missing from the wheel."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required for build smoke tests")

    wheel_path = _build_wheel(tmp_path)
    source_files = {
        path.relative_to(_SKILL_DIR).as_posix(): path
        for path in _SKILL_DIR.rglob("*")
        if path.is_file()
    }
    assert source_files, "plugin/skills/notebooklm/ source tree is unexpectedly empty"

    with zipfile.ZipFile(wheel_path) as wheel:
        for rel_path, source_path in source_files.items():
            packaged = wheel.read(f"notebooklm/data/skill/{rel_path}").decode("utf-8")
            assert _normalize(packaged) == _normalize(source_path.read_text(encoding="utf-8")), (
                f"{rel_path} differs between source and packaged wheel"
            )
