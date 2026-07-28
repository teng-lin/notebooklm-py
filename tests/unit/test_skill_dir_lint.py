"""Static lint tests for the ``plugin/skills/notebooklm/`` Agent Skill directory.

Companion to ``tests/unit/test_claude_code_plugin.py`` (which pins the
plugin-manifest side: the ``skills`` field, the anti-shadowing invariant, and
the launcher's executable bit). This file solely owns the skill *content*
invariants: frontmatter shape, the ``SKILL.md`` line budget,
progressive-disclosure link integrity between ``SKILL.md`` and
``references/``, and the PEP 723 / POSIX-bootstrap contracts the workflow
scripts and launcher must honor. (Not duplicated in
``test_claude_code_plugin.py`` -- test modules must not cross-import each
other, so each content invariant lives in exactly one file.)

Static only — no network, no subprocess, no ``notebooklm`` import — so it
runs in every environment, including ones without the ``browser``/``dev``
extras installed.

Unmarked (no ``repo_lint``), matching ``tests/unit/test_claude_code_plugin.py``:
this is an ordinary fast unit test, not a slow repo-wide audit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "plugin" / "skills" / "notebooklm"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES_DIR = SKILL_DIR / "references"
SCRIPTS_DIR = SKILL_DIR / "scripts"
NLM_LAUNCHER = SCRIPTS_DIR / "nlm"

MAX_SKILL_MD_LINES = 300

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")  # http:, https:, mailto:, ...
_PEP723_BLOCK_RE = re.compile(r"^# /// script\s*$\n(.*?)^# ///\s*$", re.MULTILINE | re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal flat ``key: value`` frontmatter parser (no YAML dependency).

    Mirrors the convention ``_app/skill.py``'s ``add_version_comment``
    assumes: single-line ``name:`` / ``description:`` pairs between a
    leading ``---`` delimiter pair. This is the sole owner of frontmatter
    *content* checks (see module docstring) -- kept local rather than
    imported elsewhere, since ``test_*`` modules must not import from other
    ``test_*`` modules (``tests/_guardrails/test_no_cross_test_imports.py``).
    """
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    fields: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _relative_link_targets(text: str) -> list[str]:
    """Link destinations from ``[text](target)`` that are relative paths.

    Filters out same-document anchors (``#foo``) and scheme-prefixed
    absolute URLs (``https://…``, ``mailto:…``) — those aren't paths inside
    the skill directory to resolve.
    """
    targets = []
    for raw in _LINK_RE.findall(text):
        target = raw.strip()
        if not target or target.startswith("#"):
            continue
        if _SCHEME_RE.match(target):
            continue
        targets.append(target)
    return targets


def _all_skill_files() -> list[Path]:
    return [p for p in SKILL_DIR.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #


def test_frontmatter_has_name_and_description() -> None:
    frontmatter = _parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    assert frontmatter.get("name") == "notebooklm", (
        f"SKILL.md frontmatter `name` must be exactly `notebooklm`, got {frontmatter.get('name')!r}"
    )
    assert frontmatter.get("description"), "SKILL.md frontmatter `description` must be non-empty"


# --------------------------------------------------------------------------- #
# SKILL.md line budget
# --------------------------------------------------------------------------- #


def test_skill_md_is_under_line_budget() -> None:
    line_count = len(SKILL_MD.read_text(encoding="utf-8").splitlines())
    assert line_count <= MAX_SKILL_MD_LINES, (
        f"SKILL.md is {line_count} lines; must stay <= {MAX_SKILL_MD_LINES} so "
        "it loads cheaply into context — move detail into references/ instead "
        "(progressive disclosure)."
    )


# --------------------------------------------------------------------------- #
# Progressive-disclosure link integrity
# --------------------------------------------------------------------------- #


def test_every_reference_file_is_linked_from_skill_md() -> None:
    linked = set(_relative_link_targets(SKILL_MD.read_text(encoding="utf-8")))
    missing = [
        f"references/{p.name}"
        for p in sorted(REFERENCES_DIR.glob("*.md"))
        if f"references/{p.name}" not in linked
    ]
    assert not missing, (
        "references/ file(s) not linked from SKILL.md — orphaned, agents won't "
        f"discover them via progressive disclosure: {missing}"
    )


def test_skill_md_relative_links_resolve() -> None:
    broken = []
    for target in _relative_link_targets(SKILL_MD.read_text(encoding="utf-8")):
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue
        if not (path_part.endswith(".md") or "scripts/" in path_part):
            continue
        if not (SKILL_DIR / path_part).is_file():
            broken.append(target)
    assert not broken, (
        f"SKILL.md has relative .md/scripts/ link(s) that do not resolve inside "
        f"{SKILL_DIR}: {broken}"
    )


# --------------------------------------------------------------------------- #
# scripts/*.py — PEP 723 inline metadata
# --------------------------------------------------------------------------- #


def test_scripts_declare_pep723_notebooklm_py_dependency() -> None:
    scripts = sorted(SCRIPTS_DIR.glob("*.py"))
    assert scripts, f"expected at least one *.py script under {SCRIPTS_DIR}"

    missing_start: list[str] = []
    missing_block: list[str] = []
    missing_dep: list[str] = []
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        first_line = text.splitlines()[0] if text else ""
        if first_line != "# /// script":
            missing_start.append(script.name)
            continue
        match = _PEP723_BLOCK_RE.search(text)
        if match is None:
            missing_block.append(script.name)
            continue
        if "notebooklm-py" not in match.group(1):
            missing_dep.append(script.name)

    assert not missing_start, (
        f"script(s) must start with a PEP 723 `# /// script` line: {missing_start}"
    )
    assert not missing_block, (
        f"script(s) missing a closed PEP 723 block (`# ///` terminator): {missing_block}"
    )
    assert not missing_dep, (
        f"script(s) whose PEP 723 block does not declare `notebooklm-py`: {missing_dep}"
    )


# --------------------------------------------------------------------------- #
# scripts/nlm — POSIX bootstrap-launcher contract
# --------------------------------------------------------------------------- #


def test_nlm_launcher_has_shebang_and_both_bootstrap_branches() -> None:
    text = NLM_LAUNCHER.read_text(encoding="utf-8")
    first_line = text.splitlines()[0] if text else ""
    assert first_line == "#!/bin/sh", f"scripts/nlm must start with `#!/bin/sh`, got {first_line!r}"
    assert "command -v notebooklm" in text, (
        "scripts/nlm must probe `command -v notebooklm` (branch 1: already on PATH)."
    )
    assert 'uvx --from "notebooklm-py[browser]"' in text, (
        'scripts/nlm must fall back to `uvx --from "notebooklm-py[browser]"` (branch 2).'
    )


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def test_all_skill_files_are_valid_utf8() -> None:
    bad: list[str] = []
    for path in _all_skill_files():
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            bad.append(f"{path.relative_to(REPO_ROOT)}: {exc}")
    assert not bad, f"non-UTF-8 file(s) under {SKILL_DIR}: {bad}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
