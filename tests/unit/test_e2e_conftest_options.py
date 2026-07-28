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
        failed=None,
    ):
        write_calls: list[tuple] = []
        return SimpleNamespace(
            stats={"skipped": reports, "passed": passed or [], "failed": failed or []},
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

    @pytest.fixture(autouse=True)
    def _enforce_floor(self, monkeypatch):
        # Default these tests to the enforced path (nightly semantics) with inline
        # exit-status delivery (no sentinel), matching the pre-existing assertions.
        # Individual tests override (delenv the flag, or set the sentinel path).
        monkeypatch.setenv("E2E_ENFORCE_COVERAGE_FLOOR", "1")
        monkeypatch.delenv("E2E_COVERAGE_FLOOR_SENTINEL", raising=False)

    @staticmethod
    def _report(
        nodeid: str,
        *,
        live_chat_ask: bool = False,
        live_generation: bool = False,
        when: str = "call",
    ):
        keywords: dict[str, int] = {}
        if live_chat_ask:
            keywords["live_chat_ask"] = 1
        if live_generation:
            keywords["live_generation"] = 1
        return SimpleNamespace(nodeid=nodeid, keywords=keywords, when=when)

    @classmethod
    def _skipped(
        cls,
        nodeid: str,
        reason: str,
        *,
        live_chat_ask: bool = False,
        live_generation: bool = False,
        when: str = "call",
    ) -> SimpleNamespace:
        report = cls._report(
            nodeid, live_chat_ask=live_chat_ask, live_generation=live_generation, when=when
        )
        report.longrepr = ("file.py", 1, f"Skipped: {reason}")
        return report

    @classmethod
    def _passed(
        cls,
        nodeid: str,
        *,
        live_chat_ask: bool = False,
        live_generation: bool = False,
        when: str = "call",
    ) -> SimpleNamespace:
        return cls._report(
            nodeid, live_chat_ask=live_chat_ask, live_generation=live_generation, when=when
        )

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

    def test_floor_not_enforced_without_flag_stays_green(self, monkeypatch):
        # Release-path semantics (#1819): with E2E_ENFORCE_COVERAGE_FLOOR unset, a
        # fully-throttled surface skips freely and never reds the job.
        monkeypatch.delenv("E2E_ENFORCE_COVERAGE_FLOOR", raising=False)
        monkeypatch.delenv("E2E_COVERAGE_FLOOR_SENTINEL", raising=False)
        conftest = _load_e2e_conftest()

        tr = self._make_reporter([self._skipped("t::a", "rate limited", live_chat_ask=True)])
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.OK

    def test_generation_floor_fails_when_all_marked_rate_limited(self, monkeypatch):
        conftest = _load_e2e_conftest()
        tr = self._make_reporter(
            [
                self._skipped(
                    "test_interactive_mind_map.py::t", "Rate limit: quota", live_generation=True
                )
            ]
        )
        session = self._finish(conftest, tr)
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED

    def test_generation_floor_allows_one_marked_pass(self, monkeypatch):
        conftest = _load_e2e_conftest()
        tr = self._make_reporter(
            [self._skipped("test_generation.py::t_audio", "Rate limit: q", live_generation=True)],
            passed=[self._passed("test_generation.py::t_report", live_generation=True)],
        )
        session = self._finish(conftest, tr)
        assert session.exitstatus == pytest.ExitCode.OK

    def test_sentinel_records_skip_event_without_failing_exit(self, monkeypatch, tmp_path):
        # Nightly delivery: a hollow surface (marked skip, no marked pass) appends a
        # SKIP event and leaves exit status alone, so a pure-skip run stays exit 0 and
        # doesn't trip the continue-on-error retry. The enforce step reconciles later.
        sentinel = tmp_path / "floor"
        monkeypatch.setenv("E2E_COVERAGE_FLOOR_SENTINEL", str(sentinel))
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_generation.py::t", "Rate limit: q", live_generation=True)]
        )
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.OK
        assert "SKIP\tlive generation" in sentinel.read_text()

    def test_sentinel_records_skip_despite_unrelated_failure(self, monkeypatch, tmp_path):
        # Masking guard (#1819): the SKIP event is still recorded when an UNRELATED
        # test failed on the main run, so a transient failure that recovers on retry
        # can't mask a hollow surface.
        sentinel = tmp_path / "floor"
        monkeypatch.setenv("E2E_COVERAGE_FLOOR_SENTINEL", str(sentinel))
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_generation.py::t", "Rate limit: q", live_generation=True)],
            failed=[self._report("test_notebooks.py::unrelated", when="call")],
        )
        self._finish(conftest, tr, exitstatus=pytest.ExitCode.TESTS_FAILED)

        assert "SKIP\tlive generation" in sentinel.read_text()

    def test_sentinel_records_pass_event_when_marked_test_passed(self, monkeypatch, tmp_path):
        # A marked pass records PASS (real coverage) even when another marked test
        # rate-limit-skipped in the same run — the enforce step reconciles SKIP vs PASS
        # so this surface does NOT breach.
        sentinel = tmp_path / "floor"
        monkeypatch.setenv("E2E_COVERAGE_FLOOR_SENTINEL", str(sentinel))
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_generation.py::a", "Rate limit: q", live_generation=True)],
            passed=[self._passed("test_generation.py::b", live_generation=True)],
        )
        self._finish(conftest, tr)

        text = sentinel.read_text()
        assert "PASS\tlive generation" in text
        assert "SKIP\tlive generation" not in text

    def test_sentinel_records_skip_when_marked_test_failed(self, monkeypatch, tmp_path):
        # A marked FAILURE is not a pass, so the surface still records SKIP (a breach
        # candidate). The retry step appends its own PASS/SKIP, and the enforce step
        # reconciles — so a marked test that fails on main then passes on retry clears
        # the surface, while one that fails then skips keeps it breached (codex/coderabbit).
        sentinel = tmp_path / "floor"
        monkeypatch.setenv("E2E_COVERAGE_FLOOR_SENTINEL", str(sentinel))
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_generation.py::a", "Rate limit: q", live_generation=True)],
            failed=[self._report("test_generation.py::b", live_generation=True, when="setup")],
        )
        self._finish(conftest, tr, exitstatus=pytest.ExitCode.TESTS_FAILED)

        assert "SKIP\tlive generation" in sentinel.read_text()

    def test_floor_falls_back_to_exitstatus_when_sentinel_unwritable(self, monkeypatch, tmp_path):
        # If the sentinel write fails, don't silently hollow-green: fall back to the
        # inline exit-code path (best-effort) (codex). Point the sentinel at a path
        # whose parent is a file, so makedirs + open both fail.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        monkeypatch.setenv("E2E_COVERAGE_FLOOR_SENTINEL", str(blocker / "floor"))
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_generation.py::a", "Rate limit: q", live_generation=True)]
        )
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED

    def test_floor_sentinel_creates_missing_parent_dir(self, monkeypatch, tmp_path):
        # A missing parent directory must not turn the enforcement signal into a
        # silent no-op — the writer creates it (gemini).
        sentinel = tmp_path / "nested" / "sub" / "floor"
        monkeypatch.setenv("E2E_COVERAGE_FLOOR_SENTINEL", str(sentinel))
        conftest = _load_e2e_conftest()

        tr = self._make_reporter(
            [self._skipped("test_generation.py::a", "Rate limit: q", live_generation=True)]
        )
        session = self._finish(conftest, tr)

        assert session.exitstatus == pytest.ExitCode.OK
        assert "SKIP\tlive generation" in sentinel.read_text()

    def test_floor_ignores_broken_run_exit_codes(self, monkeypatch):
        # A usage/internal error is its own signal — the floor must not evaluate or
        # rewrite that exit code (claude review).
        conftest = _load_e2e_conftest()
        tr = self._make_reporter(
            [self._skipped("t::a", "rate limited", live_chat_ask=True)],
        )
        session = self._finish(conftest, tr, exitstatus=pytest.ExitCode.USAGE_ERROR)

        assert session.exitstatus == pytest.ExitCode.USAGE_ERROR

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

            async def retry_failed(self, notebook_id, artifact_id):
                raise RateLimitError("Resource exhausted (status 8): quota")

            async def delete(self, notebook_id, artifact_id):
                raise RateLimitError("should never be wrapped")

        class FakeMindMaps:
            async def generate(self, notebook_id):
                raise RateLimitError("Resource exhausted (status 8): quota")

            async def list(self, notebook_id):
                raise RateLimitError("read path — should never be wrapped")

        return SimpleNamespace(artifacts=FakeArtifacts(), mind_maps=FakeMindMaps())

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

    async def test_mind_maps_generate_becomes_skip(self):
        # The #1819 gap: the interactive mind map creates via client.mind_maps.generate,
        # a different namespace than client.artifacts.generate_* — it must skip too.
        conftest = _load_e2e_conftest()
        client = self._make_client()
        conftest._install_generation_rate_limit_skip(client)

        with pytest.raises(pytest.skip.Exception) as excinfo:
            await client.mind_maps.generate("nb-1")
        assert any(p in str(excinfo.value).lower() for p in conftest._RATE_LIMIT_PHRASES)

    async def test_mind_maps_read_methods_are_not_wrapped(self):
        conftest = _load_e2e_conftest()
        client = self._make_client()
        conftest._install_generation_rate_limit_skip(client)

        with pytest.raises(RateLimitError):
            await client.mind_maps.list("nb-1")

    async def test_retry_failed_becomes_skip(self):
        # retry_failed re-runs generation and raises RateLimitError on quota, so it
        # must skip like generate_* (claude review — same #1819 regression class).
        conftest = _load_e2e_conftest()
        client = self._make_client()
        conftest._install_generation_rate_limit_skip(client)

        with pytest.raises(pytest.skip.Exception):
            await client.artifacts.retry_failed("nb-1", "art-1")


