"""Shared agent instruction loading helpers."""

from importlib import resources
from pathlib import Path

AGENT_TEMPLATE_FILES = {
    "claude": "SKILL.md",
    "gemini": "SKILL.md",
    "codex": "CODEX.md",
}

REPO_ROOT_AGENTS = Path(__file__).resolve().parents[3] / "AGENTS.md"
REPO_ROOT_CLAUDE_SKILL = Path(__file__).resolve().parents[3] / "SKILL.md"


def _read_package_data(filename: str) -> str | None:
    """Read a packaged agent template file."""
    try:
        return (resources.files("notebooklm") / "data" / filename).read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError, ModuleNotFoundError):
        return None


def get_agent_source_content(target: str) -> str | None:
    """Return bundled instructions for a supported agent target."""
    normalized = target.lower()

    # Prefer the repo-level Codex guide when running from a source checkout so
    # the CLI mirrors the instructions Codex actually sees in this repository.
    if normalized == "codex" and REPO_ROOT_AGENTS.exists():
        return REPO_ROOT_AGENTS.read_text(encoding="utf-8")

    # Prefer the repo-root skill when running from a source checkout so both
    # GitHub discovery and local CLI installs use the same source of truth.
    if normalized in ("claude", "gemini") and REPO_ROOT_CLAUDE_SKILL.exists():
        return REPO_ROOT_CLAUDE_SKILL.read_text(encoding="utf-8")

    filename = AGENT_TEMPLATE_FILES.get(normalized)
    if filename is None:
        return None

    return _read_package_data(filename)
