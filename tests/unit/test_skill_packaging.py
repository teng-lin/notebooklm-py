"""Packaging smoke tests for release contents and skill assets."""

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest


@pytest.fixture(scope="module")
def release_archives(tmp_path_factory):
    """Build the sdist and its wheel through the normal release path."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required for build smoke tests")

    repo_root = Path(__file__).resolve().parents[2]
    build_dir = tmp_path_factory.mktemp("release-dist")
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(build_dir)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    return repo_root, next(build_dir.glob("*.tar.gz")), next(build_dir.glob("*.whl"))


def test_wheel_includes_root_skill_content(release_archives):
    """The built wheel should carry the canonical repo agent docs into package data."""
    repo_root, _, wheel_path = release_archives
    with zipfile.ZipFile(wheel_path) as wheel:
        packaged_skill = wheel.read("notebooklm/data/SKILL.md").decode("utf-8")
        packaged_codex = wheel.read("notebooklm/data/CODEX.md").decode("utf-8")

    assert packaged_skill.replace("\r", "") == (repo_root / "SKILL.md").read_text(
        encoding="utf-8"
    ).replace("\r", "")
    assert packaged_codex.replace("\r", "") == (repo_root / "AGENTS.md").read_text(
        encoding="utf-8"
    ).replace("\r", "")


def test_release_archives_exclude_repository_directories(release_archives):
    """Neither release archive should ship tests or separate deployment assets."""
    _, sdist_path, wheel_path = release_archives
    excluded = {"tests", "examples", "desktop-extension", "deploy"}

    with tarfile.open(sdist_path) as sdist:
        # Source distributions wrap their contents in a versioned root directory.
        sdist_paths = [PurePosixPath(member.name).parts[1:] for member in sdist]
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_paths = [PurePosixPath(name).parts for name in wheel.namelist()]

    for archive_path, paths in ((sdist_path, sdist_paths), (wheel_path, wheel_paths)):
        unexpected = ["/".join(parts) for parts in paths if parts and parts[0] in excluded]
        assert not unexpected, f"{archive_path.name} includes excluded files: {unexpected[:10]}"


def test_sdist_retains_build_inputs_and_embedded_commit(release_archives):
    """A wheel rebuilt from the sdist keeps runtime files and its build commit."""
    _, sdist_path, wheel_path = release_archives
    required = {
        "pyproject.toml",
        "hatch_build.py",
        "README.md",
        "LICENSE",
        "SKILL.md",
        "AGENTS.md",
        "src/notebooklm/__init__.py",
        "src/notebooklm/py.typed",
        "src/notebooklm/_commit.py",
    }
    with tarfile.open(sdist_path) as sdist:
        members = {member.name.partition("/")[2]: member for member in sdist}
        assert required <= members.keys()
        commit_file = sdist.extractfile(members["src/notebooklm/_commit.py"])
        assert commit_file is not None
        embedded_commit = commit_file.read()

    with zipfile.ZipFile(wheel_path) as wheel:
        assert {"notebooklm/__init__.py", "notebooklm/py.typed"} <= set(wheel.namelist())
        assert wheel.read("notebooklm/_commit.py") == embedded_commit
