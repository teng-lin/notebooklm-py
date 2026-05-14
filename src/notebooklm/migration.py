"""Migration from legacy flat layout to profile-based directory structure.

Handles transparent migration of ~/.notebooklm/ files into
~/.notebooklm/profiles/default/ on first CLI invocation.

The migration is:
- Automatic: triggered by ensure_profiles_dir() on CLI startup
- Idempotent: safe to run multiple times
- Crash-safe: uses copy-then-delete with marker file
- Non-destructive: originals kept until all copies succeed
- Single-writer: cross-process ``filelock`` serializes concurrent CLI
  invocations so a container start-up race cannot interleave copies.
"""

import json
import logging
import shutil
import sys

from filelock import FileLock, Timeout

from ._atomic_io import atomic_update_json
from .paths import get_config_path, get_home_dir

logger = logging.getLogger(__name__)

_MIGRATION_MARKER = ".migration_complete"
_MIGRATION_LOCK = ".migration.lock"
_MIGRATION_LOCK_TIMEOUT = 30.0

# Legacy files that should be moved into profiles/default/
_LEGACY_FILES = ["storage_state.json", "context.json"]
_LEGACY_DIRS = ["browser_profile"]


class MigrationLockTimeoutError(RuntimeError):
    """Raised when ``migrate_to_profiles()`` cannot acquire the migration lock.

    Surfaces the underlying :class:`filelock.Timeout` so callers can
    distinguish a stuck peer from other migration failures (e.g., a CI
    runner whose previous job left a stale process holding the lock).
    """


def _has_legacy_files(home) -> bool:
    """Check if any legacy files exist at the home root."""
    return any((home / name).exists() for name in _LEGACY_FILES) or any(
        (home / name).is_dir() for name in _LEGACY_DIRS
    )


def migrate_to_profiles() -> bool:
    """Migrate legacy flat layout to profile-based structure.

    Checks for legacy files at the home root (storage_state.json, context.json,
    browser_profile/) and moves them into profiles/default/.

    Uses copy-then-delete with a marker file for crash safety:
    1. Copy all files/dirs to profiles/default/
    2. Delete originals
    3. Write .migration_complete marker (last — incomplete runs retry)

    The migration body runs under a cross-process ``filelock`` rooted at
    ``<home>/.migration.lock`` so concurrent CLI invocations (e.g., a
    container start-up race) cannot interleave copies. The loser of the
    race re-checks the marker inside the lock and no-ops.

    Returns:
        True if migration was performed, False if already migrated or no-op.

    Raises:
        MigrationLockTimeoutError: If the migration lock cannot be acquired
            within ``_MIGRATION_LOCK_TIMEOUT`` seconds — usually means a
            sibling process is stuck mid-migration.
    """
    # Ensure the home dir exists BEFORE the lock so ``filelock`` has a
    # writable parent for the lock file. Permissions (0o700 on POSIX) are
    # also set here, before any concurrent peer can observe an open dir.
    home = get_home_dir(create=True)

    lock_path = home / _MIGRATION_LOCK
    try:
        with FileLock(str(lock_path), timeout=_MIGRATION_LOCK_TIMEOUT):
            return _migrate_to_profiles_locked(home)
    except Timeout as exc:
        raise MigrationLockTimeoutError(
            f"Could not acquire migration lock at {lock_path} within "
            f"{_MIGRATION_LOCK_TIMEOUT:.0f}s; another process may be "
            "stuck mid-migration."
        ) from exc


