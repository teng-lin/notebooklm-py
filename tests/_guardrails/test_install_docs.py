"""Guardrail tests for installation documentation invariants.

These tests catch silent drift between `pyproject.toml`, `docs/installation.md`,
and the agent-context files (`CLAUDE.md`, `AGENTS.md`, `SKILL.md`).

When any of these tests fail, the docs are out of sync with the package — fix
the doc, not the test (unless the test is genuinely wrong).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from scripts._tracked_files import tracked_files

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover -- only hit on Python 3.10
    import tomli as tomllib  # transitive via uv.lock; declared in [dev] for safety

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLATION_MD = REPO_ROOT / "docs" / "installation.md"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
SKILL_MD = REPO_ROOT / "SKILL.md"
TROUBLESHOOTING_MD = REPO_ROOT / "docs" / "troubleshooting.md"
CHANGELOG_MD = REPO_ROOT / "CHANGELOG.md"

CANONICAL_CONTRIBUTOR_INSTALL = "uv sync --frozen --extra browser --extra dev --extra markdown"
SKILL_BROWSER_LINE_RE = re.compile(r'pip install "notebooklm-py\[browser\]"(?![\w,])')
INSTALLATION_LINK_RE = re.compile(r"\bdocs/installation\.md\b")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tracked_repository_files() -> list[Path]:
    return tracked_files(REPO_ROOT, fallback_globs=("docs/**/*", "src/**/*", "*.md"))


def _tracked_docs() -> list[Path]:
    return [
        path
        for path in _tracked_repository_files()
        if path.suffix == ".md" and path.is_relative_to(REPO_ROOT / "docs")
    ]


def _pyproject_extras() -> set[str]:
    data = tomllib.loads(_read(PYPROJECT_TOML))
    return set(data["project"]["optional-dependencies"].keys())


# ---------------------------------------------------------------------------
# §8.4 #1 — extras matrix in installation.md mirrors pyproject.toml
# ---------------------------------------------------------------------------


def test_installation_md_extras_matrix_mirrors_pyproject() -> None:
    """The extras matrix in installation.md must list exactly the keys defined
    in pyproject.toml's [project.optional-dependencies].

    Catches: "added a new extra, forgot to document" and "removed an extra
    but matrix still references it".
    """
    pyproject_extras = _pyproject_extras()
    installation_text = _read(INSTALLATION_MD)

    # Collect backticked extra names from the matrix `Extra` column.
    # Matrix rows look like: `| `browser` | ... |`. Exclude the `(none)` row.
    matrix_extras: set[str] = set()
    for line in installation_text.splitlines():
        if not line.startswith("| `") or " | " not in line:
            continue
        match = re.match(r"\|\s*`([a-z]+)`\s*\|", line)
        if match:
            matrix_extras.add(match.group(1))

    assert matrix_extras == pyproject_extras, (
        f"installation.md extras matrix is out of sync with pyproject.toml.\n"
        f"  pyproject.toml: {sorted(pyproject_extras)}\n"
        f"  installation.md: {sorted(matrix_extras)}\n"
        f"  missing from doc: {sorted(pyproject_extras - matrix_extras)}\n"
        f"  extra in doc: {sorted(matrix_extras - pyproject_extras)}"
    )


# ---------------------------------------------------------------------------
# §8.4 #2 — wrong package name `notebooklm[<extra>]` (without -py) must not
# appear anywhere outside CHANGELOG (which records the bug-fix history).
# ---------------------------------------------------------------------------


def test_no_wrong_package_name_anywhere() -> None:
    """`notebooklm[browser|cookies|markdown]` (missing the `-py` suffix) is an
    invalid PyPI package name. It must never appear in user-facing files.
    """
    bad_pattern = re.compile(r"notebooklm\[(browser|cookies|markdown)\]")
    scan_files = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        CLAUDE_MD,
        AGENTS_MD,
        SKILL_MD,
    ]

    hits: list[str] = []
    tracked = set(_tracked_repository_files())
    for path in scan_files:
        if path in tracked and path.is_file():
            for lineno, line in enumerate(_read(path).splitlines(), start=1):
                if bad_pattern.search(line):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    for path in tracked:
        relative = path.relative_to(REPO_ROOT)
        if not relative.parts or relative.parts[0] not in {"docs", "src"}:
            continue
        if path.suffix not in {".md", ".py", ".yml", ".yaml"}:
            continue
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if bad_pattern.search(line):
                hits.append(f"{relative}:{lineno}: {line.strip()}")

    assert not hits, (
        "Found `notebooklm[<extra>]` (missing `-py`) — should be `notebooklm-py[<extra>]`:\n"
        + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# §8.4 #3 — per-file install-block assertions (catches summarize-away edits)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [CLAUDE_MD, AGENTS_MD], ids=["CLAUDE.md", "AGENTS.md"])
def test_contributor_context_files_contain_canonical_uv_sync(path: Path) -> None:
    """CLAUDE.md and AGENTS.md are agent-context files for contributors working
    on this repo. They must contain the canonical `uv sync --frozen` block
    (agents replay context; trimming it forces them to reinvent — incorrectly)
    and a link to docs/installation.md.
    """
    text = _read(path)
    assert CANONICAL_CONTRIBUTOR_INSTALL in text, (
        f"{path.name} must contain the literal contributor install command "
        f"`{CANONICAL_CONTRIBUTOR_INSTALL}` — agents replay this verbatim."
    )
    assert INSTALLATION_LINK_RE.search(text), f"{path.name} must link to docs/installation.md."


def test_skill_md_contains_agent_install_pattern() -> None:
    """SKILL.md is the AGENT-facing entry point (Persona A), NOT contributor.
    It must contain the agent install pattern (`pip install "notebooklm-py[browser]"`),
    a separate line for the optional `[cookies]` install, and a link to
    docs/installation.md.

    The strict `[browser]"` regex (no extras inside the brackets) forbids a
    regression to the single-step `[browser,cookies]` form which would break
    on Python 3.13+.
    """
    text = _read(SKILL_MD)
    assert SKILL_BROWSER_LINE_RE.search(text), (
        'SKILL.md must contain `pip install "notebooklm-py[browser]"` '
        "(exact `[browser]` extra, no others bracketed in)."
    )
    assert 'pip install "notebooklm-py[cookies]"' in text, (
        'SKILL.md must contain a separate `pip install "notebooklm-py[cookies]"` line '
        "(optional install, may fail on Python 3.13+)."
    )
    assert INSTALLATION_LINK_RE.search(text), "SKILL.md must link to docs/installation.md."


def test_skill_md_does_not_use_status_for_auth() -> None:
    """SKILL.md historically claimed `notebooklm status` shows
    `Authenticated as: email@...` — that's false. `status` is context-only
    (selected notebook); auth is verified with `notebooklm auth check`.
    """
    text = _read(SKILL_MD)
    assert "Authenticated as: email" not in text, (
        "SKILL.md must not claim `notebooklm status` shows 'Authenticated as: email@...' "
        "— `status` is context-only. Use `notebooklm auth check` for auth verification."
    )
    assert "auth check" in text, (
        "SKILL.md Agent Setup Verification section must mention `notebooklm auth check`."
    )


def test_skill_md_workflows_preserve_readiness_identity_and_safe_autonomy() -> None:
    """Stateful workflow guidance must remain ID-pinned and harness-neutral."""
    text = _read(SKILL_MD)
    automatic = text.split("**Run automatically (no confirmation):**", 1)[1].split(
        "**Ask before running:**", 1
    )[0]
    confirmation = text.split("**Ask before running:**", 1)[1].split("## Quick Reference", 1)[0]
    podcast = text.split("### Research to Podcast (Interactive)", 1)[1].split(
        "### Document Analysis", 1
    )[0]
    document_analysis = text.split("### Document Analysis", 1)[1].split("### Bulk Import", 1)[0]
    bulk_import = text.split("### Bulk Import", 1)[1].split(
        "### Deep Web Research (Background Pattern)", 1
    )[0]
    deep_research = text.split("### Deep Web Research (Background Pattern)", 1)[1].split(
        "## Output Style", 1
    )[0]

    assert "notebooklm language set" not in automatic
    assert "notebooklm language set" in confirmation
    assert "`notebooklm research wait --import-all`" not in automatic
    assert "research wait --import-all" in confirmation
    assert 'status == "ready"' in text
    assert "status=READY" not in text
    assert "subagent" not in text.lower()
    assert "Task(" not in text
    assert "run safe read-only diagnosis first" in text
    assert "Mind map (`--kind note-backed`)" in text
    assert "Mind map (`--kind interactive`, default)" in text

    assert podcast.index("notebooklm create") < podcast.index("notebooklm source add")
    assert podcast.index("notebooklm source add") < podcast.index("notebooklm source wait")
    assert podcast.index("notebooklm source wait") < podcast.index("notebooklm generate audio")
    assert podcast.index("notebooklm generate audio") < podcast.index("notebooklm artifact wait")
    assert podcast.index("notebooklm artifact wait") < podcast.index("notebooklm download audio")
    assert "After confirmation, wait for **every** captured source" in podcast
    assert "After confirmation, wait for that exact artifact" in podcast
    assert "one sequential job" in podcast
    assert "only after `artifact wait` exits 0" in podcast

    assert document_analysis.index("notebooklm create") < document_analysis.index(
        "notebooklm source add"
    )
    assert document_analysis.index("notebooklm source add") < document_analysis.index(
        "notebooklm source wait"
    )
    assert document_analysis.index("notebooklm source wait") < document_analysis.index(
        "notebooklm ask"
    )

    assert deep_research.index("notebooklm source add-research") < deep_research.index(
        "notebooklm research wait"
    )
    assert "--run-id {research_run_id}" in deep_research
    assert "--import-all --timeout 1800 --json" in deep_research

    workflow_sections = {
        "podcast": podcast,
        "document analysis": document_analysis,
        "bulk import": bulk_import,
        "deep research": deep_research,
    }
    notebook_scoped_prefixes = (
        "notebooklm source ",
        "notebooklm ask ",
        "notebooklm generate ",
        "notebooklm artifact ",
        "notebooklm download ",
        "notebooklm research ",
    )
    for workflow_name, section in workflow_sections.items():
        inline_commands = re.findall(r"`(notebooklm [^`\n]+)`", section)
        fenced_commands = [
            line.strip() for line in section.splitlines() if line.strip().startswith("notebooklm ")
        ]
        commands = [*inline_commands, *fenced_commands]
        for command in commands:
            if command.startswith(notebook_scoped_prefixes):
                assert "-n {notebook_id}" in command, (
                    f"{workflow_name} command is not notebook-pinned: {command}"
                )


# ---------------------------------------------------------------------------
# §8.4 #4 — every (installation.md#anchor) cross-link resolves to a heading
# ---------------------------------------------------------------------------


def _markdown_heading_slug(heading: str) -> str:
    """GitHub markdown heading slug: lowercase, spaces → '-', strip non-word
    chars except '-'. Approximates GitHub's algorithm well enough for our anchors.
    """
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug


def _collect_headings(path: Path) -> set[str]:
    slugs: set[str] = set()
    for line in _read(path).splitlines():
        m = re.match(r"^(#+)\s+(.+?)\s*$", line)
        if m:
            slugs.add(_markdown_heading_slug(m.group(2)))
    return slugs


def test_installation_md_internal_anchors_resolve() -> None:
    """Every `(installation.md#anchor)` cross-link from any other doc must point
    to a heading that exists in installation.md.
    """
    install_anchors = _collect_headings(INSTALLATION_MD)
    cross_link_re = re.compile(r"\(([^)]*installation\.md)#([a-z0-9-]+)\)")

    failures: list[str] = []
    scan_files = _tracked_docs()
    scan_files += [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        CLAUDE_MD,
        AGENTS_MD,
        SKILL_MD,
    ]

    for path in scan_files:
        if not path.is_file():
            continue
        for lineno, line in enumerate(_read(path).splitlines(), start=1):
            for match in cross_link_re.finditer(line):
                anchor = match.group(2)
                if anchor not in install_anchors:
                    failures.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno} → "
                        f"installation.md#{anchor} (not found)"
                    )

    assert not failures, (
        "Cross-links to installation.md anchors that don't exist:\n"
        + "\n".join(failures)
        + f"\n\nAvailable anchors: {sorted(install_anchors)}"
    )


# ---------------------------------------------------------------------------
# §8.4 #5 — troubleshooting.md still has the four bare platform headings
# (cross-linked from installation.md as #linux/#macos/#windows/#wsl)
# ---------------------------------------------------------------------------


def test_troubleshooting_md_keeps_bare_platform_headings() -> None:
    """installation.md cross-links to `troubleshooting.md#linux` etc.
    If someone renames the headings (e.g., `### Linux (Debian/Ubuntu)`), the
    inbound anchors silently 404. Hold the line.
    """
    text = _read(TROUBLESHOOTING_MD)
    # Match the heading line *exactly* — `### Linux\n`, not `### Linux (...)`.
    required = ["### Linux", "### macOS", "### Windows", "### WSL"]
    missing = [h for h in required if not re.search(rf"^{re.escape(h)}\s*$", text, re.MULTILINE)]
    assert not missing, (
        f"docs/troubleshooting.md is missing bare platform headings: {missing}.\n"
        "If you intentionally renamed them, update installation.md cross-links to match."
    )


# ---------------------------------------------------------------------------
# Bonus: --all-extras must not appear outside installation.md (which contains
# the warning callout) and CHANGELOG (history).
# ---------------------------------------------------------------------------


def test_no_uv_sync_all_extras_in_canonical_install_paths() -> None:
    """`uv sync --all-extras` includes `cookies`, which fails on Python 3.13+.
    The `[all]` extra deliberately excludes `cookies`. Only the warning callout
    in installation.md is allowed to mention `--all-extras` as a flag.

    Match the actual CLI flag (`--all-extras` with leading dashes) so we don't
    false-positive on the `#all-vs-all-extras` anchor used in cross-links.
    """
    bad_pattern = re.compile(r"--all-extras\b")
    forbidden_locations: list[Path] = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        CLAUDE_MD,
        AGENTS_MD,
        SKILL_MD,
    ]
    forbidden_locations += [path for path in _tracked_docs() if path.name != "installation.md"]

    hits: list[str] = []
    for path in forbidden_locations:
        if not path.is_file():
            continue
        for lineno, line in enumerate(_read(path).splitlines(), start=1):
            if bad_pattern.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not hits, (
        "`--all-extras` found outside docs/installation.md (where the warning lives):\n"
        + "\n".join(hits)
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
