"""Unit tests for E2E conftest CLI options.

Covers the --profile flag added in issue #339 without spinning up the full
E2E suite (which requires real auth).

The E2E conftest is loaded by file path because `tests/` is not a Python
package (no `__init__.py`), so a normal `from tests.e2e import conftest`
import would fail under pytest.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

CONFTEST_PATH = Path(__file__).resolve().parents[1] / "e2e" / "conftest.py"


def _load_e2e_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_conftest", CONFTEST_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_config(profile: str | None) -> SimpleNamespace:
    return SimpleNamespace(getoption=lambda name: profile if name == "--profile" else None)


class TestProfileOptionLifecycle:
    """pytest_configure + pytest_unconfigure round-trip."""

    def test_round_trip_no_prior_env(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
        conftest = _load_e2e_conftest()
        config = _make_config("work")

        conftest.pytest_configure(config)
        assert os.environ.get("NOTEBOOKLM_PROFILE") == "work"

        conftest.pytest_unconfigure(config)
        assert "NOTEBOOKLM_PROFILE" not in os.environ

    def test_round_trip_restores_prior_env(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_PROFILE", "preset")
        conftest = _load_e2e_conftest()
        config = _make_config("work")

        conftest.pytest_configure(config)
        assert os.environ.get("NOTEBOOKLM_PROFILE") == "work"

        conftest.pytest_unconfigure(config)
        assert os.environ.get("NOTEBOOKLM_PROFILE") == "preset"

    def test_no_flag_with_prior_env_is_noop(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_PROFILE", "preset")
        conftest = _load_e2e_conftest()
        config = _make_config(None)

        conftest.pytest_configure(config)
        conftest.pytest_unconfigure(config)
        assert os.environ.get("NOTEBOOKLM_PROFILE") == "preset"

    def test_no_flag_without_prior_env_is_noop(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
        conftest = _load_e2e_conftest()
        config = _make_config(None)

        conftest.pytest_configure(config)
        conftest.pytest_unconfigure(config)
        assert "NOTEBOOKLM_PROFILE" not in os.environ


class TestArgvProfile:
    """Parsing of --profile out of argv (used at import time)."""

    def test_long_form(self):
        argv = ["pytest", "--profile", "work", "tests/e2e"]
        assert _load_e2e_conftest()._argv_profile(argv) == "work"

    def test_equals_form(self):
        argv = ["pytest", "--profile=work", "tests/e2e"]
        assert _load_e2e_conftest()._argv_profile(argv) == "work"

    def test_absent(self):
        argv = ["pytest", "tests/e2e", "-m", "e2e"]
        assert _load_e2e_conftest()._argv_profile(argv) is None

    def test_long_form_missing_value_returns_none(self):
        argv = ["pytest", "--profile"]
        assert _load_e2e_conftest()._argv_profile(argv) is None

    def test_last_occurrence_wins(self):
        argv = ["pytest", "--profile", "foo", "--profile", "bar"]
        assert _load_e2e_conftest()._argv_profile(argv) == "bar"

    def test_long_form_rejects_dash_prefixed_value(self):
        argv = ["pytest", "--profile", "--verbose"]
        assert _load_e2e_conftest()._argv_profile(argv) is None


class TestRateLimitSkipSummary:
    """pytest_terminal_summary surfaces chat rate-limit skips so green CI doesn't hide drift."""

    @staticmethod
    def _make_reporter(reports):
        write_calls: list[tuple] = []
        return SimpleNamespace(
            stats={"skipped": reports},
            write_sep=lambda *a, **kw: write_calls.append(("sep", a, kw)),
            write_line=lambda *a, **kw: write_calls.append(("line", a, kw)),
            _writes=write_calls,
        )

    @staticmethod
    def _skipped(nodeid: str, reason: str) -> SimpleNamespace:
        return SimpleNamespace(nodeid=nodeid, longrepr=("file.py", 1, f"Skipped: {reason}"))

    def test_counts_only_rate_limit_skips(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [
                self._skipped("t::a", "Chat request was rate limited"),
                self._skipped("t::b", "no auth configured"),
                self._skipped("t::c", "rejected by the API"),
                self._skipped("t::d", "Chat request failed with HTTP 429: ..."),
                self._skipped("t::e", "Too Many Requests"),
            ]
        )
        conftest.pytest_terminal_summary(tr, 0, None)

        summary = (tmp_path / "summary.md").read_text()
        assert "Rate-limit skips: 4" in summary
        assert all(nid in summary for nid in ("t::a", "t::c", "t::d", "t::e"))
        assert "t::b" not in summary
        assert "::warning::4 test(s) skipped" in capsys.readouterr().out

    def test_no_skips_emits_nothing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        conftest = _load_e2e_conftest()

        tr = self._make_reporter([self._skipped("t::a", "no auth configured")])
        conftest.pytest_terminal_summary(tr, 0, None)

        assert not (tmp_path / "summary.md").exists()
        assert capsys.readouterr().out == ""
        assert tr._writes == []

    def test_no_github_env_skips_annotations(self, monkeypatch, tmp_path, capsys):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        conftest = _load_e2e_conftest()

        tr = self._make_reporter([self._skipped("t::a", "rate limited")])
        conftest.pytest_terminal_summary(tr, 0, None)

        # Still emits the pytest section locally — just no GH-specific bits.
        assert any(call[0] == "sep" for call in tr._writes)
        assert capsys.readouterr().out == ""
