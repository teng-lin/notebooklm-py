"""Tests for ``scripts/check_workflow_secret_gates.py``.

Verifies the static check correctly accepts gated jobs and rejects
ungated jobs across the four gate shapes used in this repo:

* ``environment:`` at job level (literal name).
* ``environment:`` at job level (conditional expression).
* job-level ``if:`` guard pinning ``sender.login`` to a maintainer.
* step-level ``if:`` guard referencing ``is_standard``.

The script is imported via spec-loading (matching the sibling
``test_check_coverage_thresholds.py`` convention) because ``scripts/`` is
not a package in this repo.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_workflow_secret_gates.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_workflow_secret_gates", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_module()


def _write_workflow(dir_path: Path, name: str, body: str) -> Path:
    p = dir_path / name
    p.write_text(dedent(body).lstrip("\n"))
    return p


def _run(script, tmp_path, monkeypatch, capsys) -> tuple[int, str, str]:
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_workflow_secret_gates.py", "--workflow-dir", str(tmp_path)],
    )
    rc = script.main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_ungated_secret_fails(tmp_path, monkeypatch, capsys, script):
    _write_workflow(
        tmp_path,
        "bad.yml",
        """
        name: bad
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            steps:
            - name: leak
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err
    assert "'oops'" in err


def test_environment_literal_passes(tmp_path, monkeypatch, capsys, script):
    _write_workflow(
        tmp_path,
        "ok_env.yml",
        """
        name: ok-env
        on:
          workflow_dispatch:
        jobs:
          fine:
            runs-on: ubuntu-latest
            environment: protected-readonly
            steps:
            - name: use
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_environment_expression_passes(tmp_path, monkeypatch, capsys, script):
    # Mirrors the rpc-health.yml / nightly.yml pattern where the
    # environment is set only for workflow_dispatch and empty for
    # scheduled cron runs.
    _write_workflow(
        tmp_path,
        "ok_env_expr.yml",
        """
        name: ok-env-expr
        on:
          workflow_dispatch:
          schedule:
          - cron: '0 7 * * *'
        jobs:
          fine:
            runs-on: ubuntu-latest
            environment: ${{ github.event_name == 'workflow_dispatch' && 'protected-readonly' || '' }}
            steps:
            - name: use
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_step_if_with_is_standard_passes(tmp_path, monkeypatch, capsys, script):
    _write_workflow(
        tmp_path,
        "ok_step_if.yml",
        """
        name: ok-step-if
        on:
          workflow_dispatch:
        jobs:
          resolve:
            runs-on: ubuntu-latest
            outputs:
              is_standard: ${{ steps.r.outputs.is_standard }}
            steps:
            - id: r
              run: echo "is_standard=true" >> "$GITHUB_OUTPUT"
          fine:
            needs: resolve
            runs-on: ubuntu-latest
            steps:
            - name: use
              if: needs.resolve.outputs.is_standard == 'true'
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_step_if_block_scalar_with_is_standard_passes(tmp_path, monkeypatch, capsys, script):
    # The ``if: |`` block-scalar form must be parsed too. nightly.yml's
    # Retry step combines is_standard with steps.e2e.outcome via ``|``.
    _write_workflow(
        tmp_path,
        "ok_block_if.yml",
        """
        name: ok-block-if
        on:
          workflow_dispatch:
        jobs:
          fine:
            runs-on: ubuntu-latest
            steps:
            - name: use
              if: |
                needs.r.outputs.is_standard == 'true'
                && always()
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_job_if_with_sender_login_passes(tmp_path, monkeypatch, capsys, script):
    # claude.yml convention: pin sender.login to the maintainer at job
    # level. The step inside doesn't need its own gate because the job
    # already won't run for any other actor.
    _write_workflow(
        tmp_path,
        "ok_job_if.yml",
        """
        name: ok-job-if
        on:
          issue_comment:
            types: [created]
        jobs:
          claude:
            if: github.event.sender.login == 'teng-lin'
            runs-on: ubuntu-latest
            steps:
            - name: use
              env:
                X: ${{ secrets.CLAUDE_TOKEN }}
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_job_if_block_scalar_with_sender_login_passes(tmp_path, monkeypatch, capsys, script):
    # claude.yml uses the block-scalar form for its multi-line guard;
    # the job-level parser must accept that shape.
    _write_workflow(
        tmp_path,
        "ok_job_block_if.yml",
        """
        name: ok-job-block-if
        on:
          issue_comment:
            types: [created]
        jobs:
          claude:
            if: |
              github.event.sender.login == 'teng-lin' && (
                (github.event_name == 'issue_comment' && contains(github.event.comment.body, '@claude'))
              )
            runs-on: ubuntu-latest
            steps:
            - name: use
              env:
                X: ${{ secrets.CLAUDE_TOKEN }}
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_github_token_is_benign(tmp_path, monkeypatch, capsys, script):
    # GITHUB_TOKEN is auto-provisioned and constrained by `permissions:`,
    # so it does not need an environment gate.
    _write_workflow(
        tmp_path,
        "ok_github_token.yml",
        """
        name: ok-github-token
        on:
          workflow_dispatch:
        jobs:
          benign:
            runs-on: ubuntu-latest
            steps:
            - name: use
              env:
                GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
              run: gh issue list
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_empty_string_environment_does_not_count(tmp_path, monkeypatch, capsys, script):
    # Literal empty string is not a real environment association — must
    # still fail. (The ``${{ ... || '' }}`` expression *form* passes
    # because the conditional branch can yield a non-empty value; that's
    # covered by ``test_environment_expression_passes`` above.)
    _write_workflow(
        tmp_path,
        "bad_empty_env.yml",
        """
        name: bad-empty-env
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            environment: ""
            steps:
            - name: use
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err


def test_secret_reference_inside_comment_ignored(tmp_path, monkeypatch, capsys, script):
    # Example/documentation references inside `#` comments must not
    # trigger the gate. The verify-package.yml comment block in this PR
    # demonstrates the pattern.
    _write_workflow(
        tmp_path,
        "ok_comment.yml",
        """
        name: ok-comment
        on:
          workflow_dispatch:
        jobs:
          fine:
            # This is an example: ${{ secrets.DOCS_ONLY }} is not real.
            runs-on: ubuntu-latest
            steps:
            # Note: secrets.ALSO_DOCS would normally need a gate.
            - name: only echo
              run: echo "no secret here"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_missing_directory_returns_2(tmp_path, monkeypatch, capsys, script):
    bogus = tmp_path / "does-not-exist"
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_workflow_secret_gates.py", "--workflow-dir", str(bogus)],
    )
    rc = script.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "Not a directory" in captured.err


def test_secret_before_environment_in_same_job_passes(tmp_path, monkeypatch, capsys, script):
    # Key-order false-positive guard. ``environment:`` may appear AFTER a
    # ``${{ secrets.X }}`` reference in the same job (e.g. inside a
    # ``strategy: matrix`` block placed before the environment line by
    # an author following a convention from a different repo). The
    # checker must evaluate the gate against the JOB-FINAL state, not
    # the prefix-at-line state.
    _write_workflow(
        tmp_path,
        "ok_secret_first.yml",
        """
        name: ok-secret-first
        on:
          workflow_dispatch:
        jobs:
          fine:
            runs-on: ubuntu-latest
            strategy:
              matrix:
                tag: ["${{ secrets.MY_SECRET }}"]
            environment: protected-readonly
            steps:
            - name: use
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_secret_in_step_before_step_if_passes(tmp_path, monkeypatch, capsys, script):
    # Step-scope key-order guard. ``if:`` may appear AFTER the ``env:``
    # block in the same step. Checker must defer step-gate evaluation
    # to end-of-step rather than judge against the prefix state.
    _write_workflow(
        tmp_path,
        "ok_step_env_first.yml",
        """
        name: ok-step-env-first
        on:
          workflow_dispatch:
        jobs:
          resolve:
            runs-on: ubuntu-latest
            outputs:
              is_standard: ${{ steps.r.outputs.is_standard }}
            steps:
            - id: r
              run: echo "is_standard=true" >> "$GITHUB_OUTPUT"
          fine:
            needs: resolve
            runs-on: ubuntu-latest
            steps:
            - name: use
              env:
                X: ${{ secrets.MY_SECRET }}
              if: needs.resolve.outputs.is_standard == 'true'
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_trailing_comment_secret_reference_ignored(tmp_path, monkeypatch, capsys, script):
    # Trailing ``# ...`` comments on otherwise non-comment lines must
    # not produce false positives. Authors may write notes like
    # ``run: echo hi  # don't use ${{ secrets.X }} here``.
    _write_workflow(
        tmp_path,
        "ok_trailing_comment.yml",
        """
        name: ok-trailing-comment
        on:
          workflow_dispatch:
        jobs:
          fine:
            runs-on: ubuntu-latest
            steps:
            - name: noop with trailing comment
              run: echo hi  # don't use ${{ secrets.MY_SECRET }} here
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_is_standard_inside_block_scalar_comment_does_not_satisfy(
    tmp_path, monkeypatch, capsys, script
):
    # Defence against an `if: |` block-scalar that ONLY contains the
    # ``is_standard`` token inside a YAML comment. Such a guard does
    # NOT actually gate the run (comments are stripped before
    # expression evaluation), so the checker must reject it.
    _write_workflow(
        tmp_path,
        "bad_comment_decoy.yml",
        """
        name: bad-comment-decoy
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            steps:
            - name: leak
              if: |
                always() # is_standard
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err


def test_jobs_present_but_no_jobs_parsed_surfaces_violation(tmp_path, monkeypatch, capsys, script):
    # Robustness against silent-pass. If the YAML contains a ``jobs:``
    # header AND ``secrets.*`` references but the parser identifies no
    # jobs (e.g. unexpected indentation), the checker must surface that
    # rather than report "OK". Use a 3-space indent (legal YAML, but
    # the parser is pinned to 2-space) to trigger the miss.
    p = tmp_path / "weird_indent.yml"
    p.write_text(
        "name: weird\n"
        "on: workflow_dispatch:\n"
        "jobs:\n"
        "   weird:\n"
        "      runs-on: ubuntu-latest\n"
        "      steps:\n"
        "      - run: echo ${{ secrets.X }}\n"
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "parser identified no jobs" in err


def test_negation_guard_at_step_does_not_pass(tmp_path, monkeypatch, capsys, script):
    # An ``if: needs.r.outputs.is_standard != 'true'`` guard is the
    # inverse of the trusted condition — it explicitly RUNS on non-
    # standard branches, which is the opposite of what we want. The
    # checker must reject it (C-CODEX-3 fix).
    _write_workflow(
        tmp_path,
        "bad_negation_step.yml",
        """
        name: bad-negation-step
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            steps:
            - name: leak
              if: needs.r.outputs.is_standard != 'true'
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err


def test_negation_actor_guard_at_job_does_not_pass(tmp_path, monkeypatch, capsys, script):
    # ``if: github.actor != 'dependabot[bot]'`` substring-matches
    # ``github.actor`` but is NOT a trusted pin — it accepts every
    # actor except dependabot. Reject.
    _write_workflow(
        tmp_path,
        "bad_actor_negation.yml",
        """
        name: bad-actor-negation
        on:
          workflow_dispatch:
        jobs:
          oops:
            if: github.actor != 'dependabot[bot]'
            runs-on: ubuntu-latest
            steps:
            - name: leak
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err


def test_bare_actor_reference_does_not_pass(tmp_path, monkeypatch, capsys, script):
    # ``if: github.actor`` (truthy check — runs whenever actor is set,
    # i.e. always) is not a positive comparison and must be rejected.
    _write_workflow(
        tmp_path,
        "bad_bare_actor.yml",
        """
        name: bad-bare-actor
        on:
          workflow_dispatch:
        jobs:
          oops:
            if: github.actor
            runs-on: ubuntu-latest
            steps:
            - name: leak
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err


def test_bracket_notation_secret_detected(tmp_path, monkeypatch, capsys, script):
    # ``${{ secrets['MY_SECRET'] }}`` is a legal alternative to dot
    # notation. The checker must detect it (I-CODEX-1 fix).
    _write_workflow(
        tmp_path,
        "bad_bracket.yml",
        """
        name: bad-bracket
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            steps:
            - name: leak
              env:
                X: ${{ secrets['MY_SECRET'] }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "MY_SECRET" in err


def test_dynamic_bracket_indexing_detected(tmp_path, monkeypatch, capsys, script):
    # Dynamic indexing ``${{ secrets[matrix.name] }}`` can't be
    # statically resolved but must still trip the gate.
    _write_workflow(
        tmp_path,
        "bad_dynamic.yml",
        """
        name: bad-dynamic
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            steps:
            - name: leak
              env:
                X: ${{ secrets[matrix.name] }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.<dynamic>" in err


def test_secrets_inherit_requires_gate(tmp_path, monkeypatch, capsys, script):
    # ``secrets: inherit`` on a reusable-workflow call forwards ALL
    # caller secrets. Treat as a non-bypassable secret consumer.
    _write_workflow(
        tmp_path,
        "bad_inherit.yml",
        """
        name: bad-inherit
        on:
          workflow_dispatch:
        jobs:
          call:
            uses: ./.github/workflows/reusable.yml
            secrets: inherit
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "<inherit>" in err


def test_secrets_inherit_with_environment_passes(tmp_path, monkeypatch, capsys, script):
    # …but ``secrets: inherit`` paired with an approved environment is
    # acceptable (still gated by maintainer approval at the caller).
    _write_workflow(
        tmp_path,
        "ok_inherit_env.yml",
        """
        name: ok-inherit-env
        on:
          workflow_dispatch:
        jobs:
          call:
            uses: ./.github/workflows/reusable.yml
            secrets: inherit
            environment: protected-readonly
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_typoed_environment_name_does_not_pass(tmp_path, monkeypatch, capsys, script):
    # ``environment: protectd-readonly`` is a typo. GitHub would silently
    # auto-create that environment with no protection rules at runtime,
    # bypassing maintainer approval. The static checker must reject
    # unapproved environment names (C-CODEX-2 fix).
    _write_workflow(
        tmp_path,
        "bad_typo_env.yml",
        """
        name: bad-typo-env
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            environment: protectd-readonly
            steps:
            - name: leak
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err


def test_environment_expression_with_unapproved_literal_does_not_pass(
    tmp_path, monkeypatch, capsys, script
):
    # Conditional environment that resolves to a non-approved name on
    # one branch and empty on the other provides NO gate; reject.
    _write_workflow(
        tmp_path,
        "bad_expr_env.yml",
        """
        name: bad-expr-env
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            environment: ${{ github.event_name == 'workflow_dispatch' && 'staging' || '' }}
            steps:
            - name: leak
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err


def test_github_actor_positive_pin_passes(tmp_path, monkeypatch, capsys, script):
    # ``github.actor == '<name>'`` is the alternate spelling of the
    # sender.login pin; both are recognised as trusted job-level guards.
    _write_workflow(
        tmp_path,
        "ok_github_actor.yml",
        """
        name: ok-github-actor
        on:
          workflow_dispatch:
        jobs:
          fine:
            if: github.actor == 'teng-lin'
            runs-on: ubuntu-latest
            steps:
            - name: use
              env:
                X: ${{ secrets.MY_SECRET }}
              run: echo "$X"
        """,
    )
    rc, _out, _err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_inline_secret_on_step_header_line_detected(tmp_path, monkeypatch, capsys, script):
    # Secret appearing on the same line as the step key (e.g.
    # ``- run: echo ${{ secrets.X }}``) must still be detected. An
    # earlier version of the checker ``continue``d immediately after
    # the step header match and missed this.
    _write_workflow(
        tmp_path,
        "bad_inline_step.yml",
        """
        name: bad-inline-step
        on:
          workflow_dispatch:
        jobs:
          oops:
            runs-on: ubuntu-latest
            steps:
            - run: echo ${{ secrets.MY_SECRET }}
        """,
    )
    rc, _out, err = _run(script, tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.MY_SECRET" in err


def test_real_repo_workflows_pass(monkeypatch, capsys, script):
    """The real ``.github/workflows`` directory must pass.

    This is the load-bearing acceptance check: any new workflow file
    landing without a gate will fail this assertion via the CI quality
    job. Pinning the dependency here means a refactor of the checker
    itself can't silently weaken the real-world coverage.
    """
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_workflow_secret_gates.py", "--workflow-dir", str(workflows_dir)],
    )
    rc = script.main()
    assert rc == 0, capsys.readouterr().err
