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

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Explicit-storage browser dirs created by login carry this marker. Profile and
# legacy layouts are owned by their enclosing profile and do not need it.
BROWSER_PROFILE_OWNERSHIP_MARKER = ".notebooklm-owned"
_MAX_PATH_COMPONENT_BYTES = 255

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

    CLI startup and single-tenant server lifespans use this as the
    process-wide profile binding. Library users should pass ``profile``
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
            from ._deprecation import warn_deprecated  # local: paths is a low-level leaf

            warn_deprecated(
                f"Reading {legacy_name} from the pre-profiles home-root layout "
                f"({legacy_path}) is deprecated; run any `notebooklm` command to "
                "migrate it into profiles/default/, or move the file there manually.",
                removal="1.0",
            )
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


def profile_from_storage_path(storage_path: Path | None) -> str | None:
    """Best-effort reverse of :func:`get_storage_path`: the profile owning a file.

    Returns ``<name>`` for a storage path shaped like
    ``<home>/profiles/<name>/storage_state.json``; otherwise ``None`` — an
    explicit ``--storage <arbitrary path>`` or the legacy home-root file carries
    no profile name, and callers fall back to path-based routing (the storage
    path itself still identifies the file).

    Used so a mid-session ``NOTEBOOKLM_REFRESH_CMD`` refreshes the SAME profile
    the client was built for (e.g. ``from_storage(profile="work")``) rather than
    the process-wide default, which would refresh the wrong profile while the
    parent reloads the work path.
    """
    if storage_path is None:
        return None
    try:
        resolved = Path(storage_path).resolve()
        profiles_root = (get_home_dir() / "profiles").resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(profiles_root):
        return None
    rel = resolved.relative_to(profiles_root)
    if not rel.parts:
        return None
    return rel.parts[0]


def master_token_path_for(storage_path: Path) -> Path:
    """Return the ``master_token.json`` sibling of a storage path.

    The SOLE derivation site for the master-token sibling invariant
    (issue #2103 structural follow-up — PR-1 of the master-token relocation).
    Before this, four sites derived the sibling independently and disagreed on
    canonicalization: ``_auth/recovery.py``'s L4 rung resolved via
    ``canonical_storage_key`` (``expanduser().resolve()``), ``_app/auth_check.py``
    and the (pre-#2104) CLI login writer used unresolved ``with_name``, and
    ``cli/services/auth_refresh.py`` used a raw ``.parent`` join — so a
    symlinked or relative ``--storage`` alias could derive a DIFFERENT sibling
    at each site, silently breaking the invariant #2103/#2104 fixed for one
    site at a time. All four now call this one function.

    Canonicalizes via ``expanduser().resolve()`` (matching
    ``_auth.paths.canonical_storage_key``, duplicated inline rather than
    imported to keep this public, top-level module free of any dependency on
    the private ``_auth`` package — this module is reachable from both `cli/`
    and `_app/`, and importing a private sibling from either would violate
    their respective boundary guardrails; a public chokepoint needs no
    ledger entry for either caller).

    ``resolve()`` degrades to a best-effort (non-canonicalized) path on a
    circular symlink rather than raising: on Python 3.13+ this is
    ``Path.resolve()``'s own non-strict behavior, and on 3.10-3.12 — where a
    symlink loop instead raises ``RuntimeError`` (not ``OSError``; a CPython
    pathlib behavior change, fixed in 3.13) — this function catches it
    itself, so every supported Python version behaves the same way here: a
    stale or circular profile symlink degrades a lookup, it does not crash
    ``auth check`` or bootstrap (#2103 PR-1 review).

    Args:
        storage_path: Path to ``storage_state.json`` (any form — absolute,
            relative, ``~``-prefixed, or through a symlink).

    Returns:
        The resolved, canonical sibling ``master_token.json`` path — or a
        best-effort, non-canonicalized sibling if resolution is impossible
        (e.g. a circular symlink).
    """
    expanded = storage_path.expanduser()
    try:
        resolved = expanded.resolve()
    except (OSError, RuntimeError):
        resolved = expanded
    return resolved.with_name("master_token.json")


