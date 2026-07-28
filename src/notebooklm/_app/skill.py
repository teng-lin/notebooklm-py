"""Transport-neutral skill-install business logic.

This is the Click-free core of ``cli/skill_cmd.py``: it owns the install
target catalog (:data:`TARGETS` / :data:`SCOPES`), the per-scope/per-target
path resolution, the version-stamping helpers, and the
``create`` / ``up_to_date`` / ``overwrite`` classification a ``skill install``
makes for each target before deciding what to write. Every transport adapter
(the Click CLI today, a future HTTP / FastMCP surface tomorrow) drives this
core and renders the classification + outcome into its own surface, prompts,
and exit-code policy.

Each install target is a **directory** (``SKILL.md`` + ``references/`` +
``scripts/``), not a single file — :data:`SKILL_ENTRY` names the entry file
inside that directory that carries the version stamp and is read for
``show`` / status-version purposes.

This module also owns the **byte construction** of the uploadable skill
archive (:func:`build_skill_archive_bytes`, used by ``skill package``); it
zips the whole stamped tree, not just the entry file, so it is a pure
bytes-in/bytes-out helper over the same tree shape as install.

What stays in the CLI adapter (``cli/skill_cmd.py``):

* the actual atomic file writes (``atomic_write_text`` / ``atomic_write_bytes``)
  and directory replacement (``shutil.rmtree``) — it reads the
  ``replace_file_atomically`` helper off the command module at call time so
  the historical ``monkeypatch.setattr(skill_cmd, "replace_file_atomically",
  ...)`` test seam keeps landing, and
* the packaged-source loaders (``get_skill_source_content`` /
  ``get_skill_source_tree``) — they forward to the CLI-owned
  ``agent_templates`` package-data readers, which tests patch on the command
  module.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Name of the entry file inside each installed skill directory. This is the
#: file that carries the ``<!-- notebooklm-py vX.Y.Z -->`` version stamp and
#: is what ``get_skill_version`` / ``get_installed_content`` read.
SKILL_ENTRY = "SKILL.md"


@dataclass(frozen=True)
class SkillTarget:
    """Install target metadata."""

    label: str
    relative_path: Path


TARGETS: dict[str, SkillTarget] = {
    "claude": SkillTarget("Claude Code", Path(".claude") / "skills" / "notebooklm"),
    "agents": SkillTarget("Agent Skills", Path(".agents") / "skills" / "notebooklm"),
}
SCOPES = ("user", "project")

# Claude custom-skill upload format (chat + Cowork, Settings -> Capabilities):
# a ZIP whose root contains the skill *folder*; the folder name must equal the
# SKILL.md frontmatter ``name``.
SKILL_ARCHIVE_DIRNAME = "notebooklm"
SKILL_ARCHIVE_ENTRY = f"{SKILL_ARCHIVE_DIRNAME}/SKILL.md"
DEFAULT_ARCHIVE_FILENAME = "notebooklm-skill.zip"

# Fixed metadata so identical content yields byte-identical archives (within
# one environment -- deflate output may differ across zlib versions).
_ARCHIVE_DATE_TIME = (2020, 1, 1, 0, 0, 0)
_ARCHIVE_CREATE_SYSTEM = 3  # Unix
_ARCHIVE_EXTERNAL_ATTR = 0o100644 << 16  # S_IFREG | 0644
_ARCHIVE_EXTERNAL_ATTR_EXEC = 0o100755 << 16  # S_IFREG | 0755 (scripts/ entries)

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


def get_skill_version(skill_dir: Path) -> str | None:
    """Extract the version from an installed skill directory's entry file.

    ``skill_dir`` is the target's install *directory*; the version comment
    lives in ``skill_dir / SKILL_ENTRY`` (see :func:`add_version_comment`).
    """
    entry_path = skill_dir / SKILL_ENTRY
    if not entry_path.exists():
        return None

    with open(entry_path, encoding="utf-8") as f:
        content = f.read(500)  # Read first 500 chars

    match = re.search(r"notebooklm-py v([\d.]+)", content)
    return match.group(1) if match else None


def get_scope_root(scope: str) -> Path:
    """Resolve the root directory for a given install scope."""
    return Path.home() if scope == "user" else Path.cwd()


def get_skill_path(target: str, scope: str) -> Path:
    """Resolve the installed skill *directory* for a target and scope."""
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


def stamp_skill_files(files: dict[str, str], version: str) -> dict[str, str]:
    """Stamp a packaged skill file tree with the CLI version.

    ``files`` maps POSIX-relative path (e.g. ``"SKILL.md"``,
    ``"references/setup.md"``, ``"scripts/nlm"``) to file content, as returned
    by the CLI's ``get_skill_source_tree``. Only :data:`SKILL_ENTRY` carries
    the ``<!-- notebooklm-py vX.Y.Z -->`` version comment (via
    :func:`add_version_comment`) -- every other file passes through unchanged,
    since references/scripts have no header comment convention to stamp.
    """
    return {
        rel_path: (add_version_comment(content, version) if rel_path == SKILL_ENTRY else content)
        for rel_path, content in files.items()
    }


def build_skill_archive_bytes(stamped_files: dict[str, str]) -> bytes:
    """Build a Claude-uploadable skill archive in memory.

    Returns the bytes of a ZIP whose root contains the ``notebooklm/``
    skill folder (:data:`SKILL_ARCHIVE_DIRNAME`) holding every file in
    ``stamped_files`` (as produced by :func:`stamp_skill_files`) -- the
    entry file plus its ``references/`` and ``scripts/`` tree. Metadata is
    pinned (fixed timestamp, Unix create system, regular-file mode; ``scripts/``
    entries additionally get the executable bit, mirroring the ``chmod``
    ``install`` applies on disk) so identical input produces byte-identical
    archives within one environment. Compression is set per entry because
    ``ZipFile``'s archive-level default does not apply to an explicit
    ``ZipInfo`` (which would silently fall back to ``ZIP_STORED``).
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for rel_path, content in stamped_files.items():
            info = zipfile.ZipInfo(
                f"{SKILL_ARCHIVE_DIRNAME}/{rel_path}", date_time=_ARCHIVE_DATE_TIME
            )
            info.create_system = _ARCHIVE_CREATE_SYSTEM
            info.external_attr = (
                _ARCHIVE_EXTERNAL_ATTR_EXEC
                if rel_path.startswith("scripts/")
                else _ARCHIVE_EXTERNAL_ATTR
            )
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


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
    """Read an installed skill's entry file (:data:`SKILL_ENTRY`)."""
    entry_path = get_skill_path(target, scope) / SKILL_ENTRY
    if not entry_path.exists():
        return None
    return entry_path.read_text(encoding="utf-8")


