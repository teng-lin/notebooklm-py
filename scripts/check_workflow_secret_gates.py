"""Assert workflow jobs that consume secrets are gated.

Companion to ``check_workflow_permissions.py``. That script proves the
``GITHUB_TOKEN`` blast radius is scoped; this one proves that any *user-
provided* secret (``secrets.NOTEBOOKLM_AUTH_JSON`` etc.) only unlocks on
runs that have at least one of:

* a job-level ``environment:`` declaration (which can require maintainer
  approval before secrets resolve), OR
* a job-level ``if:`` guard that pins the run to a trusted condition — we
  recognise ``sender.login``, ``github.actor``, and ``is_standard`` as the
  three conventions in use across this repo (manual-trigger actor pin,
  webhook actor pin, and branch-class pin respectively), OR
* a step-level ``if:`` guard whose expression references an ``is_standard``
  output (the convention from ``nightly.yml`` where the ``resolve-branch``
  job sets ``outputs.is_standard`` to ``true`` only for ``main`` /
  ``release/*`` / scheduled cron triggers).

``secrets.GITHUB_TOKEN`` is *not* counted as a user-provided secret — it's
the auto-provisioned token whose scopes are bounded by the top-level
``permissions:`` block (asserted independently by
``check_workflow_permissions.py``). Anything else under ``secrets.*`` is
in scope.

The check exists to prevent silent regressions where a workflow grows a
new ``env: FOO: ${{ secrets.SOMETHING }}`` block without picking up an approved
environment or trusted actor/branch ``if:`` gate. CI rejects the change.

Usage::

    python scripts/check_workflow_secret_gates.py
    python scripts/check_workflow_secret_gates.py --workflow-dir custom/path

Exit codes::

    0  All secret-consuming jobs are gated.
    1  One or more jobs use ``secrets.*`` without an approved environment or
       trusted actor/branch guard. Offending file/line/job is printed to stderr.
    2  Argument error (missing directory, etc.).

Implementation notes
--------------------

We intentionally avoid pulling in ``PyYAML`` for parity with
``check_workflow_permissions.py`` and to keep the quality job's install
footprint small. Workflow YAML in this repo uses a regular indentation
shape (2 spaces for top-level keys, 2 per nest), so a small line-oriented
state machine is sufficient to attribute each ``secrets.X`` reference to
its enclosing job + step and to detect the gates we care about. The
existing sibling checker uses the same pattern.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Tokens that count as a non-bypassable secret. ``GITHUB_TOKEN`` is
# auto-provisioned per-run and its scopes are constrained separately by
# the ``permissions:`` checker, so it does not need an environment gate.
_BENIGN_SECRETS = frozenset({"GITHUB_TOKEN"})

# Environments that we have actually configured with maintainer-approval
# reviewers in the GitHub UI. GitHub Actions silently auto-creates a
# referenced environment that doesn't exist — with NO protection rules —
# which would make a typo (``environment: protectd-readonly``) or a
# never-configured environment pass CI AND run without approval. Pin the
# checker to this allow-list so any new environment name requires (a)
# extending this set deliberately and (b) configuring the corresponding
# GitHub Environment first. See docs/development.md → "Workflow secret
# gates".
_APPROVED_ENVIRONMENTS = frozenset({"protected-readonly"})

# Secret reference shapes:
#   * Dot notation:    ``${{ secrets.MY_SECRET }}`` — the canonical form
#                      we see everywhere in this repo.
#   * Bracket notation: ``${{ secrets['MY_SECRET'] }}`` /
#                       ``${{ secrets["MY_SECRET"] }}`` — also legal.
#   * Dynamic indexing: ``${{ secrets[matrix.name] }}`` — caught by the
#                       open-bracket sentinel ``secrets[`` even though
#                       we can't statically resolve the secret name.
# All three pass through this regex; ``_extract_secret_names`` resolves
# the captured group(s) and returns concrete names where available,
# falling back to a sentinel (``<dynamic>``) for dynamic indexing so the
# gating check still runs.
_SECRET_REF_RE = re.compile(
    r"\bsecrets"
    r"(?:"
    r"\.([A-Za-z_][A-Za-z0-9_]*)"  # .NAME
    r"|\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"  # ['NAME']
    r"|\[\s*[^'\"\s\]][^\]]*\]"  # [<dynamic>]
    r")"
)

# Reusable-workflow ``secrets: inherit`` (passes ALL caller secrets to
# the called workflow). Always treat as a secret consumer.
_SECRETS_INHERIT_RE = re.compile(r"^\s*secrets:\s*inherit\s*(#.*)?$")

# Job header: two-space indent, then `<name>:` with nothing else on the
# line. We pick up the line number to point users at the source.
_JOB_HEADER_RE = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_-]*):\s*(#.*)?$")

# Step start: four-space indent then `- ` then either `name:` or `uses:`
# (or any other step-attribute key on the same line — pytest-style YAML
# never puts an inline scalar there).
_STEP_HEADER_RE = re.compile(r"^    - ([a-z][a-z0-9_-]*):")

# ``environment:`` declaration at job level (indent 4). May be a bare value
# (``environment: foo``), a quoted value, or an expression (``${{ ... }}``).
# Any non-empty value counts as "an environment is declared on this job".
_JOB_ENVIRONMENT_RE = re.compile(r"^    environment:\s*(\S.*?)\s*(#.*)?$")

# Job-level ``if:`` guard (indent 4). Mirrors ``_STEP_IF_RE`` for the
# enclosing job scope.
_JOB_IF_RE = re.compile(r"^    if:\s*(.*?)\s*(#.*)?$")

# Step-level ``if:`` guard. May be a single-line scalar or the start of a
# YAML block scalar (``if: |``). For the block case we collect subsequent
# more-indented lines.
_STEP_IF_RE = re.compile(r"^      if:\s*(.*?)\s*(#.*)?$")

# Comment line — entirely a YAML comment (optional leading whitespace
# then ``#``). We skip these for secret detection so example references
# in step comments (``secrets.NAME`` documentation strings) don't fail
# the gate. The ``${{ ... }}`` expansion itself is YAML-significant only
# in non-comment positions, so this is sound.
_COMMENT_RE = re.compile(r"^\s*#")

# A guard expression is considered "trusted" if it matches one of these
# POSITIVE-EQUALITY patterns — substring matching is unsafe because
# ``if: github.actor != 'dependabot[bot]'`` and
# ``if: needs.foo.outputs.is_standard != 'true'`` would both pass a
# substring check while NOT actually gating to the trusted condition.
# Each pattern requires an explicit ``==`` comparison to a quoted
# literal value so an inverted guard is rejected (C-CODEX-3 fix).
#
# Tokens:
#   * ``needs.<job>.outputs.is_standard == 'true'`` — branch-class pin
#     (nightly.yml's ``resolve-branch`` job sets this for main/release/* +
#     scheduled triggers).
#   * ``github.event.sender.login == '<actor>'`` /
#     ``github.actor == '<actor>'`` — actor pin (claude.yml's
#     load-bearing security check).
_TRUSTED_GUARD_PATTERNS = (
    # is_standard == 'true' / "true"  (quoting may be single or double)
    re.compile(
        r"is_standard\s*==\s*['\"]true['\"]",
        re.IGNORECASE,
    ),
    # sender.login == '<name>' / github.event.sender.login == '<name>'
    re.compile(
        r"sender\.login\s*==\s*['\"][A-Za-z0-9_.\-\[\]]+['\"]",
        re.IGNORECASE,
    ),
    # github.actor == '<name>'
    re.compile(
        r"github\.actor\s*==\s*['\"][A-Za-z0-9_.\-\[\]]+['\"]",
        re.IGNORECASE,
    ),
)


def _expression_is_trusted_guard(expr: str) -> bool:
    return any(p.search(expr) for p in _TRUSTED_GUARD_PATTERNS)


def _environment_value_is_approved(value: str) -> bool:
    """Return True iff the ``environment:`` value names an approved env.

    Accepts:
      * a bare name in ``_APPROVED_ENVIRONMENTS`` (e.g. ``protected-readonly``)
      * a quoted name (``"protected-readonly"`` / ``'protected-readonly'``)

    Rejects everything else, including:
      * empty strings, unknown names
      * **expression form** (``${{ ... }}``). The historical conditional
        shape
        ``${{ event == 'workflow_dispatch' && 'protected-readonly' || '' }}``
        silently broke scheduled runs once the referenced secret was
        env-only (issue #1009): the empty branch resolves to "no
        environment", and env-only secrets resolve to empty under that
        branch. An expression without an empty fallback offers nothing
        over the bare ``environment: protected-readonly`` form, so we
        force the bare form — legible at a glance and impossible to
        falsy-branch around. Expression values never strip-match an
        approved name, so the check below rejects them as a side-effect
        of the literal-only match.
    """
    if not value or value in ("''", '""'):
        return False
    return value.strip().strip("'\"") in _APPROVED_ENVIRONMENTS


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workflow-dir",
        default=".github/workflows",
        help="Directory containing workflow YAML files",
    )
    args = ap.parse_args()

    workflow_dir = Path(args.workflow_dir)
    if not workflow_dir.is_dir():
        print(f"Not a directory: {workflow_dir}", file=sys.stderr)
        return 2

    violations: list[str] = []
    workflow_files = sorted(list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml")))
    for path in workflow_files:
        violations.extend(_scan_workflow(path))

    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        return 1

    print(
        "OK: every workflow job that references user-provided secrets is "
        "gated by an approved `environment:` or trusted actor/branch `if:` guard."
    )
    return 0


def _scan_workflow(path: Path) -> list[str]:
    """Return a list of violation messages for ``path``.

    A violation is a ``secrets.X`` reference (X != GITHUB_TOKEN) whose
    enclosing job has neither an ``environment:`` declaration nor a job-
    or step-level ``if:`` guard referencing a trusted condition.

    The scan is two-pass per job: we first walk every line in the job to
    collect (line_no, secret_name, step_if_or_None) hits AND the job-
    final ``environment:`` + job-level ``if:`` state, then evaluate hits
    against the now-final job state. This avoids a class of key-order
    false-positives where the secret reference appears before the
    ``environment:`` declaration in the same job (e.g. inside a
    ``strategy: matrix`` block at the top of the job body).
    """
    lines = path.read_text().splitlines()

    # Workflow-wide state.
    jobs_section_started = False
    violations: list[str] = []

    # Per-job state — reset on every job header.
    current_job: str | None = None
    current_job_has_environment = False
    current_job_if = ""
    # Step state inside the current job. Steps are addressed by ordinal
    # index (-1 = not in any step yet) so secret hits tagged with the
    # step index can be matched against the final step ``if:`` value at
    # flush time, even when ``if:`` appears AFTER the ``env:`` block in
    # the step (step-scope key-order safety).
    in_step = False
    current_step_index = -1
    step_ifs: list[str] = []
    # Block-scalar tracking applies to both job- and step-level ``if:``.
    in_if_block_scalar = False
    if_block_scalar_indent = 0
    if_block_target = "step"
    # Pending hits: (line_no, secret_name, step_index_or_-1). Evaluated
    # against ``step_ifs[step_index]`` at end-of-job. -1 means the hit
    # was at job scope (outside any step).
    pending_hits: list[tuple[int, str, int]] = []
    saw_any_job = False

    def flush_job() -> None:
        """Evaluate pending hits for the closing job, append violations."""
        if current_job is None:
            return
        if current_job_has_environment:
            return
        if _expression_is_trusted_guard(current_job_if):
            return
        for line_no, secret_name, step_index in pending_hits:
            if step_index >= 0:
                step_if = step_ifs[step_index]
                if step_if and _expression_is_trusted_guard(step_if):
                    continue
            violations.append(
                f"{path}:{line_no}: secrets.{secret_name} used in job "
                f"{current_job!r} without an `environment:` declaration "
                "and without a trusted actor/branch `if:` guard on the job or step."
            )

    def reset_for_new_job(job_name: str) -> None:
        nonlocal current_job, current_job_has_environment, current_job_if
        nonlocal in_step, current_step_index, step_ifs, pending_hits
        nonlocal in_if_block_scalar, saw_any_job
        current_job = job_name
        current_job_has_environment = False
        current_job_if = ""
        in_step = False
        current_step_index = -1
        step_ifs = []
        pending_hits = []
        in_if_block_scalar = False
        saw_any_job = True

    for i, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r")

        # Detect entry into the top-level ``jobs:`` section.
        if not jobs_section_started:
            if line.startswith("jobs:"):
                jobs_section_started = True
            continue

        # If we're inside a block-scalar ``if:`` expression, accumulate
        # continuation lines until we exit the block. The block ends when
        # we see a non-empty line whose indent is <= the ``if:`` key's
        # indent (4 for job-level, 6 for step-level). Strip per-line YAML
        # comments so a ``# is_standard`` decoy inside the block scalar
        # cannot satisfy the trusted-guard check.
        if in_if_block_scalar:
            stripped_rstrip = line.rstrip()
            indent = len(line) - len(line.lstrip(" "))
            # An empty continuation line is still part of the block scalar.
            if stripped_rstrip and indent <= if_block_scalar_indent:
                in_if_block_scalar = False
                # Fall through to normal line handling below.
            else:
                fragment = _strip_yaml_trailing_comment(stripped_rstrip.strip())
                if if_block_target == "job":
                    current_job_if += " " + fragment
                else:
                    step_ifs[current_step_index] += " " + fragment
                continue

        # Detect new job header. A job header at indent 2 closes the
        # previous job's pending-hit window.
        m_job = _JOB_HEADER_RE.match(line)
        if m_job:
            flush_job()
            reset_for_new_job(m_job.group(1))
            continue

        if current_job is None:
            continue

        # Track job-level ``environment:`` declaration. The value must name an
        # environment from ``_APPROVED_ENVIRONMENTS`` as a bare or quoted
        # literal. Expression-valued environments are rejected; use a trusted
        # job/step ``if:`` for conditional gating instead. The allow-list
        # defends against GitHub auto-creating typoed environments with no
        # protection rules.
        m_env = _JOB_ENVIRONMENT_RE.match(line)
        if m_env:
            value = m_env.group(1)
            if _environment_value_is_approved(value):
                current_job_has_environment = True
            continue

        # Track job-level ``if:`` guards (e.g. ``claude.yml`` pinning
        # ``sender.login`` to the maintainer; ``verify-artifacts.yml``
        # gating on the actor). Captured BEFORE step detection so we know
        # not to misread it as a step ``if:``.
        if not in_step:
            m_job_if = _JOB_IF_RE.match(line)
            if m_job_if:
                value = m_job_if.group(1)
                if value in ("|", ">", "|-", ">-", "|+", ">+"):
                    in_if_block_scalar = True
                    if_block_scalar_indent = 4
                    if_block_target = "job"
                    current_job_if = ""
                else:
                    current_job_if = _strip_yaml_trailing_comment(value)
                continue

        # Detect step boundary. New step appends a fresh ``if:`` slot to
        # the per-job ``step_ifs`` list so hits in this step can be
        # gated against the step's final guard at flush time, even when
        # ``if:`` follows ``env:`` in the step body.
        #
        # Important: we do NOT ``continue`` after updating step state —
        # the step header line may itself contain an inline secret
        # reference (e.g. ``- run: echo ${{ secrets.X }}``) that must
        # still be recorded against the new step. Fall through to the
        # secret-detection block below.
        m_step = _STEP_HEADER_RE.match(line)
        if m_step:
            in_step = True
            current_step_index = len(step_ifs)
            step_ifs.append("")
            # Falls through to secret detection.

        # Inside a step, capture the ``if:`` value. We attempt the match
        # only if the line did NOT just open a new step header — the
        # step-if regex is anchored at indent 6, the step-header regex
        # at indent 4 + ``- ``, so they cannot match the same line.
        elif in_step:
            m_if = _STEP_IF_RE.match(line)
            if m_if:
                value = m_if.group(1)
                if value in ("|", ">", "|-", ">-", "|+", ">+"):
                    in_if_block_scalar = True
                    if_block_scalar_indent = 6
                    if_block_target = "step"
                    step_ifs[current_step_index] = ""
                else:
                    step_ifs[current_step_index] = _strip_yaml_trailing_comment(value)
                continue

        # Skip comment lines outright — example references in docstring
        # comments (``${{ secrets.NAME }}`` documentation, security-design
        # notes, etc.) are not real expressions and must not trip the gate.
        if _COMMENT_RE.match(line):
            continue

        # Strip any trailing ``# ...`` comment off the line before secret
        # detection, so an authored-out reference (``run: echo hi  # not
        # ${{ secrets.X }}``) does not produce a spurious violation. The
        # strip is whitespace-anchored to avoid corrupting ``#`` inside
        # quoted strings (vanishingly rare in workflow YAML).
        searchable = _strip_yaml_trailing_comment(line)

        # ``secrets: inherit`` on a reusable-workflow call passes every
        # caller secret to the called workflow. Treat as a non-bypassable
        # secret consumer so it requires the same gating.
        if _SECRETS_INHERIT_RE.match(searchable):
            pending_hits.append((i, "<inherit>", current_step_index if in_step else -1))
            continue

        # Now check for ``secrets.X`` / ``secrets['X']`` / ``secrets[<expr>]``
        # references. Tag each hit with the enclosing step index (or -1
        # for job-scope hits) and defer the gating decision to flush_job(),
        # which sees the job-final environment + per-step final ``if:`` values.
        for m_secret in _SECRET_REF_RE.finditer(searchable):
            # Dot match -> group 1; bracket-quoted match -> group 2;
            # dynamic index -> neither group captures (use sentinel).
            secret_name = m_secret.group(1) or m_secret.group(2) or "<dynamic>"
            if secret_name in _BENIGN_SECRETS:
                continue
            pending_hits.append((i, secret_name, current_step_index if in_step else -1))

    flush_job()

    # Defence against silent-pass on a workflow with no recognisable
    # jobs (e.g. unexpected indentation, or a reusable-workflow stub).
    # If the file contains a top-level ``jobs:`` header but the parser
    # never identified a job, that's a parser miss — surface it so a
    # malformed checker invocation isn't mistaken for "all gated".
    if jobs_section_started and not saw_any_job:
        # Only complain when secrets are actually present in the file —
        # avoids noise on workflow stubs with no secrets at all.
        if any(_SECRET_REF_RE.search(line) for line in lines):
            violations.append(
                f"{path}: contains `jobs:` and `secrets.*` references but the "
                "parser identified no jobs (unexpected indentation?). Refusing "
                "to silently pass — please review or extend the checker."
            )

    return violations


def _strip_yaml_trailing_comment(value: str) -> str:
    """Strip a trailing ``# ...`` comment from a YAML scalar value.

    The strip is anchored on whitespace before ``#`` so substrings of
    real values (``#foo`` adjacent to a letter, e.g. ``#fragment`` in a
    URL) are preserved. ``#`` inside a quoted string is theoretically
    legal YAML but does not appear in this repo's workflow YAML; we
    accept the rare false-strip risk in exchange for false-positive
    suppression on trailing-comment examples.
    """
    return re.sub(r"\s+#.*$", "", value).strip()


if __name__ == "__main__":
    sys.exit(main())
