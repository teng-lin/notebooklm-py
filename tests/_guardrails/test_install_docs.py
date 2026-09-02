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
SKILL_MAX_BYTES = 16_384


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    """Extract a required Markdown section with actionable failures."""
    assert start in text, f"SKILL.md is missing required heading: {start}"
    remainder = text.split(start, 1)[1]
    assert end in remainder, f"SKILL.md is missing required heading after {start}: {end}"
    return remainder.split(end, 1)[0]


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
    regression to the single-step `[browser,cookies]` form, which makes the
    optional browser-cookie dependency mandatory for every agent.
    """
    text = _read(SKILL_MD)
    assert SKILL_BROWSER_LINE_RE.search(text), (
        'SKILL.md must contain `pip install "notebooklm-py[browser]"` '
        "(exact `[browser]` extra, no others bracketed in)."
    )
    assert 'pip install "notebooklm-py[cookies]"' in text, (
        'SKILL.md must contain a separate `pip install "notebooklm-py[cookies]"` line '
        "(optional browser-cookie extractor install)."
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
        "SKILL.md Setup and Authentication section must mention `notebooklm auth check`."
    )


def test_skill_md_stays_compact() -> None:
    """Keep version-specific catalogs in CLI help and maintained documentation."""
    size = len(SKILL_MD.read_bytes())
    assert size <= SKILL_MAX_BYTES, (
        f"SKILL.md is {size:,} bytes; the {SKILL_MAX_BYTES:,}-byte entrypoint budget exists "
        "to limit always-loaded context. Move conditional detail to CLI help or maintained docs."
    )


def test_skill_md_workflows_preserve_readiness_identity_and_safe_autonomy() -> None:
    """The compact skill must retain the stateful safety invariants."""
    text = _read(SKILL_MD)
    setup = _section(text, "## Setup and Authentication", "## Operating Invariants")
    operating = _section(text, "## Operating Invariants", "## Authorization Boundaries")
    authorization = _section(text, "## Authorization Boundaries", "## Command Discovery")
    workflow = _section(text, "## Canonical Source-to-Artifact Workflow", "## Deep Research")
    deep_research = _section(text, "## Deep Research", "## Generation Notes")
    python_api = _section(text, "## Python API Baseline", "## Generation Notes")
    generation = _section(text, "## Generation Notes", "## Output and Citations")
    output = _section(text, "## Output and Citations", "## Failure Handling")
    failure_handling = _section(text, "## Failure Handling", "## Skill Installation")

    assert "Gemini Notebook" in text
    brand_text = text.replace("NotebookLMClient", "")
    assert "NotebookLM" not in brand_text
    assert "Google Gemini Notebook" not in text
    assert "notebooklm auth check --test --json" in setup
    assert "may heal and persist refreshed cookies" in setup
    assert ".checks.token_fetch == true" in setup
    assert "NOTEBOOKLM_MASTER_TOKEN_JSON" in setup
    assert "secret-transport convention" in setup
    assert "not an environment variable" in setup
    assert "notebooklm auth refresh" in setup
    assert text.count("NOTEBOOKLM_AUTH_JSON") == 1
    assert "language set" in authorization
    assert "research wait --import-all" in authorization
    assert "ask --new --json" in authorization
    assert "prompt absence as consent" in authorization
    assert "research cancel" in authorization
    assert "--run-id <research_run_id>" in authorization
    assert 'status == "ready"' in text
    assert "status=READY" not in text
    assert "subagent" not in text.lower()
    assert "Task(" not in text
    assert "one sequential job" in text
    assert "only after the wait exits 0" in text
    assert "run safe read-only diagnosis first" in failure_handling
    assert "notebooklm auth check --test --passive --json" in failure_handling
    assert 'pip install "notebooklm-py[headless]"' in text
    assert "For every concurrent run" in text
    assert "NOTEBOOKLM_AUTH_JSON" not in operating
    assert "master_token.json" in operating
    assert "Never share one writable `storage_state.json`" in operating
    assert "asynchronous generators" in text
    assert "no task ID or separate `artifact wait` step" in text
    assert "Mind map (`--kind note-backed`)" in generation
    assert "Mind map (`--kind interactive`, default)" in generation
    assert "CLI polls it to completion" in generation
    assert "Both kinds accept `--instructions`" in generation
    assert "A `source wait` timeout uses exit 2" in failure_handling
    assert "`artifact wait` and `research wait` timeouts use exit 1" in failure_handling
    assert '"status": "timeout"' in failure_handling
    assert "proceed only on" in output
    assert "await resolve_chat_reference_passage(client, notebook_id, reference)" in output

    assert workflow.index("notebooklm create") < workflow.index("notebooklm source add")
    assert workflow.index("notebooklm source add") < workflow.index("notebooklm source wait")
    assert workflow.index("notebooklm source wait") < workflow.index("notebooklm generate audio")
    assert workflow.index("notebooklm generate audio") < workflow.index("notebooklm artifact wait")
    assert workflow.index("notebooklm artifact wait") < workflow.index("notebooklm download audio")
    assert "for every captured source" in workflow
    assert "-a {artifact_id} -n {notebook_id}" in workflow

    assert deep_research.index("notebooklm source add-research") < deep_research.index(
        "notebooklm research wait"
    )
    assert "--run-id {research_run_id}" in deep_research
    assert "--mode deep --no-wait --json" in deep_research
    assert "--import-all --timeout 1800 --json" in deep_research
    assert ".imported_sources[].id" in deep_research
    assert "per-phase budget" in deep_research

    assert python_api.index("client.sources.add_url") < python_api.index(
        "client.sources.wait_until_ready"
    )
    assert python_api.index("client.sources.wait_until_ready") < python_api.index("client.chat.ask")
    assert python_api.index("client.artifacts.generate_audio") < python_api.index(
        "client.artifacts.wait_for_completion"
    )
    assert python_api.index("client.artifacts.wait_for_completion") < python_api.index(
        "client.artifacts.download_audio"
    )
    assert "task.task_id" in python_api
    assert "artifact_id=task.task_id" in python_api
    assert "final.is_complete" in python_api
    assert "not thread-safe" in python_api

    workflow_sections = {
        "canonical source-to-artifact": workflow,
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
    json_required_prefixes = (
        "notebooklm create ",
        "notebooklm source add ",
        "notebooklm source add-research ",
        "notebooklm ask ",
        "notebooklm generate ",
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
            if command.startswith(json_required_prefixes):
                assert "--json" in command, (
                    f"{workflow_name} command must retain machine-readable output: {command}"
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
    """`uv sync --all-extras` includes the optional `cookies` extractor.
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
