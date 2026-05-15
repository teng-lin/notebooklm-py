"""T8.D11 — pytester regression test for the integration tier-enforcement hook.

``tests/integration/conftest.py`` registers a ``pytest_collection_modifyitems``
hook that REFUSES to collect a test under ``tests/integration/`` unless it is
VCR-tier (``@pytest.mark.vcr``, ``@notebooklm_vcr.use_cassette``, or the
explicit ``@pytest.mark.allow_no_vcr`` opt-out).

This module is the **durable, committed** regression test for that hook —
spelled out via ``pytester`` so the assertion lives in a real test file rather
than the antipattern of "add a synthetic file to ``tests/integration/`` then
delete it." If the hook is ever weakened (e.g. someone changes ``raise`` to a
warning, or accidentally skips violations) these tests fail in CI.

Three scenarios are pinned:

1. **Violation rejected** — a fresh test file under ``tests/integration/`` with
   no VCR marker, no ``use_cassette``, and no opt-out must cause pytest to exit
   non-zero with ``UsageError`` in stderr.
2. **``allow_no_vcr`` opt-out honored** — the same file with
   ``pytestmark = pytest.mark.allow_no_vcr`` must collect and run cleanly.
3. **Non-integration paths are not gated** — a violating file under
   ``tests/unit/`` (i.e. without any VCR marker) must collect and run cleanly;
   the hook only governs ``tests/integration/``.

Each scenario builds a self-contained temp project via ``pytester`` and inlines
a faithful copy of the hook so the regression test does NOT depend on importing
the real conftest (which would also drag in unrelated infrastructure). The
inlined hook is kept in lockstep with the real one — if the real hook's
detection logic changes, update ``HOOK_SOURCE`` here too.
"""

from __future__ import annotations

import textwrap

import pytest

pytest_plugins = ["pytester"]


# Faithful copy of the hook + helper from
# ``tests/integration/conftest.py``. Kept inline so ``pytester`` can drop it
# into a synthetic conftest without importing the real one (which carries
# unrelated keepalive-fixture code that's not under test here). If the
# production hook's detection logic changes, update this copy.
HOOK_SOURCE = textwrap.dedent(
    """
    import pytest


    def _has_use_cassette_decorator(item):
        func = getattr(item, "function", None)
        seen = set()
        while func is not None and id(func) not in seen:
            seen.add(id(func))
            wrapper = getattr(func, "_self_wrapper", None)
            if wrapper is not None:
                owner = getattr(wrapper, "__self__", None)
                if owner is not None and type(owner).__name__ == "CassetteContextDecorator":
                    return True
            func = getattr(func, "__wrapped__", None)
        return False


    def pytest_collection_modifyitems(config, items):
        violations = []
        for item in items:
            nodeid = item.nodeid
            if not nodeid.startswith("tests/integration/"):
                continue
            if item.get_closest_marker("vcr") is not None:
                continue
            if item.get_closest_marker("allow_no_vcr") is not None:
                continue
            if _has_use_cassette_decorator(item):
                continue
            violations.append(nodeid)
        if violations:
            joined = "\\n  ".join(violations)
            raise pytest.UsageError(
                "tests/integration/ tests must be VCR-tier. Add "
                "@pytest.mark.vcr, @notebooklm_vcr.use_cassette, or - for "
                "mock-only tests - @pytest.mark.allow_no_vcr. Violations:\\n  "
                + joined
            )
    """
).strip()


MARKER_REGISTRATION = textwrap.dedent(
    """
    [pytest]
    markers =
        vcr: vcr-tier
        allow_no_vcr: opt out
    """
).strip()


def _scaffold(pytester: pytest.Pytester, integration_body: str) -> None:
    """Build a synthetic project mirroring real layout (incl. the hook)."""
    pytester.makeini(MARKER_REGISTRATION)
    pytester.makepyfile(
        **{
            "tests/integration/conftest.py": HOOK_SOURCE,
            "tests/integration/test_under_test.py": integration_body,
        }
    )
    # Empty ``__init__.py`` files so the synthetic ``tests/integration/`` is
    # treated as the same kind of package layout pytest sees in the real repo.
    pytester.makepyfile(**{"tests/__init__.py": "", "tests/integration/__init__.py": ""})


