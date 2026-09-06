"""Fault runner scheduling, failure reporting, and cleanup without remote traffic."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import stress_fault_server as stress
from tests._fault_server.common import ScenarioFailure, ScenarioResult


async def _passing(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
    assert result.operation_id == operation_id
    result.record("plan", faults=[name])
    result.require("work completed", True)
    return result


def test_evidence_snapshots_payload_and_retains_failed_check() -> None:
    result = ScenarioResult("web", "test", "operation-1")
    payload = ["first"]
    result.record("request", values=payload)
    payload.append("second")
    assert result.events == [{"kind": "request", "values": ["first"]}]
    with pytest.raises(ScenarioFailure, match="one commit") as captured:
        result.require("one commit", False)
    assert captured.value.result is result
    assert result.checks == {"one commit": False}
    with pytest.raises(ValueError, match="duplicate"):
        result.require("one commit", True)
    with pytest.raises(TypeError):
        result.record("bad", value=object())
    with pytest.raises(ValueError):
        result.record("bad", value=float("nan"))
    with pytest.raises(TypeError, match="bool"):
        result.require("truthy", 1)  # type: ignore[arg-type]
    assert len(result.events) == 2


def test_seeded_decks_are_stable_and_cover_each_scenario() -> None:
    registry = {(b, s): _passing for b in ("web", "android") for s in ("read", "retry")}
    config = stress.RunConfig(iterations=12, seed=123)
    plan = stress.build_plan(config, registry)
    assert plan == stress.build_plan(config, dict(reversed(list(registry.items()))))
    for offset in range(0, 12, 4):
        assert {(p.backend, p.scenario) for p in plan[offset : offset + 4]} == set(registry)
    assert len({p.operation_id for p in plan}) == 12
    assert plan != stress.build_plan(stress.RunConfig(iterations=12, seed=124), registry)
    selected = stress.build_plan(
        stress.RunConfig(backend="web", scenarios=("retry",), iterations=3), registry
    )
    assert {(p.backend, p.scenario) for p in selected} == {("web", "retry")}
    with pytest.raises(ValueError, match="unknown scenario"):
        stress.build_plan(stress.RunConfig(scenarios=("missing",)), registry)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"iterations": 0},
        {"iterations": 100_001},
        {"concurrency": 65},
        {"timeout": float("inf")},
        {"scenario_timeout": float("nan")},
        {"cleanup_timeout": -1},
    ],
)
def test_invalid_config_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        stress.RunConfig(**kwargs)


@pytest.mark.asyncio
async def test_worker_pool_bounds_concurrency_and_keeps_complete_plan() -> None:
    active = maximum = 0
    two_started = asyncio.Event()

    async def scenario(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            two_started.set()
        try:
            await two_started.wait()
            await asyncio.sleep(0)
            return await _passing(name, operation_id=operation_id, result=result)
        finally:
            active -= 1

    report = await stress.run_stress(
        stress.RunConfig(iterations=7, concurrency=2), {("web", "read"): scenario}
    )
    assert maximum == 2 and active == 0
    assert report["summary"]["passed"] == 7 and report["summary"]["ok"]
    assert len(report["plan"]) == len(report["operations"]) == 7
    assert all(op["events"][1]["kind"] == "plan" for op in report["operations"])
    json.dumps(report, allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["check", "exception", "empty", "identity", "false"])
async def test_failed_scenarios_preserve_trace_and_fail_report(mode: str) -> None:
    async def scenario(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        result.record("server_received", attempt=1)
        try:
            if mode == "check":
                result.require("no replay", False)
            if mode == "exception":
                raise RuntimeError("handler crashed")
            if mode == "identity":
                return ScenarioResult("web", name, operation_id)
            if mode == "false":
                result.checks["no replay"] = False
            return result
        finally:
            result.record("cleanup", completed=True)

    report = await stress.run_stress(stress.RunConfig(iterations=1), {("web", "read"): scenario})
    assert report["summary"]["failed"] == 1 and not report["summary"]["ok"]
    operation = report["operations"][0]
    assert operation["events"][-1] == {"kind": "cleanup", "completed": True}
    assert operation["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing_plan", "unrecorded", "duplicate", "mismatched"])
async def test_passing_claim_without_matching_evidence_fails(mode: str) -> None:
    async def scenario(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        if mode != "missing_plan":
            result.record("plan", faults=["commit_then_disconnect"])
        if mode == "unrecorded":
            result.checks["one commit"] = True
        else:
            result.require("one commit", True)
        if mode == "duplicate":
            result.record("check", name="one commit", passed=True)
        if mode == "mismatched":
            result.checks = {"unobserved condition": True}
        return result

    report = await stress.run_stress(stress.RunConfig(iterations=1), {("web", "read"): scenario})
    assert report["summary"]["failed"] == 1
    assert not report["summary"]["ok"]
    assert "ValueError" in report["operations"][0]["error"]


@pytest.mark.asyncio
async def test_scenario_timeout_cancels_work_and_retains_cleanup_events() -> None:
    cleaned = asyncio.Event()

    async def stalled(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        result.record("plan", faults=["stall"])
        try:
            await asyncio.Event().wait()
        finally:
            result.record("cleanup", completed=True)
            cleaned.set()
        return result

    report = await stress.run_stress(
        stress.RunConfig(iterations=2, concurrency=1, scenario_timeout=0.02),
        {("web", "stall"): stalled},
    )
    assert cleaned.is_set()
    assert report["summary"]["timed_out"] == 2 and not report["summary"]["ok"]
    assert all(op["events"][-1]["kind"] == "cleanup" for op in report["operations"])
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("fault-")]


@pytest.mark.asyncio
async def test_aggregate_timeout_preserves_unstarted_plan_and_cancels_active_work() -> None:
    cleaned: list[str] = []

    async def stalled(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        result.record("plan", faults=["stall"])
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(operation_id)
            result.record("cleanup", completed=True)
        return result

    report = await stress.run_stress(
        stress.RunConfig(iterations=5, concurrency=2, timeout=0.02), {("web", "stall"): stalled}
    )
    assert len(cleaned) == 2
    assert report["summary"]["canceled"] == 2
    assert report["summary"]["not_started"] == 3
    assert not report["summary"]["ok"] and report["failures"]
    assert len(report["plan"]) == 5
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("fault-")]


@pytest.mark.asyncio
async def test_external_cancellation_returns_partial_failure_report() -> None:
    started = asyncio.Event()

    async def stalled(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            result.record("cleanup")
        return result

    task = asyncio.create_task(
        stress.run_stress(stress.RunConfig(iterations=2), {("web", "stall"): stalled})
    )
    await started.wait()
    task.cancel()
    report = await task
    assert report["failures"] == ["run interrupted"]
    assert not report["summary"]["ok"]


def test_environment_isolated_once_and_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", "do-not-run")
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://do-not-contact.invalid")
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "original")
    original_home = os.environ.get("HOME")
    with stress.isolated_environment():
        assert "NOTEBOOKLM_REFRESH_CMD" not in os.environ
        assert "NOTEBOOKLM_BASE_URL" not in os.environ
        assert os.environ["NOTEBOOKLM_PROFILE"] == "agent-fault-stress"
        isolated = Path(os.environ["NOTEBOOKLM_HOME"])
        assert isolated.is_dir()
        assert os.environ.get("HOME") == original_home
        os.environ["NOTEBOOKLM_ADDED_DURING_TEST"] = "temporary"
    assert not isolated.exists()
    assert os.environ["NOTEBOOKLM_REFRESH_CMD"] == "do-not-run"
    assert os.environ["NOTEBOOKLM_PROFILE"] == "original"
    assert "NOTEBOOKLM_ADDED_DURING_TEST" not in os.environ


@pytest.mark.parametrize("failure", [False, True])
def test_cli_writes_report_and_returns_failed_invariant_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: bool
) -> None:
    async def scenario(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        result.record("plan", faults=["commit_then_disconnect"])
        result.require("one commit", not failure)
        return result

    monkeypatch.setattr(stress, "load_registry", lambda _: {("web", "read"): scenario})
    destination = tmp_path / "nested" / "report.json"
    code = stress.main(["--iterations", "2", "--json-report", str(destination)])
    report = json.loads(destination.read_text())
    assert code == int(failure)
    assert report["summary"]["ok"] is not failure
    assert len(report["plan"]) == 2
    assert not list(destination.parent.glob(".report.json.*"))


def test_cli_rejects_invalid_arguments_before_loading_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_: str) -> Any:
        raise AssertionError("backend loaded for invalid arguments")

    monkeypatch.setattr(stress, "load_registry", unexpected)
    with pytest.raises(SystemExit) as captured:
        stress.main(["--timeout", "nan"])
    assert captured.value.code == 2


def test_cli_backend_loading_failure_still_writes_failure_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def missing_dependency(_: str) -> Any:
        raise ImportError("install dev dependencies")

    monkeypatch.setattr(stress, "load_registry", missing_dependency)
    destination = tmp_path / "report.json"
    assert stress.main(["--json-report", str(destination)]) == 1
    report = json.loads(destination.read_text())
    assert not report["summary"]["ok"]
    assert "ImportError" in report["failures"][0]


def test_cli_aggregate_timeout_writes_partial_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def stalled(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        result.record("plan", faults=["stall"])
        try:
            await asyncio.Event().wait()
        finally:
            result.record("cleanup", completed=True)
        return result

    monkeypatch.setattr(stress, "load_registry", lambda _: {("web", "stall"): stalled})
    destination = tmp_path / "report.json"
    assert (
        stress.main(
            [
                "--iterations",
                "2",
                "--concurrency",
                "1",
                "--timeout",
                "0.02",
                "--json-report",
                str(destination),
            ]
        )
        == 1
    )
    report = json.loads(destination.read_text())
    assert report["summary"]["canceled"] == 1
    assert report["summary"]["not_started"] == 1
    assert report["operations"][0]["events"][-1]["kind"] == "cleanup"


def test_cli_reports_a_leaked_child_even_when_it_cancels_cleanly() -> None:
    cleaned = False

    async def child() -> None:
        nonlocal cleaned
        try:
            await asyncio.Event().wait()
        finally:
            cleaned = True

    async def leaks(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        result.record("plan", faults=["leaked_child"])
        asyncio.create_task(child(), name="unexpected-child")
        result.require("misleading cleanup claim", True)
        return result

    report = stress._run_cli(stress.RunConfig(iterations=1), {("web", "leak"): leaks})
    assert not report["summary"]["ok"]
    assert any("leaked tasks" in failure for failure in report["failures"])
    assert cleaned


def test_cli_records_unhandled_background_failure() -> None:
    async def fails() -> None:
        raise RuntimeError("orphaned handler failed")

    async def scenario(name: str, *, operation_id: str, result: ScenarioResult) -> ScenarioResult:
        result.record("plan", faults=["unhandled_background_failure"])
        asyncio.create_task(fails(), name="unowned-handler")
        await asyncio.sleep(0)
        result.require("misleading success", True)
        return result

    report = stress._run_cli(stress.RunConfig(iterations=1), {("web", "broken"): scenario})
    assert not report["summary"]["ok"]
    assert any("orphaned handler failed" in failure for failure in report["failures"])


def test_cli_bounds_cancellation_resistant_cleanup_in_subprocess() -> None:
    # A broken coroutine must not strand pytest's own event loop. The CLI owns
    # its loop and reports shutdown failure even when a scenario swallows cancel.
    source = """
import asyncio
from scripts import stress_fault_server as stress

async def broken(name, *, operation_id, result):
    result.record('plan', faults=['cancellation-resistant'])
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

config = stress.RunConfig(iterations=1, timeout=0.02, scenario_timeout=0.01, cleanup_timeout=0.02)
report = stress._run_cli(config, {('web', 'broken'): broken})
assert not report['summary']['ok']
assert any('pending tasks' in failure for failure in report['failures'])
print('bounded cleanup failure recorded')
"""
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "bounded cleanup failure recorded" in completed.stdout
