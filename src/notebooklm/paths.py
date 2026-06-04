"""Path resolution for NotebookLM configuration files.

This module provides centralized path resolution that respects environment variables
and supports multi-account profiles:

- NOTEBOOKLM_HOME: Base directory for all NotebookLM files (default: ~/.notebooklm)
- NOTEBOOKLM_PROFILE: Override the active profile name

Directory structure (profile-based):
    ~/.notebooklm/
    ├── config.json              # Global config (language, default_profile)
    ├── profiles/
    │   ├── default/
    │   │   ├── storage_state.json
    │   │   ├── context.json
    │   │   └── browser_profile/
    │   ├── work/
    │   │   ├── ...

Legacy (pre-profile) structure is still supported via fallback:
    ~/.notebooklm/
    ├── config.json
    ├── storage_state.json       # Falls back here for "default" profile
    ├── context.json
    └── browser_profile/

Usage:
    from notebooklm.paths import get_home_dir, get_storage_path, resolve_profile

    # Profile-aware paths
    storage = get_storage_path()                  # Uses active profile
    storage = get_storage_path(profile="work")    # Explicit profile

    # Set active profile (CLI startup)
    set_active_profile("work")
"""

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Public surface (ADR-0012). Underscore-prefixed helpers like
# ``_read_default_profile`` and ``_reset_config_cache`` remain importable for
# test fixtures via the standard ``from notebooklm.paths import _foo`` syntax
# (Python attribute lookup is unaffected by ``__all__``); ``__all__`` only
# governs ``from notebooklm.paths import *`` and documents the intended
# public API.
__all__ = [
    "get_active_profile",
    "get_browser_profile_dir",
    "get_config_path",
    "get_context_path",
    "get_home_dir",
    "get_path_info",
    "get_profile_dir",
    "get_storage_path",
    "list_profiles",
    "read_default_profile",
    "resolve_profile",
    "set_active_profile",
]

# Module-level active profile, set once at CLI startup via set_active_profile().
# Library users should pass profile= explicitly to path functions instead.
_active_profile: str | None = None


def set_active_profile(profile: str | None) -> None:
    """Set the active profile for this process.

    Called once at CLI startup. Library users should pass ``profile``
    explicitly to path functions instead of relying on this global.
    """
    global _active_profile
    _active_profile = profile


def _reset_config_cache() -> None:
    """Reset the ``_read_default_profile`` mtime cache.

    Exposed for test fixtures that modify ``config.json`` between tests.
    """
    global _cached_default_profile, _config_mtime
    _cached_default_profile = _UNSET
    _config_mtime = 0.0


def get_active_profile() -> str | None:
    """Get the currently set active profile, or None if not set."""
    return _active_profile


def get_home_dir(create: bool = False) -> Path:
    """Get NotebookLM home directory.

    Precedence: NOTEBOOKLM_HOME env var > ~/.notebooklm

    Args:
        create: If True, create directory. On Unix, sets 0o700 permissions via
            mkdir + chmod. On Windows, skips mode= and chmod entirely because:
            - Python < 3.13: mode= is silently ignored by mkdir().
            - Python >= 3.13: mode= applies Windows ACLs that can be overly
              restrictive, blocking other processes (even the same user) from
              reading the directory.
            In both cases, Windows inherits ACLs from the parent directory.

    Returns:
        Path to the NotebookLM home directory.

    Example:
        >>> import os
        >>> os.environ["NOTEBOOKLM_HOME"] = "/custom/path"
        >>> get_home_dir()
        PosixPath('/custom/path')
    """
    if home := os.environ.get("NOTEBOOKLM_HOME"):
        path = Path(home).expanduser().resolve()
    else:
        path = Path.home() / ".notebooklm"

    if create:
        if sys.platform == "win32":
            # On Windows < Python 3.13, mode= is ignored by mkdir(). On
            # Python 3.13+, mode= applies Windows ACLs that can be overly
            # restrictive (0o700 blocks other same-user processes). Skip mode
            # entirely and let Windows inherit ACLs from the parent directory.
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Ensure correct permissions even if directory already existed
            # (protects against TOCTOU race where attacker creates dir with wrong perms)
            path.chmod(0o700)

    return path


_UNSET = object()  # Sentinel to distinguish "not cached" from "cached as None"
_cached_default_profile: str | None | object = _UNSET
_config_mtime: float = 0.0


