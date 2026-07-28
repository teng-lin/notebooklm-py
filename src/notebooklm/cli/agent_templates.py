"""Shared agent instruction loading helpers."""

from importlib import resources
from importlib.abc import Traversable
from pathlib import Path

AGENT_TEMPLATE_FILES = {
    "claude": "skill/SKILL.md",
    "codex": "CODEX.md",
}

REPO_ROOT_AGENTS = Path(__file__).resolve().parents[3] / "AGENTS.md"
REPO_ROOT_SKILL_DIR = Path(__file__).resolve().parents[3] / "plugin" / "skills" / "notebooklm"
REPO_ROOT_CLAUDE_SKILL = REPO_ROOT_SKILL_DIR / "SKILL.md"


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
    if normalized == "claude" and REPO_ROOT_CLAUDE_SKILL.exists():
        return REPO_ROOT_CLAUDE_SKILL.read_text(encoding="utf-8")

    filename = AGENT_TEMPLATE_FILES.get(normalized)
    if filename is None:
        return None

    return _read_package_data(filename)


def _read_traversable_tree(root: Traversable) -> dict[str, str]:
    """Recursively read a packaged data directory into ``{relative_posix: text}``.

    ``importlib.resources`` ``Traversable`` objects (backing wheel/zip package
    data) don't support ``rglob``, so this walks manually with ``iterdir()``.
    """
    files: dict[str, str] = {}

    def _walk(node: Traversable, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                _walk(child, f"{rel}/")
            else:
                files[rel] = child.read_text(encoding="utf-8")

    _walk(root, "")
    return files


def get_skill_source_files() -> dict[str, str] | None:
    """Return the full packaged skill directory as ``{relative_posix_path: text}``.

    Mirrors :func:`get_agent_source_content`'s checkout-first, package-data-
    fallback preference: prefers the repo-root ``plugin/skills/notebooklm/``
    checkout (keeps local CLI installs and GitHub in sync with the same
    source of truth), falling back to the wheel-packaged
    ``notebooklm/data/skill/`` tree.
    Returns ``None`` if neither is available (same exceptions swallowed as
    :func:`_read_package_data`).
    """
    if REPO_ROOT_SKILL_DIR.exists():
        return {
            path.relative_to(REPO_ROOT_SKILL_DIR).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(REPO_ROOT_SKILL_DIR.rglob("*"))
            if path.is_file()
        }

    try:
        root = resources.files("notebooklm") / "data" / "skill"
        if not root.is_dir():
            return None
        return _read_traversable_tree(root)
    except (FileNotFoundError, TypeError, ModuleNotFoundError):
        return None