class TestGenerationSkipRegistryCoverage:
    """The skip registry must cover every live generate/revise entrypoint.

    Guards against a new CREATE_ARTIFACT method (or namespace) landing without
    being wrapped for the RateLimitError→skip fixture — the exact regression class
    that caused #1819 (mind_maps.generate was created after the artifacts-only
    wrapper and slipped through).
    """

    def test_registry_covers_all_generate_and_revise_methods(self):
        from notebooklm._artifacts import ArtifactsAPI
        from notebooklm._mind_maps_api import MindMapsAPI

        conftest = _load_e2e_conftest()
        classes = {"artifacts": ArtifactsAPI, "mind_maps": MindMapsAPI}

        # Registry namespaces and the known generation-capable classes must stay in
        # lockstep — adding one without the other trips this guard.
        assert set(conftest._GENERATION_SKIP_TARGETS) == set(classes)

        for ns_name, cls in classes.items():
            matches = conftest._GENERATION_SKIP_TARGETS[ns_name]
            # generate*/revise*/retry* are the CREATE_ARTIFACT-quota surfaces that
            # raise RateLimitError before a GenerationStatus exists (retry_failed
            # re-runs generation, so it raises on quota too).
            gen_methods = [
                name
                for name in dir(cls)
                if not name.startswith("_")
                and name.startswith(("generate", "revise", "retry"))
                and callable(getattr(cls, name))
            ]
            assert gen_methods, f"{ns_name}: expected at least one generate/revise/retry method"
            uncovered = [name for name in gen_methods if not matches(name)]
            assert not uncovered, (
                f"{ns_name}: generate/revise/retry methods not in skip registry: {uncovered}"
            )