def get_master_token_path(profile: str | None = None) -> Path:
    """Get the master_token.json path for a profile (headless master-token auth).

    Sibling of storage_state.json. Holds the durable ``aas_et/`` master token at
    mode 0600; cookies minted from it are written to storage_state.json.

    Thin wrapper over :func:`master_token_path_for` — this derives the sibling
    from ``get_storage_path(profile=profile)`` rather than
    ``get_profile_dir(profile)`` directly, so it agrees with the legacy
    home-root fallback :func:`get_storage_path` applies for the ``"default"``
    profile (``get_profile_dir`` has no such fallback and would derive a
    different, profile-dir-only answer).

    This agreement relies on an invariant this function does not itself
    enforce: a master token is only ever WRITTEN by ``login --master-token``
    (CLI-only), and every CLI invocation calls ``ensure_profiles_dir()``
    (idempotent, safe on every run) before any subcommand runs — so by the
    time a token could be written, a legacy home-root ``storage_state.json``
    has already been migrated into ``profiles/<name>/``. There is
    consequently no code path today that leaves a real master token sitting
    at the OLD ``get_profile_dir``-derived location while storage is still at
    the home root (neither ``mcp`` nor ``server`` read or write a master
    token at all, as of this writing). If a future caller ever reaches a
    master-token path without first going through ``ensure_profiles_dir``,
    this function would silently fail to find an existing legacy-location
    token rather than erroring — a reviewed, currently-unreachable case
    (#2103 PR-1 review).

    Args:
        profile: Profile name. If None, uses the active profile.

    Returns:
        Path to master_token.json (may not exist).
    """
    return master_token_path_for(get_storage_path(profile=profile))


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


def get_browser_profile_dir(
    profile: str | None = None,
    *,
    storage_path: Path | None = None,
) -> Path:
    """Get browser profile directory for a profile or storage file.

    An explicit storage path gets its own sibling browser profile. The
    conventional ``storage_state.json`` name keeps the existing
    ``browser_profile`` sibling name; other filenames preserve their full
    name and append ``.browser_profile``. If that component would exceed 255
    bytes, a stable hash of the canonical storage path keeps it filesystem-safe
    and makes relative/absolute aliases converge. Without an explicit storage
    path, this falls back to the existing profile and legacy-path behavior.
    Keeping the conventional name preserves the established profile layout;
    suffixing custom names prevents same-directory storage files from sharing
    a browser session.

    Args:
        profile: Profile name. If None, uses the active profile.
        storage_path: Explicit storage path from ``--storage``. When set, it
            takes precedence over profile resolution.

    Returns:
        Path to browser_profile/ directory.
    """
    if storage_path is not None:
        if storage_path.name == "storage_state.json":
            return storage_path.parent / "browser_profile"
        candidate = storage_path.with_suffix(storage_path.suffix + ".browser_profile")
        if len(os.fsencode(candidate.name)) <= _MAX_PATH_COMPONENT_BYTES:
            return candidate
        canonical_storage = storage_path.expanduser().resolve()
        digest = hashlib.sha256(os.fsencode(canonical_storage)).hexdigest()[:16]
        return canonical_storage.parent / f"storage-{digest}.browser_profile"
    resolved = resolve_profile(profile)
    profile_path = get_profile_dir(resolved) / "browser_profile"
    return _legacy_fallback(profile_path, "browser_profile", resolved)


def browser_profile_is_owned(storage_path: Path, browser_profile: Path) -> bool:
    """Return whether a browser-profile directory is safe for NotebookLM to remove.

    A marker explicitly owns any storage-specific sidecar created by NotebookLM.
    Unmarked directories are owned only when their canonical paths match the
    legacy layout or one direct child of ``NOTEBOOKLM_HOME/profiles``. Resolving
    paths before comparison prevents a symlinked profile entry from claiming a
    directory outside the NotebookLM home.
    """
    if (browser_profile / BROWSER_PROFILE_OWNERSHIP_MARKER).is_file():
        return True

    try:
        home = get_home_dir().resolve()
        canonical_storage = storage_path.expanduser().resolve()
        canonical_browser = browser_profile.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False

    if (
        canonical_storage == home / "storage_state.json"
        and canonical_browser == home / "browser_profile"
    ):
        return True

    profiles_dir = home / "profiles"
    try:
        relative_storage = canonical_storage.relative_to(profiles_dir)
    except ValueError:
        return False
    return (
        len(relative_storage.parts) == 2
        and relative_storage.name == "storage_state.json"
        and canonical_browser == canonical_storage.parent / "browser_profile"
    )


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
        storage_path: Explicit ``--storage`` override. When set, the
            ``storage_path``, ``context_path``, and ``browser_profile_dir`` in
            the result reflect the storage-specific sibling layout rather than
            profile paths.

    Returns:
        Dict with path information and sources.
    """
    home_from_env = os.environ.get("NOTEBOOKLM_HOME")
    resolved = resolve_profile(profile)

    # --storage bypasses the profile for auth, context, and browser paths. Resolve
    # it anyway so diagnostics can show which profile would otherwise be active.
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
        "browser_profile_dir": str(get_browser_profile_dir(resolved, storage_path=storage_path)),
    }
