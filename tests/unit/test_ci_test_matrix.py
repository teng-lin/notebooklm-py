"""Regression tests for the independent GitHub Actions test matrix."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo_lint

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test.yml"
AUTH_PATCH_AUDIT_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "auth-patch-audit.yml"
)
NIGHTLY_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "nightly.yml"
VERIFY_PACKAGE_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "verify-package.yml"
)
PUBLISH_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "publish.yml"
TESTPYPI_PUBLISH_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "testpypi-publish.yml"
)
SUPPORTED_OSES = ["ubuntu-latest", "macos-latest", "windows-latest"]
SUPPORTED_PYTHONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
GENERATION_E2E = Path(__file__).resolve().parents[1] / "e2e" / "test_generation.py"
E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return next(step for step in steps if isinstance(step, dict) and step.get("name") == name)


def test_cinematic_video_is_opt_in_but_one_ordinary_video_remains_default() -> None:
    tree = ast.parse(GENERATION_E2E.read_text(encoding="utf-8"))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    cinematic = classes["TestCinematicVideoGeneration"]
    assert "pytest.mark.variants" in {ast.unparse(node) for node in cinematic.decorator_list}

    ordinary = classes["TestVideoGeneration"]
    assert "pytest.mark.variants" not in {ast.unparse(node) for node in ordinary.decorator_list}
    default = next(
        node
        for node in ordinary.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "test_generate_video_default"
    )
    assert "pytest.mark.variants" not in {ast.unparse(node) for node in default.decorator_list}


def test_readonly_e2e_tests_never_request_mutating_managed_role_fixtures() -> None:
    offenders: list[str] = []

    def inspect(
        path: Path,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        inherited_readonly: bool = False,
    ) -> None:
        decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
        if not inherited_readonly and "pytest.mark.readonly" not in decorators:
            return
        fixtures = {argument.arg for argument in (*node.args.posonlyargs, *node.args.args)}
        forbidden = fixtures & {"generation_notebook_id", "multi_source_notebook_id"}
        if forbidden:
            offenders.append(f"{path.name}::{node.name}: {sorted(forbidden)}")

    for path in sorted(E2E_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inspect(path, node)
            elif isinstance(node, ast.ClassDef):
                inherited = "pytest.mark.readonly" in {
                    ast.unparse(decorator) for decorator in node.decorator_list
                }
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        inspect(path, member, inherited_readonly=inherited)
    assert offenders == []


def test_test_matrix_is_independent_and_preserves_ci_contract() -> None:
    """The required PR matrix covers every Python on Linux plus one 3.12 cell per secondary OS."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert {"quality", "test", "repo-lint"} <= set(jobs)
    assert "auth-patch-coverage-delta" not in jobs
    assert jobs["quality"]["name"] == "Code Quality"
    assert jobs["test"]["name"] == "Test (${{ matrix.os }}, Python ${{ matrix.python-version }})"
    assert "needs" not in jobs["test"]
    assert jobs["test"]["strategy"]["fail-fast"] is False

    matrix = jobs["test"]["strategy"]["matrix"]
    # The full 3-OS by 5-Python product is nightly's job (see
    # ``test_nightly_runs_full_sha_pinned_compatibility_matrix``); PRs run the
    # reduced 7-cell matrix so the suite is not multiplied fifteen-fold per push.
    assert set(matrix) == {"include"}
    assert matrix["include"] == [
        *(
            {
                "os": "ubuntu-latest",
                "python-version": python,
                "canonical": python == "3.12",
                "windows_playwright": False,
            }
            for python in SUPPORTED_PYTHONS
        ),
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
    assert {cell["os"] for cell in matrix["include"]} == set(SUPPORTED_OSES)


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

    # The ordinary PR workflow stays coverage-free. The release/manual auth
    # delta runs in its own workflow.
    assert "--cov" not in str(test_job)
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


def test_auth_patch_coverage_delta_is_release_gated_and_manually_dispatchable() -> None:
    workflow = yaml.safe_load(AUTH_PATCH_AUDIT_WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_call", "workflow_dispatch"}
    for trigger in triggers.values():
        assert set(trigger["inputs"]) == {"custom_branch", "base_ref"}
        assert trigger["inputs"]["custom_branch"]["default"] == ""
        assert trigger["inputs"]["base_ref"]["default"] == ""

    job = workflow["jobs"]["auth-patch-coverage-delta"]
    assert job["name"] == "auth-patch-coverage-delta"
    assert job["runs-on"] == "ubuntu-latest"
    assert "if" not in job
    checkout = next(step for step in job["steps"] if "actions/checkout@" in str(step.get("uses")))
    assert checkout["with"]["ref"] == "${{ inputs.custom_branch || github.ref }}"
    assert checkout["with"]["fetch-depth"] == 0

    base_resolution = _step(job, "Resolve comparison base")
    assert base_resolution["id"] == "base"
    assert base_resolution["env"] == {"REQUESTED_BASE": "${{ inputs.base_ref }}"}
    base_command = str(base_resolution["run"])
    assert 'if [ -n "$REQUESTED_BASE" ]' in base_command
    assert 'elif [ "$GITHUB_REF_TYPE" = "tag" ]' in base_command
    assert "git tag --sort=-version:refname" in base_command
    assert "git merge-base HEAD origin/main" in base_command
    assert 'echo "base=$base"' in base_command

    base = _step(job, "Run base coverage sequence")
    head = _step(job, "Run head coverage sequence")
    for step, filename in ((base, "coverage-base.json"), (head, "coverage-head.json")):
        assert "if" not in step
        command = str(step["run"])
        assert "--dist loadgroup" in command
        assert "not repo_lint and not reality" in command
        assert "find tests/unit -maxdepth 1" in command
        assert "find tests/unit/cli -maxdepth 1" in command
        assert "tests/integration" not in command
        assert "tests/server" not in command
        assert "--cov=src/notebooklm" in command
        assert f"json:$RUNNER_TEMP/{filename}" in command
        assert "--cov-fail-under=0" in command
        assert "scripts/check_coverage_thresholds.py" not in command
        assert "playwright install" not in command

    collection = str(_step(job, "Collect base and head scenario nodes")["run"])
    assert "collection-base.json" in collection
    assert "collection-head.json" in collection
    validation = str(_step(job, "Validate auth behavior and coverage delta")["run"])
    assert "--base-collection" in validation and "--head-collection" in validation
    assert "scripts/check_auth_coverage_delta.py" in validation
    assert "--base-workspace" in validation and "--head-workspace" in validation
    assert "test_audit_auth_patch_sites.py" in validation
    assert "test_audit_auth_shared_mutations.py" in validation
    assert "test_auth_behavior_scenario_policy.py" in validation
    assert "test_auth_coverage_allowance_policy.py" in validation
    assert "test_auth_lifecycle_cleanup_policy.py" in validation
    assert "test_check_auth_coverage_delta.py" in validation
    assert "--timeout=180" in validation
    assert "scripts/check_auth_lifecycle_cleanup_policy.py" in validation
    assert "--head-collection" in validation

    upload = _step(job, "Upload auth patch coverage evidence on failure")
    assert upload["if"] == "failure()"
    assert "coverage-base.json" in str(upload["with"]["path"])
    assert "coverage-head.json" in str(upload["with"]["path"])

    publish = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    release_gate = publish["jobs"]["auth-patch-audit"]
    assert release_gate["uses"] == "./.github/workflows/auth-patch-audit.yml"
    assert publish["jobs"]["build-and-test"]["needs"] == "auth-patch-audit"


def test_nightly_runs_full_sha_pinned_compatibility_matrix() -> None:
    """Nightly owns the full 3-OS by 5-Python ordinary test matrix (PRs run a reduced one)."""
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["compatibility"]

    assert job["needs"] == "resolve-target"
    assert job["if"] == (
        "needs.resolve-target.outputs.is_standard == 'true' && "
        "(github.event_name == 'schedule' || inputs.run_compatibility)"
    )
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "os": SUPPORTED_OSES,
            "python-version": SUPPORTED_PYTHONS,
        },
    }
    assert "environment" not in job
    assert "secrets." not in str(job)

    workflow_text = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
    # PyYAML parses a bare ``on`` key as boolean ``True``.
    triggers = workflow.get("on", workflow.get(True))
    dispatch_inputs = triggers["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["run_compatibility"]["type"] == "boolean"
    # Manual dispatches (release branches) default to the full matrix because the
    # PR gate only runs the reduced 7-cell one.
    assert dispatch_inputs["run_compatibility"]["default"] is True
    assert "run_compatibility:" in workflow_text

    checkout = next(
        step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"] == {
        "ref": "${{ needs.resolve-target.outputs.sha }}",
        "fetch-depth": 1,
        "persist-credentials": False,
    }

    setup_python = _step(job, "Set up Python ${{ matrix.python-version }}")
    assert setup_python["with"]["python-version"] == "${{ matrix.python-version }}"

    install_command = str(_step(job, "Install compatibility dependencies")["run"])
    assert "uv sync --frozen" in install_command
    for extra in {"browser", "dev", "markdown", "mcp", "server", "impersonate", "cookies"}:
        assert f"--extra {extra}" in install_command

    import_command = str(_step(job, "Assert native optional dependencies import")["run"])
    assert "import curl_cffi, rookie_cookies" in import_command
    assert "callable(rookie_cookies.load)" in import_command
    assert "callable(rookie_cookies.any_browser)" in import_command

    suite_command = str(_step(job, "Run compatibility tests without coverage")["run"])
    assert "-n auto" in suite_command
    assert "--dist loadgroup" in suite_command
    assert "not repo_lint and not requires_playwright and not requires_chromium" in suite_command
    assert "--no-cov" in suite_command


def test_nightly_coverage_is_sha_pinned_secret_free_and_enforces_floors() -> None:
    """Scheduled/manual nightly owns global and per-file coverage enforcement."""
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}

    resolve_job = workflow["jobs"]["resolve-target"]
    resolve_checkout = next(
        step
        for step in resolve_job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert resolve_checkout["with"] == {
        "ref": "refs/heads/main",
        "fetch-depth": 1,
        "persist-credentials": False,
    }

    job = workflow["jobs"]["coverage"]
    assert job["needs"] == "resolve-target"
    assert job["if"] == "needs.resolve-target.outputs.is_standard == 'true'"
    assert job["runs-on"] == "ubuntu-latest"
    assert "environment" not in job
    assert "secrets." not in str(job)

    checkout = next(
        step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"] == {
        "ref": "${{ needs.resolve-target.outputs.sha }}",
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


def test_nightly_e2e_maps_backends_and_suites_to_designated_runners() -> None:
    """Full Web runs on Ubuntu, full Android on macOS, and read-only Web on Windows."""
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["e2e"]
    planner = workflow["jobs"]["plan-live-lanes"]

    assert job["strategy"]["matrix"] == "${{ fromJSON(needs.plan-live-lanes.outputs.matrix) }}"
    assert job["env"]["NOTEBOOKLM_BACKEND"] == "${{ matrix.backend }}"
    assert job["concurrency"] == {
        "group": "notebooklm-account-${{ matrix.account_slot }}",
        "queue": "max",
        "cancel-in-progress": False,
    }
    assert job["environment"] == "protected-readonly"
    assert job["timeout-minutes"] == 360

    planner_run = str(_step(planner, "Select live account slots")["run"])
    assert "--lane nightly-web-ubuntu" in planner_run
    assert "--lane nightly-android-macos" in planner_run
    assert "--lane nightly-readonly-windows" in planner_run
    assert planner_run.count('"os": "ubuntu-latest"') == 1
    assert planner_run.count('"os": "macos-latest"') == 1
    assert planner_run.count('"os": "windows-latest"') == 1
    assert planner_run.count('"backend": "web"') == 2
    assert planner_run.count('"backend": "android"') == 1
    assert planner_run.count('"mode": "full"') == 2
    assert planner_run.count('"mode": "readonly"') == 1
    assert '"selection": "readonly and not variants"' in planner_run
    assert 'if not os.environ["TEST_FILTER"]' in planner_run

    install = str(_step(job, "Install dependencies")["run"])
    assert "uv sync --frozen" in install
    assert "--extra android" in install

    auth = _step(job, "Materialize selected account")
    assert auth["env"] == {
        "NOTEBOOKLM_MASTER_TOKEN_JSON": "${{ secrets[matrix.master_token_secret_name] }}"
    }
    assert "materialize_ci_auth.py" in str(auth["run"])

    provision = _step(job, "Provision managed role copies")
    assert provision["if"] == "steps.auth.outcome == 'success' && steps.sweep.outcome == 'success'"
    provision_command = str(provision["run"])
    assert "manage_ci_e2e_notebooks.py provision" in provision_command
    assert '--mode "${{ matrix.mode }}"' in provision_command
    assert "--github-env" in provision_command

    preflight = _step(job, "Backend preflight")
    assert preflight["if"] == "steps.provision.outcome == 'success'"
    preflight_command = str(preflight["run"])
    assert "import grpc" in preflight_command
    assert "import gpsoauth" in preflight_command
    assert "client.notebooks.get(notebook_id)" in preflight_command

    journal = _step(job, "Configure generation journal policy")
    journal_command = str(journal["run"])
    assert "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE=required" in journal_command
    assert "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE=off" in journal_command

    primary = _step(job, "Run primary E2E tests")
    retry = _step(job, "Retry failed E2E tests after 10-min cool-down")
    assert retry["env"]["TEST_FILTER"] == "${{ inputs.test_filter }}"
    retry_command = str(retry["run"])
    assert 'if [ -n "$TEST_FILTER" ]' in retry_command
    assert "unset E2E_ENFORCE_COVERAGE_FLOOR" in retry_command
    assert "tests/e2e --last-failed --last-failed-no-failures=none" in retry_command

    primary_command = str(primary["run"])
    assert '-m "${{ matrix.selection }}"' in primary_command
    filtered_branch = primary_command.split("else", 1)[0]
    assert "${{ matrix.selection }}" not in filtered_branch

    curl_smoke = _step(job, "curl_cffi transport smoke")
    assert "matrix.lane == 'nightly-web-ubuntu'" in str(curl_smoke["if"])

    verifier = _step(job, "Verify generation operation journal")
    assert "--mode journal" in str(verifier["run"])
    assert job["steps"].index(verifier) < job["steps"].index(
        _step(job, "Cleanup managed role copies")
    )
    assert _step(job, "Cleanup managed role copies")["if"] == "always()"
    assert _step(job, "Purge local credentials and handles")["if"] == "always()"


def test_verify_package_live_checks_published_wheel_android_and_keeps_web_e2e() -> None:
    """Package verification proves Android deps/protos/live GetProject without replacing Web."""
    workflow = yaml.safe_load(VERIFY_PACKAGE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["verify"]

    install = str(_step(job, "Sync locked deps + non-cookies extras")["run"])
    assert "--extra android" in install

    android = _step(job, "Validate published wheel Android backend")
    assert "github.repository == 'teng-lin/notebooklm-py'" in android["if"]
    assert "steps.provision.outcome == 'success'" in android["if"]
    command = str(android["run"])
    assert "import grpc" in command
    assert "import gpsoauth" in command
    assert "read_pb2.GetProjectRequest.DESCRIPTOR.full_name" in command
    assert 'NotebookLMClient.from_storage(backend="android")' in command
    assert 'set(client.backends.values()) != {"android"}' in command
    assert "client.notebooks.get(notebook_id)" in command

    steps = job["steps"]
    assert steps.index(android) > steps.index(_step(job, "Materialize selected account"))
    assert steps.index(android) > steps.index(
        _step(job, "Install published wheel from TestPyPI (--no-deps)")
    )

    web_e2e = _step(job, "Run primary E2E tests")
    assert "NOTEBOOKLM_BACKEND" not in job.get("env", {})
    assert 'pytest tests/e2e -m "not variants"' in str(web_e2e["run"])


@pytest.mark.parametrize("workflow_path", [PUBLISH_WORKFLOW, TESTPYPI_PUBLISH_WORKFLOW])
def test_release_publish_smokes_install_impersonate_extra(workflow_path: Path) -> None:
    """Published-wheel unit smoke must install every CI-required transport."""
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    job = workflow["jobs"]["build-and-test"]
    install = str(_step(job, "Install built wheel + release-smoke extras in a clean venv")["run"])

    assert '"${WHEEL}[browser,dev,markdown,impersonate]"' in install


def test_pr_and_release_workflows_verify_clean_base_wheel() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    quality = workflow["jobs"]["quality"]
    pr_smoke = str(_step(quality, "Verify built base wheel without browser extra")["run"])
    assert "uv build --wheel" in pr_smoke
    assert "scripts/check_base_wheel.py" in pr_smoke

    for workflow_path in (PUBLISH_WORKFLOW, TESTPYPI_PUBLISH_WORKFLOW):
        release = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        job = release["jobs"]["build-and-test"]
        smoke = _step(job, "Verify built base wheel without browser extra")
        assert "scripts/check_base_wheel.py" in str(smoke["run"])
        assert job["steps"].index(smoke) > job["steps"].index(
            _step(job, "Upload distribution artifacts")
        )


def test_verify_package_downloads_exact_wheel_before_clean_base_smoke() -> None:
    workflow = yaml.safe_load(VERIFY_PACKAGE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["verify"]
    download = _step(job, "Download exact published wheel for base-install smoke")
    smoke = _step(job, "Verify published base wheel without browser extra")

    assert "pip download" in str(download["run"])
    assert "--no-deps" in str(download["run"])
    assert "--only-binary=:all:" in str(download["run"])
    assert "published-dist/notebooklm_py-*.whl" in str(smoke["run"])
    assert "scripts/check_base_wheel.py" in str(smoke["run"])
    assert job["steps"].index(smoke) < job["steps"].index(
        _step(job, "Sync locked deps + non-cookies extras")
    )


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

    assert job["needs"] == "resolve-target"
    assert job["if"] == "needs.resolve-target.outputs.is_standard == 'true'"
    assert job["runs-on"] == "ubuntu-latest"
    assert "environment" not in job
    assert "secrets." not in str(job)

    checkout = next(
        step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ needs.resolve-target.outputs.sha }}"

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
