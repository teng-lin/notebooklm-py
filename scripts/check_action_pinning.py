"""Assert third-party actions in privileged workflows are SHA-pinned.

Pinning third-party GitHub Actions to a 40-character commit SHA (instead
of a floating tag like ``@v1`` or ``@release/v1``) is a defence against
upstream tag-hijacking and silent supply-chain compromise: a malicious
push to a tag we trust would otherwise execute under our secrets on the
next workflow run. This check is the static gate that keeps the
SHA-pinning invariant from regressing.

Scope:

* **Privileged workflows** — the hardcoded list below — run jobs that
  carry deploy keys, OIDC tokens, repo write scopes, or maintainer
  credentials. Every ``uses:`` referencing a third-party repo in those
  workflows must be SHA-pinned.
* **First-party ``actions/*`` actions** (e.g. ``actions/checkout``,
  ``actions/setup-python``) are owned by GitHub and may stay on floating
  tags. Dependabot still bumps them on a weekly cadence (see
  ``.github/dependabot.yml``).
* Non-privileged workflows are out of scope; they don't see secrets and
  can churn at floating-tag speed.

Comment convention: when a SHA is pinned, the line should be followed by
a human-readable tag comment so reviewers can read the intent without
visiting GitHub. Example::

    uses: pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b  # @ v1.14.0

The comment is advisory — this script does not enforce its presence
(Dependabot rewrites only the SHA, and a missing comment is a polish
finding, not a security gap). We enforce only the SHA itself.

Usage::

    python scripts/check_action_pinning.py
    python scripts/check_action_pinning.py --workflow-dir custom/path

Exit codes:
    0  Every third-party action in every privileged workflow is SHA-pinned.
    1  One or more violations found (printed to stderr with file:line).
    2  Argument error / privileged workflow file missing.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from pathlib import Path

# Privileged workflows. Each one carries secrets, OIDC, or maintainer-only
# triggers; a hijacked third-party action in any of these can exfiltrate
# credentials or push malicious artifacts under our identity. Keep this
# list synced with the workflows that declare an ``environment:`` gate or
# reference ``${{ secrets.* }}`` — see ``check_workflow_secret_gates.py``
# for the runtime companion to this static check.
PRIVILEGED_WORKFLOWS: tuple[str, ...] = (
    "publish.yml",
    "testpypi-publish.yml",
    "claude.yml",
    "rpc-health.yml",
    "nightly.yml",
    "verify-artifacts.yml",
    "verify-package.yml",
)

# ``uses:`` lines we'll inspect. The ref (everything after ``@``) is the
# focus of the check; we only need owner/repo to decide whether the
# action is first-party. Composite-action paths (``./.github/...``) and
# Docker image refs (``docker://...``) are intentionally NOT matched —
# they're a different trust model and aren't used in this repo today.
_USES_RE = re.compile(
    r"""
    ^\s*-?\s*               # optional list dash + leading indent
    uses:\s+                # the ``uses:`` key
    (?P<owner>[A-Za-z0-9._-]+)
    /
    (?P<repo>[A-Za-z0-9._/-]+?)   # repo may include path-into-monorepo
    @
    (?P<ref>\S+)            # whatever is after ``@`` up to next whitespace
    """,
    re.VERBOSE,
)

# A SHA is a 40-character lowercase hex string. Anything else — including
# short SHAs (``@cef221``), tags (``@v1``, ``@release/v1``), branches
# (``@main``) — is treated as a floating ref.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Owners we treat as first-party (GitHub-owned, no SHA pin required).
# Mirrors the ``_ALLOWED_READ_SCOPES`` frozenset convention used by
# ``check_workflow_permissions.py`` for similar small lookup tables.
# ``github/*`` (e.g. ``github/codeql-action``) is GitHub-owned too but
# lives in a separate org; expand this set deliberately if a ``github/*``
# action ever lands in a privileged workflow.
_FIRST_PARTY_OWNERS = frozenset({"actions"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workflow-dir",
        default=".github/workflows",
        help="Directory containing workflow YAML files (default: .github/workflows)",
    )
    args = ap.parse_args()

    workflow_dir = Path(args.workflow_dir)
    if not workflow_dir.is_dir():
        print(f"Not a directory: {workflow_dir}", file=sys.stderr)
        return 2

    missing: list[str] = []
    for name in PRIVILEGED_WORKFLOWS:
        if not (workflow_dir / name).is_file():
            missing.append(name)
    if missing:
        # A privileged workflow disappearing is itself worth a hard fail:
        # either the list is stale or someone deleted security infra.
        # Surface so a maintainer either restores the file or updates
        # PRIVILEGED_WORKFLOWS deliberately.
        for name in missing:
            print(
                f"{workflow_dir / name}: privileged workflow file missing",
                file=sys.stderr,
            )
        return 2

    violations: list[str] = []
    for name in PRIVILEGED_WORKFLOWS:
        path = workflow_dir / name
        for lineno, owner, repo, ref in _iter_uses(path):
            if owner in _FIRST_PARTY_OWNERS:
                # actions/* is GitHub-owned; floating tag is acceptable.
                continue
            if _SHA_RE.match(ref):
                # Third-party + 40-char SHA = pinned. OK.
                continue
            violations.append(
                f"{path}:{lineno}: third-party action not SHA-pinned: "
                f"{owner}/{repo}@{ref} (expected 40-char commit SHA)"
            )

    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        print(
            f"\n{len(violations)} violation(s). "
            "Replace floating tag with the resolved commit SHA, e.g.:\n"
            "  gh api repos/<owner>/<repo>/commits/<tag> --jq .sha\n"
            "Then add a comment with the human-readable tag for review "
            "readability, e.g. ``# @ v1.2.3``.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: all third-party actions in {len(PRIVILEGED_WORKFLOWS)} privileged "
        f"workflows are SHA-pinned"
    )
    return 0


def _iter_uses(path: Path) -> Iterator[tuple[int, str, str, str]]:
    """Yield ``(lineno, owner, repo, ref)`` for each ``uses:`` line in ``path``.

    ``lineno`` is 1-based to match editor / grep / GitHub line numbering.
    """
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        m = _USES_RE.match(line)
        if not m:
            continue
        yield lineno, m.group("owner"), m.group("repo"), m.group("ref")


if __name__ == "__main__":
    sys.exit(main())
