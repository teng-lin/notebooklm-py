"""Multi-user-data-profile cookie extraction for Chromium-family browsers.

Chrome and its forks store cookies in a per-user-data-profile SQLite DB at
``<user-data-dir>/<profile-dir>/Cookies``. ``profile-dir`` is ``Default``
for the first profile and ``Profile <N>`` for additional ones. The
human-friendly profile name (the one shown in the Chrome profile picker)
lives in ``<user-data-dir>/Local State`` under
``profile.info_cache.<profile-dir>.name``.

``rookiepy.chrome()`` (and siblings) read the ``Default`` profile only —
hard-coded inside rookiepy. Users with multiple Chrome user-profiles
signed in to different Google accounts see ``--browser-cookies chrome``
silently skip every non-Default account (issue #571).

This module:

1. Discovers every populated user-data profile across the chromium family.
2. Reads cookies from each via ``rookiepy.any_browser(db_path, domains=…)``,
   which (unlike ``rookiepy.chromium_based``) successfully decrypts non-Default
   Chrome cookies on macOS — empirically verified, the
   ``missing osx_key_service`` blocker noted in #511 only affects
   ``chromium_based`` and not ``any_browser``.

Public surface:

* :class:`ChromiumProfile`
* :func:`discover_chromium_profiles`
* :func:`resolve_chromium_profile`
* :func:`read_chromium_profile_cookies`
* :func:`is_chromium_browser`
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Browsers whose on-disk layout matches Chrome's (``<root>/<profile-dir>/Cookies``
# plus ``<root>/Local State``). Excludes Firefox-family (separate profile model
# via ``profiles.ini`` + ``cookies.sqlite``) and Safari.
_CHROMIUM_BROWSERS: frozenset[str] = frozenset(
    {"chrome", "chromium", "brave", "edge", "arc", "vivaldi", "opera", "opera-gx"}
)


def _canonical_chromium_browser_name(browser_name: str) -> str:
    """Normalize supported chromium-family aliases to their on-disk key."""
    return browser_name.strip().lower().replace("_", "-")


def is_chromium_browser(browser_name: str) -> bool:
    """Return True when ``browser_name`` uses Chrome's user-data-profile layout."""
    return _canonical_chromium_browser_name(browser_name) in _CHROMIUM_BROWSERS


def _macos_user_data_dirs() -> dict[str, Path]:
    home = Path.home()
    app_support = home / "Library" / "Application Support"
    return {
        "chrome": app_support / "Google" / "Chrome",
        "chromium": app_support / "Chromium",
        "brave": app_support / "BraveSoftware" / "Brave-Browser",
        "edge": app_support / "Microsoft Edge",
        "arc": app_support / "Arc" / "User Data",
        "vivaldi": app_support / "Vivaldi",
        "opera": app_support / "com.operasoftware.Opera",
        "opera-gx": app_support / "com.operasoftware.OperaGX",
    }


def _linux_user_data_dirs() -> dict[str, Path]:
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    return {
        "chrome": xdg / "google-chrome",
        "chromium": xdg / "chromium",
        "brave": xdg / "BraveSoftware" / "Brave-Browser",
        "edge": xdg / "microsoft-edge",
        "vivaldi": xdg / "vivaldi",
        "opera": xdg / "opera",
        "opera-gx": xdg / "opera-gx",
        # Arc on Linux has no stable directory yet; omit until upstream stabilizes.
    }


def _windows_user_data_dirs() -> dict[str, Path]:
    out: dict[str, Path] = {}
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local)
        out.update(
            {
                "chrome": base / "Google" / "Chrome" / "User Data",
                "chromium": base / "Chromium" / "User Data",
                "brave": base / "BraveSoftware" / "Brave-Browser" / "User Data",
                "edge": base / "Microsoft" / "Edge" / "User Data",
                "vivaldi": base / "Vivaldi" / "User Data",
            }
        )
    # Opera (stable + GX) stores its user-data dir under %APPDATA% (Roaming),
    # NOT %LOCALAPPDATA%, despite using Chromium's profile layout. Splitting
    # the table off the wrong env var would silently no-op for Windows Opera
    # users with multiple profiles.
    roaming = os.environ.get("APPDATA")
    if roaming:
        base_roaming = Path(roaming)
        out["opera"] = base_roaming / "Opera Software" / "Opera Stable"
        out["opera-gx"] = base_roaming / "Opera Software" / "Opera GX Stable"
    return out