def classify_target(target: str, scope: str, stamped_files: dict[str, str]) -> tuple[str, Path]:
    """Classify what an install would do for a single target.

    ``stamped_files`` maps POSIX-relative path to the stamped content that
    would be written for each file in the packaged skill tree (see
    :func:`stamp_skill_files`). Returns ``(status, skill_path)`` where
    ``skill_path`` is the target's install *directory* and ``status`` is one
    of :data:`TARGET_CREATE`, :data:`TARGET_UP_TO_DATE`, or
    :data:`TARGET_OVERWRITE`.

    ``TARGET_UP_TO_DATE`` requires every file in ``stamped_files`` to already
    exist under ``skill_path`` with byte-identical content AND no extra files
    to be present there -- a stale leftover (e.g. from a prior skill version,
    or an old single-file install occupying the same directory) forces
    ``TARGET_OVERWRITE`` so ``install`` can replace the whole tree.
    """
    skill_path = get_skill_path(target, scope)
    if not skill_path.exists():
        return TARGET_CREATE, skill_path
    if not skill_path.is_dir():
        # Occupied by a non-directory (e.g. a stray file blocking the install
        # path) -- nothing to compare file-by-file, so surface as differing.
        return TARGET_OVERWRITE, skill_path

    try:
        installed_files = {
            path.relative_to(skill_path).as_posix()
            for path in skill_path.rglob("*")
            if path.is_file()
        }
        if installed_files != set(stamped_files):
            return TARGET_OVERWRITE, skill_path
        for rel_path, content in stamped_files.items():
            if (skill_path / rel_path).read_text(encoding="utf-8") != content:
                return TARGET_OVERWRITE, skill_path
    except OSError:
        # Unreadable existing tree -- treat as differing so we surface intent.
        return TARGET_OVERWRITE, skill_path

    return TARGET_UP_TO_DATE, skill_path


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
    "DEFAULT_ARCHIVE_FILENAME",
    "SCOPES",
    "SKILL_ARCHIVE_DIRNAME",
    "SKILL_ARCHIVE_ENTRY",
    "SKILL_ENTRY",
    "TARGET_CREATE",
    "TARGET_OVERWRITE",
    "TARGET_UP_TO_DATE",
    "TARGETS",
    "SkillTarget",
    "add_version_comment",
    "build_skill_archive_bytes",
    "classify_target",
    "get_installed_content",
    "get_package_version",
    "get_scope_root",
    "get_skill_path",
    "get_skill_version",
    "iter_targets",
    "remove_empty_parents",
    "report_mixed_no_clobber_up_to_date",
    "stamp_skill_files",
]
