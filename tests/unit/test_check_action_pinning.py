"""Tests for ``scripts/check_action_pinning.py``.

The script enforces that every third-party action (i.e. not ``actions/*``)
in the privileged workflow set is referenced by a 40-character commit SHA
rather than a floating tag. Tests cover:

* SHA-pinned third-party action -> pass.
* Floating-tag third-party action (e.g. ``@v1``) -> fail with the
  offending file:line in the error.
* Floating-tag *first-party* (``actions/checkout@v6``) -> still pass.
* Branch / short-SHA refs treated as floating -> fail.
* Inline ``# @ v1.2.3`` comment after the SHA is tolerated.
* Argument errors (missing privileged workflow file, bad directory) -> rc 2.

Script is imported via spec-loading to match the convention used by
``test_check_workflow_secret_gates.py`` and ``test_check_coverage_thresholds.py``
(``scripts/`` is not a Python package in this repo).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from textwrap import dedent

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_action_pinning.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_action_pinning", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_module()


# Tiny skeleton workflow body that compiles to valid YAML and contains
# exactly one ``uses:`` line. Tests parameterize the ref portion.
_WORKFLOW_TEMPLATE = """
name: example
on: push
permissions:
  contents: read
jobs:
  example:
    runs-on: ubuntu-latest
    steps:
    - uses: {uses}