def _migrate_to_profiles_locked(home) -> bool:
    """Core migration body, executed while holding the migration lock.

    Split out so the lock-acquisition wrapper stays small and the locked
    region is straightforward to reason about.
    """
    profiles_dir = home / "profiles"
    default_dir = profiles_dir / "default"

    # Inside the lock, re-check the marker FIRST. If another process completed
    # the migration while we were waiting, the marker is the authoritative
    # signal — return immediately as a no-op. The marker lives at
    # ``default_dir / _MIGRATION_MARKER`` (NOT the home root); see the
    # marker-write site at the end of this function.
    if (default_dir / _MIGRATION_MARKER).exists() and not _has_legacy_files(home):
        return False

    # Already migrated: profiles dir exists and no legacy files left to clean up
    if profiles_dir.exists() and not _has_legacy_files(home):
        return False

    # Check for legacy files
    legacy_files = [home / name for name in _LEGACY_FILES if (home / name).exists()]
    legacy_dirs = [home / name for name in _LEGACY_DIRS if (home / name).is_dir()]

    if not legacy_files and not legacy_dirs:
        # Fresh install — home dir permissions already set by caller.
        if sys.platform == "win32":
            profiles_dir.mkdir(exist_ok=True)
        else:
            profiles_dir.mkdir(exist_ok=True, mode=0o700)
        logger.debug("Created profiles directory (fresh install)")
        return True

    # Migrate legacy files into profiles/default/
    if sys.platform == "win32":
        default_dir.mkdir(parents=True, exist_ok=True)
    else:
        default_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    logger.info("Migrating legacy layout to profiles/default/")

    # Copy files — overwrite if source is newer (e.g., login wrote fresh
    # cookies to the legacy root path after a previous migration).
    for src in legacy_files:
        dst = default_dir / src.name
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            logger.debug("Skipping %s (profile copy is same age or newer)", src.name)
        else:
            shutil.copy2(src, dst)
            if sys.platform != "win32":
                dst.chmod(src.stat().st_mode)
            logger.debug("Copied %s → %s", src.name, dst)

    # Copy directories (skip if destination already exists)
    for src in legacy_dirs:
        dst = default_dir / src.name
        if dst.exists():
            logger.debug("Skipping %s/ (already exists in profile)", src.name)
        else:
            shutil.copytree(src, dst)
            logger.debug("Copied %s/ → %s/", src.name, dst)

    # Remove originals (copies already in place as fallback). Tolerate
    # already-removed paths (a previous interrupted run, or another
    # concurrent migration that partially completed before we entered).
    for src in legacy_files:
        try:
            src.unlink()
        except FileNotFoundError:
            pass
        logger.debug("Removed legacy %s", src.name)

    for src in legacy_dirs:
        shutil.rmtree(src, ignore_errors=True)
        logger.debug("Removed legacy %s/", src.name)

    # Update config.json with default_profile
    _set_default_profile_in_config()

    # Write marker LAST — signals that migration is fully complete.
    # If the process dies before this point, next run retries (safe because
    # copies use exist_ok/rmtree and originals may already be gone).
    marker = default_dir / _MIGRATION_MARKER
    marker.write_text("migrated\n", encoding="utf-8")

    logger.info("Migration complete: legacy files moved to profiles/default/")
    return True


def _set_default_profile_in_config() -> None:
    """Add default_profile to config.json if not already present.

    Uses :func:`atomic_update_json` so a concurrent ``notebooklm profile
    switch`` or ``language set`` running during migration cannot lose
    updates by interleaving read-modify-write cycles.
    """
    config_path = get_config_path()
    # Ensure parent dir has the same 0o700 permissions as the legacy code path.
    if sys.platform == "win32":
        config_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _ensure_default(data: dict) -> dict:
        if "default_profile" not in data:
            data["default_profile"] = "default"
        return data

    try:
        atomic_update_json(config_path, _ensure_default)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Migration config update failed; leaving as-is: %s", e)


def ensure_profiles_dir() -> None:
    """Ensure the profiles directory exists, migrating if needed.

    This is the single entry point for migration, called from CLI startup.
    Idempotent — safe to call on every CLI invocation. Also handles:
    - Fresh installs (no profiles dir)
    - Partial migrations (profiles dir exists but legacy files remain)
    - Interrupted migrations from older versions (marker + leftover legacy files)
    """
    home = get_home_dir()
    profiles_dir = home / "profiles"
    if not profiles_dir.exists() or _has_legacy_files(home):
        migrate_to_profiles()
