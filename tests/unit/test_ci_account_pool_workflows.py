"""Static contracts for the rotating-account live CI cutover."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo_lint

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
LIVE_NAMES = ("nightly.yml", "rpc-health.yml", "verify-package.yml")


def _load(name: str) -> dict[str, object]:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    return [step for step in job["steps"] if isinstance(step, dict)]


def _step(job: dict[str, object], step_id: str) -> dict[str, object]:
    return next(step for step in _steps(job) if step.get("id") == step_id)


def test_detached_verifier_is_removed_from_workflow_and_pinning_inventories() -> None:
    assert not (WORKFLOW_DIR / "verify-artifacts.yml").exists()
    pinning = (ROOT / "scripts" / "check_action_pinning.py").read_text(encoding="utf-8")
    assert '"verify-artifacts.yml"' not in pinning


def test_live_workflows_resolve_only_main_before_planning_or_secrets() -> None:
    for name in LIVE_NAMES:
        workflow = _load(name)
        resolver = workflow["jobs"]["resolve-target"]
        resolver_text = str(resolver)
        assert "refs/heads/main" in resolver_text
        assert "release/" not in resolver_text
        assert "secrets." not in resolver_text
        assert "secrets[" not in resolver_text

        planner_name = "plan-live-lanes" if name != "verify-package.yml" else "plan-account"
        planner = workflow["jobs"][planner_name]
        assert "secrets." not in str(planner)
        assert "secrets[" not in str(planner)
        checkout = next(step for step in _steps(planner) if "checkout@" in str(step.get("uses")))
        assert checkout["with"]["ref"] == "${{ needs.resolve-target.outputs.sha }}"


def test_secret_bearing_jobs_have_both_literal_gates_and_exact_sha_checkout() -> None:
    jobs = [
        (_load("nightly.yml")["jobs"]["e2e"], "${{ needs.resolve-target.outputs.sha }}"),
        (
            _load("rpc-health.yml")["jobs"]["health-check"],
            "${{ needs.resolve-target.outputs.sha }}",
        ),
        (
            _load("rpc-health.yml")["jobs"]["android-grpc-health"],
            "${{ needs.resolve-target.outputs.sha }}",
        ),
        (_load("verify-package.yml")["jobs"]["verify"], "${{ needs.resolve-target.outputs.sha }}"),
    ]
    for job, expected_sha in jobs:
        assert job["environment"] == "protected-readonly"
        condition = str(job["if"])
        assert "github.repository == 'teng-lin/notebooklm-py'" in condition
        assert "needs.resolve-target.outputs.is_standard == 'true'" in condition
        checkout = next(step for step in _steps(job) if "checkout@" in str(step.get("uses")))
        assert checkout["with"]["ref"] == expected_sha
        assert checkout["with"]["persist-credentials"] is False


def test_each_authenticated_job_queues_by_one_non_secret_slot() -> None:
    live_jobs = [
        _load("nightly.yml")["jobs"]["e2e"],
        _load("rpc-health.yml")["jobs"]["health-check"],
        _load("rpc-health.yml")["jobs"]["android-grpc-health"],
        _load("verify-package.yml")["jobs"]["verify"],
    ]
    for job in live_jobs:
        concurrency = job["concurrency"]
        assert str(concurrency["group"]).startswith("notebooklm-account-${{")
        assert concurrency["queue"] == "max"
        assert "cancel-in-progress" not in concurrency
        text = str(job)
        assert text.count("NOTEBOOKLM_MASTER_TOKEN_JSON': '${{ secrets[") == 1
        assert "secrets.NOTEBOOKLM_MASTER_TOKEN_JSON" not in text
        assert "secrets.NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID" not in text
        assert "secrets.NOTEBOOKLM_GENERATION_NOTEBOOK_ID" not in text


def test_master_token_and_template_secrets_are_never_job_scoped() -> None:
    for name in LIVE_NAMES:
        for job in _load(name)["jobs"].values():
            job_env = job.get("env", {}) if isinstance(job, dict) else {}
            assert "secrets." not in str(job_env)
            assert "secrets[" not in str(job_env)


def test_nightly_full_copy_journal_and_cleanup_dag_is_explicit() -> None:
    workflow = _load("nightly.yml")
    job = workflow["jobs"]["e2e"]
    ids = {step.get("id") for step in _steps(job)}
    assert {
        "auth",
        "sweep",
        "provision",
        "preflight",
        "journal_policy",
        "primary",
        "lastfailed",
        "retry",
        "smoke",
        "coverage",
        "verifier_budget",
        "verifier",
        "cleanup",
        "purge",
    } <= ids

    assert _step(job, "sweep")["if"] == "steps.auth.outcome == 'success'"
    assert "steps.sweep.outcome == 'success'" in str(_step(job, "provision")["if"])
    assert _step(job, "preflight")["if"] == "steps.provision.outcome == 'success'"
    assert _step(job, "journal_policy")["if"] == "steps.preflight.outcome == 'success'"
    assert _step(job, "primary")["if"] == "steps.journal_policy.outcome == 'success'"
    assert _step(job, "cleanup")["if"] == "always()"
    assert _step(job, "purge")["if"] == "always()"

    provision = str(_step(job, "provision")["run"])
    assert "--mode full" in provision
    assert "--github-env" in provision
    journal = str(_step(job, "journal_policy")["run"])
    assert "matrix.backend" in journal
    assert "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE=required" in journal
    assert "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE=off" in journal
    verifier = str(_step(job, "verifier")["run"])
    assert "--mode journal" in verifier
    assert "steps.verifier_budget.outputs.timeout" in verifier
    verifier_if = str(_step(job, "verifier")["if"])
    assert "steps.journal_policy.outcome == 'success'" in verifier_if
    assert "steps.primary.outcome == 'success'" in verifier_if
    assert "steps.retry.outcome == 'success'" in verifier_if
    assert "steps.verifier_budget.outcome == 'success'" in verifier_if
    assert _steps(job).index(_step(job, "verifier")) < _steps(job).index(_step(job, "cleanup"))


def test_rpc_and_package_lanes_have_their_designated_lifecycles() -> None:
    rpc = _load("rpc-health.yml")["jobs"]
    web = rpc["health-check"]
    assert "--mode rpc" in str(_step(web, "provision")["run"])
    assert _step(web, "health")["if"] == "steps.preflight.outcome == 'success'"
    assert _step(web, "cleanup")["if"] == "always()"
    assert _step(web, "purge")["if"] == "always()"

    android = rpc["android-grpc-health"]
    assert "--backend android" in str(_step(android, "template_validate")["run"])
    assert "secrets.NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID" in str(_step(android, "health"))
    assert "provision" not in {step.get("id") for step in _steps(android)}
    assert _step(android, "purge")["if"] == "always()"

    package = _load("verify-package.yml")["jobs"]["verify"]
    assert "--mode full" in str(_step(package, "provision")["run"])
    assert "JOURNAL_MODE=off" in str(_step(package, "journal_policy")["run"])
    assert "||" in str(_step(package, "telemetry")["run"])
    assert _step(package, "cleanup")["if"] == "always()"
    assert _step(package, "purge")["if"] == "always()"


def test_aggregate_producer_policy_matches_lane_contract() -> None:
    nightly = str(_load("nightly.yml"))
    assert "inputs.test_filter == '' && 'required' || 'off'" in nightly
    assert "--lane nightly-android-windows --mode full --producer required" in nightly

    rpc = str(_load("rpc-health.yml"))
    assert "--lane rpc-health-web --mode rpc --producer required" in rpc
    assert "--lane rpc-health-android --mode template --producer required" in rpc

    package = str(_load("verify-package.yml"))
    assert "--lane verify-package --mode full --producer required" in package