"""


def _write_privileged_set(
    dir_path: Path,
    *,
    uses_per_file: dict[str, str] | None = None,
    default_uses: str = "actions/checkout@v6",
) -> None:
    """Materialize all privileged workflows in ``dir_path``.

    The script asserts every name in ``PRIVILEGED_WORKFLOWS`` exists — so
    we have to create the whole set even if only one is interesting. The
    name list is pulled live from the script module so a future PR that
    adds a 7th privileged workflow doesn't silently leave the test
    helper one short. Pass ``uses_per_file`` to override the ``uses:``
    line for specific files.
    """
    uses_per_file = uses_per_file or {}
    for name in _load_module().PRIVILEGED_WORKFLOWS:
        body = _WORKFLOW_TEMPLATE.format(uses=uses_per_file.get(name, default_uses))
        (dir_path / name).write_text(dedent(body).lstrip("\n"))


def _run(script, tmp_path, monkeypatch, capsys) -> tuple[int, str, str]:
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_action_pinning.py", "--workflow-dir", str(tmp_path)],
    )
    rc = script.main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_all_pinned_passes(tmp_path, monkeypatch, capsys, script):
    """SHA-pinned third-party + floating first-party = clean."""
    pinned = "pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b"
    _write_privileged_set(
        tmp_path,
        uses_per_file={"publish.yml": pinned, "testpypi-publish.yml": pinned},
        # The rest use floating actions/checkout — first-party, allowed.
    )
    rc, out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0, err
    assert "OK" in out


def test_first_party_floating_is_allowed(tmp_path, monkeypatch, capsys, script):
    """``actions/*`` floating tag is explicitly OK."""
    _write_privileged_set(tmp_path, default_uses="actions/setup-python@v6")
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0, err


def test_sha_with_trailing_tag_comment_passes(tmp_path, monkeypatch, capsys, script):
    """The ``# @ v1.x`` comment after a SHA must not confuse the parser."""
    line = "pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b  # @ v1.14.0"
    _write_privileged_set(tmp_path, uses_per_file={"publish.yml": line})
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0, err


# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------


def test_floating_third_party_tag_fails(tmp_path, monkeypatch, capsys, script):
    """A bare ``@v1`` on a third-party action is the canonical violation."""
    _write_privileged_set(
        tmp_path,
        uses_per_file={"publish.yml": "pypa/gh-action-pypi-publish@release/v1"},
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "publish.yml" in err
    assert "pypa/gh-action-pypi-publish" in err
    assert "release/v1" in err
    # File:line format is required so the violation is jump-to-able.
    assert "publish.yml:" in err


def test_floating_third_party_v1_fails(tmp_path, monkeypatch, capsys, script):
    _write_privileged_set(
        tmp_path,
        uses_per_file={"claude.yml": "anthropics/claude-code-action@v1"},
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "claude.yml" in err
    assert "anthropics/claude-code-action" in err


def test_short_sha_treated_as_floating(tmp_path, monkeypatch, capsys, script):
    """7-char abbreviated SHA must NOT satisfy the gate — easy to spoof."""
    _write_privileged_set(
        tmp_path,
        uses_per_file={"rpc-health.yml": "peter-evans/create-issue-from-file@cef2210"},
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "rpc-health.yml" in err


def test_branch_ref_fails(tmp_path, monkeypatch, capsys, script):
    """Branch refs like ``@main`` are even worse than tag refs."""
    _write_privileged_set(
        tmp_path,
        uses_per_file={"nightly.yml": "peter-evans/create-issue-from-file@main"},
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "nightly.yml" in err


def test_uppercase_sha_rejected(tmp_path, monkeypatch, capsys, script):
    """The 40-char regex is lowercase-only — keeps the canonical form.

    Git SHA output is conventionally lowercase; rejecting uppercase
    forces the line to round-trip through ``gh api ... --jq .sha`` (or
    equivalent) rather than a hand-typed value, which is the
    documented look-up workflow.
    """
    uppercase_sha = "CEF221092ED1BACB1CC03D23A2D87D1D172E277B"
    _write_privileged_set(
        tmp_path,
        uses_per_file={"publish.yml": f"pypa/gh-action-pypi-publish@{uppercase_sha}"},
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1


def test_multiple_violations_all_reported(tmp_path, monkeypatch, capsys, script):
    """Every violation, not just the first, should appear in stderr."""
    _write_privileged_set(
        tmp_path,
        uses_per_file={
            "publish.yml": "pypa/gh-action-pypi-publish@release/v1",
            "claude.yml": "anthropics/claude-code-action@v1",
        },
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "pypa/gh-action-pypi-publish" in err
    assert "anthropics/claude-code-action" in err
    assert "2 violation" in err


# ---------------------------------------------------------------------------
# Multiple uses: lines per file
# ---------------------------------------------------------------------------


def test_violation_on_second_uses_line(tmp_path, monkeypatch, capsys, script):
    """A clean first ``uses:`` must not mask a dirty second one."""
    # rpc-health.yml has three peter-evans/create-issue-from-file uses.
    # Pin two, leave one floating.
    pinned = "peter-evans/create-issue-from-file@fca9117c27cdc29c6c4db3b86c48e4115a786710"
    body = dedent(
        f"""
        name: rpc-health
        on: schedule
        permissions:
          contents: read
        jobs:
          ex:
            runs-on: ubuntu-latest
            steps:
            - uses: actions/checkout@v6
            - uses: {pinned}
            - uses: {pinned}
            - uses: peter-evans/create-issue-from-file@v6
        """
    ).lstrip("\n")
    _write_privileged_set(tmp_path)
    (tmp_path / "rpc-health.yml").write_text(body)
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    # The offending ``@v6`` line is line 12 of the body above (1-based).
    assert "rpc-health.yml:12" in err


# ---------------------------------------------------------------------------
# Argument / IO errors
# ---------------------------------------------------------------------------


def test_missing_privileged_file_returns_rc_2(tmp_path, monkeypatch, capsys, script):
    """Deleting a privileged workflow trips a hard fail (rc=2, not silent pass)."""
    _write_privileged_set(tmp_path)
    (tmp_path / "claude.yml").unlink()
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 2
    assert "claude.yml" in err


def test_nonexistent_workflow_dir(tmp_path, monkeypatch, capsys, script):
    bogus = tmp_path / "no-such-dir"
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_action_pinning.py", "--workflow-dir", str(bogus)],
    )
    rc = script.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "Not a directory" in captured.err


# ---------------------------------------------------------------------------
# Real repo guard
# ---------------------------------------------------------------------------


def test_real_repo_workflows_are_pinned(monkeypatch, script):
    """End-to-end: the actual ``.github/workflows`` in the repo must pass.

    This is the regression net: if anyone reverts a SHA pin to a floating
    tag without realizing it's privileged, this test fails immediately
    (mirrors what the CI quality job will catch on the same PR).
    """
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_action_pinning.py",
            "--workflow-dir",
            str(REPO_ROOT / ".github" / "workflows"),
        ],
    )
    err_buf = io.StringIO()
    out_buf = io.StringIO()
    with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(out_buf):
        rc = script.main()
    assert rc == 0, f"Real repo failed pinning check:\n{err_buf.getvalue()}"