def _read_default_profile() -> str | None:
    """Read default_profile from config.json (cached by mtime).

    Standalone config reader in paths.py to avoid circular imports
    with cli/language_cmd.py (which imports from paths.py).

    Returns:
        The default profile name, or None if not configured or on any error.
    """
    global _cached_default_profile, _config_mtime

    config_path = get_home_dir() / "config.json"
    if not config_path.exists():
        _cached_default_profile = None
        _config_mtime = 0.0
        return None

    try:
        mtime = config_path.stat().st_mtime
        if mtime == _config_mtime and _cached_default_profile is not _UNSET:
            return _cached_default_profile  # type: ignore[return-value]

        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            _cached_default_profile = None
            _config_mtime = mtime
            return None
        value = data.get("default_profile")
        # Guard against non-string values (e.g., {"default_profile": 123})
        _cached_default_profile = value if isinstance(value, str) else None
        _config_mtime = mtime
        return _cached_default_profile
    except (json.JSONDecodeError, OSError):
        _cached_default_profile = _UNSET
        _config_mtime = 0.0
        return None


# Public alias for ``_read_default_profile``. The implementation is module-
# private (underscore prefix) but the value it reads is part of the public
# configuration contract, so callers outside paths.py may use this name.
read_default_profile = _read_default_profile


def resolve_profile(profile: str | None = None) -> str:
    """Resolve the active profile name.

    Precedence:
    1. Explicit ``profile`` argument (from --profile CLI flag)
    2. Module-level ``_active_profile`` (set via set_active_profile)
    3. ``NOTEBOOKLM_PROFILE`` environment variable
    4. ``default_profile`` from config.json
    5. Fallback: ``"default"``

    Args:
        profile: Explicit profile name. If provided, returned directly.

    Returns:
        Resolved profile name (never None).
    """
    if profile:
        return profile
    if _active_profile:
        return _active_profile
    if env_profile := os.environ.get("NOTEBOOKLM_PROFILE"):
        return env_profile
    if config_profile := _read_default_profile():
        return config_profile
    return "default"


def get_profile_dir(profile: str | None = None, create: bool = False) -> Path:
    """Get directory for a specific profile.

    Args:
        profile: Profile name. If None, resolves via resolve_profile().
        create: If True, create directory with 0o700 permissions.

    Returns:
        Path to the profile directory (e.g., ~/.notebooklm/profiles/default/).

    Raises:
        ValueError: If the resolved profile name would escape the profiles directory
            (e.g., path traversal via ``../``).
    """
    resolved = resolve_profile(profile)
    profiles_root = get_home_dir() / "profiles"
    path = (profiles_root / resolved).resolve()

    # Guard against path traversal (e.g., profile="../../etc") and names that
    # resolve to the profiles root itself (e.g., profile=".") which would let
    # delete/rename operate on the entire profiles directory.
    resolved_root = profiles_root.resolve()
    if not path.is_relative_to(resolved_root) or path == resolved_root:
        raise ValueError(f"Invalid profile name: {resolved!r}")

    if create:
        if sys.platform == "win32":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    return path


def _legacy_fallback(profile_path: Path, legacy_name: str, resolved_profile: str) -> Path:
    """Return legacy path if profile path doesn't exist and profile is "default".

    This ensures pre-migration users (and library users who never trigger CLI
    migration) continue to work seamlessly.

    Args:
        profile_path: The profile-based path to check.
        legacy_name: Filename/dirname at the home root (e.g., "storage_state.json").
        resolved_profile: Already-resolved profile name (avoids redundant resolution).
    """
    if not profile_path.exists() and resolved_profile == "default":
        legacy_path = get_home_dir() / legacy_name
        if legacy_path.exists():
            logger.debug(
                "Using legacy path %s (profile path %s not found)",
                legacy_path,
                profile_path,
            )
            return legacy_path
    return profile_path


def list_profiles() -> list[str]:
    """List all available profile names.

    Returns:
        Sorted list of profile directory names, or empty list if none exist.
    """
    profiles_dir = get_home_dir() / "profiles"
    if not profiles_dir.exists():
        return []
    return sorted(d.name for d in profiles_dir.iterdir() if d.is_dir())