def _platform_user_data_dirs() -> dict[str, Path]:
    if sys.platform == "darwin":
        return _macos_user_data_dirs()
    if sys.platform in ("win32", "cygwin"):
        return _windows_user_data_dirs()
    return _linux_user_data_dirs()


# Per-platform, per-browser user-data directory roots. Tests can override by
# patching this function via ``monkeypatch.setattr``.
def _user_data_dir(browser_name: str) -> Path | None:
    """Return the chromium-family browser's user-data directory, or None.

    ``None`` means the browser isn't part of the chromium family OR the
    current platform has no documented path for it.
    """
    return _platform_user_data_dirs().get(_canonical_chromium_browser_name(browser_name))


@dataclass(frozen=True)
class ChromiumProfile:
    """A single Chromium user-data profile with a populated Cookies DB.

    Attributes:
        browser: The chromium-family browser this profile belongs to
            (e.g. ``"chrome"``, ``"brave"``).
        directory_name: The on-disk directory name (``"Default"``, ``"Profile 1"``).
            This is the canonical identifier — stable across renames in the UI.
        human_name: The user-visible profile name from ``Local State``
            (``"Personal"``, ``"Work"``). Falls back to ``directory_name`` when
            ``Local State`` is missing or doesn't list this profile.
        cookies_db: Absolute path to the profile's ``Cookies`` SQLite DB.
    """

    browser: str
    directory_name: str
    human_name: str
    cookies_db: Path


# Matches Chrome's profile directory naming: ``Default`` or ``Profile <N>``.
# Guest sessions never persist cookies (the ``Guest Profile`` dir may exist
# but has no ``Cookies`` file), so we drop it from the regex to make intent
# explicit rather than relying on the file-existence filter to skip it.
_PROFILE_DIR_RE = re.compile(r"^(Default|Profile\s+\d+)$")


