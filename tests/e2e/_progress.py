"""CI-only E2E progress, including a heartbeat while a test awaits the service."""

from __future__ import annotations

import re
import threading
import time
from collections import Counter

import pytest
from scripts._ci_progress import report, safe_test_name, write_summary


class E2EProgress:
    def __init__(self) -> None:
        self.total = 0
        self.started = time.monotonic()
        self.current: tuple[str, float] | None = None
        self.results: dict[str, str] = {}
        self.failures: set[tuple[str, str]] = set()
        self.failure_types: dict[tuple[str, str], str] = {}
        self.reruns = 0
        self.collection_errors = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def pytest_sessionstart(self, session) -> None:
        self._thread = threading.Thread(target=self._heartbeat, daemon=True, name="e2e-progress")
        self._thread.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(30):
            self._report_current()

    def _report_current(self) -> None:
        current = self.current
        if current is not None:
            name, started = current
            report(f"E2E still running: {name}; test elapsed={time.monotonic() - started:.0f}s")
        else:
            report("E2E waiting for collection or session teardown")

    def pytest_collection_finish(self, session) -> None:
        self.total = len(session.items)
        report(f"E2E collected {self.total} selected tests", summary=True)

    def pytest_collectreport(self, report) -> None:
        if report.failed:
            self.collection_errors += 1

    def pytest_runtest_logstart(self, nodeid, location) -> None:
        name = safe_test_name(nodeid)
        self.current = (name, time.monotonic())
        self.results[nodeid] = "running"
        # A rerun replaces the earlier attempt's failure in the final summary.
        self.failures = {entry for entry in self.failures if entry[0] != nodeid}
        self.failure_types = {
            key: value for key, value in self.failure_types.items() if key[0] != nodeid
        }
        report(f"E2E [{len(self.results)}/{self.total}] START {name}")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        yield
        if call.excinfo is not None:
            name = call.excinfo.type.__name__
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name):
                self.failure_types[(item.nodeid, call.when)] = name

    def pytest_runtest_logreport(self, report) -> None:
        if report.outcome == "rerun":
            self.reruns += 1
            self.results[report.nodeid] = "rerun"
        elif report.failed:
            self.results[report.nodeid] = "failed"
            self.failures.add((report.nodeid, report.when))
        elif self.results.get(report.nodeid) != "failed":
            if report.skipped:
                self.results[report.nodeid] = (
                    "xfailed" if hasattr(report, "wasxfail") else "skipped"
                )
            elif report.when == "call":
                self.results[report.nodeid] = "xpassed" if hasattr(report, "wasxfail") else "passed"

    def pytest_runtest_logfinish(self, nodeid, location) -> None:
        current = self.current
        duration = time.monotonic() - current[1] if current else 0
        outcome = self.results.get(nodeid, "incomplete")
        diagnostics = "; ".join(
            f"{phase}: {self.failure_types.get((nodeid, phase), 'failure')}"
            for failed_node, phase in sorted(self.failures)
            if failed_node == nodeid
        )
        report(
            f"E2E {outcome.upper()} {safe_test_name(nodeid)}; elapsed={duration:.1f}s"
            + (f"; {diagnostics}; see pytest traceback below" if diagnostics else ""),
            error=outcome == "failed",
        )
        self.current = None

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self.pytest_unconfigure()
        counts = Counter(self.results.values())
        detail = " ".join(f"{outcome}={count}" for outcome, count in sorted(counts.items()))
        report(
            f"E2E finished: exit={int(exitstatus)} {detail} reruns={self.reruns} "
            f"collection_errors={self.collection_errors}; "
            f"elapsed={time.monotonic() - self.started:.0f}s",
            summary=True,
        )
        if self.failures:
            write_summary("\n| Failed E2E test | Phase | Exception type |\n| --- | --- | --- |")
            for nodeid, phase in sorted(self.failures):
                kind = self.failure_types.get((nodeid, phase), "see pytest traceback")
                write_summary(f"| `{safe_test_name(nodeid)}` | {phase} | {kind} |")

    def pytest_unconfigure(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
