import os
import sys

import pytest

# Add project root to sys.path so we can import scripts
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from scripts.check_claude_md_freshness import (
    _extract_intentional_omissions,
    _extract_paths,
    _extract_unreasoned_omissions,
    _repository_structure_section,
    main,
)

pytestmark = pytest.mark.repo_lint


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
    assert "tests/unit/test_cli.py" in paths


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
    assert "tests" in paths
    assert "tests/conftest.py" in paths


def test_extract_intentional_omissions_requires_reason():
    text = """
    ### Repository Structure Intentional Omissions

    - `src/notebooklm/_compat.py` - Transitional compatibility shim.
    - `src/notebooklm/_missing_reason.py`
    """

    assert _extract_intentional_omissions(text) == {
        "src/notebooklm/_compat.py": "Transitional compatibility shim."
    }


def test_extract_unreasoned_omissions_ignores_general_notes():
    text = """
    ### Repository Structure Intentional Omissions

    - Note: omit `src/notebooklm/_small_helper.py` because it is small.
    - `src/notebooklm/_missing_reason.py`
    """

    assert _extract_unreasoned_omissions(text) == ["- `src/notebooklm/_missing_reason.py`"]


def test_repository_structure_heading_must_match_exactly():
    text = """
    ### Repository Structure Intentional Omissions

    - `src/notebooklm/not_a_documented_path.py` - This section appears first.

    ### Repository Structure

    src/notebooklm/
    ├── __init__.py
    """

    paths = _extract_paths(_repository_structure_section(text))
    assert "src/notebooklm/__init__.py" in paths
    assert "src/notebooklm/not_a_documented_path.py" not in paths


def test_main_success(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/notebooklm").mkdir(parents=True)
    (repo / "src/notebooklm/__init__.py").touch()
    (repo / "src/notebooklm/client.py").touch()
    (repo / "src/notebooklm/_small_helper.py").touch()

    claude_md = repo / "CLAUDE.md"
    # Explicit utf-8 — the tree-decoration chars `├──` / `│` are outside
    # cp1252 and Windows CI would otherwise crash with UnicodeEncodeError.
    claude_md.write_text(
        """
        ### Repository Structure

        src/notebooklm/
        ├── __init__.py
        ├── client.py

        ### Repository Structure Intentional Omissions

        - `src/notebooklm/_small_helper.py` - Small helper covered by client.py.
        """,
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


def test_main_fails_for_undocumented_top_level_module(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/notebooklm").mkdir(parents=True)
    (repo / "src/notebooklm/__init__.py").touch()
    (repo / "src/notebooklm/_new_important_module.py").touch()

    claude_md = repo / "CLAUDE.md"
    claude_md.write_text(
        "### Repository Structure\n\nsrc/notebooklm/\n├── __init__.py",
        encoding="utf-8",
    )

    assert main(["--claude-md", str(claude_md), "--repo-root", str(repo)]) == 1


def test_main_allows_intentional_omission_with_reason(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/notebooklm").mkdir(parents=True)
    (repo / "src/notebooklm/__init__.py").touch()
    (repo / "src/notebooklm/_small_helper.py").touch()

    claude_md = repo / "CLAUDE.md"
    claude_md.write_text(
        """
        ### Repository Structure

        src/notebooklm/
        ├── __init__.py

        ### Repository Structure Intentional Omissions

        These helpers from `src/notebooklm/` are intentionally omitted
        when a subsystem row already covers them:

        - `src/notebooklm/_small_helper.py` - Small helper covered by the subsystem row.
        """,
        encoding="utf-8",
    )

    assert main(["--claude-md", str(claude_md), "--repo-root", str(repo)]) == 0


def test_main_fails_for_intentional_omission_without_reason(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/notebooklm").mkdir(parents=True)
    (repo / "src/notebooklm/__init__.py").touch()
    (repo / "src/notebooklm/_small_helper.py").touch()

    claude_md = repo / "CLAUDE.md"
    claude_md.write_text(
        """
        ### Repository Structure

        src/notebooklm/
        ├── __init__.py

        ### Repository Structure Intentional Omissions

        - `src/notebooklm/_small_helper.py`
        """,
        encoding="utf-8",
    )

    assert main(["--claude-md", str(claude_md), "--repo-root", str(repo)]) == 1


def test_main_does_not_double_report_unreasoned_omission(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/notebooklm").mkdir(parents=True)
    (repo / "src/notebooklm/__init__.py").touch()
    (repo / "src/notebooklm/_small_helper.py").touch()

    claude_md = repo / "CLAUDE.md"
    claude_md.write_text(
        """
        ### Repository Structure

        src/notebooklm/
        ├── __init__.py

        ### Repository Structure Intentional Omissions

        - `src/notebooklm/_small_helper.py`
        """,
        encoding="utf-8",
    )

    assert main(["--claude-md", str(claude_md), "--repo-root", str(repo)]) == 1
    captured = capsys.readouterr()
    assert "Intentional omissions without parseable reasons:" in captured.err
    assert "Undocumented top-level src/notebooklm modules/packages:" not in captured.err


def test_main_fails_for_stale_intentional_omission(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src/notebooklm").mkdir(parents=True)
    (repo / "src/notebooklm/__init__.py").touch()

    claude_md = repo / "CLAUDE.md"
    claude_md.write_text(
        """
        ### Repository Structure

        src/notebooklm/
        ├── __init__.py

        ### Repository Structure Intentional Omissions

        - `src/notebooklm/_deleted_helper.py` - Deleted helper retained by mistake.
        """,
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
