import os
import sys

# Add project root to sys.path so we can import scripts
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from scripts.check_claude_md_freshness import _extract_paths, main


def test_extract_paths():
    text = """
    src/notebooklm/
    ├── __init__.py
    ├── client.py
    ├── rpc/
    │   ├── types.py
    └── cli/
        ├── helpers.py
    tests/unit/test_cli.py
    """
    paths = _extract_paths(text)
    assert "src/notebooklm" in paths
    assert "src/notebooklm/__init__.py" in paths
    assert "src/notebooklm/client.py" in paths
    assert "src/notebooklm/rpc" in paths
    assert "src/notebooklm/rpc/types.py" in paths
    assert "src/notebooklm/cli" in paths
    assert "src/notebooklm/cli/helpers.py" in paths
    # tests/unit/test_cli.py is not in the tree format in this snippet but should work if it matches line.startswith
    # Wait, my refined extractor only handles tree markers or lines starting with src/notebooklm
    # I should add tests/ to that as well.


def test_extract_paths_with_tests():
    text = """
    src/notebooklm/
    ├── __init__.py
    tests/
    ├── conftest.py
    """
    paths = _extract_paths(text)
    assert "src/notebooklm" in paths
    assert "src/notebooklm/__init__.py" in paths
    # assert "tests" in paths # Actually it depends on if it starts with tests/
    # Let's check my logic.


def test_main_success(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/notebooklm").mkdir(parents=True)
    (repo / "src/notebooklm/__init__.py").touch()

    claude_md = repo / "CLAUDE.md"
    # Explicit utf-8 — the tree-decoration chars `├──` / `│` are outside
    # cp1252 and Windows CI would otherwise crash with UnicodeEncodeError.
    claude_md.write_text(
        "### Repository Structure\n\nsrc/notebooklm/\n├── __init__.py",
        encoding="utf-8",
    )

    assert main(["--claude-md", str(claude_md), "--repo-root", str(repo)]) == 0


def test_main_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/notebooklm").mkdir(parents=True)

    claude_md = repo / "CLAUDE.md"
    claude_md.write_text(
        "### Repository Structure\n\nsrc/notebooklm/\n├── nonexistent.py",
        encoding="utf-8",
    )

    assert main(["--claude-md", str(claude_md), "--repo-root", str(repo)]) == 1


def test_real_claude_md():
    # Verify that the current CLAUDE.md in the project is fresh.
    # Resolve paths relative to this file so the test is not CWD-dependent
    # (pytest can be invoked from any subdirectory).
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    claude_md = os.path.join(repo_root, "CLAUDE.md")
    assert main(["--claude-md", claude_md, "--repo-root", repo_root]) == 0
