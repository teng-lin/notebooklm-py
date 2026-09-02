"""Guard the rebrand-host lane's ISOLATION in ``.github/workflows/rpc-health.yml``.

The nightly health workflow files its "Non-transient ERROR detected" issue with
a dedup probe that searches **by title alone**. So an issue-filing lane that can
fail every night is not merely noisy — after its first issue opens, the dedup
check suppresses every subsequent issue sharing that title, including the
main health-check degradation issue.

The rebrand-host probe (``notebook.google.com``) therefore gets its own titles,
a dedup key PER CAPABILITY, and a *state-change* trigger rather than a recurring
error. It can report either direction without changing the main lane's exit code.

The same reasoning applies one level down (#2062): the lane's state file is the
next run's baseline, so anything it records must have been genuinely observed
tonight and genuinely reported. Hence the two further invariants pinned here —
the document read back must be one this run wrote, and a transition that could
not be reported must not advance the cached baseline.

These assertions are cheap and the failure they prevent is silent and total —
a suppressed alarm looks identical to a healthy night.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rpc-health.yml"

# The main lane's title. The rebrand lane must never reuse it, nor any string
# that would collide with its ``in:title "..."`` dedup search.
MAIN_ERROR_TITLE = "RPC Health Check: Non-transient ERROR detected"
REBRAND_TITLE = "Rebrand host RPC availability changed"

DETECT_STEP = "Detect rebrand-host state change"
REPORT_STEP = "Report rebrand-host state changes"
SAVE_STEP = "Save rebrand-host lane state"


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["health-check"]["steps"]
    assert isinstance(steps, list)
    return steps


def _step(name: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"step not found in rpc-health.yml: {name!r}")


def _step_index(name: str) -> int:
    for index, step in enumerate(_steps()):
        if step.get("name") == name:
            return index
    raise AssertionError(f"step not found in rpc-health.yml: {name!r}")


def _issue_steps() -> list[dict[str, Any]]:
    return [
        step
        for step in _steps()
        if str(step.get("uses", "")).startswith("peter-evans/create-issue-from-file")
    ]


def test_health_step_feeds_the_rebrand_state_file() -> None:
    """Without the state file the lane cannot report a *change*, only a state."""
    run = _step("Run RPC Health Check")["run"]
    assert "--rebrand-state-file rebrand-state.json" in run


def test_previous_and_current_state_live_in_separate_files() -> None:
    """The read slot and the write slot must not be the same path (#2062).

    ``rebrand-state.json`` is restored from cache BEFORE the script runs, and it
    carries a run-scoped ``changed`` flag. Sharing one path means a run whose
    script exits early (``load_auth`` exits 2 on an auth failure and writes
    nothing) leaves yesterday's document in place for the detect step to read as
    tonight's result.
    """
    run = _step("Run RPC Health Check")["run"]
    assert "--rebrand-previous-state-file rebrand-state-prev.json" in run
    assert "--rebrand-run-id" in run

    rename = _step("Move restored rebrand state into the previous-run slot")["run"]
    assert "mv rebrand-state.json rebrand-state-prev.json" in rename
    # And it must happen after the restore but before the script runs.
    assert (
        _step_index("Restore rebrand-host lane state")
        < _step_index("Move restored rebrand state into the previous-run slot")
        < _step_index("Run RPC Health Check")
    )


def test_nightly_never_forces_a_base_url() -> None:
    """The scheduled run must stay on the default host.

    ``--base-url`` exists for manual investigation. The scheduled main signal
    must exercise whichever host the shipped configuration selects by default.
    """
    assert "--base-url" not in _step("Run RPC Health Check")["run"]


def test_rebrand_issue_title_is_distinct_from_every_other_lane() -> None:
    """The rebrand lane files through ``gh``, never through a shared title."""
    titles = [step["with"]["title"] for step in _issue_steps()]
    assert MAIN_ERROR_TITLE in titles
    assert len(titles) == len(set(titles)), f"duplicate issue titles share a dedup key: {titles}"
    # No pinned-action lane may claim the rebrand title: that lane's titles are
    # built per capability inside the report step.
    assert not any(title.startswith(REBRAND_TITLE) for title in titles)


def test_rebrand_dedup_probe_searches_its_own_title() -> None:
    run = _step(REPORT_STEP)["run"]
    assert f'in:title "{REBRAND_TITLE}"' in run
    assert MAIN_ERROR_TITLE not in run


def test_rebrand_dedup_key_is_per_capability() -> None:
    """One key for both capabilities loses the second transition (#2062).

    Each observed transition is folded into the state document that becomes the
    next run's baseline. So a ``chat`` transition suppressed by an open
    ``batchexecute`` issue is not merely deferred — the next run compares
    against the already-updated baseline, sees no change, and never reports it.
    """
    run = _step(REPORT_STEP)["run"]
    assert f'TITLE="{REBRAND_TITLE}: ${{capability}}"' in run
    # Exact-title match, so the shared search prefix cannot cross-suppress.
    assert "select(.title == env.TITLE)" in run
    # And an already-open issue is UPDATED, never silently dropped.
    assert "gh issue comment" in run


def test_rebrand_issue_fires_only_on_a_state_change() -> None:
    """A permanently-absent rebrand host must file nothing, ever."""
    condition = _step(REPORT_STEP)["if"]
    assert "steps.rebrand.outputs.changed == 'true'" in condition
    # And it must not be gated on the health exit code — that is the other lane.
    assert "steps.health.outputs.exit_code" not in condition


def test_baseline_is_saved_after_reporting_and_only_on_success() -> None:
    """The cache is the next run's baseline; it may not absorb what went unreported."""
    assert _step_index(SAVE_STEP) > _step_index(REPORT_STEP) > _step_index(DETECT_STEP)
    condition = _step(SAVE_STEP)["if"]
    assert "steps.rebrand_report.outputs.report_failed != 'true'" in condition
    assert "hashFiles('rebrand-state.json') != ''" in condition
    # A report step that died outright reported nothing either.
    assert "steps.rebrand_report.outcome != 'failure'" in condition


def test_main_issue_lanes_are_untouched_by_the_rebrand_lane() -> None:
    """Every health-script lane still keys off ``steps.health.outputs.exit_code``.

    The bundle-drift lane is a different script (``steps.bundle``) and keeps its
    own gate. Authentication is intentionally shared because either script can
    prove the homepage is unavailable; all remaining health-script lanes stay
    keyed only to their health exit code.
    """
    expected = {
        "RPC ID Mismatch Detected": "1",
        MAIN_ERROR_TITLE: "3",
        "Studio customization table drifted — re-capture format enums": "4",
    }
    seen = set()
    for step in _issue_steps():
        title = step["with"]["title"]
        if title not in expected:
            continue
        seen.add(title)
        assert f"steps.health.outputs.exit_code == '{expected[title]}'" in step["if"]
    assert seen == set(expected)

    auth = next(
        step
        for step in _issue_steps()
        if step["with"]["title"] == "RPC Health Check: Authentication Failure"
    )
    assert "steps.health.outputs.exit_code == '2'" in auth["if"]
    assert "steps.bundle.outputs.exit_code == '2'" in auth["if"]
    assert "||" in auth["if"]


# ---------------------------------------------------------------------------
# Executable coverage of the two shell steps.
#
# The structural assertions above pin the wiring; these RUN the step scripts
# straight out of the YAML against a stubbed ``gh`` and ``uv``, because both
# fixed bugs are behavioural — "a stale document is read as tonight's result"
# and "the second capability's transition is swallowed" — and neither is
# visible in the wiring alone.
# ---------------------------------------------------------------------------

BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(BASH is None, reason="needs bash to run the step scripts")

# A ``gh`` stand-in: records every invocation, and answers ``issue list`` from
# GH_STUB_OPEN_TITLES exactly as the real ``--jq`` filter would (empty when the
# title is not open). GH_STUB_FAIL_ON makes one subcommand fail.
_GH_STUB = """\
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["GH_STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
subcommand = argv[1] if len(argv) > 1 else ""
if subcommand == os.environ.get("GH_STUB_FAIL_ON"):
    sys.stderr.write("stub failure\\n")
    sys.exit(1)
if subcommand == "list":
    open_titles = [t for t in os.environ.get("GH_STUB_OPEN_TITLES", "").split("\\n") if t]
    if os.environ.get("TITLE") in open_titles:
        print("77")
sys.exit(0)
"""

# ``uv run python -`` -> this interpreter. The step scripts shell out to uv;
# the tests must not depend on uv being on PATH or on a synced project.
_UV_STUB = """\
#!/bin/sh
[ "$1" = run ] || exit 64
shift 2
exec "$UV_STUB_PYTHON" "$@"
"""


def _stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_py = bin_dir / "gh_stub.py"
    gh_py.write_text(_GH_STUB, encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "$UV_STUB_PYTHON" "{gh_py}" "$@"\n', encoding="utf-8")
    uv = bin_dir / "uv"
    uv.write_text(_UV_STUB, encoding="utf-8")
    gh.chmod(0o755)
    uv.chmod(0o755)
    return bin_dir


def _run_step(
    step_name: str,
    workdir: Path,
    *,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    script = workdir / "step.sh"
    script.write_text(_step(step_name)["run"], encoding="utf-8")
    full_env = {
        **os.environ,
        "PATH": f"{_stub_bin(workdir)}{os.pathsep}{os.environ['PATH']}",
        "UV_STUB_PYTHON": sys.executable,
        "GITHUB_REPOSITORY": "teng-lin/notebooklm-py",
        "GITHUB_SHA": "deadbeef",
        "GITHUB_OUTPUT": str(workdir / "step-output"),
        "GITHUB_STEP_SUMMARY": str(workdir / "step-summary"),
        "GH_STUB_LOG": str(workdir / "gh-calls.jsonl"),
        **env,
    }
    assert BASH is not None
    return subprocess.run(
        [BASH, str(script)],
        cwd=workdir,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _outputs(workdir: Path) -> dict[str, str]:
    raw = (
        (workdir / "step-output").read_text(encoding="utf-8")
        if (workdir / "step-output").exists()
        else ""
    )
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def _gh_calls(workdir: Path) -> list[list[str]]:
    path = workdir / "gh-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _state_doc(run_id: str | None, transitions: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "version": 2,
        "host": "notebook.google.com",
        "run_id": run_id,
        "state": {"batchexecute": "PRESENT", "chat": "PRESENT"},
        "observed": {"batchexecute": "PRESENT", "chat": "PRESENT"},
        "transitions": transitions,
        "changed": bool(transitions),
    }


def _transition(capability: str) -> dict[str, str]:
    return {"capability": capability, "from": "ABSENT", "to": "PRESENT", "detail": "d"}


@pytest.fixture
def lane_workdir(tmp_path: Path) -> Path:
    (tmp_path / "health-report.txt").write_text(
        "REBRAND  host: notebook.google.com\nREBRAND  chat  PRESENT - ok\n", encoding="utf-8"
    )
    return tmp_path


@requires_bash
def test_detect_ignores_a_state_file_this_run_did_not_write(lane_workdir: Path) -> None:
    """The restored document's run-scoped ``changed`` flag is not tonight's news.

    This is the regression for the early-exit path: the script never ran, so
    the only ``rebrand-state.json`` on disk is the cache's — and it says a
    transition happened, on some previous night.
    """
    (lane_workdir / "rebrand-state.json").write_text(
        json.dumps(_state_doc("111", [_transition("batchexecute")])), encoding="utf-8"
    )
    result = _run_step(DETECT_STEP, lane_workdir, env={"GITHUB_RUN_ID": "222"})
    assert result.returncode == 0, result.stderr
    assert _outputs(lane_workdir)["changed"] == "false"
    # And the lane going dark is annotated, not silent.
    assert "::warning::" in result.stdout and "not this run" in result.stdout


@requires_bash
def test_detect_reports_a_change_this_run_actually_wrote(lane_workdir: Path) -> None:
    (lane_workdir / "rebrand-state.json").write_text(
        json.dumps(_state_doc("222", [_transition("batchexecute")])), encoding="utf-8"
    )
    result = _run_step(DETECT_STEP, lane_workdir, env={"GITHUB_RUN_ID": "222"})
    assert result.returncode == 0, result.stderr
    assert _outputs(lane_workdir)["changed"] == "true"


@requires_bash
def test_detect_treats_an_unstamped_document_as_not_ours(lane_workdir: Path) -> None:
    """A document from before the stamp existed cannot prove its provenance."""
    doc = _state_doc(None, [_transition("chat")])
    doc.pop("run_id")
    (lane_workdir / "rebrand-state.json").write_text(json.dumps(doc), encoding="utf-8")
    result = _run_step(DETECT_STEP, lane_workdir, env={"GITHUB_RUN_ID": "222"})
    assert _outputs(lane_workdir)["changed"] == "false", result.stderr


@requires_bash
def test_report_files_one_issue_per_capability(lane_workdir: Path) -> None:
    (lane_workdir / "rebrand-state.json").write_text(
        json.dumps(_state_doc("1", [_transition("batchexecute"), _transition("chat")])),
        encoding="utf-8",
    )
    result = _run_step(REPORT_STEP, lane_workdir, env={})
    assert result.returncode == 0, result.stderr
    created = [call for call in _gh_calls(lane_workdir) if call[:2] == ["issue", "create"]]
    titles = {call[call.index("--title") + 1] for call in created}
    assert titles == {
        f"{REBRAND_TITLE}: batchexecute",
        f"{REBRAND_TITLE}: chat",
    }
    assert _outputs(lane_workdir)["report_failed"] == "false"


@requires_bash
def test_an_open_issue_never_suppresses_another_capability(lane_workdir: Path) -> None:
    """The bug this lane was fixed for: ``chat`` must not vanish behind ``batchexecute``.

    With one shared dedup key an open ``batchexecute`` issue swallowed the
    ``chat`` transition — permanently, since the state file had already
    baselined it.
    """
    (lane_workdir / "rebrand-state.json").write_text(
        json.dumps(_state_doc("1", [_transition("batchexecute"), _transition("chat")])),
        encoding="utf-8",
    )
    result = _run_step(
        REPORT_STEP,
        lane_workdir,
        env={"GH_STUB_OPEN_TITLES": f"{REBRAND_TITLE}: batchexecute"},
    )
    assert result.returncode == 0, result.stderr
    calls = _gh_calls(lane_workdir)
    created = [call for call in calls if call[:2] == ["issue", "create"]]
    commented = [call for call in calls if call[:2] == ["issue", "comment"]]
    # The chat transition still gets its own issue...
    assert [call[call.index("--title") + 1] for call in created] == [f"{REBRAND_TITLE}: chat"]
    # ...and the already-open batchexecute one is updated, not dropped.
    assert len(commented) == 1 and commented[0][2] == "77"


@requires_bash
def test_report_failure_is_surfaced_so_the_baseline_is_not_advanced(lane_workdir: Path) -> None:
    (lane_workdir / "rebrand-state.json").write_text(
        json.dumps(_state_doc("1", [_transition("chat")])), encoding="utf-8"
    )
    result = _run_step(REPORT_STEP, lane_workdir, env={"GH_STUB_FAIL_ON": "create"})
    assert result.returncode == 0, result.stderr
    assert _outputs(lane_workdir)["report_failed"] == "true"
