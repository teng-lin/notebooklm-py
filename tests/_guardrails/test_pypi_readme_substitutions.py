"""Guardrail test for the PyPI README link-rewriting substitutions.

The README ships to PyPI via ``hatch-fancy-pypi-readme``, which rewrites
relative repo links to version-tagged absolute GitHub URLs through an explicit
per-path substitution allowlist in ``pyproject.toml``. Any relative repo-root
link in ``README.md`` that lacks a matching substitution ships as a bare
relative link and 404s on the PyPI project page (issue #1473).

This test fails loud whenever a README edit adds a new relative repo link
(``](docs/...)``, ``](FOO.md)``, ``](LICENSE)``) that no substitution covers,
so a missing rewrite can't silently recur. Fix the substitutions in
``pyproject.toml`` (not this test) when it fails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover -- only hit on Python 3.10
    import tomli as tomllib  # transitive via uv.lock; declared in [dev] for safety

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
README_MD = REPO_ROOT / "README.md"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"

# Relative Markdown link targets in the README that point at the repo (not an
# external URL, anchor, or mailto). Captures the link target inside ``](...)``.
README_LINK_RE = re.compile(r"\]\((?!https?://|#|mailto:)([^)]+)\)")


def _readme_relative_targets() -> set[str]:
    text = README_MD.read_text(encoding="utf-8")
    targets: set[str] = set()
    for target in README_LINK_RE.findall(text):
        # Keep the literal link target as written (including any ``#anchor`` or
        # ``?query`` suffix). hatch-fancy-pypi-readme runs its substitution
        # regexes against the raw README text, and the exact-file patterns are
        # ``)``-anchored (e.g. ``\]\(AGENTS\.md\)``), so ``](AGENTS.md#x)`` is
        # NOT rewritten and would 404. Stripping the suffix here would mask that
        # exact regression the guardrail exists to catch.
        if target:
            targets.add(target)
    return targets


def _substitution_patterns() -> list[str]:
    data = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
    hooks = data["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    return [sub["pattern"] for sub in hooks["substitutions"]]


def test_every_relative_readme_link_has_a_substitution() -> None:
    patterns = _substitution_patterns()
    compiled = [re.compile(pattern) for pattern in patterns]

    uncovered: list[str] = []
    for target in sorted(_readme_relative_targets()):
        # Reconstruct the literal ``](target)`` fragment the substitution
        # patterns match against in the assembled README.
        fragment = f"]({target})"
        if not any(rx.search(fragment) for rx in compiled):
            uncovered.append(target)

    assert not uncovered, (
        "README.md has relative repo links with no hatch-fancy-pypi-readme "
        "substitution rule; these 404 on the PyPI project page. Add a "
        "substitution block in pyproject.toml for each (see issue #1473): "
        f"{uncovered}"
    )
