from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aggregate_ci_e2e_outcomes.py"
SPEC = importlib.util.spec_from_file_location("aggregate_ci_e2e_outcomes", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
aggregate_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = aggregate_module
SPEC.loader.exec_module(aggregate_module)


def successful(lane: str) -> dict[str, str]:
    return dict.fromkeys(aggregate_module.POLICIES[lane].applicable, "success")


@pytest.mark.parametrize("lane", list(aggregate_module.POLICIES))
def test_every_lane_accepts_all_success(lane: str) -> None:
    policy = aggregate_module.POLICIES[lane]
    states = successful(lane)
    if "lastfailed" in states:
        states["lastfailed"] = "not_applicable"
        states["retry"] = "not_applicable"
    assert aggregate_module.aggregate(lane=lane, mode=policy.mode, states=states) == []


def test_successful_retry_recovers_only_primary_failure() -> None:
    states = successful("nightly-web-ubuntu")
    states["primary"] = "failure"
    assert aggregate_module.aggregate(lane="nightly-web-ubuntu", mode="full", states=states) == []


def test_missing_lastfailed_keeps_primary_red() -> None:
    states = successful("nightly-web-ubuntu")
    states.update(primary="failure", lastfailed="failure", retry="not_applicable")
    failures = aggregate_module.aggregate(lane="nightly-web-ubuntu", mode="full", states=states)
    assert "phase_failed:primary" in failures
    assert "retry_unavailable:lastfailed" in failures


@pytest.mark.parametrize("phase", ["auth", "purge", "cleanup"])
def test_root_required_phase_cannot_be_skipped(phase: str) -> None:
    states = successful("nightly-web-ubuntu")
    states.update(lastfailed="not_applicable", retry="not_applicable")
    states[phase] = "not_applicable"
    assert f"required_phase_skipped:{phase}" in aggregate_module.aggregate(
        lane="nightly-web-ubuntu", mode="full", states=states
    )


def test_failed_setup_requires_consumers_to_be_not_applicable() -> None:
    states = successful("nightly-web-ubuntu")
    states["provision"] = "failure"
    states["preflight"] = "not_applicable"
    states["journal_policy"] = "not_applicable"
    states["primary"] = "not_applicable"
    states["lastfailed"] = "not_applicable"
    states["retry"] = "not_applicable"
    states["smoke"] = "not_applicable"
    states["coverage"] = "not_applicable"
    states["verifier"] = "not_applicable"
    failures = aggregate_module.aggregate(lane="nightly-web-ubuntu", mode="full", states=states)
    assert failures == ["phase_failed:provision"]


def test_dependency_violation_and_simultaneous_cleanup_purge_failures() -> None:
    states = successful("rpc-health-web")
    states["auth"] = "failure"
    states["cleanup"] = "failure"
    states["purge"] = "failure"
    failures = aggregate_module.aggregate(lane="rpc-health-web", mode="rpc", states=states)
    assert "dependency_violation:sweep" in failures
    assert "phase_failed:auth" in failures
    assert "phase_failed:cleanup" in failures
    assert "phase_failed:purge" in failures


def test_wrong_mode_or_phase_set_is_configuration_error() -> None:
    with pytest.raises(aggregate_module.OutcomeError):
        aggregate_module.aggregate(lane="verify-package", mode="rpc", states={})
    with pytest.raises(aggregate_module.OutcomeError, match="phase set"):
        aggregate_module.aggregate(lane="verify-package", mode="full", states={})


def test_filtered_web_lane_allows_only_explicit_nonproducer_verifier_skip() -> None:
    states = successful("nightly-web-ubuntu")
    states.update(lastfailed="not_applicable", retry="not_applicable", verifier="not_applicable")
    assert (
        aggregate_module.aggregate(
            lane="nightly-web-ubuntu", mode="full", states=states, producer=False
        )
        == []
    )
    assert "required_phase_skipped:verifier" in aggregate_module.aggregate(
        lane="nightly-web-ubuntu", mode="full", states=states, producer=True
    )


def test_verifier_remains_required_after_retry_failure() -> None:
    states = successful("nightly-web-ubuntu")
    states.update(primary="failure", lastfailed="success", retry="failure")
    assert "required_phase_skipped:verifier" in aggregate_module.aggregate(
        lane="nightly-web-ubuntu",
        mode="full",
        states={**states, "verifier": "not_applicable"},
    )
    failures = aggregate_module.aggregate(lane="nightly-web-ubuntu", mode="full", states=states)
    assert "phase_failed:retry" in failures
    assert "dependency_violation:verifier" not in failures


@pytest.mark.parametrize("state", ["success", "failure", "not_applicable"])
def test_verify_package_telemetry_is_advisory(state: str) -> None:
    states = successful("verify-package")
    states.update(lastfailed="not_applicable", retry="not_applicable", telemetry=state)
    assert aggregate_module.aggregate(lane="verify-package", mode="full", states=states) == []


def test_verify_package_telemetry_cannot_run_after_blocked_setup() -> None:
    states = successful("verify-package")
    states.update(
        provision="failure",
        preflight="not_applicable",
        journal_policy="not_applicable",
        primary="not_applicable",
        lastfailed="not_applicable",
        retry="not_applicable",
        telemetry="success",
    )
    failures = aggregate_module.aggregate(lane="verify-package", mode="full", states=states)
    assert "dependency_violation:telemetry" in failures


def test_cli_diagnostics_never_contain_resource_values(capsys) -> None:
    states = successful("rpc-health-android")
    states["health"] = "failure"
    args = ["--lane", "rpc-health-android", "--mode", "template"]
    for name, state in states.items():
        args.extend(["--phase", f"{name}={state}"])
    assert aggregate_module.main(args) == 1
    rendered = capsys.readouterr().err
    assert "phase_failed:health" in rendered
    assert "notebook_id" not in rendered
