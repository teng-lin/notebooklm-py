"""Regression tests for E2E auto-marking order."""

from __future__ import annotations

import textwrap

import pytest

pytest_plugins = ["pytester"]


def test_e2e_auto_marker_is_visible_to_marker_selection_and_root_fixture(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyprojecttoml(
        """
        [tool.pytest.ini_options]
        markers = ["e2e: end-to-end tests"]
        """
    )
    pytester.makeconftest(
        textwrap.dedent(
            """
            import pytest
            from pathlib import Path

            E2E_TEST_DIR = Path(__file__).resolve().parent / "tests" / "e2e"

            def _is_path_under(path: Path, directory: Path) -> bool:
                try:
                    path.resolve().relative_to(directory.resolve())
                except ValueError:
                    return False
                return True

            def pytest_itemcollected(item):
                if _is_path_under(Path(item.path), E2E_TEST_DIR):
                    item.add_marker(pytest.mark.e2e)

            @pytest.fixture(autouse=True)
            def root_home_isolation(request):
                request.node._saw_e2e_marker_at_setup = (
                    request.node.get_closest_marker("e2e") is not None
                )
            """
        )
    )
    (pytester.path / "tests" / "e2e").mkdir(parents=True)
    e2e_test = pytester.path / "tests" / "e2e" / "test_marker_contract.py"
    e2e_test.write_text(
        textwrap.dedent(
            """
            def test_marker_seen_by_fixture(request):
                assert request.node.get_closest_marker("e2e") is not None
                assert request.node._saw_e2e_marker_at_setup is True
            """
        ),
        encoding="utf-8",
    )

    result = pytester.runpytest("-q", "tests/e2e", "-m", "e2e")

    result.assert_outcomes(passed=1)


def test_e2e_auto_marker_makes_not_e2e_select_nothing(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(
        """
        [tool.pytest.ini_options]
        markers = ["e2e: end-to-end tests"]
        """
    )
    pytester.makeconftest(
        textwrap.dedent(
            """
            import pytest
            from pathlib import Path

            E2E_TEST_DIR = Path(__file__).resolve().parent / "tests" / "e2e"

            def pytest_itemcollected(item):
                try:
                    Path(item.path).resolve().relative_to(E2E_TEST_DIR.resolve())
                except ValueError:
                    return
                item.add_marker(pytest.mark.e2e)
            """
        )
    )
    (pytester.path / "tests" / "e2e").mkdir(parents=True)
    e2e_test = pytester.path / "tests" / "e2e" / "test_marker_contract.py"
    e2e_test.write_text("def test_e2e(): pass\n", encoding="utf-8")

    result = pytester.runpytest("-q", "tests/e2e", "-m", "not e2e")

    result.assert_outcomes(deselected=1)
