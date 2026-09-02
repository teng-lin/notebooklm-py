"""Regression tests for the independent GitHub Actions test matrix."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo_lint

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test.yml"
NIGHTLY_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "nightly.yml"
VERIFY_PACKAGE_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "verify-package.yml"
)


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return next(step for step in steps if isinstance(step, dict) and step.get("name") == name)


def test_test_matrix_is_independent_and_preserves_ci_contract() -> None:
    """The required matrix covers every Python plus one secondary-OS cell."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert {"quality", "test", "repo-lint"} <= set(jobs)
    assert jobs["quality"]["name"] == "Code Quality"
    assert jobs["test"]["name"] == "Test (${{ matrix.os }}, Python ${{ matrix.python-version }})"
    assert "needs" not in jobs["test"]
    assert jobs["test"]["strategy"]["fail-fast"] is False

    matrix = jobs["test"]["strategy"]["matrix"]
    assert matrix == {
        "include": [
            {
                "os": "ubuntu-latest",
                "python-version": "3.10",
                "canonical": False,
                "windows_playwright": False,
            },
            {
                "os": "ubuntu-latest",
                "python-version": "3.11",
                "canonical": False,
                "windows_playwright": False,
            },
            {
                "os": "ubuntu-latest",
                "python-version": "3.12",
                "canonical": True,
                "windows_playwright": False,
            },
            {
                "os": "ubuntu-latest",
                "python-version": "3.13",
                "canonical": False,
                "windows_playwright": False,
            },
            {
                "os": "ubuntu-latest",
                "python-version": "3.14",
                "canonical": False,
                "windows_playwright": False,
            },
            {
                "os": "macos-latest",
                "python-version": "3.12",
                "canonical": False,
                "windows_playwright": False,
            },
            {
                "os": "windows-latest",
                "python-version": "3.12",
                "canonical": False,
                "windows_playwright": True,
            },
        ]
    }


