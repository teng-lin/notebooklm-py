"""Unit tests for E2E conftest CLI options.

Covers the --profile flag added in issue #339 without spinning up the full
E2E suite (which requires real auth).

The E2E conftest is loaded by file path so these unit tests can execute a fresh
copy of the hook module without invoking pytest's conftest discovery or the
authenticated E2E suite.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from notebooklm.exceptions import RateLimitError

CONFTEST_PATH = Path(__file__).resolve().parents[1] / "e2e" / "conftest.py"
pytest_plugins = ["pytester"]


def _load_e2e_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_conftest", CONFTEST_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_config(profile: str | None) -> SimpleNamespace:
    return SimpleNamespace(getoption=lambda name: profile if name == "--profile" else None)


class _FakeItem:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.markers: list[object] = []

    def add_marker(self, marker: object) -> None:
        self.markers.append(marker)


class TestE2EMarkerContract:
    """E2E files are marked before pytest applies -m deselection."""

    def test_item_under_e2e_directory_gets_e2e_marker(self):
        conftest = _load_e2e_conftest()
        item = _FakeItem(conftest.E2E_TEST_DIR / "test_chat.py")

        conftest.pytest_itemcollected(item)

        assert [marker.name for marker in item.markers] == ["e2e"]

    def test_item_outside_e2e_directory_is_not_marked(self):
        conftest = _load_e2e_conftest()
        item = _FakeItem(Path(__file__))

        conftest.pytest_itemcollected(item)

        assert item.markers == []

    def test_path_helper_uses_resolved_containment(self):
        conftest = _load_e2e_conftest()

        assert conftest._is_path_under(
            conftest.E2E_TEST_DIR / "test_chat.py", conftest.E2E_TEST_DIR
        )
        assert not conftest._is_path_under(Path(__file__), conftest.E2E_TEST_DIR)


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
    def _make_reporter(
        reports,
        *,
        passed=None,
    ):
        write_calls: list[tuple] = []
        return SimpleNamespace(
            stats={"skipped": reports, "passed": passed or []},
            write_sep=lambda *a, **kw: write_calls.append(("sep", a, kw)),
            write_line=lambda *a, **kw: write_calls.append(("line", a, kw)),
            _writes=write_calls,
        )

    @staticmethod
    def _make_session(reporter, *, exitstatus=pytest.ExitCode.OK):
        pluginmanager = SimpleNamespace(
            get_plugin=lambda name: reporter if name == "terminalreporter" else None
        )
        return SimpleNamespace(
            config=SimpleNamespace(pluginmanager=pluginmanager),
            exitstatus=exitstatus,
        )

    @classmethod
    def _finish(
        cls,
        conftest,
        reporter,
        *,
        exitstatus=pytest.ExitCode.OK,
    ) -> SimpleNamespace:
        session = cls._make_session(reporter, exitstatus=exitstatus)
        conftest.pytest_sessionfinish(session, exitstatus)
        return session

    @staticmethod
    def _report(nodeid: str, *, live_chat_ask: bool = False, when: str = "call"):
        keywords = {"live_chat_ask": 1} if live_chat_ask else {}
        return SimpleNamespace(nodeid=nodeid, keywords=keywords, when=when)

    @classmethod
    def _skipped(
        cls,
        nodeid: str,
        reason: str,
        *,
        live_chat_ask: bool = False,
        when: str = "call",
    ) -> SimpleNamespace:
        report = cls._report(nodeid, live_chat_ask=live_chat_ask, when=when)
        report.longrepr = ("file.py", 1, f"Skipped: {reason}")
        return report

    @classmethod
    def _passed(
        cls, nodeid: str, *, live_chat_ask: bool = False, when: str = "call"
    ) -> SimpleNamespace:
        return cls._report(nodeid, live_chat_ask=live_chat_ask, when=when)

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
                self._skipped("t::f", "chat rate-limited by upstream"),
            ]
        )
        conftest.pytest_terminal_summary(tr, 0, None)

        summary = (tmp_path / "summary.md").read_text()
        assert "Rate-limit skips: 5" in summary
        assert all(nid in summary for nid in ("t::a", "t::c", "t::d", "t::e", "t::f"))
        assert "t::b" not in summary
        assert "::warning::5 test(s) skipped" in capsys.readouterr().out

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

    def test_live_chat_floor_fails_when_marked_asks_all_rate_limited(self, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [
                self._skipped(
                    "test_server_live.py::TestRestServerLiveChat::test_chat_ask_returns_answer",
                    "chat rate-limited (surfaced through the REST route)",
                    live_chat_ask=True,
                )
            ]
        )
        conftest.pytest_terminal_summary(tr, pytest.ExitCode.OK, None)
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
        assert any(
            call[0] == "sep" and call[1][1] == "live chat coverage floor failed"
            for call in tr._writes
        )
        assert capsys.readouterr().out == ""

    def test_live_chat_floor_allows_one_marked_pass(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_chat.py::test_rate_limited", "rate limited", live_chat_ask=True)],
            passed=[self._passed("test_chat.py::test_ok", live_chat_ask=True)],
        )
        conftest.pytest_terminal_summary(tr, pytest.ExitCode.OK, None)
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.OK
        assert not any(
            call[0] == "sep" and call[1][1] == "live chat coverage floor failed"
            for call in tr._writes
        )

    def test_live_chat_floor_ignores_unmarked_passes(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_chat.py::test_rate_limited", "rate limited", live_chat_ask=True)],
            passed=[self._passed("test_unrelated.py::test_ok")],
        )
        conftest.pytest_terminal_summary(tr, pytest.ExitCode.OK, None)
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED

    def test_live_chat_floor_ignores_unmarked_rate_limit_skips(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        conftest = _load_e2e_conftest()

        tr = self._make_reporter([self._skipped("test_generation.py::test_audio", "rate limited")])
        conftest.pytest_terminal_summary(tr, pytest.ExitCode.OK, None)
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.OK

    def test_live_chat_floor_counts_setup_rate_limit_skips(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [
                self._skipped(
                    "test_chat.py::test_auth_skip",
                    "rate limited",
                    live_chat_ask=True,
                    when="setup",
                )
            ]
        )
        conftest.pytest_terminal_summary(tr, pytest.ExitCode.OK, None)
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED

    def test_live_chat_floor_does_not_rewrite_non_green_exitstatus(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_chat.py::test_rate_limited", "rate limited", live_chat_ask=True)],
        )
        conftest.pytest_terminal_summary(tr, pytest.ExitCode.USAGE_ERROR, None)
        session = self._finish(conftest, tr, exitstatus=pytest.ExitCode.USAGE_ERROR)

        assert session.exitstatus == pytest.ExitCode.USAGE_ERROR
        assert not any(
            call[0] == "sep" and call[1][1] == "live chat coverage floor failed"
            for call in tr._writes
        )

    def test_live_chat_floor_changes_real_pytest_exit_code(self, pytester: pytest.Pytester):
        pytester.makepyprojecttoml(
            """
            [tool.pytest.ini_options]
            markers = [
                "live_chat_ask: chat ask floor marker",
            ]
            """
        )
        pytester.makeconftest(
            """
            import pytest

            def pytest_sessionfinish(session, exitstatus):
                terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
                if exitstatus == pytest.ExitCode.OK and terminalreporter.stats.get("skipped"):
                    session.exitstatus = pytest.ExitCode.TESTS_FAILED

            def pytest_terminal_summary(terminalreporter, exitstatus, config):
                if exitstatus == pytest.ExitCode.OK and terminalreporter.stats.get("skipped"):
                    terminalreporter.write_sep("=", "live chat coverage floor failed")
            """
        )
        pytester.makepyfile(
            test_floor="""
            import pytest

            @pytest.mark.live_chat_ask
            def test_all_chat_asks_rate_limited():
                pytest.skip("chat rate-limited by upstream")
            """
        )

        result = pytester.runpytest_subprocess("-q")

        assert result.ret == pytest.ExitCode.TESTS_FAILED
        result.stdout.fnmatch_lines(["*live chat coverage floor failed*"])


class TestGenerationRateLimitSkip:
    """_install_generation_rate_limit_skip turns typed RateLimitError into skips.

    The RPC layer raises RateLimitError from generate_* before any
    GenerationStatus exists, so assert_generation_started's is_rate_limited
    path never runs. Only the typed RateLimitError may skip; every other
    exception must propagate (no-xfail-live-service-errors policy).
    """

    @staticmethod
    def _make_client():
        class FakeArtifacts:
            async def generate_audio(self, notebook_id):
                raise RateLimitError(
                    "API rate limit or quota exceeded. Please wait before retrying."
                )

            async def generate_video(self, notebook_id):
                return f"video:{notebook_id}"

            async def revise_slide(self, notebook_id):
                raise ValueError("not a rate limit")

            async def delete(self, notebook_id, artifact_id):
                raise RateLimitError("should never be wrapped")

        return SimpleNamespace(artifacts=FakeArtifacts())

    async def test_rate_limit_error_becomes_skip(self):
        conftest = _load_e2e_conftest()
        client = self._make_client()
        conftest._install_generation_rate_limit_skip(client)

        with pytest.raises(pytest.skip.Exception) as excinfo:
            await client.artifacts.generate_audio("nb-1")

        # Reason must match _RATE_LIMIT_PHRASES so pytest_terminal_summary
        # surfaces the skip in the rate-limit section + GH annotations.
        reason = str(excinfo.value).lower()
        assert any(phrase in reason for phrase in conftest._RATE_LIMIT_PHRASES)

    async def test_other_exceptions_propagate(self):
        conftest = _load_e2e_conftest()
        client = self._make_client()
        conftest._install_generation_rate_limit_skip(client)

        with pytest.raises(ValueError, match="not a rate limit"):
            await client.artifacts.revise_slide("nb-1")

    async def test_successful_calls_pass_through_per_method(self):
        # Closure safety: each wrapped name must bind its own original.
        conftest = _load_e2e_conftest()
        client = self._make_client()
        conftest._install_generation_rate_limit_skip(client)

        assert await client.artifacts.generate_video("nb-1") == "video:nb-1"

    async def test_non_generation_methods_are_not_wrapped(self):
        conftest = _load_e2e_conftest()
        client = self._make_client()
        conftest._install_generation_rate_limit_skip(client)

        with pytest.raises(RateLimitError):
            await client.artifacts.delete("nb-1", "art-1")