def _load_local_state_names(user_data_dir: Path) -> dict[str, str]:
    """Map ``profile-dir-name`` → human-readable name from ``Local State``.

    Returns ``{}`` when ``Local State`` is absent or malformed — caller falls
    back to the directory name.
    """
    local_state = user_data_dir / "Local State"
    if not local_state.is_file():
        return {}
    try:
        data: Any = json.loads(local_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    info_cache = data.get("profile", {}).get("info_cache", {})
    if not isinstance(info_cache, dict):
        return {}
    names: dict[str, str] = {}
    for dir_name, entry in info_cache.items():
        if isinstance(entry, dict):
            human = entry.get("name")
            if isinstance(human, str) and human.strip():
                names[dir_name] = human.strip()
    return names


def discover_chromium_profiles(browser_name: str) -> list[ChromiumProfile]:
    """Enumerate populated user-data profiles for a chromium-family browser.

    A profile is "populated" when its ``Cookies`` SQLite file exists. Empty /
    never-used profiles are skipped (no point trying to read them).

    Returns an empty list when:

    * ``browser_name`` is not a chromium-family browser
    * The browser has no documented user-data dir on this platform
    * The user-data dir doesn't exist (browser not installed)
    * No profile has a populated Cookies DB

    The list is ordered with ``Default`` first (if present), then other
    profiles in directory-name order — stable across runs.
    """
    if not is_chromium_browser(browser_name):
        return []
    browser_key = _canonical_chromium_browser_name(browser_name)
    user_data_dir = _user_data_dir(browser_key)
    if user_data_dir is None or not user_data_dir.is_dir():
        return []

    human_names = _load_local_state_names(user_data_dir)
    profiles: list[ChromiumProfile] = []
    for entry in user_data_dir.iterdir():
        if not entry.is_dir():
            continue
        if not _PROFILE_DIR_RE.match(entry.name):
            continue
        cookies_db = entry / "Cookies"
        if not cookies_db.is_file():
            continue
        profiles.append(
            ChromiumProfile(
                browser=browser_key,
                directory_name=entry.name,
                human_name=human_names.get(entry.name, entry.name),
                cookies_db=cookies_db,
            )
        )

    def _sort_key(p: ChromiumProfile) -> tuple[int, str]:
        # Default first, then Profile N in numeric order, then anything else.
        if p.directory_name == "Default":
            return (0, "")
        match = re.match(r"Profile\s+(\d+)$", p.directory_name)
        if match:
            return (1, f"{int(match.group(1)):08d}")
        return (2, p.directory_name)

    profiles.sort(key=_sort_key)
    return profiles


def _format_chromium_profile_choice(profile: ChromiumProfile) -> str:
    return f"{profile.human_name} (directory: {profile.directory_name})"


def _format_chromium_profile_choices(profiles: list[ChromiumProfile]) -> str:
    if not profiles:
        return "none"
    return ", ".join(_format_chromium_profile_choice(profile) for profile in profiles)


def resolve_chromium_profile(browser_name: str, profile_selector: str) -> ChromiumProfile:
    """Resolve a ``browser::profile`` selector to one Chromium profile.

    The selector matches the stable on-disk directory name first
    (``"Default"``, ``"Profile 1"``), then the human-readable profile name
    from ``Local State`` (``"Work"``, ``"Personal"``). Directory names win
    because UI names are user-editable and can collide.

    Raises:
        ValueError: When the browser is unsupported, the selector is empty,
            no populated profiles exist, no profile matches, or the human name
            matches more than one profile.
    """
    browser_key = _canonical_chromium_browser_name(browser_name)
    selector = profile_selector.strip()

    if browser_key not in _CHROMIUM_BROWSERS:
        raise ValueError(
            f"'{browser_name}' is not a Chromium-family browser; profile selectors "
            "only work with chrome, chromium, brave, edge, arc, vivaldi, opera, "
            "and opera-gx."
        )
    if not selector:
        raise ValueError(
            f"Empty Chromium profile selector for {browser_key}. Use "
            f"'{browser_key}::<profile-name-or-directory>', for example "
            f"'{browser_key}::Profile 1'."
        )

    profiles = discover_chromium_profiles(browser_key)
    if not profiles:
        raise ValueError(f"No populated {browser_key} profiles were found.")

    selector_key = selector.casefold()
    directory_matches = [
        profile for profile in profiles if profile.directory_name.casefold() == selector_key
    ]
    if directory_matches:
        return directory_matches[0]

    human_name_matches = [
        profile for profile in profiles if profile.human_name.casefold() == selector_key
    ]
    if len(human_name_matches) == 1:
        return human_name_matches[0]
    if len(human_name_matches) > 1:
        directories = ", ".join(f"'{profile.directory_name}'" for profile in human_name_matches)
        raise ValueError(
            f"{browser_key} profile name '{profile_selector}' is ambiguous. "
            f"Use the on-disk profile directory instead: {directories}. "
            f"Available profiles: {_format_chromium_profile_choices(profiles)}."
        )

    raise ValueError(
        f"{browser_key} profile '{profile_selector}' was not found. "
        f"Available profiles: {_format_chromium_profile_choices(profiles)}."
    )


def read_chromium_profile_cookies(
    profile: ChromiumProfile, *, domains: list[str]
) -> list[dict[str, Any]]:
    """Read and decrypt cookies from a single Chromium user-data profile.

    Uses ``rookiepy.any_browser(db_path, domains=…)``, which auto-detects the
    encryption scheme. On macOS it reads the per-browser Safe Storage key from
    Keychain transparently — verified working for non-Default Chrome profiles
    against the real ``rookiepy.chromium_based`` blocker described in #511.

    Args:
        profile: A profile from :func:`discover_chromium_profiles`.
        domains: Domain allowlist to forward to rookiepy.

    Returns:
        Raw cookie dicts (rookiepy's native shape).

    Raises:
        ImportError: When the optional ``rookiepy`` dependency isn't
            installed. The CLI fan-out caller converts this to a friendly
            "pip install 'notebooklm-py[cookies]'" message.
        OSError, RuntimeError: Propagated from rookiepy on read or decrypt
            failure (locked DB, corrupt Keychain item, etc.). Caller decides
            whether to skip-and-warn or propagate.
    """
    import rookiepy  # imported lazily; rookiepy is an optional extra

    return rookiepy.any_browser(str(profile.cookies_db), domains=domains)
