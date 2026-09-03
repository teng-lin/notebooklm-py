"""Static contracts for the rotating-account live CI cutover."""

from __future__ import annotations

import os
import subprocess
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


def test_job_level_env_never_uses_step_only_runner_context() -> None:
    for name in LIVE_NAMES:
        workflow = _load(name)
        for job_name, job in workflow["jobs"].items():
            assert "${{ runner." not in str(job.get("env", {})), f"{name}:{job_name}"


def test_manifest_paths_use_a_store_owned_private_child_directory() -> None:
    jobs = (
        _load("nightly.yml")["jobs"]["e2e"],
        _load("rpc-health.yml")["jobs"]["health-check"],
        _load("verify-package.yml")["jobs"]["verify"],
    )
    for job in jobs:
        configure = next(
            step for step in _steps(job) if step.get("name") == "Configure runner-local paths"
        )
        assert "CI_E2E_MANIFEST=$RUNNER_TEMP/notebooklm-e2e/manifest.json" in str(configure["run"])


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


@pytest.mark.parametrize("name", LIVE_NAMES)
@pytest.mark.parametrize(
    ("event_name", "ref", "expected"),
    [
        ("schedule", "refs/heads/main", "true"),
        ("workflow_dispatch", "refs/heads/main", "true"),
        ("schedule", "refs/heads/feature", "false"),
        ("workflow_dispatch", "refs/heads/release/0.9", "false"),
        ("workflow_dispatch", "refs/tags/v0.9.0", "false"),
    ],
)
def test_trusted_target_shell_accepts_only_exact_main(
    name: str,
    event_name: str,
    ref: str,
    expected: str,
    tmp_path: Path,
) -> None:
    workflow = _load(name)
    resolver = workflow["jobs"]["resolve-target"]
    step_id = "trust" if name == "verify-package.yml" else "resolve"
    command = str(_step(resolver, step_id)["run"])
    assert "EVENT_NAME" not in command
    output = tmp_path / f"{name}-{event_name}-{expected}.out"
    completed = subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "EVENT_NAME": event_name,
            "GITHUB_REF_VALUE": ref,
            "GITHUB_OUTPUT": str(output),
        },
    )
    assert completed.returncode == (0 if expected == "true" else 1)
    values = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert values["is_standard"] == expected


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
        assert concurrency["cancel-in-progress"] is False
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
    assert job["defaults"]["run"]["shell"] == "bash"
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
    assert '--mode "${{ matrix.mode }}"' in provision
    assert "--github-env" in provision
    journal = str(_step(job, "journal_policy")["run"])
    assert "matrix.lane" in journal
    assert "nightly-web-ubuntu" in journal
    assert "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE=required" in journal
    assert "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE=off" in journal
    verifier = str(_step(job, "verifier")["run"])
    assert "--mode journal" in verifier
    assert "steps.verifier_budget.outputs.timeout" in verifier
    verifier_if = str(_step(job, "verifier")["if"])
    assert "matrix.lane == 'nightly-web-ubuntu'" in verifier_if
    assert "inputs.test_filter == ''" in verifier_if
    assert "steps.journal_policy.outcome == 'success'" in verifier_if
    assert "steps.primary.outcome == 'success'" in verifier_if
    assert "steps.lastfailed.outcome == 'failure'" in verifier_if
    assert "steps.retry.outcome == 'success'" in verifier_if
    assert "steps.verifier_budget.outcome == 'success'" in verifier_if
    coverage_if = str(_step(job, "coverage")["if"])
    assert "inputs.test_filter" not in coverage_if
    assert "steps.lastfailed.outcome == 'failure'" in coverage_if
    purge = str(_step(job, "purge")["run"])
    assert 'journal.with_name(f".{journal.name}.lock")' in purge
    assert _steps(job).index(_step(job, "verifier")) < _steps(job).index(_step(job, "cleanup"))


def test_rpc_and_package_lanes_have_their_designated_lifecycles() -> None:
    rpc = _load("rpc-health.yml")["jobs"]
    assert "${GITHUB_SHA}" not in str(rpc)
    web = rpc["health-check"]
    for step_name in (
        "Extract failing methods for ERROR issue",
        "Report rebrand-host state changes",
    ):
        step = next(item for item in _steps(web) if item.get("name") == step_name)
        assert step["env"]["CHECKED_SHA"] == "${{ needs.resolve-target.outputs.sha }}"
        assert "${CHECKED_SHA}" in str(step["run"])
    assert "--mode rpc" in str(_step(web, "provision")["run"])
    assert _step(web, "health")["if"] == "steps.preflight.outcome == 'success'"
    assert _step(web, "cleanup")["if"] == "always()"
    assert _step(web, "purge")["if"] == "always()"
    report_if = str(_step(web, "report")["if"])
    assert "steps.health.outcome == 'success'" in report_if
    assert "steps.health.outcome == 'failure'" in report_if
    report_run = str(_step(web, "report")["run"])
    assert "REPORT_UPLOAD_OUTCOME" in report_run
    assert "REBRAND_REPORT_FAILED" in report_run

    android = rpc["android-grpc-health"]
    assert "--backend android" in str(_step(android, "template_validate")["run"])
    assert "secrets.NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID" in str(_step(android, "health"))
    assert "provision" not in {step.get("id") for step in _steps(android)}
    assert _step(android, "purge")["if"] == "always()"

    package = _load("verify-package.yml")["jobs"]["verify"]
    assert "--mode full" in str(_step(package, "provision")["run"])
    assert "JOURNAL_MODE=off" in str(_step(package, "journal_policy")["run"])
    assert "||" in str(_step(package, "telemetry")["run"])
    assert "steps.lastfailed.outcome == 'failure'" in str(_step(package, "telemetry")["if"])
    aggregate = str(_step(package, "aggregate")["run"])
    assert "steps.lastfailed.outcome == 'failure'" in aggregate
    assert "json.loads" in str(_step(package, "lastfailed")["run"])
    assert _step(package, "cleanup")["if"] == "always()"
    assert _step(package, "purge")["if"] == "always()"


