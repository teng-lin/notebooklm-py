"""Regression tests for the required external-reality test contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tests.conftest as test_config


class _Config:
    def __init__(self, *, required: bool = True, xdist: bool = False) -> None:
        self.required = required
        self.pluginmanager = SimpleNamespace(
            get_plugin=lambda _name: None,
            hasplugin=lambda name: xdist and name == "xdist",
        )

    def getoption(self, name: str, default=None) -> bool:
        if name == "--require-reality":
            return self.required
        if name == "numprocesses":
            return 2 if self.pluginmanager.hasplugin("xdist") else None
        if name == "dist":
            return "loadfile" if self.pluginmanager.hasplugin("xdist") else "no"
        return default


def _item(nodeid: str, *markers: str) -> SimpleNamespace:
    return SimpleNamespace(nodeid=nodeid, keywords=set(markers))


def _session(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(config=_Config(), items=list(items), exitstatus=pytest.ExitCode.OK)


_PROBE_MARKERS = {
    "tests/unit/cli/test_playwright_login_coverage.py::"
    "test_probe_source_detects_both_states_against_real_playwright": "requires_playwright",
    "tests/unit/cli/test_playwright_login_coverage.py::"
    "test_chromium_launches_headless_against_real_playwright": "requires_chromium",
}


def test_required_collection_rejects_deselected_expected_probe(monkeypatch) -> None:
    expected = sorted(test_config.REQUIRED_REALITY_PROBES)
    session = _session(_item(expected[0], "reality", "requires_playwright"))
    monkeypatch.setattr(test_config, "_REALITY_REPORTS", {})

    with pytest.raises(pytest.UsageError, match="missing expected probes"):
        test_config.pytest_collection_finish(session)


def test_required_mode_rejects_xdist() -> None:
    with pytest.raises(pytest.UsageError, match="cannot be combined with xdist"):
        test_config.pytest_configure(_Config(xdist=True))


def test_required_collection_rejects_unrecognized_dependency(monkeypatch) -> None:
    items = [
        _item(nodeid, "reality", "requires_unknown")
        for nodeid in test_config.REQUIRED_REALITY_PROBES
    ]
    session = _session(*items)
    monkeypatch.setattr(test_config, "_REALITY_REPORTS", {})

    with pytest.raises(pytest.UsageError, match="lack a recognized dependency"):
        test_config.pytest_collection_finish(session)


def test_required_execution_rejects_skipped_probe(monkeypatch) -> None:
    items = [
        _item(nodeid, "reality", _PROBE_MARKERS[nodeid])
        for nodeid in test_config.REQUIRED_REALITY_PROBES
    ]
    session = _session(*items)
    monkeypatch.setattr(test_config, "_REALITY_REPORTS", {})
    test_config.pytest_collection_finish(session)

    for nodeid in test_config.REQUIRED_REALITY_PROBES:
        test_config.pytest_runtest_logreport(
            SimpleNamespace(nodeid=nodeid, when="setup", outcome="skipped")
        )
    test_config.pytest_sessionfinish(session, pytest.ExitCode.OK)

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_required_execution_accepts_one_passing_call_per_probe(monkeypatch) -> None:
    items = [
        _item(nodeid, "reality", _PROBE_MARKERS[nodeid])
        for nodeid in test_config.REQUIRED_REALITY_PROBES
    ]
    session = _session(*items)
    monkeypatch.setattr(test_config, "_REALITY_REPORTS", {})
    test_config.pytest_collection_finish(session)

    for nodeid in test_config.REQUIRED_REALITY_PROBES:
        test_config.pytest_runtest_logreport(
            SimpleNamespace(nodeid=nodeid, when="call", outcome="passed")
        )
    test_config.pytest_sessionfinish(session, pytest.ExitCode.OK)

    assert session.exitstatus == pytest.ExitCode.OK