def get_storage_path(profile: str | None = None) -> Path:
    """Get storage_state.json path for a profile.

    Falls back to legacy home-root path for the "default" profile if the
    profile-based path doesn't exist (pre-migration compatibility).

    Args:
        profile: Profile name. If None, uses the active profile.

    Returns:
        Path to storage_state.json.
    """
    resolved = resolve_profile(profile)
    profile_path = get_profile_dir(resolved) / "storage_state.json"
    return _legacy_fallback(profile_path, "storage_state.json", resolved)


def get_context_path(
    profile: str | None = None,
    *,
    storage_path: Path | None = None,
) -> Path:
    """Get context.json path for a profile or storage file.

    Precedence:
        1. ``storage_path`` (explicit ``--storage <path>`` CLI flag) →
           returns a sibling ``<storage_path>.context.json``. This isolates
           context per storage file so two ``--storage`` invocations cannot
           see each other's notebook selection.
        2. Profile-based path (``profiles/<name>/context.json``).
        3. Legacy home-root fallback (``~/.notebooklm/context.json`` for the
           "default" profile when the profile path doesn't exist).

    Args:
        profile: Profile name. If None, uses the active profile.
        storage_path: Explicit storage_state.json path from --storage flag.
            When set, returns a sibling context file and skips profile lookup.

    Returns:
        Path to context.json.
    """
    if storage_path is not None:
        return storage_path.with_suffix(storage_path.suffix + ".context.json")
    resolved = resolve_profile(profile)
    profile_path = get_profile_dir(resolved) / "context.json"
    return _legacy_fallback(profile_path, "context.json", resolved)


def get_browser_profile_dir(profile: str | None = None) -> Path:
    """Get browser profile directory for a profile.

    Falls back to legacy home-root path for the "default" profile if the
    profile-based path doesn't exist (pre-migration compatibility).

    Args:
        profile: Profile name. If None, uses the active profile.

    Returns:
        Path to browser_profile/ directory.
    """
    resolved = resolve_profile(profile)
    profile_path = get_profile_dir(resolved) / "browser_profile"
    return _legacy_fallback(profile_path, "browser_profile", resolved)


def get_config_path() -> Path:
    """Get config.json path (global, not per-profile).

    Returns:
        Path to config.json within NOTEBOOKLM_HOME.
    """
    return get_home_dir() / "config.json"


def get_path_info(
    profile: str | None = None,
    *,
    storage_path: Path | None = None,
) -> dict[str, str]:
    """Get diagnostic info about resolved paths.

    Useful for debugging and the ``status`` / ``doctor`` commands.

    Args:
        profile: Profile name. If None, uses the active profile.
        storage_path: Explicit ``--storage`` override. When set, ``storage_path``
            and ``context_path`` in the result reflect the sibling-context
            layout rather than the profile path.

    Returns:
        Dict with path information and sources.
    """
    home_from_env = os.environ.get("NOTEBOOKLM_HOME")
    resolved = resolve_profile(profile)

    # Determine profile source. When --storage is set, profile resolution is
    # effectively bypassed for the auth + context paths — but we still resolve
    # ``profile`` because consumers may want to know what profile *would* be
    # active otherwise. Make the override clear in the label so ``status
    # --paths`` doesn't claim "CLI flag (--storage)" set the profile.
    other_profile_set = (
        profile
        or _active_profile
        or os.environ.get("NOTEBOOKLM_PROFILE")
        or _read_default_profile()
    )
    if storage_path is not None:
        profile_source = (
            "CLI flag (--storage, profile ignored)" if other_profile_set else "CLI flag (--storage)"
        )
    elif profile:
        profile_source = "CLI flag"
    elif _active_profile:
        profile_source = "CLI flag (--profile)"
    elif os.environ.get("NOTEBOOKLM_PROFILE"):
        profile_source = "NOTEBOOKLM_PROFILE env var"
    elif _read_default_profile():
        profile_source = "config.json"
    else:
        profile_source = "default"

    resolved_storage = (
        str(storage_path) if storage_path is not None else str(get_storage_path(resolved))
    )
    resolved_context = str(get_context_path(resolved, storage_path=storage_path))

    return {
        "home_dir": str(get_home_dir()),
        "home_source": "NOTEBOOKLM_HOME" if home_from_env else "default (~/.notebooklm)",
        "profile": resolved,
        "profile_source": profile_source,
        "profile_dir": str(get_profile_dir(resolved)),
        "storage_path": resolved_storage,
        "context_path": resolved_context,
        "config_path": str(get_config_path()),
        "browser_profile_dir": str(get_browser_profile_dir(resolved)),
    }