def test_pr_matrix_runs_once_without_coverage_and_canonical_owns_reality() -> None:
    """Every cell runs the suite once; canonical alone owns browser contracts."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    test_job = workflow["jobs"]["test"]

    marker_filter = "not repo_lint and not requires_playwright and not requires_chromium"
    suite_step = _step(test_job, "Run tests without coverage")
    suite_command = str(suite_step["run"])
    assert "if" not in suite_step
    assert marker_filter in suite_command
    assert "-n auto" in suite_command
    assert "--dist loadgroup" in suite_command
    assert "--no-cov" in suite_command

    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    assert "--cov" not in workflow_text
    step_names = {step.get("name") for step in test_job["steps"]}
    assert "Run tests with coverage" not in step_names
    assert "Run compatibility tests without coverage" not in step_names
    assert "Assert per-file coverage floors" not in step_names

    canonical_steps = {
        "Get Playwright version",
        "Cache Playwright browsers",
        "Install Playwright browsers",
        "Install Playwright system dependencies (Linux)",
        "Run required external-reality probes",
        "Run Playwright-dependent unit tests serially",
        "Run critical contract guards",
    }
    for name in canonical_steps:
        assert _step(test_job, name)["if"] == "matrix.canonical"

    reality_command = str(_step(test_job, "Run required external-reality probes")["run"])
    assert "-m reality" in reality_command
    assert "--require-reality" in reality_command

    playwright_command = str(_step(test_job, "Run Playwright-dependent unit tests serially")["run"])
    assert "tests/unit" in playwright_command
    assert "(requires_playwright or requires_chromium) and not reality" in playwright_command
    assert "-n 0" in playwright_command
    assert "--no-cov" in playwright_command

    critical_command = str(_step(test_job, "Run critical contract guards")["run"])
    assert "-n auto" in critical_command
    assert "--timeout=180" in critical_command
    assert "--no-cov" in critical_command
    assert "test_baseline_registry_is_non_trivial" in critical_command
    assert "test_baseline_matches_committed_file" in critical_command
    assert "test_no_flat_cookie_projection_reaches_an_http_request" in critical_command
    assert "test_no_cli_module_imports_minting_primitives" in critical_command
    assert "test_no_bare_master_token_derivation_outside_paths_module" in critical_command
    assert "test_raw_sync_playwright_is_confined_to_policy_gateway" in critical_command
    assert "test_wire_contract.py::test_every_adapter_constant_is_declared" in critical_command
    assert (
        "test_builtin_shadowed_annotations.py::"
        "test_class_body_annotations_do_not_name_a_shadowed_builtin"
    ) in critical_command
    assert "tests/unit/test_ci_test_matrix.py" in critical_command

    smoke = _step(test_job, "Run Windows Playwright compatibility smoke serially")
    assert smoke["if"] == "matrix.windows_playwright"
    smoke_command = str(smoke["run"])
    assert (
        "tests/unit/test_windows_compatibility.py::TestPlaywrightSmokeTest::"
        "test_playwright_initializes_with_context_manager"
    ) in smoke_command
    assert "-m requires_playwright" in smoke_command
    assert "-n 0" in smoke_command
    assert "--no-cov" in smoke_command


def test_nightly_coverage_is_sha_pinned_secret_free_and_enforces_floors() -> None:
    """Scheduled/manual nightly owns global and per-file coverage enforcement."""
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}

    resolve_job = workflow["jobs"]["resolve-branch"]
    resolve_checkout = next(
        step
        for step in resolve_job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert resolve_checkout["with"] == {
        "ref": "refs/heads/${{ steps.resolve.outputs.branch }}",
        "fetch-depth": 1,
        "persist-credentials": False,
    }

    job = workflow["jobs"]["coverage"]
    assert job["needs"] == "resolve-branch"
    assert job["if"] == "needs.resolve-branch.outputs.is_standard == 'true'"
    assert job["runs-on"] == "ubuntu-latest"
    assert "environment" not in job
    assert "secrets." not in str(job)

    checkout = next(
        step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"] == {
        "ref": "${{ needs.resolve-branch.outputs.sha }}",
        "fetch-depth": 1,
        "persist-credentials": False,
    }

    e2e_checkout = next(
        step
        for step in workflow["jobs"]["e2e"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert e2e_checkout["with"] == checkout["with"]

    setup_python = _step(job, "Set up Python")
    assert setup_python["uses"] == "actions/setup-python@v7"
    assert setup_python["with"]["python-version"] == "3.12"
    assert _step(job, "Install uv")["uses"] == (
        "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78"
    )

    install_command = str(_step(job, "Install dependencies")["run"])
    assert "uv sync --frozen" in install_command
    for extra in {"browser", "dev", "markdown", "mcp", "server", "impersonate", "cookies"}:
        assert f"--extra {extra}" in install_command

    assert _step(job, "Install Playwright browsers")["run"] == (
        "uv run playwright install chromium"
    )
    assert _step(job, "Install Playwright system dependencies (Linux)")["run"] == (
        "uv run playwright install-deps chromium"
    )

    ordinary_step = _step(job, "Run ordinary tests with coverage")
    ordinary_command = str(ordinary_step["run"])
    assert "-n auto" in ordinary_command
    assert "--dist loadgroup" in ordinary_command
    assert "not repo_lint and not requires_playwright and not requires_chromium" in ordinary_command
    assert "--cov=src/notebooklm" in ordinary_command
    assert "--cov-report=" in ordinary_command
    assert "--cov-fail-under=0" in ordinary_command

    playwright_step = _step(job, "Append Playwright-dependent unit coverage")
    playwright_command = str(playwright_step["run"])
    assert "tests/unit" in playwright_command
    assert "(requires_playwright or requires_chromium) and not reality" in playwright_command
    assert "-n 0" in playwright_command
    assert "--cov=src/notebooklm" in playwright_command
    assert "--cov-append" in playwright_command
    assert "--cov-report=json:coverage.json" in playwright_command
    assert "--cov-fail-under=90" in playwright_command

    floor_step = _step(job, "Assert per-file coverage floors")
    assert floor_step["run"] == (
        "uv run python scripts/check_coverage_thresholds.py --coverage-json coverage.json"
    )
    assert job["steps"].index(ordinary_step) < job["steps"].index(playwright_step)
    assert job["steps"].index(playwright_step) < job["steps"].index(floor_step)


def test_nightly_e2e_runs_explicit_web_and_android_backends() -> None:
    """Authenticated nightly coverage cannot silently remain Web-only."""
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["e2e"]

    assert "${{ matrix.backend }}" in job["name"]
    assert job["strategy"]["matrix"]["include"] == [
        {"os": "ubuntu-latest", "backend": "web", "generation_notebook": "shared"},
        {"os": "windows-latest", "backend": "web", "generation_notebook": "unused"},
        {"os": "ubuntu-latest", "backend": "android", "generation_notebook": "scratch"},
    ]
    assert job["env"]["NOTEBOOKLM_BACKEND"] == "${{ matrix.backend }}"
    assert "${{ matrix.backend }}" in job["concurrency"]["group"]

    install = str(_step(job, "Install dependencies")["run"])
    assert "uv sync --frozen" in install
    assert "--extra android" in install

    preflight = _step(job, "Assert Android dependencies and live auth")
    assert preflight["if"] == "matrix.backend == 'android'"
    preflight_command = str(preflight["run"])
    assert "import grpc" in preflight_command
    assert "import gpsoauth" in preflight_command
    assert 'backend="android"' in preflight_command
    assert "client.notebooks.get(notebook_id)" in preflight_command

    bind_generation = _step(job, "Bind shared Web generation notebook")
    assert bind_generation["if"] == "matrix.generation_notebook == 'shared'"
    assert bind_generation["env"] == {
        "NOTEBOOKLM_GENERATION_NOTEBOOK_ID": ("${{ secrets.NOTEBOOKLM_GENERATION_NOTEBOOK_ID }}")
    }
    assert "GITHUB_ENV" in str(bind_generation["run"])

    create_scratch = _step(job, "Create isolated Android generation notebook")
    assert create_scratch["if"] == "matrix.generation_notebook == 'scratch'"
    create_command = str(create_scratch["run"])
    assert "NOTEBOOKLM_GENERATION_NOTEBOOK_ID" in create_command
    assert "GITHUB_ENV" in create_command
    assert 'NotebookLMClient.from_storage(backend="android")' in create_command
    assert "client.notebooks.create" in create_command
    assert "client.sources.add_text" in create_command
    assert "client.notebooks.delete" in create_command
    assert "os.fsync" in create_command
    assert "os.replace" in create_command
    assert create_command.index("persist_notebook_id(id_path, notebook.id)") < create_command.index(
        "client.sources.add_text"
    )
    assert "preserving the original persistence failure" in create_command
    assert "inline deletion " in create_command
    assert "was unconfirmed; the finalizer will retry" in create_command
    assert create_command.count("id_path.unlink(missing_ok=True)") == 1

    cleanup_scratch = _step(job, "Delete isolated Android generation notebook")
    assert cleanup_scratch["if"] == "${{ always() && matrix.generation_notebook == 'scratch' }}"
    cleanup_command = str(cleanup_scratch["run"])
    assert 'NotebookLMClient.from_storage(backend="android")' in cleanup_command
    assert "client.notebooks.delete" in cleanup_command
    assert job["steps"].index(cleanup_scratch) > job["steps"].index(
        _step(job, "Enforce coverage floors")
    )

    primary = _step(job, "Run E2E tests")
    retry = _step(job, "Retry failed E2E tests after 10-min cool-down")
    for step in (primary, retry):
        assert "NOTEBOOKLM_GENERATION_NOTEBOOK_ID" not in step["env"]
    assert retry["env"]["TEST_FILTER"] == "${{ inputs.test_filter }}"
    retry_command = str(retry["run"])
    assert 'if [ -n "$TEST_FILTER" ]' in retry_command
    assert "unset E2E_ENFORCE_COVERAGE_FLOOR" in retry_command
    assert "tests/e2e --last-failed --last-failed-no-failures=none" in retry_command

    curl_smoke = _step(job, "curl_cffi transport smoke (live, minimal)")
    assert "matrix.backend == 'web'" in str(curl_smoke["if"])
    assert "NOTEBOOKLM_GENERATION_NOTEBOOK_ID" not in curl_smoke["env"]


def test_verify_package_live_checks_published_wheel_android_and_keeps_web_e2e() -> None:
    """Package verification proves Android deps/protos/live GetProject without replacing Web."""
    workflow = yaml.safe_load(VERIFY_PACKAGE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["verify"]

    install = str(_step(job, "Sync locked deps + non-cookies extras")["run"])
    assert "--extra android" in install

    android = _step(job, "Validate published wheel Android backend")
    assert android["if"] == "github.repository == 'teng-lin/notebooklm-py'"
    command = str(android["run"])
    assert "import grpc" in command
    assert "import gpsoauth" in command
    assert "read_pb2.GetProjectRequest.DESCRIPTOR.full_name" in command
    assert 'NotebookLMClient.from_storage(backend="android")' in command
    assert "client.notebooks.get(notebook_id)" in command

    steps = job["steps"]
    assert steps.index(android) > steps.index(_step(job, "Materialize auth profile"))
    assert steps.index(android) > steps.index(
        _step(job, "Install published wheel from TestPyPI (--no-deps)")
    )

    web_e2e = _step(job, "Run E2E tests")
    assert "NOTEBOOKLM_BACKEND" not in job.get("env", {})
    assert "NOTEBOOKLM_BACKEND" not in web_e2e["env"]
    assert 'pytest tests/e2e -m "not variants"' in str(web_e2e["run"])


def test_repository_lint_is_a_bounded_manual_only_job() -> None:
    """Deep repo audits have one manual lane, not one per compatibility cell."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["repo-lint"]

    assert job["name"] == "Repository Lint (manual)"
    assert job["if"] == "github.event_name == 'workflow_dispatch'"
    assert "needs" not in job

    command = str(_step(job, "Run repository lint tests")["run"])
    assert "-m repo_lint" in command
    assert "-n auto" in command
    assert "--timeout=180" in command
    assert "--no-cov" in command
    assert "--cov=" not in command


def test_repository_lint_is_scheduled_once_in_nightly() -> None:
    """AST-heavy audits run automatically once against the resolved nightly SHA."""
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["repo-lint"]

    assert job["needs"] == "resolve-branch"
    assert job["if"] == "needs.resolve-branch.outputs.is_standard == 'true'"
    assert job["runs-on"] == "ubuntu-latest"
    assert "environment" not in job
    assert "secrets." not in str(job)

    checkout = next(
        step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ needs.resolve-branch.outputs.sha }}"

    command = str(_step(job, "Run repository lint tests")["run"])
    assert "-m repo_lint" in command
    assert "-n auto" in command
    assert "--timeout=180" in command
    assert "--no-cov" in command


def test_cassette_and_fixture_scans_run_once_in_quality() -> None:
    """Portable secret scans are not repeated across compatibility cells."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    quality_names = {step.get("name") for step in workflow["jobs"]["quality"]["steps"]}
    test_names = {step.get("name") for step in workflow["jobs"]["test"]["steps"]}
    scans = {"Assert cassettes are sanitized", "Check fixtures for credential leaks"}

    assert scans <= quality_names
    assert scans.isdisjoint(test_names)