def test_aggregate_producer_policy_matches_lane_contract() -> None:
    nightly = str(_load("nightly.yml"))
    assert "inputs.test_filter == '' && 'required' || 'off'" in nightly
    assert "--lane nightly-android-macos --mode full --producer required" in nightly
    assert "--lane nightly-readonly-windows --mode readonly --producer required" in nightly

    rpc = str(_load("rpc-health.yml"))
    assert "--lane rpc-health-web --mode rpc --producer required" in rpc
    assert "--lane rpc-health-android --mode template --producer required" in rpc

    package = str(_load("verify-package.yml"))
    assert "--lane verify-package --mode full --producer required" in package


def test_safe_summaries_cover_selection_and_lifecycle_counts() -> None:
    planners = [
        _load("nightly.yml")["jobs"]["plan-live-lanes"],
        _load("rpc-health.yml")["jobs"]["plan-live-lanes"],
        _load("verify-package.yml")["jobs"]["plan-account"],
    ]
    for planner in planners:
        summary = next(
            str(step["run"])
            for step in _steps(planner)
            if "GITHUB_STEP_SUMMARY" in str(step.get("run", ""))
        )
        assert "enabled slot count" in summary
        assert "Selection" in summary or "SELECTION_MODE" in summary
        assert "master_token_secret_name" not in summary
        assert "secret" not in summary.lower()

    nightly = _load("nightly.yml")["jobs"]["e2e"]
    nightly_provision = str(_step(nightly, "provision")["run"])
    assert "Template contract: version=1 fingerprint=" in nightly_provision
    assert '"readonly": ("reference",)' in nightly_provision
    assert "Copy outcomes: total={len(roles)}" in nightly_provision
    assert "Clean-role residuals: generation=0 multi-source=0" in nightly_provision
    assert 'row["notebook_id"]' not in nightly_provision
    assert "Coverage floor:" in str(_step(nightly, "coverage")["run"])

    rpc = _load("rpc-health.yml")["jobs"]["health-check"]
    rpc_provision = str(_step(rpc, "provision")["run"])
    assert "Copy outcomes: total=1" in rpc_provision
    assert "Clean-role residuals: rpc=0" in rpc_provision
    assert 'row["notebook_id"]' not in rpc_provision

    package = _load("verify-package.yml")["jobs"]["verify"]
    package_provision = str(_step(package, "provision")["run"])
    assert "Copy outcomes: total=3" in package_provision
    assert "Clean-role residuals: generation=0 multi-source=0" in package_provision
    assert 'row["notebook_id"]' not in package_provision


def test_rpc_reports_do_not_stream_raw_output_and_drop_files_on_scrub_failure() -> None:
    workflow = _load("rpc-health.yml")
    web = workflow["jobs"]["health-check"]
    android = workflow["jobs"]["android-grpc-health"]
    health = str(_step(web, "health")["run"])
    bundle = str(_step(web, "bundle")["run"])
    android_health = str(_step(android, "health")["run"])
    assert "tee health-report.txt" not in health
    assert "tee bundle-drift-report.txt" not in bundle
    assert "tee android-canary-report.txt" not in android_health

    scrub_steps = [
        next(
            step
            for step in _steps(web)
            if step.get("name") == "Scrub secrets from health-report.txt"
        ),
        next(
            step
            for step in _steps(web)
            if step.get("name") == "Scrub secrets from bundle-drift-report.txt"
        ),
        next(
            step
            for step in _steps(android)
            if step.get("name") == "Scrub secrets from android-canary-report.txt"
        ),
    ]
    for step in scrub_steps:
        command = str(step["run"])
        assert "trap 'rm -f" in command
        assert "trap - ERR" in command

    diagnostic = (ROOT / "scripts" / "check_rpc_health.py").read_text(encoding="utf-8")
    assert "repr(data)" not in diagnostic
    assert "WARNING: Notebook {temp.notebook_id}" not in diagnostic
