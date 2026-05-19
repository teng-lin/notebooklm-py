"""pytester regression test for the ``requires_playwright`` marker hook.

``tests/conftest.py`` registers a ``pytest_collection_modifyitems`` hook that
auto-skips items carrying ``@pytest.mark.requires_playwright`` when the
``playwright`` Python package is not installed. This file pins that behavior
end-to-end via ``pytester`` so the hook does not silently regress.

Three scenarios:

1. **Playwright installed** — a marked test runs normally (no skip).
2. **Playwright missing** — the same marked test is skipped with the
   install-hint reason.
3. **Playwright missing + unmarked test** — the hook leaves it alone.

Each scenario builds a self-contained synthetic project via ``pytester`` and
inlines a faithful copy of the hook so the regression test does NOT depend on
importing the real conftest. The inlined hook is kept in lockstep with the
real one — if the production hook's detection logic changes, update
``HOOK_SOURCE`` here too.
"""

from __future__ import annotations

import textwrap

import pytest

pytest_plugins = ["pytester"]


# Faithful copy of the hook + the ``_PLAYWRIGHT_INSTALLED`` constant from
# ``tests/conftest.py``. The constant is parameterized via an env var so the
# scenarios below can flip it without mutating the real module. The
# production constant uses ``importlib.util.find_spec("playwright")`` — a
# pure boolean, equivalently shaped by the env var here.
HOOK_SOURCE = textwrap.dedent(
    """
    import os
    import pytest

    _PLAYWRIGHT_INSTALLED = os.environ.get("FAKE_PLAYWRIGHT_INSTALLED") == "1"


    def pytest_configure(config):
        config.addinivalue_line(
            "markers", "requires_playwright: skip when playwright is missing"
        )


    def pytest_collection_modifyitems(config, items):
        if _PLAYWRIGHT_INSTALLED:
            return
        skip_marker = pytest.mark.skip(
            reason="playwright not installed; install with: uv sync --extra browser"
        )
        for item in items:
            if "requires_playwright" in item.keywords:
                item.add_marker(skip_marker)
    """
).strip()


MARKED_TEST = textwrap.dedent(
    """
    import pytest


    @pytest.mark.requires_playwright
    def test_marked():
        assert True
    """
).strip()


UNMARKED_TEST = textwrap.dedent(
    """
    def test_unmarked():
        assert True
    """
).strip()


PYTEST_INI = textwrap.dedent(
    """
    [pytest]
    asyncio_default_fixture_loop_scope = function
    """
).strip()


def _scaffold(pytester: pytest.Pytester, *, test_body: str) -> None:
    pytester.makeini(PYTEST_INI)
    # ``pytester.makepyfile`` appends ``.py`` internally via
    # ``Path.with_suffix(".py")``, so keys are passed *without* the
    # extension (idiomatic per the pytest docs).
    pytester.makepyfile(conftest=HOOK_SOURCE, test_under_test=test_body)


def test_marker_is_noop_when_playwright_installed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_PLAYWRIGHT_INSTALLED = True`` → marked test runs normally."""
    monkeypatch.setenv("FAKE_PLAYWRIGHT_INSTALLED", "1")
    _scaffold(pytester, test_body=MARKED_TEST)
    result = pytester.runpytest("-v")
    assert result.ret == 0
    result.assert_outcomes(passed=1)


def test_marker_skips_when_playwright_missing(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_PLAYWRIGHT_INSTALLED = False`` → marked test is skipped with the install hint."""
    monkeypatch.setenv("FAKE_PLAYWRIGHT_INSTALLED", "0")
    _scaffold(pytester, test_body=MARKED_TEST)
    result = pytester.runpytest("-v", "-rs")
    assert result.ret == 0
    result.assert_outcomes(skipped=1)
    # Skip reason must surface the install hint so users know how to enable.
    assert "uv sync --extra browser" in result.stdout.str()


def test_unmarked_test_unaffected_when_playwright_missing(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook only touches marked items — unmarked tests run regardless.

    Guards against the hook accidentally over-reaching to every test in the
    suite when playwright is missing.
    """
    monkeypatch.setenv("FAKE_PLAYWRIGHT_INSTALLED", "0")
    _scaffold(pytester, test_body=UNMARKED_TEST)
    result = pytester.runpytest("-v")
    assert result.ret == 0
    result.assert_outcomes(passed=1)
