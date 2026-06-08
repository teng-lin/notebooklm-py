"""Transport-neutral skill-install business logic.

This is the Click-free core of ``cli/skill_cmd.py``: it owns the install
target catalog (:data:`TARGETS` / :data:`SCOPES`), the per-scope/per-target
path resolution, the version-stamping helpers, and the
``create`` / ``up_to_date`` / ``overwrite`` classification a ``skill install``
makes for each target before deciding what to write. Every transport adapter
(the Click CLI today, a future HTTP / FastMCP surface tomorrow) drives this
core and renders the classification + outcome into its own surface, prompts,
and exit-code policy.

What stays in the CLI adapter (``cli/skill_cmd.py``):

* the actual atomic file write (``atomic_write_text``) — it reads the
  ``replace_file_atomically`` helper off the command module at call time so the
  historical ``monkeypatch.setattr(skill_cmd, "replace_file_atomically", ...)``
  test seam keeps landing, and
* the packaged-source loader (``get_skill_source_content``) — it forwards to
  the CLI-owned ``agent_templates`` package-data reader, which tests patch on
  the command module.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillTarget:
    """Install target metadata."""

    label: str
    relative_path: Path


TARGETS: dict[str, SkillTarget] = {
    "claude": SkillTarget("Claude Code", Path(".claude") / "skills" / "notebooklm" / "SKILL.md"),
    "agents": SkillTarget("Agent Skills", Path(".agents") / "skills" / "notebooklm" / "SKILL.md"),
}
SCOPES = ("user", "project")

# Per-target classification used by ``skill install`` to decide whether each
# target needs a write, would clobber differing content, or is already in sync.
TARGET_CREATE = "create"
TARGET_UP_TO_DATE = "up_to_date"
TARGET_OVERWRITE = "overwrite"


def get_package_version() -> str:
    """Get the current package version."""
    try:
        from .. import __version__

        return __version__
    except ImportError:
        return "unknown"


def get_skill_version(skill_path: Path) -> str | None:
    """Extract version from skill file header comment."""
    if not skill_path.exists():
        return None

    with open(skill_path, encoding="utf-8") as f:
        content = f.read(500)  # Read first 500 chars

    match = re.search(r"notebooklm-py v([\d.]+)", content)
    return match.group(1) if match else None


def get_scope_root(scope: str) -> Path:
    """Resolve the root directory for a given install scope."""
    return Path.home() if scope == "user" else Path.cwd()


def get_skill_path(target: str, scope: str) -> Path:
    """Resolve the installed skill path for a target and scope."""
    return get_scope_root(scope) / TARGETS[target].relative_path


def iter_targets(target: str) -> list[str]:
    """Expand 'all' into concrete targets."""
    return list(TARGETS) if target == "all" else [target]


def add_version_comment(content: str, version: str) -> str:
    """Embed the CLI version into a skill file."""
    version_comment = f"<!-- notebooklm-py v{version} -->\n"

    if "---" in content:
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return f"---{parts[1]}---\n{version_comment}{parts[2].lstrip()}"

    return version_comment + content


def remove_empty_parents(skill_path: Path, scope: str) -> None:
    """Remove empty skill directories without touching the scope root."""
    stop_at = get_scope_root(scope)
    current = skill_path.parent
    while current != stop_at:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def get_installed_content(target: str, scope: str) -> str | None:
    """Read an installed skill file."""
    skill_path = get_skill_path(target, scope)
    if not skill_path.exists():
        return None
    return skill_path.read_text(encoding="utf-8")


def classify_target(target: str, scope: str, stamped_content: str) -> tuple[str, Path]:
    """Classify what an install would do for a single target.

    Returns ``(status, skill_path)`` where ``status`` is one of
    :data:`TARGET_CREATE`, :data:`TARGET_UP_TO_DATE`, or :data:`TARGET_OVERWRITE`.
    """
    skill_path = get_skill_path(target, scope)
    if not skill_path.exists():
        return TARGET_CREATE, skill_path
    try:
        existing = skill_path.read_text(encoding="utf-8")
    except OSError:
        # Unreadable existing file -- treat as differing so we surface intent.
        return TARGET_OVERWRITE, skill_path
    if existing == stamped_content:
        return TARGET_UP_TO_DATE, skill_path
    return TARGET_OVERWRITE, skill_path


def report_mixed_no_clobber_up_to_date(
    emit: Callable[[str], None],
    *,
    skipped_up_to_date: Sequence[object],
    skipped_no_clobber: Sequence[object],
    installed_paths: Sequence[object],
    failed_targets: Sequence[object],
) -> None:
    """Report up-to-date targets when ``--no-clobber`` skipped other targets."""
    if skipped_up_to_date and skipped_no_clobber and not installed_paths and not failed_targets:
        emit(f"[green]Up to date[/green] {len(skipped_up_to_date)} target(s)")


__all__ = [
    "SCOPES",
    "TARGET_CREATE",
    "TARGET_OVERWRITE",
    "TARGET_UP_TO_DATE",
    "TARGETS",
    "SkillTarget",
    "add_version_comment",
    "classify_target",
    "get_installed_content",
    "get_package_version",
    "get_scope_root",
    "get_skill_path",
    "get_skill_version",
    "iter_targets",
    "remove_empty_parents",
    "report_mixed_no_clobber_up_to_date",
]