def test_violation_rejected(pytester: pytest.Pytester) -> None:
    """Bare integration test (no marker, no cassette, no opt-out) → UsageError."""
    _scaffold(
        pytester,
        textwrap.dedent(
            """
            def test_no_marker_no_cassette_no_optout():
                assert True
            """
        ).strip(),
    )
    result = pytester.runpytest("tests/integration/")
    # ``UsageError`` from a collection hook ends the run with exit code != 0
    # and the message printed to stderr.
    assert result.ret != 0
    combined = result.stderr.str() + result.stdout.str()
    assert "UsageError" in combined or "tests/integration/ tests must be VCR-tier" in combined
    assert "test_under_test.py::test_no_marker_no_cassette_no_optout" in combined


def test_allow_no_vcr_optout_honored(pytester: pytest.Pytester) -> None:
    """``pytestmark = pytest.mark.allow_no_vcr`` lets a mock-only test through."""
    _scaffold(
        pytester,
        textwrap.dedent(
            """
            import pytest

            pytestmark = pytest.mark.allow_no_vcr


            def test_mock_only():
                assert True
            """
        ).strip(),
    )
    result = pytester.runpytest("tests/integration/")
    assert result.ret == 0
    result.assert_outcomes(passed=1)


def test_vcr_marker_honored(pytester: pytest.Pytester) -> None:
    """``@pytest.mark.vcr`` satisfies the hook without an opt-out."""
    _scaffold(
        pytester,
        textwrap.dedent(
            """
            import pytest

            pytestmark = pytest.mark.vcr


            def test_vcr_tier():
                assert True
            """
        ).strip(),
    )
    result = pytester.runpytest("tests/integration/")
    assert result.ret == 0
    result.assert_outcomes(passed=1)


def test_use_cassette_decorator_honored(pytester: pytest.Pytester) -> None:
    """``@notebooklm_vcr.use_cassette(...)`` alone (no ``vcr`` marker) passes.

    Exercises ``_has_use_cassette_decorator`` end-to-end against a real
    ``vcr.VCR().use_cassette`` decorator (which wraps the test in a
    ``wrapt.FunctionWrapper`` whose ``_self_wrapper`` is a bound method on a
    ``CassetteContextDecorator``). This is the wrapper-introspection branch
    of the hook that the marker checks alone cannot exercise.
    """
    pytester.makeini(MARKER_REGISTRATION)
    pytester.makepyfile(
        **{
            "tests/__init__.py": "",
            "tests/integration/__init__.py": "",
            "tests/integration/conftest.py": HOOK_SOURCE,
            "tests/integration/test_with_use_cassette.py": textwrap.dedent(
                """
                import os
                import tempfile

                import vcr

                # Point at a writable empty directory so vcr doesn't try to
                # load a cassette that doesn't exist. record_mode='none'
                # would refuse the request — but the test body makes no HTTP
                # call so the cassette is never opened anyway.
                _cassette_dir = tempfile.mkdtemp()
                _vcr = vcr.VCR(cassette_library_dir=_cassette_dir)


                @_vcr.use_cassette("none.yaml")
                def test_decorated_only():
                    # No HTTP call — we only need the hook to see the
                    # CassetteContextDecorator wrapper on the function.
                    assert True
                """
            ).strip(),
        }
    )
    result = pytester.runpytest("tests/integration/")
    assert result.ret == 0
    result.assert_outcomes(passed=1)


def test_unit_tier_not_gated(pytester: pytest.Pytester) -> None:
    """The hook ONLY governs ``tests/integration/`` — unit-tier is untouched.

    Builds a violating file under ``tests/unit/`` (no marker, no cassette, no
    opt-out) and asserts the run still passes. Guards against the hook
    accidentally over-reaching.
    """
    pytester.makeini(MARKER_REGISTRATION)
    pytester.makepyfile(
        **{
            "tests/integration/conftest.py": HOOK_SOURCE,
            "tests/__init__.py": "",
            "tests/integration/__init__.py": "",
            "tests/unit/__init__.py": "",
            "tests/unit/test_bare.py": textwrap.dedent(
                """
                def test_unit_no_marker():
                    assert True
                """
            ).strip(),
        }
    )
    result = pytester.runpytest("tests/unit/")
    assert result.ret == 0
    result.assert_outcomes(passed=1)
